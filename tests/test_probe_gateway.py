import importlib
from pathlib import Path
import queue
import sys
from types import SimpleNamespace

import pytest

from shadow.trial import probe
from test_probe_dispatch import hermes_dispatch
from test_setup_check import _job, _plugin


def _activation():
    return {'name': 'shadow', 'key': 'shadow',
            'activated_now': {'hooks': ['pre_llm_call']}, 'deferred': {}}


@pytest.fixture
def gateway(tmp_path, monkeypatch, hermes_dispatch):
    constants = importlib.import_module('hermes_constants')
    control = importlib.import_module('gateway.control_socket')
    activation = importlib.import_module('hermes_cli.plugins_activation')
    runtime = importlib.import_module('hermes_cli.gateway_runtime')
    home = tmp_path / 'home'
    monkeypatch.setattr(constants, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(constants, 'get_default_hermes_root', lambda: home)
    return SimpleNamespace(home=home, control=control, activation=activation,
                           runtime=runtime, manager=hermes_dispatch.manager,
                           plugins=hermes_dispatch.plugins)


@pytest.mark.parametrize('answer', [
    {'reloaded': False, 'error': 'not served'},
    {'reloaded': True, 'plugins': [], 'activations': []},
    {'reloaded': True, 'plugins': ['shadow'], 'activations': []},
    {'reloaded': True, 'plugins': ['shadow'],
     'activations': [{**_activation(), 'missing_dependencies': ['missing-package']}]},
])
def test_gateway_refresh_failure_never_launches_controller(tmp_path, monkeypatch, gateway, answer):
    requests = []
    monkeypatch.setattr(gateway.control, 'query_gateway_control',
                        lambda home, verb, **kwargs: requests.append((home, verb)) or answer)
    _, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    launched = []
    plugin.launcher.popen = lambda *args, **kwargs: launched.append(args) or SimpleNamespace()
    monkeypatch.setattr(probe, 'wait_for_result', lambda *args, **kwargs: None)
    result = probe.launch_setup(plugin.launcher, job, timeout=0.05, killer=lambda _: None)
    assert launched == []
    assert requests == [(gateway.home, 'reload-plugins')]
    assert result['incomplete'] is True
    assert result['reason_code'].startswith('gateway_')


def test_gateway_reload_replaces_resident_hook_before_launch(tmp_path, monkeypatch, gateway):
    from hermes_cli.plugins import PluginContext, PluginManifest

    plugin_dir = tmp_path / 'resident-shadow'
    plugin_dir.mkdir()
    source = plugin_dir / '__init__.py'
    manifest = PluginManifest(name='shadow', version='0.1.0', source='user', path=str(plugin_dir))
    manager = gateway.manager

    def discover():
        module = manager._load_directory_module(manifest)
        module.register(PluginContext(manifest, manager))

    def write_hook(generation):
        source.write_text('def register(ctx):\n'
                          '    ctx.register_hook("pre_llm_call", '
                          f'lambda **event: {{"generation": {generation!r}}})\n')

    monkeypatch.setattr(manager, '_discover_and_load_inner', discover)
    monkeypatch.setattr(manager, '_refresh_secret_sources_after_discovery', lambda: None)
    monkeypatch.setattr(manager, '_re_register_config_hooks_after_force', lambda: None)
    write_hook('old')
    manager.discover_and_load()
    write_hook('current-installed-build')
    manager.discover_and_load()
    assert manager.invoke_hook('pre_llm_call') == [{'generation': 'old'}]

    requests = []

    def control(home, verb, **kwargs):
        requests.append((home, verb))
        manager.discover_and_load(force=True)
        return {'reloaded': True, 'plugins': ['shadow'], 'activations': [_activation()]}

    monkeypatch.setattr(gateway.control, 'query_gateway_control', control)
    _, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    observed = []

    def launch(*args, **kwargs):
        observed.extend(manager.invoke_hook('pre_llm_call'))
        return SimpleNamespace()

    plugin.launcher.popen = launch
    monkeypatch.setattr(probe, 'wait_for_result', lambda *args, **kwargs: None)
    probe.launch_setup(plugin.launcher, job, killer=lambda _: None)
    assert observed == [{'generation': 'current-installed-build'}]
    assert requests == [(gateway.home, 'reload-plugins')]


@pytest.mark.parametrize('state,expected', [('absent', None), ('ready', 'gateway_refresh_unconfirmed'),
                                           ('starting', 'gateway_refresh_unconfirmed'),
                                           ('inaccessible', 'gateway_refresh_unconfirmed')])
def test_unanswered_reload_requires_proven_absent_owner(monkeypatch, gateway, state, expected):
    from shadow.trial.probe_gateway import refresh_setup_gateway

    monkeypatch.setattr(gateway.control, 'query_gateway_control', lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway.runtime, 'discover_gateway_endpoint',
                        lambda home: SimpleNamespace(state=state))
    assert refresh_setup_gateway(gateway.home) == expected


def test_query_file_classic_submit_reaches_refreshed_shadow_hook(tmp_path, monkeypatch, gateway):
    from agent.turn_context import _collect_pre_llm_call_context
    from hermes_cli.gateway_agent import GatewayAgent
    from hermes_cli.main_query_file import _read_query_file
    from hermes_cli.plugins import PluginContext, PluginManifest

    _, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    manager = gateway.manager
    monkeypatch.setattr(probe.SetupProbe, 'run',
                        lambda self: {'passed': False, 'incomplete': True})
    bridge = SimpleNamespace(hook=plugin.pre_llm_call)
    monkeypatch.setitem(sys.modules, 'shadow_gateway_replay_hook', bridge)
    plugin_dir = tmp_path / 'resident-shadow'
    plugin_dir.mkdir()
    source = plugin_dir / '__init__.py'
    manifest = PluginManifest(name='shadow', version='0.1.0', source='user', path=str(plugin_dir))

    def discover():
        manager._load_directory_module(manifest).register(PluginContext(manifest, manager))

    monkeypatch.setattr(manager, '_discover_and_load_inner', discover)
    monkeypatch.setattr(manager, '_refresh_secret_sources_after_discovery', lambda: None)
    monkeypatch.setattr(manager, '_re_register_config_hooks_after_force', lambda: None)
    source.write_text('def register(ctx):\n'
                      '    ctx.register_hook("pre_llm_call", lambda **event: None)\n')
    manager.discover_and_load()
    source.write_text('from shadow_gateway_replay_hook import hook\n'
                      'def register(ctx):\n'
                      '    ctx.register_hook("pre_llm_call", hook)\n')

    def reload(home, verb, **kwargs):
        assert verb == 'reload-plugins'
        manager.discover_and_load(force=True)
        return {'reloaded': True, 'plugins': ['shadow'], 'activations': [_activation()]}

    monkeypatch.setattr(gateway.control, 'query_gateway_control', reload)
    state = SimpleNamespace(info={}, gaps=0, attached_gaps=0, pending=[])
    submitted, blocks, provider_calls, replies = [], [], [], []
    events = queue.Queue()

    def rpc(method, **params):
        if method == 'config.set':
            return {}
        assert method == 'prompt.submit'
        submitted.append(params['text'])
        _collect_pre_llm_call_context(
            SimpleNamespace(session_id=params['session_id'], model='offline', platform='cli'),
            effective_task_id='offline', turn_id='offline', original_user_message=params['text'],
            messages=[{'role': 'user', 'content': params['text']}], conversation_history=[], blocks=blocks,
        )
        if not blocks:
            provider_calls.append(params['text'])
        events.put({'method': 'event', 'params': {
            'type': 'message.complete', 'session_id': params['session_id'],
            'payload': {'text': blocks[0] if blocks else 'inert provider response'},
        }})
        return {'admission_id': 'offline'}

    def launch(argv, **kwargs):
        args = SimpleNamespace(query_file=argv[argv.index('--query-file') + 1], query=None)
        assert '-Q' in argv
        _read_query_file(args)
        agent = GatewayAgent.__new__(GatewayAgent)
        agent.session_id = 'offline-setup'
        agent.session_api_calls = 0
        agent._voice_turn_pending = False
        agent._pending_cli_user_message = None
        agent.clarify_callback = None
        agent._moa_preset = agent._turn_route = agent._turn_reasoning = None
        agent._reasoning_callback = agent._show_reasoning_sent = None
        agent.connection = SimpleNamespace(events=events, state=lambda sid: state, call=rpc,
                                           start=lambda: None, epoch=1)
        agent._result = lambda history, message, complete, turn: complete
        replies.append(agent.run_conversation(args.query))
        return SimpleNamespace()

    plugin.launcher.popen = launch
    result = probe.launch_setup(plugin.launcher, job, timeout=0.05, killer=lambda _: None)
    assert submitted == [probe.prompt_text(job)]
    assert provider_calls == []
    assert len(blocks) == 1
    assert replies == [{'text': blocks[0]}]
    assert result['incomplete'] is True


def test_gateway_command_skips_reload_of_its_own_plugins(tmp_path, monkeypatch, gateway):
    monkeypatch.setitem(sys.modules, 'gateway.run', SimpleNamespace(_gateway_runner_ref=lambda: object()))
    requests = []
    monkeypatch.setattr(gateway.control, 'query_gateway_control',
                        lambda *args, **kwargs: requests.append(args) or {'reloaded': False})
    _, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    launched = []
    plugin.launcher.popen = lambda *args, **kwargs: launched.append(args) or SimpleNamespace()
    monkeypatch.setattr(probe, 'wait_for_result', lambda *args, **kwargs: None)
    result = probe.launch_setup(plugin.launcher, job, killer=lambda _: None)
    assert requests == []
    assert len(launched) == 1
    assert result['timed_out'] is True


def test_imported_gateway_module_without_a_runner_still_requires_refresh(tmp_path, monkeypatch, gateway):
    from shadow.trial.probe_gateway import refresh_setup_gateway

    monkeypatch.setitem(sys.modules, 'gateway.run', SimpleNamespace(_gateway_runner_ref=lambda: None))
    requests = []
    monkeypatch.setattr(gateway.control, 'query_gateway_control',
                        lambda *args, **kwargs: requests.append(args) or {'reloaded': False})
    assert refresh_setup_gateway(gateway.home) == 'gateway_refresh_refused'
    assert len(requests) == 1


@pytest.mark.parametrize('crash', [False, True])
def test_resident_gateway_runs_distinct_setup_jobs_but_refuses_replayed_job(tmp_path, monkeypatch, gateway, crash):
    _, plugin = _plugin(tmp_path)
    first = _job(tmp_path)
    second = probe.new_job(home=first['home'], workspace=first['workspace'],
                           provider=first['provider'], model=first['model'], profile=first['profile'])
    monkeypatch.setitem(sys.modules, 'gateway.run', SimpleNamespace(_gateway_runner_ref=lambda: plugin))
    reloads, ran, blocked = [], [], []
    monkeypatch.setattr(gateway.control, 'query_gateway_control',
                        lambda *args, **kwargs: reloads.append(args))

    def run(self):
        ran.append(self.job['id'])
        if crash:
            raise RuntimeError('offline setup failure')
        return {'passed': False, 'incomplete': True}

    def launch(argv, **kwargs):
        message = Path(argv[argv.index('--query-file') + 1]).read_text()
        blocked.append(plugin.pre_llm_call(session_id=f'setup-{len(blocked)}', user_message=message))
        return SimpleNamespace()

    monkeypatch.setattr(probe.SetupProbe, 'run', run)
    plugin.launcher.popen = launch
    for job in (first, second, first):
        result = probe.launch_setup(plugin.launcher, job, timeout=0.05, killer=lambda _: None)
        assert result['passed'] is False and result['incomplete'] is True
        assert result.get('timed_out') is not True
    assert ran == [first['id'], second['id']]
    assert reloads == []
    assert all(result['action'] == 'block' for result in blocked)
    assert plugin.launcher.isolation_verified is False


def test_resident_gateway_refuses_overlapping_setup_job(tmp_path, monkeypatch, gateway):
    _, plugin = _plugin(tmp_path)
    first = _job(tmp_path)
    second = probe.new_job(home=first['home'], workspace=first['workspace'],
                           provider=first['provider'], model=first['model'], profile=first['profile'])
    ran = []

    def run(self):
        ran.append(self.job['id'])
        probe.write_job(tmp_path, second)
        answer = plugin.pre_llm_call(session_id='overlapping', user_message=probe.prompt_text(second))
        assert answer['action'] == 'block'
        assert 'could not complete' in answer['message']
        return {'passed': False, 'incomplete': True}

    monkeypatch.setattr(probe.SetupProbe, 'run', run)
    probe.write_job(tmp_path, first)
    plugin.pre_llm_call(session_id='first', user_message=probe.prompt_text(first))
    assert ran == [first['id']]
    result = probe.wait_for_result(plugin.launcher, second, timeout=0.05, pause=lambda _: None)
    assert result['incomplete'] is True and result['passed'] is False
    assert result['model'] is None
