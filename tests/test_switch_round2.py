import pytest

from shadow.switch import Engine, Proposal, Store
from test_switch_safety import NEW, OLD, admit, engine, request, start


def test_idle_store_adopts_another_process_saved_identity_salt(tmp_path):
    path, salt_path = tmp_path / 'switch.db', tmp_path / 'switch.salt'
    first = Store(path=path, salt_path=salt_path)
    first.identity('conversation')
    assert not path.exists()
    assert not salt_path.exists()
    second = Store(path=path, salt_path=salt_path)
    owner = Engine(second, verified_pair=lambda *_: True)
    owner.start(Proposal('saved', OLD, NEW, 'better'))
    owner.advance()
    owner.advance()
    with first.transaction() as holder:
        assert holder[0].id == owner.record.id
    assert first.identity('conversation') == second.identity('conversation')
    gateway = Engine(first)
    admit(gateway)
    assert request(gateway)['request']['model'] == NEW.model


def test_idle_engine_discards_hashed_caches_when_adopting_saved_salt(tmp_path):
    path, salt_path = tmp_path / 'switch.db', tmp_path / 'switch.salt'
    gateway = Engine(Store(path=path, salt_path=salt_path))
    gateway.exclude_session('conversation')
    owner = Engine(Store(path=path, salt_path=salt_path), verified_pair=lambda *_: True)
    owner.start(Proposal('saved', OLD, NEW, 'better'))
    owner.advance()
    owner.advance()
    admit(gateway)
    assert request(gateway)['request']['model'] == NEW.model
    assert gateway.excluded_sessions == set()


@pytest.mark.parametrize('phase', ['done', 'undone'])
@pytest.mark.parametrize('retained_turn', [False, True])
def test_pending_config_never_changes_a_sent_gradual_turn(engine, phase, retained_turn):
    engine.set_default = lambda _: None
    start(engine)
    engine.advance()
    engine.advance()
    admit(engine)
    if not retained_turn:
        assert request(engine)['request']['model'] == NEW.model
    with engine.store.transaction() as holder:
        holder[0].phase = 'done'
        holder[0].draining = dict(holder[0].assignments)
        holder[0].default_changed = True
    if retained_turn:
        admit(engine, turn='retained')
        assert request(engine, turn='retained')['request']['model'] == NEW.model
    with engine.store.transaction() as holder:
        holder[0].phase = phase
        holder[0].config_pending = 'new' if phase == 'done' else 'old'
    turn_id = 'retained' if retained_turn else 'turn-1'
    assert request(engine, turn=turn_id, api_request_id='tool-loop')['request']['model'] == NEW.model


@pytest.mark.parametrize('action', ['stop', 'undo', 'pause'])
@pytest.mark.parametrize('sent', [False, True])
def test_ending_switch_restores_unsent_turns_and_keeps_sent_turns(engine, action, sent):
    from test_switch_states import turn

    engine.set_default = lambda _: None
    start(engine)
    engine.advance()
    engine.advance()
    admit(engine)
    if sent:
        assert request(engine)['request']['model'] == NEW.model
    if action == 'pause':
        for index in range(3):
            turn(engine, index, failed=True)
        assert engine.record.phase == 'paused'
    else:
        engine.undo(stop=action == 'stop')
    result = request(engine, api_request_id='after-ending')
    assert (result['request']['model'] if result else OLD.model) == (NEW.model if sent else OLD.model)


@pytest.mark.parametrize('command', ['help', 'status', 'shadow', 'clear', 'unknown'])
def test_unrelated_slash_commands_do_not_write_or_retry_config(engine, command):
    start(engine)
    with engine.store.transaction() as holder:
        holder[0].config_pending = 'new'
    writes = []
    engine.set_default = writes.append
    before = engine.store.connection.total_changes
    engine.command(command=command, args_raw='', session_key='conversation')
    assert engine.store.connection.total_changes == before
    assert writes == []
    assert engine.record.config_pending == 'new'


def test_pending_config_retries_are_rate_limited_and_new_intent_is_immediate(engine, monkeypatch):
    from test_switch_states import CROSS

    now, writes = [100.0], []
    monkeypatch.setattr('shadow.switch.engine.monotonic', lambda: now[0], raising=False)
    def broken(route):
        writes.append(route)
        raise RuntimeError('fixture config failure')
    engine.set_default = broken
    with pytest.raises(RuntimeError):
        engine.start(Proposal('saved', OLD, CROSS, 'better'))
    for _ in range(5):
        engine.command(command='retry', args_raw='', session_key='conversation')
        engine.reconcile_config()
    assert writes == [CROSS]
    now[0] += 29
    engine.reconcile_config()
    assert writes == [CROSS]
    now[0] += 1
    engine.set_default = writes.append
    engine.reconcile_config()
    assert writes == [CROSS, CROSS]
    assert engine.record.config_pending is None
    engine.set_default = broken
    with pytest.raises(RuntimeError):
        engine.undo()
    assert writes[-1] == OLD
    assert engine.record.config_pending == 'old'


def test_undo_restoration_bypasses_a_failed_new_default_retry_delay(engine):
    from test_switch_states import CROSS

    writes = []
    def broken(route):
        writes.append(route)
        raise RuntimeError('fixture config failure')
    engine.set_default = broken
    with pytest.raises(RuntimeError):
        engine.start(Proposal('saved', OLD, CROSS, 'better'))
    engine.set_default = writes.append
    engine.undo()
    assert writes == [CROSS, OLD]
    assert engine.record.phase == 'undone'
    assert engine.record.config_pending is None


def test_reader_cannot_cache_a_partially_written_salt_file(tmp_path, monkeypatch):
    path, salt_path = tmp_path / 'switch.db', tmp_path / 'switch.salt'
    reader = Store(path=path, salt_path=salt_path)
    writer = Store(path=path, salt_path=salt_path)
    import os
    original = os.fdopen
    def observe_file_creation(*args, **kwargs):
        reader.identity('conversation')
        return original(*args, **kwargs)
    monkeypatch.setattr('shadow.switch.store.os.fdopen', observe_file_creation)
    owner = Engine(writer, verified_pair=lambda *_: True)
    owner.start(Proposal('saved', OLD, NEW, 'better'))
    assert reader.identity('conversation') == writer.identity('conversation')
