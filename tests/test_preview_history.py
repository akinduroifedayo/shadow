"""Preview history: bounded selection time, redaction of exactly what Shadow keeps, and an explicit failure reason."""

import json
import random
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext
from shadow.common.copy import copy, load_copy
from shadow.common.scrub import Scrubber, outbound
from shadow.desktop_rpc import DesktopRpc
from shadow.desktop_source import ProfileSource
import shadow.trial.picking as picking
from shadow.trial.picking import FOLLOW_UP, TOKEN_RESERVE, Selection, Turn, export_sessions, pick_turns
from shadow.trial.plugin import TrialPlugin

COPY = load_copy()
CANARY = 'CANARY'


def fake_redact(text):
    """Stands in for agent.redact.redact_sensitive_text(force=True): masks every canary token."""
    return re.sub(CANARY + r'[A-Za-z0-9_]*', '***', text)


def conversation(cwd, turns, *, sid='long'):
    messages = []
    for t in range(turns):
        messages += [
            {'id': f'{sid}-{t}', 'role': 'user', 'content': f'Please refactor parser module {t} and explain the retry logic. Mail me at dev{t}@example.com'},
            {'role': 'assistant', 'content': 'Working.', 'tool_calls': [{'id': f'c{t}', 'type': 'function', 'function': {'name': 'terminal', 'arguments': json.dumps({'command': f'pytest -k case{t}', 'token': f'tok-{t}-abcdefgh'})}}]},
            {'role': 'tool', 'content': 'ok ' * 40, 'tool_call_id': f'c{t}'},
            {'role': 'assistant', 'content': 'Done. ' + 'the parser now retries with backoff ' * 6},
        ]
    return {'id': sid, 'cwd': str(cwd), 'source': 'cli', 'messages': messages}


def test_long_conversation_preview_selects_in_bounded_time(tmp_path):
    sessions = [conversation(tmp_path, 1000)]
    start = time.perf_counter()
    result = pick_turns(sessions, seed=1)
    elapsed = time.perf_counter() - start
    assert len(result.turns) == 3
    assert elapsed < 5, f'selecting from one 1000-turn conversation took {elapsed:.1f}s'


def reference_pick(sessions, *, seed, count=30, excluded_sources=('email',), previous=()):
    """Shadow 16ebe83's selection, kept verbatim as the oracle for equivalent output."""
    def history(messages):
        rows = []
        for m in messages:
            if m.get('role') == 'tool':
                rows.append({'role': 'tool', 'content': '<previous tool output omitted>'})
            elif m.get('role') in {'user', 'assistant'}:
                row = {'role': m['role'], 'content': m.get('content') or ''}
                if m.get('tool_calls'):
                    row['tool_calls'] = m['tool_calls']
                rows.append(row)
        return json.dumps(rows, ensure_ascii=False)

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
                  or any(marker in str(m.get('content', '')) for m in messages[:1] for marker in ('shadow-controller:', 'shadow-setup-check:'))):
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
            payload = outbound({'goal': goal, 'context': history(messages[:index])}, scrubber)
            if len(payload['goal']) + len(FOLLOW_UP) + TOKEN_RESERVE > 16000 or len(payload['context']) > 32000:
                left['too_long'] += 1
                continue
            candidates.append(Turn(str(message.get('id', f'{session["id"]}:{index}')), str(session.get('conversation_id') or session['id']), source, str(Path(cwd).resolve()), payload['goal'], payload['context']))
    rng = random.Random(seed)
    rng.shuffle(candidates)
    picked, seen, ids = [], {t.goal for t in previous}, {t.id for t in previous}
    conversations = Counter(t.conversation_id for t in previous)
    unique = []
    for turn in candidates:
        if turn.goal in seen or turn.id in ids:
            left['duplicate'] += 1
            continue
        seen.add(turn.goal)
        unique.append(turn)
    for turn in unique:
        if conversations[turn.conversation_id] >= 3:
            left['conversation_limit'] += 1
        elif len(picked) >= count:
            left['not_picked'] += 1
        else:
            picked.append(turn)
            conversations[turn.conversation_id] += 1
    return Selection(picked, total, len(unique), dict(left), dict(scrubber.counts), seed)


