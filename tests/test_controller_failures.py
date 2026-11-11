import json
import logging

import pytest

from shadow.common.copy import load_copy
from shadow.common.storage import Store
from shadow.desktop_rpc import DesktopRpc
from shadow.desktop_source import ProfileSource
from test_controller_runtime import queued_trial
from test_plugin import service


def attached_trial(tmp_path):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    ctx.hook('pre_llm_call', session_id='controller',
             user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}')
    return ctx, plugin, record


@pytest.mark.parametrize('event,reason,code', [
    ({'failed': True, 'turn_exit_reason': 'repeated_outer_errors(private failure detail)'},
     'controller_failed', 'repeated_outer_errors'),
    ({'interrupted': True, 'turn_exit_reason': 'interrupted_during_api_call'},
     'controller_interrupted', 'interrupted_during_api_call'),
    ({'interrupted': True, 'turn_exit_reason': 'interrupted_by_user'},
     'controller_interrupted', 'interrupted_by_user'),
    ({'interrupted': True, 'turn_exit_reason': 'interrupted_by_system(private failure detail)'},
     'controller_interrupted', 'interrupted_by_system'),
    ({'failed': True, 'turn_exit_reason': 'ollama_runtime_context_too_small'},
     'controller_failed', 'ollama_runtime_context_too_small'),
    ({'failed': True, 'turn_exit_reason': 'private failure detail'},
     'controller_failed', 'unknown'),
    ({'failed': True, 'interrupted': True, 'turn_exit_reason': 'interpreter_shutdown'},
     'controller_failed', 'interpreter_shutdown'),
])
def test_session_failure_keeps_reason_and_stops_children(tmp_path, event, reason, code):
    ctx, plugin, record = attached_trial(tmp_path)
    ctx.tools['shadow_trial_step']({}, session_id='controller')
    stale = plugin.store.get(record['id'])
    ctx.hook('on_session_end', session_id='controller', **event)
    saved = plugin.store.get(record['id'])
    assert saved['state'] == 'interrupted' and saved['stop_reason'] == reason
    assert saved['controller_stop_details']['turn_exit_reason'] == code
    assert len(ctx.subagent_lifecycle.cancelled) == 2
    plugin.store.save(stale)
    saved = Store(tmp_path / 'trial.sqlite').get(record['id'])
    assert saved['stop_reason'] == reason
    assert saved['controller_stop_details']['turn_exit_reason'] == code
    assert 'private failure detail' not in json.dumps(saved)
    ctx.hook('on_session_end', session_id='controller', completed=True)
    assert plugin.store.get(record['id'])['stop_reason'] == reason
    text = plugin.command('status')
    page = DesktopRpc(ProfileSource(plugin), load_copy()).page_state({'test_id': record['id']})
    for rendered in (text, page['stopped']['summary']):
        assert ('failed' if event.get('failed') else 'interrupted') in rendered.lower()
        assert 'Start a new test with' not in rendered
        if event.get('failed'):
            assert code in rendered
        assert 'private failure detail' not in rendered


def test_step_launch_exception_records_type_without_exception_text(tmp_path, caplog):
    ctx, plugin, record = attached_trial(tmp_path)
    launch = ctx.subagent_lifecycle.launch
    calls = []

    def fail_second(request):
        calls.append(request)
        if len(calls) == 2:
            raise RuntimeError('private launch failure detail')
        return launch(request)

    ctx.subagent_lifecycle.launch = fail_second
    with caplog.at_level(logging.WARNING):
        result = json.loads(ctx.tools['shadow_trial_step']({}, session_id='controller'))
    assert result['continue'] is False and result['state'] == 'interrupted'
    saved = plugin.store.get(record['id'])
    assert saved['stop_reason'] == 'step_error'
    assert saved['controller_stop_details']['error_type'] == 'RuntimeError'
    assert len(ctx.subagent_lifecycle.cancelled) == 1
    assert 'RuntimeError' in caplog.text
    assert 'private launch failure detail' not in caplog.text + json.dumps(saved) + json.dumps(result)
    assert 'failed' in plugin.command('status').lower()
    assert 'RuntimeError' in plugin.command('status')
    assert 'Start a new test with' not in plugin.command('status')


@pytest.mark.parametrize('details,expected', [
    ({'turn_exit_reason': 'timeout', 'error_type': 'RuntimeError'}, 'timeout'),
    ({'error_type': 'RuntimeError'}, 'RuntimeError'),
    ({}, 'unknown'),
])
def test_controller_failure_reason_is_visible_in_status_and_desktop(tmp_path, details, expected):
    ctx, plugin, record = attached_trial(tmp_path)
    assert plugin.store.controller_stopped(record['id'], record['controller_token'],
                                           'controller_failed', details=details)
    page = DesktopRpc(ProfileSource(plugin), load_copy()).page_state({'test_id': record['id']})
    for text in (plugin.command('status'), page['stopped']['summary']):
        assert f'Failure reason: {expected}.' in text
        assert 'Check the recorded failure reason' not in text


@pytest.mark.parametrize('state', ['finished', 'cancelled'])
def test_failed_session_end_does_not_replace_terminal_result(tmp_path, state):
    ctx, plugin, record = attached_trial(tmp_path)
    plugin.engine.record['state'] = state
    plugin.engine.record['stop_reason'] = state
    plugin.engine.save()
    ctx.hook('on_session_end', session_id='controller', failed=True, turn_exit_reason='interpreter_shutdown')
    assert plugin.store.get(record['id'])['state'] == state
