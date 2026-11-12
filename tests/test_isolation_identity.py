import json
import sys
from copy import deepcopy
from dataclasses import asdict
from types import ModuleType, SimpleNamespace

import pytest

from shadow.trial import isolation
from shadow.trial.controller import ControllerLauncher, IsolationRequired
from test_pair_probe import _proof, hermes_agent_context, pair_config_backend


def passed_checks():
    return {name: True for name in isolation.CHECK_NAMES}


@pytest.fixture
def runtime(monkeypatch):
    config = {'compression': {'checkpoint_required': False}, 'delegation': {}, 'memory': {}, 'plugins': {'enabled': ['shadow', 'context']}}
    plugins = [{'key': 'shadow', 'version': '0.1.0', 'enabled': True}, {'key': 'context', 'version': '1.0.0', 'enabled': True}]
    config_module = ModuleType('hermes_cli.config')
    config_module.load_config_readonly = lambda: config
    manager_module = ModuleType('hermes_cli.plugins')
    manager_module.get_plugin_manager = lambda: SimpleNamespace(list_plugins=lambda: plugins)
    discovery_module = ModuleType('hermes_cli.plugins_discovery')
    discovery_module.collect_directory_manifests = lambda: []
    for name, module in [('hermes_cli.config', config_module), ('hermes_cli.plugins', manager_module), ('hermes_cli.plugins_discovery', discovery_module)]:
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(isolation, 'hermes_revision', lambda: ('9.9.9', 'abcdef'))
    monkeypatch.setattr(isolation, 'plugin_version', lambda: '0.1.0')
    monkeypatch.setattr(isolation, 'plugin_hash', lambda: 'shadow-files')
    return config, plugins


@pytest.mark.parametrize('change', ['checkpoint', 'delegation', 'memory', 'plugins', 'plugin_version'])
def test_configuration_changes_revoke_start(tmp_path, runtime, change):
    config, plugins = runtime
    isolation.write_record(tmp_path, isolation.current_identity('demo'), passed_checks())
    launcher = ControllerLauncher('demo', tmp_path)
    assert launcher.isolation_verified
    if change == 'checkpoint':
        config['compression']['checkpoint_required'] = True
    elif change == 'delegation':
        config['delegation']['context'] = 'all'
    elif change == 'memory':
        config['memory']['provider'] = 'new-memory'
    elif change == 'plugins':
        config['plugins']['enabled'].append('new-context')
    else:
        plugins[1]['version'] = '2.0.0'
    assert ControllerLauncher('demo', tmp_path).isolation_verified is False


@pytest.mark.parametrize('change', ['memory', 'delegation', 'shadow_permission', 'other_plugin'])
def test_saving_pair_proof_keeps_isolation_until_a_real_setting_changes(tmp_path, runtime, pair_config_backend, change):
    from shadow.switch.pair_probe import save_pair

    config, _ = runtime
    config['plugins']['entries'] = {
        'shadow': {'allow_gateway_injection': True, 'settings': {}},
        'context': {'settings': {'include_memory': False}},
    }
    config['memory']['memory_enabled'] = False
    config['delegation']['inherit_mcp_toolsets'] = False
    pair_config_backend.document = config
    isolation.write_record(tmp_path, isolation.current_identity('demo'), passed_checks())
    launcher = ControllerLauncher('demo', tmp_path)
    assert launcher.isolation_verified
    assert save_pair(pair_config_backend.ctx, _proof())
    saved = deepcopy(config)
    assert launcher.isolation_verified
    assert config == saved
    if change == 'shadow_permission':
        config['plugins']['entries']['shadow']['allow_gateway_injection'] = False
    elif change == 'other_plugin':
        config['plugins']['entries']['context']['settings']['include_memory'] = True
    elif change == 'memory':
        config['memory']['memory_enabled'] = True
    else:
        config['delegation']['inherit_mcp_toolsets'] = True
    assert launcher.isolation_verified is False


@pytest.mark.parametrize('initial_entries', [None, {}, {'shadow': {}}, {'shadow': {'settings': {}}}])
def test_shadow_settings_do_not_revoke_isolation_when_their_containers_are_new(tmp_path, runtime, initial_entries):
    config, _ = runtime
    if initial_entries is not None:
        config['plugins']['entries'] = initial_entries
    isolation.write_record(tmp_path, isolation.current_identity('demo'), passed_checks())
    launcher = ControllerLauncher('demo', tmp_path)
    assert launcher.isolation_verified
    settings = config['plugins'].setdefault('entries', {}).setdefault('shadow', {}).setdefault('settings', {})
    settings.update(web_lookups=False, future_shadow_setting={'value': 1})
    saved = deepcopy(config)
    assert launcher.isolation_verified
    assert config == saved
    del config['plugins']['entries']['shadow']['settings']
    assert launcher.isolation_verified