def mixed_history(tmp_path, rng):
    sessions = []
    for c in range(12):
        messages = []
        for t in range(rng.randint(1, 30)):
            body = rng.choice(['Please explain the cache invalidation in detail.', 'hi', '/model', 'word ' * rng.choice([20, 3500]),
                               'Use key sk-ant-abcdefghijklmnop1234 and email a@b.co please now.', '{"api_key": "abc", "q": "Please run it"}'])
            messages.append({'id': f'{c}-{t}', 'role': 'user', 'content': body, 'active': rng.random() > 0.1, 'compacted': rng.random() > 0.5})
            if rng.random() > 0.2:
                messages.append({'role': 'assistant', 'content': rng.choice(['ok', 'A long answer ' * 30, None]),
                                 **({'tool_calls': [{'id': f'k{t}', 'function': {'name': 'read', 'arguments': '{"password": "hunter2", "path": "/tmp"}'}}]} if rng.random() > 0.5 else {})})
                messages.append({'role': 'tool', 'content': 'Bearer abcdefghijklmnop', 'tool_call_id': f'k{t}'})
        sessions.append({'id': f's{c}', 'conversation_id': f'conv{c % 5}', 'cwd': str(tmp_path) if c % 7 else None,
                         'source': 'email' if c == 3 else 'cli', 'messages': messages})
    return sessions


@pytest.mark.parametrize('seed', range(6))
def test_selection_matches_the_shipped_selection(tmp_path, seed):
    sessions = mixed_history(tmp_path, random.Random(seed))
    previous = reference_pick(sessions, seed=seed + 100).turns[:5]
    assert pick_turns(sessions, seed=seed, previous=previous) == reference_pick(sessions, seed=seed, previous=previous)
    assert pick_turns([conversation(tmp_path, 40)], seed=seed) == reference_pick([conversation(tmp_path, 40)], seed=seed)


