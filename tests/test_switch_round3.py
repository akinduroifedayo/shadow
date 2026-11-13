from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
from threading import Event

import pytest

from shadow.switch import Engine, Proposal, Store
from shadow.switch.copy import status
from shadow.switch.domain import CHAT_MOVE_PHASES, Turn, is_active
from test_switch_safety import NEW, OLD, admit, engine, identity, request, start
from test_switch_plugin import plugin
from test_switch_states import CROSS


@pytest.mark.parametrize('pending_cleared', [False, True])
def test_two_engines_restore_old_default_after_undo_during_write(tmp_path, pending_cleared):
    import sqlite3

    path = tmp_path / 'switch.db'
    def make_store():
        return Store(path=path, connection_factory=lambda: sqlite3.connect(path, check_same_thread=False))
    entered, release = Event(), Event()
    writes = []
    def write(route):
        if route == NEW:
            entered.set()
            assert release.wait(5)
        writes.append(route)
    first = Engine(make_store(), verified_pair=lambda *_: True, set_default=write)
    second = Engine(make_store(), set_default=write)
    first.start(Proposal('saved', OLD, NEW, 'better'))
    with first.store.transaction() as holder:
        holder[0].phase = 'done'
        holder[0].config_pending = 'new'
    with ThreadPoolExecutor(max_workers=1) as worker:
        writing = worker.submit(first.reconcile_config)
        try:
            assert entered.wait(5)
            second.undo()
            if pending_cleared:
                with second.store.transaction() as holder:
                    holder[0].config_pending = None
        finally:
            release.set()
        writing.result(timeout=5)
    assert writes[-1] == OLD
    assert first.record.phase == 'undone'
    assert first.record.config_pending is None
    assert not first.record.default_changed


def test_status_immediately_retries_the_pending_default(plugin, monkeypatch):
    _, service = plugin
    monkeypatch.setattr('shadow.switch.engine.monotonic', lambda: 100.0)
    writes = []
    def broken(route):
        writes.append(route)
        raise RuntimeError('fixture config failure')
    service.engine.set_default = broken
    with pytest.raises(RuntimeError):
        service.engine.start(Proposal('saved', OLD, CROSS, 'better'))
    service.engine.set_default = writes.append
    service.command('status')
    assert writes == [CROSS, CROSS]
    assert service.engine.record.config_pending is None


@pytest.mark.parametrize('action', ['undo', 'stop'])
def test_user_action_immediately_retries_pending_restoration(plugin, monkeypatch, action):
    _, service = plugin
    monkeypatch.setattr('shadow.switch.engine.monotonic', lambda: 100.0)
    service.engine.start(Proposal('saved', OLD, CROSS, 'better'))
    writes = []
    def broken(route):
        writes.append(route)
        raise RuntimeError('fixture config failure')
    service.engine.set_default = broken
    with pytest.raises(RuntimeError):
        service.engine.undo(stop=action == 'stop')
    service.engine.set_default = writes.append
    service.action(action)
    assert writes == [OLD, OLD]
    assert service.engine.record.config_pending is None


@pytest.mark.parametrize('marker', ['shadow-controller:', 'shadow-setup-check:'])
def test_trial_controller_marker_excludes_the_session_without_trial_hook_order(engine, marker):
    start(engine)
    engine.advance()
    engine.advance()
    engine.session_started(session_id='conversation', platform='cli')
    engine.admit(**identity(), model=OLD.model, platform='cli',
                 user_message=marker + 'saved:opaque-fixture-token')
    assert request(engine) is None
    admit(engine, turn='next')
    assert request(engine, turn='next') is None
    assert engine.record.sessions == {}


@pytest.mark.parametrize('first_action', ['undo', 'stop'])
@pytest.mark.parametrize('surface', ['command', 'action'])
def test_repeated_undo_replies_with_status_without_repeating_writes(plugin, first_action, surface):
    ctx, service = plugin
    writes = []
    service.engine.set_default = writes.append
    service.engine.start(Proposal('saved', OLD, CROSS, 'better'))
    assert service.action(first_action)
    if surface == 'command':
        reply = ctx.commands['shadow']('undo')
    else:
        reply = service.action('undo')
    assert reply == status(service.engine.record)
    assert reply
    assert writes == [CROSS, OLD]


