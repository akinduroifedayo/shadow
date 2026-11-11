import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from shadow.common.storage import Store
from shadow.trial.plugin import register
from test_controller_failures import attached_trial
from test_controller_runtime import queued_trial
from test_plugin import service
from test_probe_dispatch import hermes_dispatch


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
@pytest.mark.parametrize('reloads', [1, 2])
@pytest.mark.parametrize('start_hook', [False, True])
def test_reload_blocks_inflight_child_and_preserves_progress(tmp_path, monkeypatch, hermes_dispatch, layer, reloads, start_hook):
    ctx, plugin, record = attached_trial(tmp_path)
    ctx.subagent_lifecycle.start_hook = start_hook
    ctx.tools['shadow_trial_step']({}, session_id='controller')
    assert len(plugin.engine.flights) == 2
    child = next(iter(plugin.guard.children))
    before = plugin.store.get(record['id'])
    monkeypatch.setattr('shadow.trial.plugin.Store', lambda: Store(tmp_path / 'trial.sqlite'))
    for _ in range(reloads):
        ctx.hooks.clear()
        ctx.middleware.clear()
        register(ctx)
    guard = ctx.shadow_trial.guard
    assert guard.pre_llm_call(session_id='telegram-1') is None
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='telegram-1') is None
    assert guard.tool_execution(tool_name='terminal', args={}, session_id='telegram-1',
                                next_call=lambda _: 'normal chat tool') == 'normal chat tool'
    hermes_dispatch.manager._hooks['pre_tool_call'] = ctx.hooks['pre_tool_call']
    hermes_dispatch.manager._middleware['tool_execution'] = [ctx.middleware['tool_execution']]
    attempts = []
    args = {'command': 'fixture command that must never execute'}
    if layer == 'veto':
        ref = hermes_dispatch.executor._ToolCallRef('terminal', args, '', 'reload-call', [])
        block, _ = hermes_dispatch.executor._pre_tool_block(SimpleNamespace(session_id=child), ref)
        assert block == ctx.shadow_trial.guard.stop_message
    else:
        result = hermes_dispatch.middleware.run_tool_execution_middleware(
            'terminal', args, lambda value: attempts.append(value) or 'real tool ran', session_id=child)
        assert json.loads(result)['error'] == ctx.shadow_trial.guard.stop_message
    assert attempts == []
    saved = plugin.store.get(record['id'])
    assert saved['state'] == 'interrupted' and saved['stop_reason'] == 'plugin_reloaded'
    assert saved['turns'] == before['turns'] and saved['spend'] == before['spend']
    assert ctx.shadow_trial.active_record() is None
    text = ctx.shadow_trial.command('status')
    assert 'reloaded' in text and 'Finished turns and spend are saved.' in text
    result = json.loads(ctx.tools['shadow_trial_step']({}, session_id='controller'))
    assert result['continue'] is False


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_registration_protects_foreign_controller_without_stopping_it(tmp_path, monkeypatch, state):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    record.update(state=state, controller_session_id='foreign-controller')
    plugin.store.save(record)
    plugin.store.controller_started(record['id'], record['controller_token'], os.getpid() + 100000)
    monkeypatch.setattr('shadow.trial.plugin.Store', lambda: Store(tmp_path / 'trial.sqlite'))
    ctx.hooks.clear()
    register(ctx)
    assert plugin.store.get(record['id'])['state'] == state
    guard = ctx.shadow_trial.guard
    assert guard.pre_llm_call(session_id='telegram-1') is None
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='telegram-1') is None
    assert guard.pre_llm_call(session_id='unidentified-child', parent_session_id='foreign-controller')['action'] == 'block'
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='unidentified-child')['action'] == 'block'
    assert json.loads(guard.tool_execution(tool_name='terminal', args={}, session_id='unidentified-child',
        next_call=lambda _: pytest.fail('real tool ran')))['error'] == guard.stop_message


