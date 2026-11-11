import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from shadow.switch import Engine, Proposal, Store
from shadow.switch.copy import copy
from shadow.switch.plugin import SwitchPlugin
from test_switch_safety import OLD, NEW, identity, start
from test_switch_states import CROSS, turn


def test_undo_in_the_holders_final_read_restores_default_before_return(tmp_path, monkeypatch):
    path = tmp_path / 'switch.db'
    writes = []
    first = Engine(Store(path=path), verified_pair=lambda *_: True, set_default=writes.append)
    second = Engine(Store(path=path), set_default=writes.append)
    first.start(Proposal('saved', OLD, NEW, 'better'))
    with first.store.transaction() as holder:
        holder[0].phase = 'done'
        holder[0].config_pending = 'new'
    read = Engine.record.fget
    undone = []

    def undo_after_empty_read(engine):
        record = read(engine)
        if engine is first and record.config_pending is None and not undone:
            undone.append(True)
            second.undo()
        return record

    monkeypatch.setattr(Engine, 'record', property(undo_after_empty_read))
    first.reconcile_config()
    assert writes == [NEW, OLD]
    assert second.record.phase == 'undone'
    assert second.record.config_pending is None
    assert not second.record.default_changed


@pytest.mark.parametrize('hook', ['session_started', 'admit'])
@pytest.mark.parametrize('action', ['undo', 'stop'])
@pytest.mark.parametrize('recovering_holder', [False, True])
def test_next_hook_recovers_restore_after_holder_fails(tmp_path, hook, action, recovering_holder):
    path = tmp_path / 'switch.db'
    writes = []
    second = Engine(Store(path=path), set_default=writes.append)

    def fail_after_undo(route):
        writes.append(route)
        second.undo(stop=action == 'stop')
        raise RuntimeError('fixture interrupted writer')

    first = Engine(Store(path=path), set_default=fail_after_undo)
    with pytest.raises(RuntimeError, match='fixture interrupted writer'):
        first.start(Proposal('saved', OLD, CROSS, 'better'))
    assert writes == [CROSS]
    assert second.record.config_pending == 'old'
    first.set_default = writes.append
    recovering = first if recovering_holder else second
    getattr(recovering, hook)(**identity(), platform='cli', model=CROSS.model)
    assert writes == [CROSS, OLD]
    assert second.record.config_pending is None
    assert not second.record.default_changed
    assert second.record.phase == ('stopped' if action == 'stop' else 'undone')


@pytest.mark.parametrize('hook', ['session_started', 'admit'])
def test_hook_restore_retries_stay_throttled(tmp_path, monkeypatch, hook):
    now = [100.0]
    monkeypatch.setattr('shadow.switch.engine.monotonic', lambda: now[0])
    writes = []
    engine = Engine(Store(path=tmp_path / 'switch.db'), set_default=writes.append)
    engine.start(Proposal('saved', OLD, CROSS, 'better'))

    def fail(route):
        writes.append(route)
        raise RuntimeError('fixture config unavailable')

    engine.set_default = fail
    with pytest.raises(RuntimeError):
        engine.undo()
    engine.set_default = writes.append
    for _ in range(3):
        getattr(engine, hook)(**identity(), platform='cli', model=CROSS.model)
    assert writes == [CROSS, OLD]
    assert engine.record.config_pending == 'old'
    now[0] += 30
    getattr(engine, hook)(**identity(), platform='cli', model=CROSS.model)
    assert writes == [CROSS, OLD, OLD]
    assert engine.record.config_pending is None


def test_repeated_intent_at_lock_release_is_bounded_and_recovers_later(tmp_path, monkeypatch):
    path = tmp_path / 'switch.db'
    writes = []
    first = Engine(Store(path=path), set_default=writes.append)
    second = Engine(Store(path=path), set_default=writes.append)
    first.start(Proposal('saved', OLD, CROSS, 'better'))
    read = Engine.record.fget
    arrivals = []

    def requeue_after_empty_read(engine):
        record = read(engine)
        if engine is first and record.config_pending is None and len(arrivals) < 10:
            arrivals.append(True)
            with second.store.transaction() as holder:
                holder[0].config_pending = 'old'
        return record

    monkeypatch.setattr(Engine, 'record', property(requeue_after_empty_read))
    first.undo()
    assert 2 <= writes.count(OLD) <= 4
    assert second.record.config_pending == 'old'
    monkeypatch.setattr(Engine, 'record', property(read))
    second.session_started(session_id='later-chat', platform='cli')
    assert writes[-1] == OLD
    assert second.record.config_pending is None


