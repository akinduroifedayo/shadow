from datetime import datetime

import pytest

from shadow.switch import Engine, Proposal, Store
from shadow.switch.plugin import SwitchPlugin
from test_switch_safety import OLD, NEW, identity, request, start
from test_switch_states import CROSS, turn, watch_turn


def fail_writer(route):
    raise RuntimeError('fixture config unavailable')


def test_admit_records_watch_turn_before_config_writer_raises(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr('shadow.switch.engine.monotonic', lambda: now[0])
    engine = Engine(Store(path=tmp_path / 'switch.db'), set_default=fail_writer)
    with pytest.raises(RuntimeError, match='fixture config unavailable'):
        engine.start(Proposal('saved', OLD, CROSS, 'better'))
    engine.session_started(session_id='watch-chat', platform='cli')
    now[0] += 30

    with pytest.raises(RuntimeError, match='fixture config unavailable'):
        engine.admit(**identity('watch-chat'), model=CROSS.model, platform='cli')

    saved = list(engine.record.turns.values())
    assert len(saved) == 1
    assert saved[0].session_id == engine.store.identity('watch-chat')
    assert saved[0].turn_id == engine.store.identity('turn-1')
    assert saved[0].target == 'new'


def test_admit_keeps_completed_conversation_route_when_writer_raises(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr('shadow.switch.engine.monotonic', lambda: now[0])
    engine = Engine(Store(path=tmp_path / 'switch.db'), verified_pair=lambda *_: True,
                    set_default=fail_writer)
    start(engine, step_turns=1)
    turn(engine, 'step1')
    key = turn(engine, 'step2')
    with pytest.raises(RuntimeError, match='fixture config unavailable'):
        turn(engine, 'step3')
    assert engine.record.phase == 'done'
    now[0] += 30

    with pytest.raises(RuntimeError, match='fixture config unavailable'):
        engine.admit(**identity(key['session_id'], 'next-turn'), model=OLD.model, platform='cli')

    assert request(engine, key['session_id'], 'next-turn')['request']['model'] == NEW.model


def test_session_start_enrolls_chat_before_config_writer_raises(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr('shadow.switch.engine.monotonic', lambda: now[0])
    engine = Engine(Store(path=tmp_path / 'switch.db'), set_default=fail_writer)
    with pytest.raises(RuntimeError, match='fixture config unavailable'):
        engine.start(Proposal('saved', OLD, CROSS, 'better'))
    now[0] += 30

    with pytest.raises(RuntimeError, match='fixture config unavailable'):
        engine.session_started(session_id='watch-chat', session_key='chat-key', platform='cli')

    assert engine.record.sessions == {engine.store.identity('watch-chat'): 'cli'}
    assert engine.record.session_keys == {engine.store.identity('chat-key'): engine.store.identity('watch-chat')}


def test_manual_choice_allows_new_switch_despite_abandoned_config_intent(tmp_path):
    engine = Engine(Store(path=tmp_path / 'switch.db'), verified_pair=lambda *_: True,
                    set_default=fail_writer)
    with pytest.raises(RuntimeError, match='fixture config unavailable'):
        engine.start(Proposal('saved', OLD, CROSS, 'better'))
    engine.command(command='model', args_raw='chosen', session_key='chat-key')
    assert engine.record.phase == 'manual'
    assert engine.record.config_pending == 'new'

    engine.start(Proposal('replacement-test', OLD, NEW, 'better'), switch_id='replacement')

    assert engine.record.id == 'replacement'
    assert engine.record.phase == 'running'
    assert engine.record.config_pending is None


@pytest.mark.parametrize('target', [NEW, CROSS])
def test_start_without_file_locking_preserves_existing_record(tmp_path, monkeypatch, target):
    writes = []
    engine = Engine(Store(path=tmp_path / 'switch.db'), verified_pair=lambda *_: True,
                    set_default=writes.append)
    engine.start(Proposal('saved', OLD, target, 'better'))
    engine.undo()
    before = engine.record.to_dict()
    writes.clear()
    monkeypatch.setattr('shadow.switch.engine.fcntl', None)

    with pytest.raises(RuntimeError, match='locking'):
        engine.start(Proposal('replacement-test', OLD, target, 'better'))

    assert engine.record.to_dict() == before
    assert engine.store.row_count() == 1
    assert writes == []


@pytest.mark.parametrize('target', [NEW, CROSS])
def test_resume_without_file_locking_preserves_paused_record(tmp_path, monkeypatch, target):
    writes = []
    engine = Engine(Store(path=tmp_path / 'switch.db'), verified_pair=lambda *_: True,
                    set_default=writes.append)
    engine.start(Proposal('saved', OLD, target, 'better'))
    for i in range(3):
        (watch_turn if target == CROSS else turn)(engine, i, failed=True)
    assert engine.record.phase == 'paused'
    before = engine.record.to_dict()
    writes.clear()
    monkeypatch.setattr('shadow.switch.engine.fcntl', None)

    with pytest.raises(RuntimeError, match='locking'):
        engine.resume()

    assert engine.record.to_dict() == before
    assert writes == []


@pytest.mark.parametrize('target', [NEW, CROSS])
def test_panel_history_keeps_public_lifecycle_rows_after_acknowledgement_and_restart(tmp_path, target):
    path = tmp_path / 'switch.db'
    engine = Engine(Store(path=path), verified_pair=lambda *_: True, set_default=lambda _: None)
    engine.start(Proposal('saved', OLD, target, 'better'))
    if target == NEW:
        turn(engine, 'counted')
        engine.advance()
    for i in range(3):
        (watch_turn if target == CROSS else turn)(engine, i, failed=True)
    engine.resume()
    if target == NEW:
        engine.advance()
    engine.undo()
    with engine.store.transaction() as holder:
        holder[0].history.extend([
            {'kind': 'step2', 'at': '2026-11-10T00:00:00+00:00'},
            {'kind': 'request_debug', 'at': '2026-11-10T00:00:01+00:00',
             'session_id': engine.store.identity('private-chat'), 'turns': 99},
        ])
    service = SwitchPlugin(None, engine, config_reader=lambda: {})

    history = service.panel()['history']

    expected = ([('watch_start', None, None), ('watch_trip', None, None),
                 ('watch_start', None, None), ('watch_undo', None, None)]
                if target == CROSS else
                [('step1', None, None), ('step2', 1, True), ('rollback', None, None),
                 ('resume', None, None), ('step2', 0, True), ('undo', None, None)])
    assert [(row['kind'], row['turns'], row['manual']) for row in history] == expected + [('step2', None, None)]
    assert all(set(row) == {'kind', 'at', 'turns', 'manual', 'reason'} for row in history)
    assert [row['reason'] for row in history if row['kind'] in {'rollback', 'watch_trip'}] == ['requests']
    assert all(row['reason'] is None for row in history if row['kind'] not in {'rollback', 'watch_trip'})
    assert all(datetime.fromisoformat(row['at']).tzinfo is not None for row in history)
    service.action('acknowledge', switch_id=engine.record.id,
                   notice_ids=[notice['id'] for notice in service.panel()['notices']])
    assert service.panel()['notices'] == []
    restarted = SwitchPlugin(None, Engine(Store(path=path)), config_reader=lambda: {})
    assert restarted.panel()['history'] == history


def test_excluding_previously_counted_session_keeps_next_step_turns_nonnegative(tmp_path):
    engine = Engine(Store(path=tmp_path / 'switch.db'), verified_pair=lambda *_: True)
    start(engine, step_turns=2)
    turn(engine, 'first')
    key = turn(engine, 'second')
    assert engine.record.step == 2
    engine.exclude_session(key['session_id'])
    assert engine.record.counts('new').turns == 0

    engine.advance()

    notice = next(event for event in engine.record.notices if event['kind'] == 'step3')
    assert notice['turns'] == 0
    assert notice['manual'] is True
    service = SwitchPlugin(None, engine, config_reader=lambda: {})
    assert service.panel()['history'][-1]['turns'] == 0