@pytest.mark.parametrize('state', ['finished', 'cancelled', 'interrupted'])
def test_terminal_reload_record_does_not_rearm_controller(tmp_path, monkeypatch, state):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    record.update(state=state, stop_reason='plugin_reloaded', controller_session_id='old-controller')
    plugin.store.save(record)
    monkeypatch.setattr('shadow.trial.plugin.Store', lambda: Store(tmp_path / 'trial.sqlite'))
    ctx.hooks.clear()
    register(ctx)
    guard = ctx.shadow_trial.guard
    assert guard.pre_llm_call(session_id='telegram-1') is None
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='telegram-1') is None
    assert guard.pre_llm_call(session_id='ordinary-child', parent_session_id='old-controller') is None


@pytest.mark.parametrize('observer', ['subagent_start', 'pre_llm_call'])
def test_child_is_saved_before_first_provider_request(tmp_path, monkeypatch, observer):
    ctx, plugin, record = attached_trial(tmp_path)
    plugin.guard.expect('fixture-child-token', tmp_path)
    if observer == 'subagent_start':
        ctx.hook(observer, parent_session_id='controller', child_session_id='saved-child',
                 child_goal='fixture-child-token')
    else:
        ctx.hook(observer, parent_session_id='controller', session_id='saved-child',
                 user_message='fixture-child-token')
    assert ctx.capture == []
    monkeypatch.setattr('shadow.trial.plugin.Store', lambda: Store(tmp_path / 'trial.sqlite'))
    for _ in range(2):
        ctx.hooks.clear()
        ctx.middleware.clear()
        register(ctx)
    guard = ctx.shadow_trial.guard
    assert guard.pre_llm_call(session_id='telegram-1') is None
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='saved-child')['action'] == 'block'
    assert guard.stop_message in guard.tool_execution(tool_name='terminal', args={}, session_id='saved-child',
                                                     next_call=lambda _: pytest.fail('real tool ran'))


@pytest.mark.parametrize('observer', ['subagent_start', 'pre_llm_call'])
def test_late_first_child_stays_blocked_after_second_reload(tmp_path, monkeypatch, observer):
    ctx, plugin, record = attached_trial(tmp_path)
    monkeypatch.setattr('shadow.trial.plugin.Store', lambda: Store(tmp_path / 'trial.sqlite'))
    ctx.hooks.clear()
    ctx.middleware.clear()
    register(ctx)
    assert ctx.shadow_trial.store.get(record['id'])['state'] == 'interrupted'
    if observer == 'subagent_start':
        ctx.hook(observer, parent_session_id='controller', child_session_id='late-child')
    else:
        ctx.hook(observer, parent_session_id='controller', session_id='late-child')
    ctx.hooks.clear()
    ctx.middleware.clear()
    register(ctx)
    guard = ctx.shadow_trial.guard
    assert guard.pre_llm_call(session_id='telegram-1') is None
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='late-child')['action'] == 'block'
    assert guard.stop_message in guard.tool_execution(tool_name='terminal', args={}, session_id='late-child',
                                                     next_call=lambda _: pytest.fail('real tool ran'))


def test_concurrent_recovered_descendants_stay_blocked_after_reload(tmp_path, monkeypatch):
    ctx, plugin, record = attached_trial(tmp_path)
    ctx.tools['shadow_trial_step']({}, session_id='controller')
    parent = next(iter(plugin.guard.children))
    monkeypatch.setattr('shadow.trial.plugin.Store', lambda: Store(tmp_path / 'trial.sqlite'))
    ctx.hooks.clear()
    ctx.middleware.clear()
    register(ctx)
    recovered = ctx.shadow_trial
    snapshots = threading.Barrier(2)
    list_records = recovered.store.list

    def read_concurrent_snapshots():
        records = list_records()
        try:
            snapshots.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return records

    monkeypatch.setattr(recovered.store, 'list', read_concurrent_snapshots)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(recovered.subagent_start, parent_session_id=parent,
                                  child_session_id=child) for child in ('nested-1', 'nested-2')]
        for future in futures:
            future.result(timeout=5)
    ctx.hooks.clear()
    ctx.middleware.clear()
    register(ctx)
    guard = ctx.shadow_trial.guard
    assert guard.pre_llm_call(session_id='telegram-1') is None
    for child in ('nested-1', 'nested-2'):
        assert guard.pre_tool_call(tool_name='terminal', args={}, session_id=child)['action'] == 'block'
        assert guard.stop_message in guard.tool_execution(tool_name='terminal', args={}, session_id=child,
                                                         next_call=lambda _: pytest.fail('real tool ran'))