def test_unavailable_config_cannot_verify(tmp_path, runtime, monkeypatch):
    identity = isolation.current_identity('demo')
    isolation.write_record(tmp_path, identity, passed_checks())
    monkeypatch.setattr(sys.modules['hermes_cli.config'], 'load_config_readonly', lambda: (_ for _ in ()).throw(OSError('unavailable')))
    assert ControllerLauncher('demo', tmp_path).isolation_verified is False


def test_installed_plugin_version_changes_revoke_start_before_reload(tmp_path, runtime, monkeypatch):
    manifest = SimpleNamespace(key='context', name='context', version='1.0.0', source='user')
    monkeypatch.setattr(sys.modules['hermes_cli.plugins_discovery'], 'collect_directory_manifests', lambda: [manifest])
    isolation.write_record(tmp_path, isolation.current_identity('demo'), passed_checks())
    launcher = ControllerLauncher('demo', tmp_path)
    assert launcher.isolation_verified
    manifest.version = '2.0.0'
    assert launcher.isolation_verified is False


def test_handwritten_public_record_cannot_unlock_start(tmp_path):
    identity = isolation.Identity('9.9.9', 'abcdef', '0.1.0', 'shadow-files', 'demo')
    body = dict(asdict(identity), passed=True, checked_at='2026-11-10T12:00:00Z', checks=passed_checks(), digest=isolation.digest(passed_checks()))
    (tmp_path / 'isolation.json').write_text(json.dumps(body))
    assert ControllerLauncher('demo', tmp_path, identity=identity).isolation_verified is False


@pytest.mark.parametrize('field,value', [('checked_at', '2099-01-01T00:00:00Z'), ('digest', 'forged'), ('extra', True)])
def test_editing_any_record_field_revokes_start(tmp_path, field, value):
    identity = isolation.Identity('9.9.9', 'abcdef', '0.1.0', 'shadow-files', 'demo')
    isolation.write_record(tmp_path, identity, passed_checks())
    body = isolation.read_record(tmp_path)
    body[field] = value
    (tmp_path / 'isolation.json').write_text(json.dumps(body))
    assert ControllerLauncher('demo', tmp_path, identity=identity).isolation_verified is False


def test_record_cannot_be_copied_to_another_data_directory(tmp_path):
    identity = isolation.Identity('9.9.9', 'abcdef', '0.1.0', 'shadow-files', 'demo')
    original = tmp_path / 'original'
    copied = tmp_path / 'copied'
    copied.mkdir()
    isolation.write_record(original, identity, passed_checks())
    (copied / 'isolation.json').write_bytes((original / 'isolation.json').read_bytes())
    assert ControllerLauncher('demo', copied, identity=identity).isolation_verified is False


def test_authentication_material_stays_outside_the_record_directory(tmp_path):
    identity = isolation.Identity('9.9.9', 'abcdef', '0.1.0', 'shadow-files', 'demo')
    directory = tmp_path / 'plugin-data' / 'shadow'
    isolation.write_record(directory, identity, passed_checks())
    assert [path.name for path in directory.iterdir()] == ['isolation.json']
    key = isolation.verifier_key_path(directory)
    assert not key.is_relative_to(directory)
    assert key.stat().st_mode & 0o777 == 0o600
    assert isolation.matches(isolation.read_record(directory), identity, 'demo')
    key.unlink()
    assert ControllerLauncher('demo', directory, identity=identity).isolation_verified is False
    assert not key.exists()


@pytest.mark.parametrize('change', ['files', 'configuration', 'hermes'])
def test_launch_rechecks_identity_after_a_previous_verification(tmp_path, runtime, monkeypatch, change):
    config, _ = runtime
    isolation.write_record(tmp_path, isolation.current_identity('demo'), passed_checks())
    launched = []
    launcher = ControllerLauncher('demo', tmp_path, popen=lambda *args, **kwargs: launched.append(args) or SimpleNamespace(pid=1234))
    assert launcher.isolation_verified
    if change == 'files':
        monkeypatch.setattr(isolation, 'plugin_hash', lambda: 'changed-shadow-files')
    elif change == 'configuration':
        config['compression']['checkpoint_required'] = True
    else:
        monkeypatch.setattr(isolation, 'hermes_revision', lambda: ('9.9.9', 'new-commit'))
    with pytest.raises(IsolationRequired):
        launcher.launch({'id': 'trial', 'controller_token': 'not-a-secret', 'turns': []})
    assert launched == []
