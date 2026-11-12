"""Setup proofs call Shadow policies with an inert terminal executor."""

import importlib
import sys
from types import SimpleNamespace

import pytest
import yaml

from test_real_tools import require_hermes_reference


@pytest.fixture
def hermes_dispatch(tmp_path, monkeypatch):
    source = require_hermes_reference()
    before = dict(sys.modules)
    monkeypatch.syspath_prepend(str(source))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes-home'))
    monkeypatch.setitem(sys.modules, 'hermes_yaml', yaml)
    terminal = SimpleNamespace(get_active_env=lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, 'tools.terminal_tool_lifecycle', terminal)
    plugins = importlib.import_module('hermes_cli.plugins')
    middleware = importlib.import_module('hermes_cli.middleware')
    executor = importlib.import_module('agent.tool_executor')
    lifecycle = importlib.import_module('hermes_cli.lifecycle')
    manager = plugins.PluginManager(scope_key=str(tmp_path / 'hermes-home'))
    monkeypatch.setattr(plugins, '_delivery_manager', lambda: manager)
    monkeypatch.setattr(plugins, '_resolve_hook_callback_timeout', lambda: 0)
    monkeypatch.setattr(lifecycle, '_observe', lambda *_args, **_kwargs: None)

    def register(guard):
        manager._hooks['pre_tool_call'] = [guard.pre_tool_call]
        manager._middleware['tool_execution'] = [guard.tool_execution]
        return manager

    try:
        yield SimpleNamespace(register=register, manager=manager, plugins=plugins,
                              middleware=middleware, executor=executor)
    finally:
        for name, module in list(sys.modules.items()):
            path = getattr(module, '__file__', '') or ''
            if str(source) in path:
                if name in before:
                    sys.modules[name] = before[name]
                else:
                    sys.modules.pop(name, None)


def dispatch_case(tmp_path, hermes_dispatch=None):
    from shadow.trial.reads import SafeReads
    from shadow.trial.safety import ReplayGuard

    guard = ReplayGuard()
    guard.add_controller('controller')
    guard.expect('real-child-token', tmp_path)
    guard.subagent_start(parent_session_id='controller', child_session_id='real-child',
                         child_goal='real-child-token')
    guard.reads = SafeReads(guard)
    tokens = {'scope': 'allowed-note-canary', 'secret': 'secret-canary', 'outside': 'outside-canary'}
    (tmp_path / 'notes.txt').write_text(tokens['scope'])
    (tmp_path / '.env').write_text(tokens['secret'])
    (tmp_path.parent / 'outside.txt').write_text(tokens['outside'])
    if hermes_dispatch is not None:
        hermes_dispatch.register(guard)
    return guard, tokens


def test_registered_dispatch_proves_independent_layers(tmp_path):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    guard, tokens = dispatch_case(tmp_path)
    original = guard.children['real-child']
    assert run_dispatch_checks(guard, 'real-child', tmp_path, tokens) == {
        'veto': True, 'middleware': True, 'read_only': True, 'secrets': True,
    }
    assert guard.children['real-child'] is original
    assert original.stopped is False
    assert set(guard.children) == {'real-child'}


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
def test_bypassed_callback_fails_proof(tmp_path, monkeypatch, layer):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    guard, tokens = dispatch_case(tmp_path)
    if layer == 'veto':
        monkeypatch.setattr(guard, 'pre_tool_call', lambda **kwargs: None)
    else:
        monkeypatch.setattr(guard, 'tool_execution', lambda **kwargs: kwargs['next_call'](kwargs['args']))
    assert run_dispatch_checks(guard, 'real-child', tmp_path, tokens)[layer] is False


def test_weakened_middleware_cannot_hide_behind_veto_stop(tmp_path, monkeypatch):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    guard, tokens = dispatch_case(tmp_path)
    original = guard.tool_execution

    def allow_terminal(**kwargs):
        if kwargs['tool_name'] == 'terminal':
            return kwargs['next_call'](kwargs['args'])
        return original(**kwargs)

    monkeypatch.setattr(guard, 'tool_execution', allow_terminal)
    checks = run_dispatch_checks(guard, 'real-child', tmp_path, tokens)
    assert checks['veto'] is True
    assert checks['middleware'] is False


@pytest.mark.parametrize('path,token', [('.env', 'secret'), ('../outside.txt', 'outside')])
def test_returning_forbidden_canary_fails_proof(tmp_path, monkeypatch, path, token):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    guard, tokens = dispatch_case(tmp_path)
    original = guard.tool_execution

    def leak(**kwargs):
        if kwargs['args'].get('path') == path:
            return tokens[token]
        return original(**kwargs)

    monkeypatch.setattr(guard, 'tool_execution', leak)
    checks = run_dispatch_checks(guard, 'real-child', tmp_path, tokens)
    assert checks['read_only'] is True
    assert checks['secrets'] is False


def test_inert_executor_records_attempt_when_policy_is_removed(tmp_path, monkeypatch):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    guard, tokens = dispatch_case(tmp_path)
    monkeypatch.setattr(guard, 'pre_tool_call', lambda **_kwargs: None)
    monkeypatch.setattr(guard, 'tool_execution', lambda **kwargs: kwargs['next_call'](kwargs['args']))
    assert run_dispatch_checks(guard, 'real-child', tmp_path, tokens) == {
        'veto': False, 'middleware': False, 'read_only': False, 'secrets': False,
        'reason_code': 'read_only_scope_token_missing,read_only_middleware_executor_reached',
    }


@pytest.mark.parametrize('failure', ['child_missing', 'scope_mismatch'])
def test_read_only_reports_invalid_child_scope(tmp_path, failure):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    guard, tokens = dispatch_case(tmp_path)
    scope = tmp_path
    if failure == 'child_missing':
        del guard.children['real-child']
    else:
        scope = tmp_path.parent
    checks = run_dispatch_checks(guard, 'real-child', scope, tokens)
    assert checks['read_only'] is False
    assert checks['reason_code'] == f'read_only_{failure}'


def test_read_only_diagnoses_protected_profile_scope(tmp_path):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    scope = tmp_path / '.hermes' / 'profiles' / 'demo' / 'scope'
    scope.mkdir(parents=True)
    guard, tokens = dispatch_case(scope)
    checks = run_dispatch_checks(guard, 'real-child', scope, tokens)
    assert checks['veto'] is True
    assert checks['middleware'] is True
    assert checks['secrets'] is True
    assert checks['read_only'] is False
    assert set(checks['reason_code'].split(',')) == {
        'read_only_veto_executor_not_reached',
        'read_only_veto_result_mismatch',
        'read_only_scope_token_missing',
    }


def test_dispatch_read_scope_accepts_a_symlink_alias(tmp_path):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    scope = tmp_path / 'real'
    scope.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(scope, target_is_directory=True)
    guard, tokens = dispatch_case(alias)
    assert run_dispatch_checks(guard, 'real-child', alias, tokens)['read_only'] is True


def test_failure_reason_survives_result_file_and_rendering(tmp_path):
    import json
    from shadow.trial.probe import failure_text, write_result

    reason = 'read_only_scope_token_missing,read_only_middleware_executor_reached'
    body = write_result(tmp_path, 'fixture', {'read_only': False, 'reason_code': reason}, sessions=[])
    assert body['reason_code'] == reason
    saved = json.loads((tmp_path / 'setup-result-fixture.json').read_text())
    assert reason in failure_text(saved)
