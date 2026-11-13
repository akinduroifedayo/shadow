"""Offline replay of the controller launch, discovery, and stop lifecycle."""

import importlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, selection
from shadow.common.copy import load_copy
from shadow.common.storage import Store
from shadow.desktop_rpc import DesktopRpc
from shadow.desktop_source import ProfileSource
from shadow.trial.controller import ControllerLauncher
from shadow.trial.engine import TrialEngine
from shadow.trial.isolation import CHECK_NAMES, Identity, write_record
from test_plugin import service
from test_probe_dispatch import hermes_dispatch


def queued_trial(tmp_path, plugin):
    record = TrialEngine(plugin.ctx, plugin.store, plugin.guard).prepare(
        selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    record['state'] = 'queued'
    plugin.store.save(record)
    plugin.start_controller(record)
    return plugin.store.get(record['id'])


def test_launched_controller_describes_then_calls_deferred_step(tmp_path, monkeypatch, hermes_dispatch):
    monkeypatch.setitem(sys.modules, 'snowballstemmer', SimpleNamespace(stemmer=lambda _: SimpleNamespace(stemWord=lambda word: word)))
    monkeypatch.setitem(sys.modules, 'tools.connectors.search', SimpleNamespace(
        connections_in_scope=lambda _: False, connector_entries_by_group=lambda *_a, **_kw: {},
        connectors_unavailable=lambda *_a, **_kw: pytest.fail('remote discovery attempted'),
        remote_schemas_for=lambda *_a, **_kw: ({}, None)))
    from tools import registry as registry_module, tool_search
    from hermes_cli.plugins_context import PluginContext
    from hermes_cli.plugins_manifest import PluginManifest

    ctx, plugin = service(tmp_path)
    identity = Identity('fixture', '7f3ac61295', 'fixture', 'fixture-hash', 'shadow-demo')
    write_record(tmp_path, identity, {name: True for name in CHECK_NAMES})
    launched = []
    plugin.launcher = ControllerLauncher('shadow-demo', tmp_path, identity=identity,
                                        popen=lambda argv, **kw: launched.append((argv, kw)) or SimpleNamespace(pid=123))
    record = queued_trial(tmp_path, plugin)
    argv, options = launched[0]
    assert argv[:5] == ['hermes', '-p', 'shadow-demo', 'chat', '--query-file']
    assert '-Q' in argv and '--ignore-rules' in argv and options['start_new_session']
    prompt = Path(argv[5]).read_text()
    ctx.hook('pre_llm_call', session_id='controller', user_message=prompt)
    hermes_dispatch.register(plugin.guard)
    registry = registry_module.ToolRegistry()
    monkeypatch.setattr(registry_module, 'registry', registry)
    monkeypatch.setattr(registry_module, 'discover_builtin_tools', lambda: None)
    monkeypatch.setattr(hermes_dispatch.plugins, 'discover_plugins', lambda: None)
    manager = hermes_dispatch.manager
    manager._discovered = True
    monkeypatch.setattr(hermes_dispatch.plugins, 'get_plugin_manager', lambda: manager)
    context = PluginContext(PluginManifest(name='shadow-trial'), manager)
    schema = {'name': 'shadow_trial_step', 'description': 'Advance the Shadow trial.',
              'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
    context.register_tool('shadow_trial_step', 'shadow_controller', schema, ctx.tools['shadow_trial_step'])
    tools_config = importlib.import_module('hermes_cli.tools_config')
    monkeypatch.setattr(tools_config, '_xai_credentials_present', lambda: False)
    model_tools = importlib.import_module('model_tools')
    monkeypatch.setattr(model_tools, 'registry', registry)
    agent = SimpleNamespace(session_id='controller',
                            enabled_toolsets=sorted(tools_config._get_platform_tools({}, 'cli')),
                            disabled_toolsets=None)
    assert 'shadow_controller' in agent.enabled_toolsets
    definitions = [{'type': 'function', 'function': schema}]
    config = tool_search.ToolSearchConfig.from_raw({'enabled': 'on'})
    monkeypatch.setattr(tool_search, 'load_config_readonly', lambda: config)
    monkeypatch.setattr(tool_search, 'load_config', lambda: config)
    assembled = tool_search.assemble_tool_defs(definitions, config=config)
    assert assembled.activated
    assert 'shadow_trial_step' not in {t['function']['name'] for t in assembled.tool_defs}
    assert 'shadow_trial_step' in hermes_dispatch.executor._tool_search_scoped_names(agent)

    def dispatch(name, args, handler):
        name, args, scope_error = hermes_dispatch.executor._unwrap_tool_search_call(
            agent, name, args)
        assert scope_error is None
        directive = plugin.guard.pre_tool_call(tool_name=name, args=args, session_id='controller')
        assert directive is None, directive
        result = plugin.guard.tool_execution(tool_name=name, args=args, next_call=handler,
                                             original_args=args, session_id='controller')
        assert plugin.guard.stop_message not in result
        return json.loads(result)

    described = dispatch('tool_describe', {'names': ['shadow_trial_step']},
                         lambda args: tool_search.dispatch_tool_describe(args, current_tool_defs=definitions, config=config))
    assert described['tools']['shadow_trial_step']['parameters'] == schema['parameters']
    for _ in range(168):
        result = dispatch('tool_call', {'calls': [{'name': 'shadow_trial_step', 'arguments': {}}]},
                          lambda args: ctx.tools['shadow_trial_step'](args, session_id='controller'))
        if not result['continue']:
            break
    else:
        pytest.fail('controller did not finish within its launch bound')
    assert result['state'] == 'finished'
    assert plugin.store.get(record['id'])['totals']['pairs'] == 20


@pytest.mark.parametrize('tool', ['tool_search'])
@pytest.mark.parametrize('layer', ['veto', 'middleware'])
def test_discovery_is_controller_only(tmp_path, tool, layer):
    ctx, plugin = service(tmp_path)
    guard = plugin.guard
    guard.add_controller('controller', exclusive=True)
    guard.expect('fixture-token', tmp_path)
    guard.subagent_start(parent_session_id='controller', child_session_id='child', child_goal='fixture-token')
    if layer == 'veto':
        assert guard.pre_tool_call(tool_name=tool, args={}, session_id='controller') is None
        assert guard.pre_tool_call(tool_name=tool, args={}, session_id='child')['message'] == guard.stop_message
    else:
        assert guard.tool_execution(tool_name=tool, args={}, session_id='controller', next_call=lambda _: 'schema') == 'schema'
        result = guard.tool_execution(tool_name=tool, args={}, session_id='child', next_call=lambda _: pytest.fail('child discovery executed'))
        assert guard.stop_message in result


@pytest.mark.parametrize('tool', ['terminal', 'tool_call', 'unknown'])
def test_controller_denial_never_uses_replay_stop(tmp_path, tool):
    _, plugin = service(tmp_path)
    plugin.guard.add_controller('controller', exclusive=True)
    directive = plugin.guard.pre_tool_call(tool_name=tool, args={}, session_id='controller')
    assert directive['action'] == 'block' and directive['message'] != plugin.guard.stop_message
    result = plugin.guard.tool_execution(tool_name=tool, args={}, session_id='controller', next_call=lambda _: pytest.fail('tool ran'))
    assert 'error' in json.loads(result) and plugin.guard.stop_message not in result


@pytest.mark.parametrize('surface', ['status', 'results', 'card', 'try'])
def test_dead_controller_stops_and_allows_a_new_trial(tmp_path, surface):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait(timeout=10)
    plugin.store.controller_started(record['id'], record['controller_token'], child.pid)
    plugin.launcher.is_alive = ControllerLauncher().is_alive
    plugin.launcher.isolation_verified = False
    if surface == 'card':
        page = DesktopRpc(ProfileSource(plugin), load_copy()).page_state({'test_id': record['id']})
        assert page['mode'] == 'stopped'
        assert page['top']['try']['disabled'] is False
        text = page['stopped']['summary']
    elif surface == 'try':
        text = plugin.command(f'try {TRIAL.model}')
        assert 'already running' not in text
    else:
        text = plugin.command('status' if surface == 'status' else f'results {record["id"]}')
    saved = plugin.store.get(record['id'])
    assert saved['state'] == 'interrupted' and saved['stop_reason'] == 'controller_exited'
    assert plugin.active_record() is None
    if surface != 'try':
        assert 'stopped' in text.lower() and 'controller' in text.lower()
    assert len(plugin.launcher.launched) == 1


def test_controller_session_end_stops_while_process_is_alive(tmp_path):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    ctx.hook('pre_llm_call', session_id='controller', user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}')
    plugin.launcher.is_alive = lambda _: True
    ctx.tools['shadow_trial_step']({}, session_id='controller')
    ctx.hook('on_session_end', session_id='unrelated', completed=True)
    assert plugin.store.get(record['id'])['state'] == 'running'
    ctx.hook('on_session_end', session_id='controller', completed=True, turn_exit_reason='completed')
    saved = plugin.store.get(record['id'])
    assert saved['state'] == 'interrupted' and saved['stop_reason'] == 'controller_exited'
    assert len(ctx.subagent_lifecycle.cancelled) == 2
    assert json.loads(ctx.tools['shadow_trial_step']({}, session_id='controller'))['continue'] is False
    assert 'stopped' in plugin.command('status').lower()
    ctx.answers = ['start']
    assert 'Try GPT-6 Luna' in plugin.command(f'try {TRIAL.model}')
    restarted = plugin.store.get()
    assert restarted['id'] != record['id']
    ctx.hook('pre_llm_call', session_id='controller', user_message=f'shadow-controller:{restarted["id"]}:{restarted["controller_token"]}')
    for _ in range(248):
        if not json.loads(ctx.tools['shadow_trial_step']({}, session_id='controller'))['continue']:
            break
    assert plugin.store.get(restarted['id'])['state'] == 'finished'
    assert plugin.store.get(restarted['id'])['totals']['pairs'] == 30


def test_controller_stop_survives_stale_writes_and_ignores_stale_claim(tmp_path):
    first, second = Store(tmp_path / 'trial.sqlite'), Store(tmp_path / 'trial.sqlite')
    first.save({'id': 'fixture', 'created_at': 1, 'state': 'queued'})
    old = first.claim_controller('fixture', None)
    claim = second.claim_controller('fixture', None, expected_claim=old)
    stale = first.get('fixture')
    assert not first.controller_stopped('fixture', old, 'controller_exited')
    assert second.controller_stopped('fixture', claim, 'controller_exited')
    first.save({**stale, 'state': 'running', 'spend': .02})
    saved = second.get('fixture')
    assert saved['state'] == 'interrupted' and saved['stop_reason'] == 'controller_exited'
    assert saved['spend'] == .02
    assert first.claim_controller('fixture', None, expected_claim=claim) is None


def test_finished_controller_end_preserves_verdict(tmp_path):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    ctx.hook('pre_llm_call', session_id='controller', user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}')
    for _ in range(168):
        if not json.loads(ctx.tools['shadow_trial_step']({}, session_id='controller'))['continue']:
            break
    ctx.hook('on_session_end', session_id='controller', completed=True)
    plugin.launcher.is_alive = lambda _: False
    assert plugin.store.get(record['id'])['state'] == 'finished'
    assert plugin.store.get(record['id'])['totals']['pairs'] == 20


def test_partial_verdict_keeps_controller_exit_reason(tmp_path):
    ctx, plugin = service(tmp_path)
    record = TrialEngine(ctx, plugin.store, plugin.guard).prepare(selection(tmp_path, 30), CURRENT, TRIAL, [OUTSIDE])
    record['state'] = 'queued'
    plugin.store.save(record)
    plugin.start_controller(record)
    record = plugin.store.get(record['id'])
    ctx.hook('pre_llm_call', session_id='controller', user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}')
    for _ in range(248):
        ctx.tools['shadow_trial_step']({}, session_id='controller')
        if sum(t['usable'] for t in plugin.store.get(record['id'])['turns']) == 20:
            break
    ctx.hook('on_session_end', session_id='controller', completed=True)
    rpc = DesktopRpc(ProfileSource(plugin), load_copy())
    params = {'test_id': record['id'], 'turn_id': 't0'}
    assert rpc.turn_open(params)['can_pick']
    assert rpc.turn_reveal({**params, 'pick': 'same'})['revealed']
    text = plugin.command(f'results {record["id"]}', terminal=True)
    assert 'controller ended' in text
    page = DesktopRpc(ProfileSource(plugin), load_copy()).page_state({'test_id': record['id']})
    assert page['mode'] == 'result' and 'controller ended' in page['verdict']['reason']
    assert plugin.store.get(record['id'])['totals']['pairs'] == 20


def test_watcher_posts_stopped_reason_when_controller_dies(tmp_path):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    plugin.launcher.is_alive = lambda _: False
    plugin.watch(record['id'])
    assert len(ctx.messages) == 1
    assert 'stopped' in ctx.messages[0] and 'controller ended' in ctx.messages[0]


def test_controller_stop_migrates_an_existing_profile_database(tmp_path):
    path = tmp_path / 'existing.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE trial_controllers (id TEXT PRIMARY KEY, claim TEXT NOT NULL, pid INTEGER, restarts INTEGER NOT NULL, owner_pid INTEGER NOT NULL DEFAULT 0)')
        db.execute('INSERT INTO trial_controllers VALUES (?, ?, ?, ?, ?)', ('fixture', 'old-claim', 123, 3, 122))
    store = Store(path)
    store.save({'id': 'fixture', 'created_at': 1, 'state': 'running'})
    assert store.controller_stopped('fixture', 'old-claim', 'controller_exited')
    assert store.get('fixture')['state'] == 'interrupted'
    store.save({'id': 'new', 'created_at': 2, 'state': 'queued'})
    assert store.claim_controller('new', None)
