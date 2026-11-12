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
    manager = plugins.PluginManager(scope_key=str(tmp_path / 'hermes-home'))
    monkeypatch.setattr(plugins, 'get_plugin_manager', lambda: manager)
    monkeypatch.setattr(manager, 'discover_and_load', lambda: None)
    monkeypatch.setattr(plugins, 'load_config_readonly',
                        lambda: {'plugins': {'hook_callback_timeout': 0}})
    from types import ModuleType
    observers = ModuleType('hermes_cli.observability')
    observers.observe_lifecycle = lambda *args, **kwargs: None
    observers.handles_hook = lambda *args: False
    monkeypatch.setitem(sys.modules, 'hermes_cli.observability', observers)
    from hermes_cli.plugins_context import PluginContext
    from hermes_cli.plugins_manifest import PluginManifest
    context = PluginContext(PluginManifest(name='shadow'), manager)
    registrations = []

    def register(guard):
        for registration in registrations:
            registration.dispose()
        registrations[:] = [context.register_hook('pre_tool_call', guard.pre_tool_call),
                            context.register_middleware('tool_execution', guard.tool_execution)]
        return manager

    try:
        yield SimpleNamespace(register=register, registrations=registrations, manager=manager, plugins=plugins,
                              middleware=middleware, executor=executor)
    finally:
        for name, module in list(sys.modules.items()):
            path = getattr(module, '__file__', '') or ''
            if str(source) in path:
                if name in before:
                    sys.modules[name] = before[name]
                else:
                    sys.modules.pop(name, None)


def active_registrations():
    return {name: SimpleNamespace(active=True) for name in ('veto', 'middleware')}


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


def test_guard_callbacks_prove_independent_layers(tmp_path):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    guard, tokens = dispatch_case(tmp_path)
    original = guard.children['real-child']
    assert run_dispatch_checks(guard, 'real-child', tmp_path, tokens, registrations=active_registrations()) == {
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
    assert run_dispatch_checks(guard, 'real-child', tmp_path, tokens, registrations=active_registrations())[layer] is False


def test_weakened_middleware_cannot_hide_behind_veto_stop(tmp_path, monkeypatch):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    guard, tokens = dispatch_case(tmp_path)
    original = guard.tool_execution

    def allow_terminal(**kwargs):
        if kwargs['tool_name'] == 'terminal':
            return kwargs['next_call'](kwargs['args'])
        return original(**kwargs)

    monkeypatch.setattr(guard, 'tool_execution', allow_terminal)
    checks = run_dispatch_checks(guard, 'real-child', tmp_path, tokens, registrations=active_registrations())
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
    checks = run_dispatch_checks(guard, 'real-child', tmp_path, tokens, registrations=active_registrations())
    assert checks['read_only'] is True
    assert checks['secrets'] is False


def test_inert_executor_records_attempt_when_policy_is_removed(tmp_path, monkeypatch):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    guard, tokens = dispatch_case(tmp_path)
    monkeypatch.setattr(guard, 'pre_tool_call', lambda **_kwargs: None)
    monkeypatch.setattr(guard, 'tool_execution', lambda **kwargs: kwargs['next_call'](kwargs['args']))
    assert run_dispatch_checks(guard, 'real-child', tmp_path, tokens, registrations=active_registrations()) == {
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
    checks = run_dispatch_checks(guard, 'real-child', scope, tokens, registrations=active_registrations())
    assert checks['read_only'] is False
    assert checks['reason_code'] == f'read_only_{failure}'


def test_read_only_diagnoses_protected_profile_scope(tmp_path):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    scope = tmp_path / '.hermes' / 'profiles' / 'demo' / 'scope'
    scope.mkdir(parents=True)
    guard, tokens = dispatch_case(scope)
    checks = run_dispatch_checks(guard, 'real-child', scope, tokens, registrations=active_registrations())
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
    assert run_dispatch_checks(guard, 'real-child', alias, tokens, registrations=active_registrations())['read_only'] is True


def test_failure_reason_survives_result_file_and_rendering(tmp_path):
    import json
    from shadow.trial.probe import failure_text, write_result

    reason = 'read_only_scope_token_missing,read_only_middleware_executor_reached'
    body = write_result(tmp_path, 'fixture', {'read_only': False, 'reason_code': reason}, sessions=[])
    assert body['reason_code'] == reason
    saved = json.loads((tmp_path / 'setup-result-fixture.json').read_text())
    assert reason in failure_text(saved)


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
@pytest.mark.parametrize('state', ['missing', 'inactive'])
def test_unregistered_callback_fails_proof(tmp_path, layer, state):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    guard, tokens = dispatch_case(tmp_path)
    registrations = active_registrations()
    if state == 'missing':
        del registrations[layer]
    else:
        registrations[layer].active = False
    checks = run_dispatch_checks(guard, 'real-child', tmp_path, tokens, registrations=registrations)
    assert checks[layer] is False
    assert checks['secrets'] is False
    assert checks[{'veto': 'middleware', 'middleware': 'veto'}[layer]] is True


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
def test_disposed_hermes_registration_fails_proof(tmp_path, hermes_dispatch, layer):
    from shadow.trial.probe_dispatch import run_dispatch_checks

    guard, tokens = dispatch_case(tmp_path, hermes_dispatch)
    registrations = dict(zip(('veto', 'middleware'), hermes_dispatch.registrations))
    assert all(run_dispatch_checks(guard, 'real-child', tmp_path, tokens,
                                   registrations=registrations).values())
    registrations[layer].dispose()
    checks = run_dispatch_checks(guard, 'real-child', tmp_path, tokens, registrations=registrations)
    assert checks[layer] is False and checks['secrets'] is False
