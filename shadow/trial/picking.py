import json
import random
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from ..common.copy import copy
from ..common.scrub import Scrubber

FOLLOW_UP = '\nNobody is available to answer follow-up questions.'
TOKEN_RESERVE = 256
MARKERS = ('shadow-controller:', 'shadow-setup-check:')
SESSION_FIELDS = ('id', 'conversation_id', 'source', 'cwd', 'shadow_controller')
MESSAGE_FIELDS = ('id', 'role', 'content', 'tool_calls', 'active', 'compacted')
FLAG_FIELDS = ('active', 'compacted')


class HistoryUnavailable(ValueError):
    """Recent history could not be read. Carries no export bytes."""

    def __init__(self):
        super().__init__(copy('trial.history_unavailable'))


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


def _host_redact():
    try:
        from agent.redact import redact_sensitive_text
    except Exception:
        return None
    return lambda text: redact_sensitive_text(text, force=True)


class _Redactor:
    """Force-redact every string and key in what Shadow keeps, the way `sessions export --redact` does."""

    def __init__(self, redact):
        self.redact = redact
        self.cache = {}

    def __call__(self, value):
        if isinstance(value, str):
            clean = self.cache.get(value)
            if clean is None:
                clean = self.redact(value)
                if len(value) <= 256 and len(self.cache) < 4096:
                    self.cache[value] = clean
            return clean
        if isinstance(value, list):
            return [self(v) for v in value]
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                masked = self(key) if isinstance(key, str) else key
                unique, n = masked, 2
                while unique in out:
                    unique, n = f'{masked}#{n}', n + 1
                out[unique] = self(item)
            return out
        return value


def _project(session, redact):
    """Keep only the fields selection reads, redact them, and drop everything else before it is retained."""
    messages = session.get('messages') or []
    kept = {key: redact(session[key]) for key in SESSION_FIELDS if key in session}
    first = next((m for m in messages if m.get('active', True) or m.get('compacted', False)), None)
    if first is not None and first.get('role') == 'tool' and any(marker in str(first.get('content', '')) for marker in MARKERS):
        kept['shadow_controller'] = True
    rows = []
    for message in messages:
        if message.get('role') == 'tool':
            rows.append({'role': 'tool', **{key: message[key] for key in FLAG_FIELDS if key in message}})
        else:
            rows.append({key: redact(message[key]) for key in MESSAGE_FIELDS if key in message})
    kept['messages'] = rows
    return kept


def export_sessions(profile, days=7, runner=subprocess.run, redact=None):
    """Export recent sessions and keep a redacted projection of the fields Shadow uses.

    Host-wide `--redact` force-redacts every stored string, including tool outputs and metadata that
    selection discards, which made large histories miss the export deadline. Shadow redacts the same
    way, with the same host function, but only on what it keeps. Without host redaction it reads nothing.
    """
    redact = redact or _host_redact()
    if redact is None:
        raise HistoryUnavailable()
    clean = _Redactor(redact)
    sessions = None
    try:
        result = runner(['hermes', '-p', profile, 'sessions', 'export', '-', '--newer-than', f'{days}d'],
                        capture_output=True, text=True, check=True, timeout=60)
        sessions = [_project(json.loads(line), clean) for line in result.stdout.splitlines() if line.strip()]
    except Exception:  # any failure, including in host redaction, must not carry export text onward
        sessions = None
    # Raised outside the handler so neither the message nor the exception chain holds export text.
    if sessions is None:
        raise HistoryUnavailable()
    return sessions


def _history_row(message):
    if message.get('role') == 'tool':
        return {'role': 'tool', 'content': '<previous tool output omitted>'}
    if message.get('role') in {'user', 'assistant'}:
        row = {'role': message['role'], 'content': message.get('content') or ''}
        if message.get('tool_calls'):
            row['tool_calls'] = message['tool_calls']
        return row
    return None


def pick_turns(sessions, *, seed, count=30, excluded_sources=('email',), previous=()):
    scrubber = Scrubber()
    rows = Scrubber()
    replay_suffix = re.escape(FOLLOW_UP) + '(?:' + re.escape('\n' + copy('trial.tools_unavailable')) + ')?\n[0-9a-f]{32}$'
    candidates, left, total = [], Counter(), 0
    for session in sessions:
        messages = [m for m in session.get('messages', []) if m.get('active', True) or m.get('compacted', False)]
        source = session.get('source', 'cli')
        cwd = session.get('cwd')
        shadow = bool(session.get('shadow_controller')) or any(marker in str(m.get('content', '')) for m in messages[:1] for marker in MARKERS)
        cwd_ok = bool(cwd) and Path(cwd).is_absolute() and Path(cwd).is_dir()
        # Each earlier message is scrubbed once; a turn's context is the prefix of those rows, with the
        # same text and the same removal counts as scrubbing the whole serialized prefix again.
        texts, length, prefix = [], 2, Counter()
        for index, message in enumerate(messages):
            if message.get('role') == 'user':
                total += 1
                reason = _left_out(message, messages, index, source, cwd_ok, shadow, replay_suffix, excluded_sources)
                if reason:
                    left[reason] += 1
                else:
                    goal = scrubber.text(message.get('content') or '')
                    scrubber.counts.update(prefix)
                    if len(goal) + len(FOLLOW_UP) + TOKEN_RESERVE > 16000 or length > 32000:
                        left['too_long'] += 1
                    else:
                        context = '[' + ', '.join(texts) + ']'
                        candidates.append(Turn(str(message.get('id', f'{session["id"]}:{index}')), str(session.get('conversation_id') or session['id']), source, str(Path(cwd).resolve()), goal, context))
            row = _history_row(message)
            if row is not None:
                before = rows.counts.copy()
                text = json.dumps(rows.scrub(row), ensure_ascii=False)
                prefix.update(rows.counts - before)
                length += len(text) + (2 if texts else 0)
                texts.append(text)
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


def _left_out(message, messages, index, source, cwd_ok, shadow, replay_suffix, excluded_sources):
    goal = message.get('content') or ''
    if not isinstance(goal, str) or goal.lstrip().startswith('/'):
        return 'command'
    if re.search(replay_suffix, goal) or shadow:
        return 'shadow_session'
    if source in excluded_sources:
        return 'source'
    replies = []
    for row in messages[index + 1:]:
        if row.get('role') == 'user':
            break
        if row.get('role') == 'assistant':
            replies.append(row)
    if not replies:
        return 'no_reply'
    if not cwd_ok:
        return 'unknown_cwd'
    if len(goal) < 20 and not any(r.get('tool_calls') or len(r.get('content') or '') >= 200 for r in replies):
        return 'trivial'
    return None