@pytest.mark.parametrize('replacement_route', [NEW, CROSS])
def test_start_refuses_replacement_while_holder_restores_default(tmp_path, replacement_route):
    path = tmp_path / 'switch.db'
    writes = []
    second = Engine(Store(path=path), verified_pair=lambda *_: True, set_default=writes.append)
    proposal = Proposal('replacement-test', OLD, replacement_route, 'better')
    from shadow.common.discovery import ModelCatalog
    from shadow.common.models import Model
    models = [Model(route.provider, route.model) for route in (OLD, replacement_route)]
    service = SwitchPlugin(SimpleNamespace(), second, config_reader=lambda: {},
                           proposal_lookup=lambda _: proposal,
                           catalog_reader=lambda: ModelCatalog.from_models(models[0], models))

    def refuse_replacement_during_restore(route):
        writes.append(route)
        if route == OLD:
            before = second.record.to_dict()
            with pytest.raises(ValueError, match='a switch is in progress'):
                second.start(proposal, switch_id='replacement')
            assert service.command('switch replacement-test') == copy('switch.error.busy')
            assert second.record.to_dict() == before

    first = Engine(Store(path=path), set_default=refuse_replacement_during_restore)
    first.start(Proposal('saved', OLD, CROSS, 'better'), switch_id='original')
    writes.clear()
    first.undo()
    assert writes == [OLD]
    assert second.record.id == 'original'
    assert second.record.phase == 'undone'
    assert second.record.config_pending is None
    assert not second.record.default_changed


def test_import_without_fcntl_keeps_idle_use_and_refuses_file_backed_starts(tmp_path):
    script = textwrap.dedent("""
        import builtins
        from pathlib import Path
        import sqlite3
        import sys

        original_import = builtins.__import__
        def without_fcntl(name, *args, **kwargs):
            if name == 'fcntl':
                raise ModuleNotFoundError("fixture platform has no fcntl")
            return original_import(name, *args, **kwargs)
        builtins.__import__ = without_fcntl

        from shadow.switch import Engine, Proposal, Route, Store
        old = Route('fixture', 'old', 'https://fixture.invalid/v1', 'chat_completions')
        new = Route('fixture', 'new', 'https://fixture.invalid/v1', 'chat_completions')
        cross = Route('other', 'different', 'https://other.invalid/v1', 'responses')
        writes = []
        engine = Engine(Store(path=Path(sys.argv[1])), verified_pair=lambda *_: True,
                        set_default=writes.append)
        assert engine.record is None
        for target in (new, cross):
            try:
                engine.start(Proposal('saved', old, target, 'better'))
            except RuntimeError as error:
                assert 'locking' in str(error)
            else:
                raise AssertionError('file-backed start must require cross-process locking')
            assert engine.record is None
            assert engine.store.row_count() == 0
            assert not Path(sys.argv[1]).exists()
        assert writes == []

        memory = Engine(Store(sqlite3.connect(':memory:')), verified_pair=lambda *_: True,
                        set_default=writes.append)
        memory.start(Proposal('gradual', old, new, 'better'))
        memory.undo()
        memory.start(Proposal('cross', old, cross, 'better'))
        assert writes == [cross]
        assert memory.record.config_pending is None
    """)
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'switch.db')],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('manual_moves', [(True, True), (True, False), (False, True), (False, False)])
def test_step_notices_save_actual_turn_counts_and_move_source_across_resume(tmp_path, manual_moves):
    path = tmp_path / 'switch.db'
    engine = Engine(Store(path=path), verified_pair=lambda *_: True)
    start(engine, step_turns=3)
    expected = []
    for previous_step, manual in enumerate(manual_moves, start=1):
        for i in range(2):
            turn(engine, f'{previous_step}-old-{i}', side='old')
        observed = (0 if previous_step == 1 else 2) if manual else 3
        for i in range(observed):
            turn(engine, f'{previous_step}-new-{i}')
        if manual:
            engine.advance()
        assert engine.record.step == previous_step + 1
        expected.append({'kind': f'step{previous_step + 1}', 'turns': observed, 'manual': manual})

    def transitions(events):
        return [{key: event[key] for key in ('kind', 'turns', 'manual')}
                for event in events if event['kind'] in {'step2', 'step3'}]

    restarted = Engine(Store(path=path))
    assert transitions(restarted.record.notices) == expected
    assert transitions(restarted.record.history) == expected
    engine.drain_notices()
    for i in range(3):
        turn(engine, f'failed-{i}', failed=True)
    assert engine.record.phase == 'paused'
    engine.resume()
    engine.advance()
    assert transitions(restarted.record.history) == expected + [
        {'kind': 'step2', 'turns': 0, 'manual': True}]
    assert transitions(restarted.record.notices) == [{'kind': 'step2', 'turns': 0, 'manual': True}]