def raw_export(cwd):
    """One exported session with a canary in every field: kept fields must be masked, discarded fields dropped."""
    return {
        'id': 'raw1', 'source': 'cli', 'cwd': str(cwd), 'title': f'{CANARY}_TITLE', 'system_prompt': f'{CANARY}_SYSTEM',
        'model_config': {'api_key': f'{CANARY}_MODELCFG'}, 'billing_base_url': f'https://u:{CANARY}_URL@example.com',
        'messages': [
            {'id': 'm1', 'role': 'user', 'content': f'Please fix the deploy script, my key is {CANARY}_USER thanks', 'reasoning': f'{CANARY}_REASON1'},
            {'id': 'm2', 'role': 'assistant', 'content': f'Running it with {CANARY}_ASSIST.', 'reasoning': f'{CANARY}_REASON2',
             'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 'terminal', 'arguments': json.dumps({'command': 'deploy', 'env': {f'{CANARY}_KEYNAME': f'{CANARY}_NESTED'}})}}]},
            {'id': 'm3', 'role': 'tool', 'content': f'{CANARY}_TOOLOUT', 'tool_call_id': 'c1', 'tool_name': f'{CANARY}_TOOLNAME'},
            {'id': 'm4', 'role': 'assistant', 'content': 'Deployed. ' * 30},
            {'id': 'm5', 'role': 'user', 'content': 'Now please add a rollback step to the same deploy script.'},
            {'id': 'm6', 'role': 'assistant', 'content': 'Added the rollback step. ' * 20},
        ],
    }


def runner_for(stdout, calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr='')
    return run


def test_export_keeps_only_used_fields_and_redacts_them(tmp_path):
    calls = []
    sessions = export_sessions('demo', runner=runner_for(json.dumps(raw_export(tmp_path)) + '\n', calls), redact=fake_redact)
    argv, kwargs = calls[0]
    assert argv == ['hermes', '-p', 'demo', 'sessions', 'export', '-', '--newer-than', '7d']
    assert kwargs['timeout'] == 60
    text = json.dumps(sessions)
    assert CANARY not in text, text
    session, = sessions
    assert set(session) <= {'id', 'conversation_id', 'source', 'cwd', 'shadow_controller', 'messages'}
    tool = session['messages'][2]
    assert tool == {'role': 'tool'}
    assert all(set(m) <= {'id', 'role', 'content', 'tool_calls', 'active', 'compacted'} for m in session['messages'])
    arguments = json.loads(session['messages'][1]['tool_calls'][0]['function']['arguments'])
    assert arguments == {'command': 'deploy', 'env': {'***': '***'}}
    selection = pick_turns(sessions, seed=1)
    assert {t.id for t in selection.turns} == {'m1', 'm5'}
    assert CANARY not in json.dumps([t.record() for t in selection.turns])


def test_export_fails_closed_without_host_redaction(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_redact(name, *args, **kwargs):
        if name == 'agent.redact':
            raise ImportError('no host redaction')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_redact)
    calls = []
    with pytest.raises(getattr(picking, 'HistoryUnavailable', LookupError)):
        export_sessions('demo', runner=runner_for(json.dumps(raw_export(tmp_path)), calls))
    assert calls == []


def failing(kind):
    def run(argv, **kwargs):
        raw = json.dumps(raw_export('/tmp'))
        if kind == 'timeout':
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'], output=raw, stderr=raw)
        if kind == 'exit':
            raise subprocess.CalledProcessError(1, argv, output=raw, stderr=raw)
        if kind == 'missing':
            raise FileNotFoundError(f'hermes {raw}')
        return subprocess.CompletedProcess(argv, 0, stdout=raw[:-7] + '\n', stderr='')
    return run


@pytest.mark.parametrize('kind', ['timeout', 'exit', 'missing', 'malformed'])
def test_export_failures_carry_no_history(kind):
    with pytest.raises(getattr(picking, 'HistoryUnavailable', LookupError)) as caught:
        export_sessions('demo', runner=failing(kind), redact=fake_redact)
    assert str(caught.value) == COPY['trial.history_unavailable']
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert CANARY not in repr(caught.value.args)


def profile_source(tmp_path, runner):
    ctx = FakeContext()
    writes = []
    store = type('S', (), {'list': lambda self: [], 'get': lambda self, _: None, 'save': lambda self, r: writes.append(r), 'prune': lambda self: None})()
    launcher = type('L', (), {'isolation_verified': True, 'profile': 'default'})()
    original = picking.export_sessions
    picking_export = lambda profile, days=7: original(profile, days, runner=runner, redact=fake_redact)
    instance = TrialPlugin(ctx, store=store, current=CURRENT, catalog=[CURRENT, TRIAL, OUTSIDE], launcher=launcher, config_reader=lambda: {})
    return ProfileSource(instance), ctx, writes, picking_export


@pytest.mark.parametrize('kind', ['timeout', 'exit', 'malformed'])
def test_desktop_preview_explains_unreadable_history_and_queues_nothing(tmp_path, monkeypatch, caplog, kind):
    source, ctx, writes, export = profile_source(tmp_path, failing(kind))
    monkeypatch.setattr('shadow.trial.picking.export_sessions', export)
    rpc = DesktopRpc(source, COPY).methods()
    reply = rpc['start.card']({'model': TRIAL.model})
    assert reply == {'error': {'message': COPY['trial.history_unavailable']}}
    started = rpc['act']({'action': 'start', 'model': TRIAL.model, 'intent': 'switch'})
    assert started == {'error': {'message': COPY['trial.history_unavailable']}}
    assert writes == [] and ctx.capture == [] and source.trial.engine is None
    assert CANARY not in caplog.text


def test_cli_try_explains_unreadable_history(tmp_path, monkeypatch):
    source, ctx, writes, export = profile_source(tmp_path, failing('timeout'))
    monkeypatch.setattr('shadow.trial.plugin.export_sessions', export)
    reply = source.trial.command(f'try {TRIAL.provider}::{TRIAL.model}')
    assert reply == COPY['trial.history_unavailable']
    assert writes == [] and ctx.capture == []