def test_old_engine_save_preserves_recovered_descendant_quarantine(tmp_path, monkeypatch):
    ctx, plugin, record = attached_trial(tmp_path)
    ctx.tools['shadow_trial_step']({}, session_id='controller')
    parent = next(iter(plugin.guard.children))
    monkeypatch.setattr('shadow.trial.plugin.Store', lambda: Store(tmp_path / 'trial.sqlite'))
    ctx.hooks.clear()
    ctx.middleware.clear()
    register(ctx)
    ctx.hook('subagent_start', parent_session_id=parent, child_session_id='late-descendant')
    plugin.engine.save()
    ctx.hooks.clear()
    ctx.middleware.clear()
    register(ctx)
    guard = ctx.shadow_trial.guard
    assert guard.pre_llm_call(session_id='telegram-1') is None
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='late-descendant')['action'] == 'block'
    assert guard.stop_message in guard.tool_execution(tool_name='terminal', args={}, session_id='late-descendant',
                                                     next_call=lambda _: pytest.fail('real tool ran'))


@pytest.mark.parametrize('ending', ['cancel', 'interrupt'])
@pytest.mark.parametrize('start_hook', [False, True])
def test_trial_end_during_child_construction_keeps_child_blocked(tmp_path, monkeypatch, ending, start_hook):
    ctx, plugin, record = attached_trial(tmp_path)
    ctx.subagent_lifecycle.start_hook = start_hook
    ctx.subagent_lifecycle.tool = 'terminal'
    launch = ctx.subagent_lifecycle.launch

    def stop_during_construction(request):
        if ending == 'cancel':
            plugin.engine.cancel()
        else:
            plugin.engine.interrupt('controller_failed')
        return launch(request)

    ctx.subagent_lifecycle.launch = stop_during_construction
    assert plugin.engine.step('controller')['continue'] is False
    guard = plugin.guard
    assert guard.pre_llm_call(session_id='telegram-1') is None
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='telegram-1') is None
    monkeypatch.setattr('shadow.trial.plugin.Store', lambda: Store(tmp_path / 'trial.sqlite'))
    for _ in range(2):
        ctx.hooks.clear()
        ctx.middleware.clear()
        register(ctx)
    guard = ctx.shadow_trial.guard
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='child1')['action'] == 'block'
    assert guard.stop_message in guard.tool_execution(tool_name='terminal', args={}, session_id='child1',
                                                     next_call=lambda _: pytest.fail('real tool ran'))


def test_registration_keeps_an_attached_controller_running(tmp_path):
    ctx, plugin, record = attached_trial(tmp_path)
    register(ctx, plugin=plugin)
    assert plugin.store.get(record['id'])['state'] == 'running'
    assert json.loads(plugin.step({}, session_id='controller'))['continue'] is True


@pytest.mark.parametrize('state', ['queued', 'running'])
@pytest.mark.parametrize('terminal', [False, True])
@pytest.mark.parametrize('yes', [False, True])
def test_verify_refuses_active_trial_before_confirmation_or_launch(tmp_path, state, terminal, yes):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    if state == 'running':
        ctx.hook('pre_llm_call', session_id='controller',
                 user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}')
    plugin.setup_launch = lambda *_: pytest.fail('setup check launched during trial')
    text = plugin.command('doctor --verify' + (' --yes' if yes else ''), terminal=terminal)
    assert text == f'Test {record["id"]} is still running.'
    assert ctx.questions == []
    assert plugin.store.get(record['id'])['state'] == state
