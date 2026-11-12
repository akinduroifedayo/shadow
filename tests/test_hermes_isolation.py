"""Public Hermes contracts used by Shadow, checked against SHADOW_HERMES_REF."""

import ast

import pytest

from test_real_tools import require_hermes_reference


def definitions(path):
    names = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update({target.id: node for target in targets if isinstance(target, ast.Name)})
    return names


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
    ('hermes_cli/config_providers.py', {'coerce_provider_id', 'stringify_provider_map'}),
    ('hermes_cli/inventory.py', {'ConfigContext'}),
    ('hermes_cli/auth.py', {'get_auth_status'}),
    ('hermes_cli/local_runtime/bootstrap.py', {'staged_model_ids'}),
    ('hermes_cli/providers.py', {'LLAMACPP_ALIASES', 'LLAMACPP_PROVIDER_ID',
                                 'custom_provider_aliases', 'custom_provider_slug'}),
    ('providers/__init__.py', {'bound_profile_resolver'}),
    ('agent/auxiliary_hooks.py', {'start_aux_call_hooks', 'finish_aux_call_hooks'}),
    ('agent/models_dev.py', {'PROVIDER_TO_MODELS_DEV', 'fetch_models_dev', 'get_model_capabilities'}),
    ('agent/secret_scope.py', {'current_secret_scope', 'serves_routed_profile'}),
    ('agent/usage_pricing.py', {'get_pricing_entry', 'resolve_billing_route'}),
    ('hermes_constants.py', {'get_hermes_home', 'resolve_reasoning_config'}),
    ('hermes_state.py', {'SessionDB'}),
    ('plugins/plugin_storage.py', {'plugin_db'}),
    ('gateway/session_context.py', {'get_session_env'}),
    ('toolsets.py', {'resolve_toolset'}),
])
def test_public_profile_readers_and_hook_builders_exist(path, names):
    assert all(not name.startswith('_') for name in names)
    assert names <= definitions(require_hermes_reference() / path).keys()


def test_auxiliary_hooks_receive_the_bare_registered_task_key():
    source = require_hermes_reference()
    entry = definitions(source / 'hermes_cli/plugins_aux_tasks.py')['build_auxiliary_task_entry']
    dictionaries = [node for node in ast.walk(entry) if isinstance(node, ast.Dict)]
    assert any(ast.literal_eval(key) == 'key' and ast.unparse(value) == 'key'
               for node in dictionaries for key, value in zip(node.keys, node.values) if key is not None)

    client = definitions(source / 'agent/auxiliary_client.py')
    scope = ast.unparse(client['_relay_aux_call_scope'])
    assert "task = args[0] if args else kwargs.get('task')" in scope
    assert "'task': str(task or 'unknown')" in scope
    metadata = ast.unparse(client['_relay_auxiliary_metadata'])
    assert "'auxiliary_task': str(context['task'])" in metadata
    calls = [node for function in client.values() for node in ast.walk(function)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id in {'run_with_aux_hooks', 'arun_with_aux_hooks'}]
    assert calls
    for call in calls:
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in call.keywords}
        assert keywords['aux_task'] == "str(metadata.get('auxiliary_task') or '')"
    hooks = definitions(source / 'agent/auxiliary_hooks.py')
    for name in ('run_with_aux_hooks', 'arun_with_aux_hooks'):
        starts = [node for node in ast.walk(hooks[name]) if isinstance(node, ast.Call)
                  and ast.unparse(node.func) == 'start_aux_call_hooks']
        assert len(starts) == 1
        assert any(keyword.arg == 'aux_task' and ast.unparse(keyword.value) == 'aux_task'
                   for keyword in starts[0].keywords)


@pytest.mark.parametrize('key,accepted', [
    ('shadow_pair_0123456789abcdef0123456789abcdef', True),
    ('shadow_pair_bad-key', False),
    ('shadow_pair_bad/key', False),
])
def test_pair_task_keys_pass_hermes_validation(key, accepted):
    source = require_hermes_reference()
    entry = definitions(source / 'hermes_cli/plugins_aux_tasks.py')['build_auxiliary_task_entry']
    checks = []
    for node in entry.body:
        if isinstance(node, ast.ImportFrom):
            break
        if isinstance(node, ast.If):
            checks.append(compile(ast.Expression(node.test), '<auxiliary task validation>', 'eval'))
    assert checks
    assert (not any(eval(check, {'key': key}) for check in checks)) is accepted


def test_auxiliary_payloads_carry_pair_observation_fields():
    source = require_hermes_reference()
    hooks = definitions(source / 'agent/auxiliary_hooks.py')['_AuxCallHooks']
    methods = {node.name: node for node in hooks.body if isinstance(node, ast.FunctionDef)}
    base_updates = [node for node in ast.walk(methods['__init__']) if isinstance(node, ast.Call)
                    and ast.unparse(node.func) == 'self.base.update']
    assert len(base_updates) == 1
    base = {keyword.arg: ast.unparse(keyword.value) for keyword in base_updates[0].keywords}
    assert {'aux_task', 'api_request_id', 'provider', 'model', 'base_url', 'api_mode'} <= base.keys()
    assert base['aux_task'] == 'aux_task'

    pre_calls = [node for node in ast.walk(methods['pre']) if isinstance(node, ast.Call)
                 and ast.unparse(node.func) == '_fire']
    assert len(pre_calls) == 1
    pre = pre_calls[0]
    assert ast.unparse(pre.args[0]) == 'PRE_AUXILIARY_CALL'
    assert any(keyword.arg is None and ast.unparse(keyword.value) == 'self.base'
               for keyword in pre.keywords)
    assert 'request_messages' in {keyword.arg for keyword in pre.keywords}

    post = methods['post']
    payloads = [node for node in ast.walk(post) if isinstance(node, ast.Call)
                and ast.unparse(node.func) == 'dict' and node.args
                and ast.unparse(node.args[0]) == 'self.base']
    assert len(payloads) == 1
    assert 'error' in {keyword.arg for keyword in payloads[0].keywords}
    updates = [node for node in ast.walk(post) if isinstance(node, ast.Call)
               and ast.unparse(node.func) == 'payload.update']
    assert updates
    assert all({'finish_reason', 'response_model'} <= {keyword.arg for keyword in call.keywords}
               for call in updates)
    assert '_fire(POST_AUXILIARY_CALL, **payload)' in ast.unparse(post)


def test_replay_lifecycle_remains_a_public_service():
    source = require_hermes_reference()
    service = definitions(source / 'agent/subagent_lifecycle.py')['SubagentLifecycleService']
    methods = {node.name for node in service.body if isinstance(node, ast.FunctionDef)}
    assert {'launch', 'wait', 'result'} <= methods
