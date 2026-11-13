import argparse
from copy import deepcopy
import sys
from types import ModuleType

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL
from shadow.common.models import Model
from test_plugin import service


@pytest.fixture
def host(monkeypatch):
    manifest = ModuleType('hermes_cli.plugins_manifest')
    manifest.running_hermes_version = lambda: '0.23.1'
    manifest.version_satisfies = lambda spec, version: tuple(map(int, version.split('.'))) >= (0, 23, 1)
    monkeypatch.setitem(sys.modules, manifest.__name__, manifest)
    capabilities = ModuleType('hermes_cli.plugin_capabilities')
    capabilities.plugin_capability_granted = lambda plugin, capability, config: capability in config['plugins']['entries'][plugin].get('granted_capabilities', [])
    monkeypatch.setitem(sys.modules, capabilities.__name__, capabilities)
    return manifest


def ready(plugin):
    config = {'plugins': {'entries': {'shadow-trial': {
        'allow_gateway_injection': True,
        'granted_capabilities': ['llm.provider_override', 'llm.model_override'],
    }}}}
    plugin.config_reader = lambda: config
    return config


@pytest.mark.parametrize('surface', ['chat', 'cli'])
def test_doctor_reports_host_profile_usable_models_and_grants(tmp_path, host, surface, capsys):
    ctx, plugin = service(tmp_path)
    ctx.profile_name = 'work'
    config = ready(plugin)
    config['providers'] = {'private': {'api_key': 'never-print-this-key'}}
    plugin.catalog = [CURRENT, TRIAL, OUTSIDE, Model('offline', 'deepseek-chat', reachable=False)]
    before = deepcopy(config)
    if surface == 'chat':
        text = ctx.commands['shadow']('doctor')
    else:
        setup, handler = ctx.cli['shadow']
        parser = argparse.ArgumentParser()
        setup(parser)
        handler(parser.parse_args(['doctor']))
        text = capsys.readouterr().out
    assert 'Hermes 0.23.1 meets plugin.yaml requires_hermes >=0.23.1.' in text
    assert 'Active profile: work' in text
    for model in (CURRENT, TRIAL, OUTSIDE):
        assert f'{model.provider}::{model.model}' in text
    assert 'offline::deepseek-chat' not in text
    assert 'never-print-this-key' not in text
    assert 'Third-maker judge: available' in text
    assert 'llm.provider_override: granted' in text
    assert 'llm.model_override: granted' in text
    assert 'allow_gateway_injection' not in text
    assert 'Fix: hermes -p work model' in text
    assert config == before and not ctx.capture and not plugin.store.list()


@pytest.mark.parametrize('version', ['0.23.0', '0.22.9'])
def test_doctor_reports_incompatible_hermes_and_update_command(tmp_path, host, version):
    host.running_hermes_version = lambda: version
    ctx, plugin = service(tmp_path)
    ready(plugin)
    text = ctx.commands['shadow']('doctor')
    assert f'Hermes {version} does not meet plugin.yaml requires_hermes >=0.23.1.' in text
    assert 'Fix: hermes update' in text


def test_doctor_reports_unknown_hermes_version(tmp_path, host):
    def unavailable():
        raise ImportError('host version unavailable')
    host.running_hermes_version = unavailable
    ctx, plugin = service(tmp_path)
    ready(plugin)
    text = ctx.commands['shadow']('doctor')
    assert 'Hermes version could not be detected' in text
    assert 'Fix: hermes update' in text


def test_doctor_reports_missing_models_judge_and_profile_scoped_fixes(tmp_path, host):
    ctx, plugin = service(tmp_path)
    ctx.profile_name = 'work'
    ready(plugin)
    plugin.catalog = [Model(CURRENT.provider, CURRENT.model, reachable=False)]
    text = ctx.commands['shadow']('doctor')
    assert 'Usable models: none' in text
    assert 'at least one other model to try' in text
    assert 'Third-maker judge: unavailable' in text
    assert 'Fix: hermes -p work model' in text
    assert 'hermes shadow try <model> --judge <judge>' not in text
    assert not ctx.capture


@pytest.mark.parametrize('catalog', [[CURRENT, TRIAL], [CURRENT, OUTSIDE], [CURRENT, Model('custom', 'unknown-model')]])
def test_backup_judging_does_not_count_as_third_maker(tmp_path, host, catalog):
    ctx, plugin = service(tmp_path)
    ready(plugin)
    plugin.catalog = catalog
    assert plugin.availability() == (True, True)
    text = ctx.commands['shadow']('doctor')
    assert 'Third-maker judge: unavailable' in text
    assert 'Fix: hermes model' in text


def test_doctor_uses_host_grants_and_exact_enable_fix(tmp_path, host):
    ctx, plugin = service(tmp_path)
    ctx.profile_name = 'work'
    config = ready(plugin)
    entry = config['plugins']['entries']['shadow-trial']
    entry['granted_capabilities'] = []
    entry['llm'] = {'allow_provider_override': True, 'allow_model_override': True}
    entry['allow_gateway_injection'] = False
    before = deepcopy(config)
    text = ctx.commands['shadow']('doctor')
    assert 'llm.provider_override: not granted' in text
    assert 'llm.model_override: not granted' in text
    assert 'Fix: hermes -p work plugins enable shadow-trial' in text
    assert 'allow_gateway_injection' not in text
    assert 'config set plugins.entries.shadow-trial.llm.' not in text
    assert config == before


def test_doctor_falls_back_to_legacy_grants_when_host_api_is_absent(tmp_path, host, monkeypatch):
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_capabilities', None)
    ctx, plugin = service(tmp_path)
    config = ready(plugin)
    entry = config['plugins']['entries']['shadow-trial']
    entry.pop('granted_capabilities')
    entry['llm'] = {'allow_provider_override': True, 'allow_model_override': True}
    assert 'not granted' not in ctx.commands['shadow']('doctor')
    entry['llm']['allow_model_override'] = False
    text = ctx.commands['shadow']('doctor')
    assert 'llm.model_override: not granted' in text
    assert 'Fix: hermes plugins enable shadow-trial' in text


def test_named_profile_keeps_live_verification_fixes_in_the_same_profile(tmp_path, host):
    from shadow.switch.doctor import pair_doctor
    ctx, plugin = service(tmp_path)
    ctx.profile_name = 'work'
    ready(plugin)
    plugin.launcher.isolation_verified = False
    text = ctx.commands['shadow']('doctor')
    text += pair_doctor({}, profile_name=ctx.profile_name)
    assert 'hermes -p work shadow doctor --verify' in text
    assert 'hermes -p work shadow doctor --verify-pair' in text
    assert 'Run hermes shadow doctor' not in text
    assert not ctx.capture