@pytest.mark.parametrize('cleanup', ['exclude', 'model_command'])
def test_finishing_another_turn_during_cleanup_keeps_other_chats_routing(engine, monkeypatch, cleanup):
    start(engine)
    engine.advance()
    engine.advance()
    for session in ('conversation', 'finishing', 'remaining'):
        admit(engine, session)
    with engine.store.transaction() as holder:
        holder[0].phase = 'done'
        holder[0].draining = dict(holder[0].assignments)
    for session in ('conversation', 'finishing', 'remaining'):
        admit(engine, session, turn='retained')
    original = Turn.__getattribute__
    finished = []
    def finish_during_read(turn, name):
        value = original(turn, name)
        if name == 'session_id' and not finished:
            finished.append(True)
            engine.finish(**identity('finishing', 'retained'))
        return value
    monkeypatch.setattr(Turn, '__getattribute__', finish_during_read)
    if cleanup == 'exclude':
        engine.exclude_session('conversation')
    else:
        engine.command(command='model', args_raw='different', session_key='conversation')
    assert finished
    assert request(engine, turn='retained') is None
    assert request(engine, 'remaining', turn='retained')['request']['model'] == NEW.model


@pytest.mark.parametrize('phase', ['running', 'watch', 'paused', 'done', 'undone', 'stopped', 'manual'])
def test_instructed_chat_move_phase_contract_excludes_ended_switches(engine, phase):
    record = start(engine)
    record.phase = phase
    if phase in CHAT_MOVE_PHASES:
        assert is_active(record)
    if phase in {'watch', 'paused'}:
        assert phase in CHAT_MOVE_PHASES


def write_default_in_process(path, default_path, target, entered, release):
    def write(route):
        if route == target:
            entered.set()
            assert release.wait(5)
        Path(default_path).write_text(f'{route.provider}\n{route.model}')
    Engine(Store(path=Path(path)), set_default=write).reconcile_config()


@pytest.mark.parametrize('target', [NEW, CROSS])
def test_separate_process_finishes_undo_with_the_old_provider_and_model(tmp_path, target):
    path, default_path = tmp_path / 'switch.db', tmp_path / 'defaults'
    def write(route):
        default_path.write_text(f'{route.provider}\n{route.model}')
    owner = Engine(Store(path=path), verified_pair=lambda *_: True, set_default=write)
    owner.start(Proposal('saved', OLD, target, 'better'))
    with owner.store.transaction() as holder:
        holder[0].phase = 'done'
        holder[0].config_pending = 'new'
    context = multiprocessing.get_context('spawn')
    entered, release = context.Event(), context.Event()
    process = context.Process(target=write_default_in_process,
                              args=(path, default_path, target, entered, release))
    process.start()
    try:
        assert entered.wait(5)
        owner.undo()
        assert owner.record.phase == 'undone'
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
    assert default_path.read_text() == f'{OLD.provider}\n{OLD.model}'
    assert owner.record.config_pending is None
    assert not owner.record.default_changed


def test_salt_publication_during_session_start_never_persists_a_throwaway_identity(tmp_path, monkeypatch):
    path = tmp_path / 'switch.db'
    gateway = Engine(Store(path=path))
    owner = Engine(Store(path=path), verified_pair=lambda *_: True)
    read = Store.read
    published = []
    def publish_before_read(store):
        if store is gateway.store and not published:
            published.append(True)
            start(owner)
            owner.advance()
            owner.advance()
        return read(store)
    monkeypatch.setattr(Store, 'read', publish_before_read)
    gateway.session_started(session_id='conversation', session_key='chat', platform='cli')
    record = gateway.record
    session = owner.store.identity('conversation')
    key = owner.store.identity('chat')
    assert set(record.sessions) <= {session}
    assert set(record.session_keys) <= {key}
    admit(gateway)
    assert request(gateway)['request']['model'] == NEW.model
