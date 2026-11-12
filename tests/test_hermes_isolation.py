"""Public Hermes contracts used by Shadow, checked against SHADOW_HERMES_REF."""

import ast

import pytest

from test_real_tools import require_hermes_reference


def definitions(path):
    return {node.name: node for node in ast.parse(path.read_text()).body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))}


def test_plugin_context_exposes_shadow_services_and_registrations():
    source = require_hermes_reference()
    context = definitions(source / 'hermes_cli/plugins_context.py')['PluginContext']
    methods = {node.name for node in context.body if isinstance(node, ast.FunctionDef)}
    assert {'get_config', 'set_config', 'llm', 'subagent_lifecycle', 'register_hook',
            'register_middleware', 'register_auxiliary_task'} <= methods


def test_completion_supports_messages_route_overrides_and_task_identity():
    source = require_hermes_reference()
    module = definitions(source / 'agent/plugin_llm.py')
    methods = {node.name: node for node in module['PluginLlm'].body if isinstance(node, ast.FunctionDef)}
    complete = methods['complete']
    assert complete.args.args[1].arg == 'messages'
    assert {'provider', 'model', 'max_tokens', 'timeout', 'task'} <= {
        arg.arg for arg in complete.args.kwonlyargs}
    assert 'PluginLlmTrustError' in module
    fields = {node.target.id for node in module['PluginLlmCompleteResult'].body
              if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
    assert {'text', 'provider', 'model'} <= fields


@pytest.mark.parametrize('path,names', [
    ('hermes_cli/config.py', {'read_raw_config_readonly', 'load_config_readonly'}),
    ('hermes_cli/gateway_runtime.py', {'discover_gateway_endpoint'}),
    ('hermes_cli/models_pricing.py', {'get_pricing_for_provider'}),
    ('agent/auxiliary_hooks.py', {'start_aux_call_hooks', 'finish_aux_call_hooks'}),
    ('agent/models_dev.py', {'fetch_models_dev', 'get_model_capabilities'}),
    ('hermes_constants.py', {'get_hermes_home', 'resolve_reasoning_config'}),
    ('toolsets.py', {'resolve_toolset'}),
])
def test_public_profile_readers_and_hook_builders_exist(path, names):
    assert names <= definitions(require_hermes_reference() / path).keys()


def test_replay_lifecycle_remains_a_public_service():
    source = require_hermes_reference()
    service = definitions(source / 'agent/subagent_lifecycle.py')['SubagentLifecycleService']
    methods = {node.name for node in service.body if isinstance(node, ast.FunctionDef)}
    assert {'launch', 'wait', 'result'} <= methods
