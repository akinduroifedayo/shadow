"""Gateway toolset selection keeps deferred controller tools in the session scope."""

import importlib
import sys
from types import SimpleNamespace

import pytest

from test_probe_dispatch import hermes_dispatch


@pytest.mark.parametrize('config,allowed', [
    ({}, True),
    ({'platform_toolsets': {'cli': ['shadow_controller']},
      'known_plugin_toolsets': {'cli': ['shadow_controller']}}, True),
    ({'platform_toolsets': {'cli': ['file']},
      'known_plugin_toolsets': {'cli': ['shadow_controller']}}, False),
    ({'agent': {'disabled_toolsets': ['shadow_controller']}}, False),
])
@pytest.mark.parametrize('platform', ['cli', 'telegram'])
def test_gateway_selected_toolsets_scope_the_deferred_controller_step(
        hermes_dispatch, monkeypatch, config, allowed, platform):
    monkeypatch.setitem(sys.modules, 'snowballstemmer', SimpleNamespace(
        stemmer=lambda _: SimpleNamespace(stemWord=lambda word: word)))
    monkeypatch.setitem(sys.modules, 'tools.connectors.search', SimpleNamespace(
        connections_in_scope=lambda _: False, connector_entries_by_group=lambda *_a, **_kw: {},
        connectors_unavailable=lambda *_a, **_kw: pytest.fail('remote discovery attempted'),
        remote_schemas_for=lambda *_a, **_kw: ({}, None)))
    from tools import registry as registry_module
    from hermes_cli.plugins_context import PluginContext
    from hermes_cli.plugins_manifest import PluginManifest

    registry = registry_module.ToolRegistry()
    monkeypatch.setattr(registry_module, 'registry', registry)
    monkeypatch.setattr(registry_module, 'discover_builtin_tools', lambda: None)
    monkeypatch.setattr(hermes_dispatch.plugins, 'discover_plugins', lambda: None)
    manager = hermes_dispatch.manager
    manager._discovered = True
    monkeypatch.setattr(hermes_dispatch.plugins, 'get_plugin_manager', lambda: manager)
    context = PluginContext(PluginManifest(name='shadow'), manager)
    schema = {'name': 'shadow_trial_step', 'description': 'Advance the Shadow trial.',
              'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
    context.register_tool('shadow_trial_step', 'shadow_controller', schema,
                          lambda _args: '{"continue": false}')
    tools_config = importlib.import_module('hermes_cli.tools_config')
    monkeypatch.setattr(tools_config, '_xai_credentials_present', lambda: False)
    model_tools = importlib.import_module('model_tools')
    monkeypatch.setattr(model_tools, 'registry', registry)
    from tools import tool_search

    search_config = tool_search.ToolSearchConfig.from_raw({'enabled': 'on'})
    monkeypatch.setattr(tool_search, 'load_config_readonly', lambda: search_config)
    monkeypatch.setattr(tool_search, 'load_config', lambda: search_config)
    config = {key: {platform: value['cli']} if key in {'platform_toolsets', 'known_plugin_toolsets'} else value
              for key, value in config.items()}
    enabled = sorted(tools_config._get_platform_tools(config, platform))
    agent = SimpleNamespace(enabled_toolsets=enabled,
                            disabled_toolsets=config.get('agent', {}).get('disabled_toolsets'))
    name, arguments, error = hermes_dispatch.executor._unwrap_tool_search_call(
        agent, 'tool_call', {'calls': [{'name': 'shadow_trial_step', 'arguments': {}}]})
    if allowed:
        assert 'shadow_controller' in enabled
        assert (name, arguments, error) == ('shadow_trial_step', {}, None)
    else:
        assert 'shadow_controller' not in enabled
        assert error is not None
