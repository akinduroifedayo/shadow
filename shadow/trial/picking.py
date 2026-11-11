import json
import random
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from ..common.copy import copy
from ..common.scrub import Scrubber, outbound

FOLLOW_UP = '\nNobody is available to answer follow-up questions.'
TOKEN_RESERVE = 256


@dataclass(frozen=True)
class Turn:
    id: str
    conversation_id: str
    source: str
    cwd: str
    goal: str
    context: str

    def record(self):
        return asdict(self)


@dataclass
class Selection:
    turns: list[Turn]
    total: int
    qualified: int
    left_out: dict
    scrub_counts: dict
    seed: int


def export_sessions(profile, days=7, runner=subprocess.run):
    result = runner(['hermes', '-p', profile, 'sessions', 'export', '-', '--newer-than', f'{days}d', '--redact'], capture_output=True, text=True, check=True, timeout=60)
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def _history(messages):
    history = []
    for m in messages:
        if m.get('role') == 'tool':
            history.append({'role': 'tool', 'content': '<previous tool output omitted>'})
        elif m.get('role') in {'user', 'assistant'}:
            row = {'role': m['role'], 'content': m.get('content') or ''}
            if m.get('tool_calls'):
                row['tool_calls'] = m['tool_calls']
            history.append(row)
    return json.dumps(history, ensure_ascii=False)


def pick_turns(sessions, *, seed, count=30, excluded_sources=('email',), previous=()):
    scrubber = Scrubber()
    replay_suffix = re.escape(FOLLOW_UP) + '(?:' + re.escape('\n' + copy('trial.tools_unavailable')) + ')?\n[0-9a-f]{32}$'
    candidates, left, total = [], Counter(), 0
    for session in sessions:
        messages = [m for m in session.get('messages', []) if m.get('active', True) or m.get('compacted', False)]
        for index, message in enumerate(messages):
            if message.get('role') != 'user':
                continue
            total += 1
            goal = message.get('content') or ''
            reason = None
            following = []
            for row in messages[index + 1:]:
                if row.get('role') == 'user':
                    break
                following.append(row)
            replies = [r for r in following if r.get('role') == 'assistant']
            source = session.get('source', 'cli')
            cwd = session.get('cwd')
            if not isinstance(goal, str) or goal.lstrip().startswith('/'):
                reason = 'command'
            elif (re.search(replay_suffix, goal) or session.get('shadow_controller')
                  or any(marker in str(m.get('content', ''))
                         for m in messages[:1] for marker in ('shadow-controller:', 'shadow-setup-check:'))):
                reason = 'shadow_session'
            elif source in excluded_sources:
                reason = 'source'
            elif not replies:
                reason = 'no_reply'
            elif not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                reason = 'unknown_cwd'
            elif len(goal) < 20 and not any(r.get('tool_calls') or len(r.get('content') or '') >= 200 for r in replies):
                reason = 'trivial'
            if reason:
                left[reason] += 1
                continue
            payload = outbound({'goal': goal, 'context': _history(messages[:index])}, scrubber)
            if len(payload['goal']) + len(FOLLOW_UP) + TOKEN_RESERVE > 16000 or len(payload['context']) > 32000:
                left['too_long'] += 1
                continue
            candidates.append(Turn(str(message.get('id', f'{session["id"]}:{index}')), str(session.get('conversation_id') or session['id']), source, str(Path(cwd).resolve()), payload['goal'], payload['context']))
    rng = random.Random(seed)
    rng.shuffle(candidates)
    picked = []
    seen = {t.goal for t in previous}
    ids = {t.id for t in previous}
    conversations = Counter(t.conversation_id for t in previous)
    unique = []
    for turn in candidates:
        if turn.goal in seen or turn.id in ids:
            left['duplicate'] += 1
            continue
        seen.add(turn.goal)
        unique.append(turn)
    qualified = len(unique)
    for turn in unique:
        if conversations[turn.conversation_id] >= 3:
            left['conversation_limit'] += 1
        elif len(picked) >= count:
            left['not_picked'] += 1
        else:
            picked.append(turn)
            conversations[turn.conversation_id] += 1
    return Selection(picked, total, qualified, dict(left), dict(scrubber.counts), seed)
