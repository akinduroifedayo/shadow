from types import ModuleType, SimpleNamespace
import sys

import pytest

from shadow.common.copy import load_copy
from shadow.common.discovery import ModelCatalog, ModelUnavailable, discover_models
from shadow.common.models import Model


@pytest.fixture
def hermes_profile(monkeypatch):
    configs = {
        'openrouter': {'models': ['future/aurora', 'future/ember'], 'key_env': 'SHADOW_TEST_ROUTER_KEY'},
        'deepseek': {'models': ['deepseek-chat'], 'key_env': 'SHADOW_TEST_DEEPSEEK_KEY'},
    }
    context = SimpleNamespace(current_provider='openrouter', current_model='future/aurora',
                              user_providers=configs, custom_providers=[], excluded_providers=[])
    monkeypatch.setattr('shadow.common.discovery._local_context', lambda: context)
    inventory = ModuleType('hermes_cli.inventory')
    inventory.load_picker_context = lambda: context
    def forbidden(*args, **kwargs):
        pytest.fail('discovery called a credential resolver or refresh-capable inventory')
    inventory.build_models_payload = forbidden
    auth = ModuleType('hermes_cli.auth')
    auth.get_auth_status = lambda provider: {'logged_in': False}
    auth.resolve_codex_runtime_credentials = forbidden
    runtime = ModuleType('hermes_cli.runtime_provider')
    runtime.resolve_runtime_provider = forbidden
    metadata = ModuleType('agent.models_dev')
    metadata.PROVIDER_TO_MODELS_DEV = {'openrouter': 'openrouter', 'deepseek': 'deepseek', 'openai-codex': 'openai'}
    metadata.get_model_capabilities = lambda *args, **kwargs: SimpleNamespace(supports_reasoning=None)
    calls = []
    data = {}
    def cached_models(*, allow_network=True):
        calls.append(allow_network)
        assert allow_network is False, 'models.dev must remain offline'
        return data
    metadata.fetch_models_dev = cached_models
    local = ModuleType('hermes_cli.local_runtime.bootstrap')
    local.staged_model_ids = lambda: []
    scope = ModuleType('agent.secret_scope')
    scope.current_secret_scope = lambda: None
    scope.serves_routed_profile = lambda: False
    from pathlib import Path
    constants = SimpleNamespace(get_hermes_home=lambda: '/fixture/profile-one')
    monkeypatch.setattr('shadow.common.discovery.ProviderPresence._home',
                        staticmethod(lambda: Path(constants.get_hermes_home()).resolve()))
    routes = ModuleType('hermes_cli.providers')
    routes.LLAMACPP_PROVIDER_ID = 'llamacpp'
    routes.LLAMACPP_ALIASES = {'llamacpp', 'llama.cpp', 'llama-cpp'}
    def slug(name, key=''):
        value = (key or name).lower().replace(' ', '-')
        return value if value.startswith('custom:') else 'custom:' + value
    routes.custom_provider_slug = slug
    routes.custom_provider_aliases = lambda name, key='': {name.lower(), key.lower(), slug(name, key)} - {''}
    monkeypatch.setenv('SHADOW_TEST_ROUTER_KEY', 'fixture-value')
    for name, module in [('hermes_cli.inventory', inventory), ('hermes_cli.auth', auth),
                         ('hermes_cli.runtime_provider', runtime),
                         ('agent.models_dev', metadata), ('hermes_cli.providers', routes),
                         ('agent.secret_scope', scope),
                         ('hermes_cli.local_runtime.bootstrap', local)]:
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(context=context, configs=configs, calls=calls, data=data,
                           inventory=inventory, local=local, runtime=runtime, auth=auth,
                           scope=scope, constants=constants)


def test_provider_plugin_metadata_is_not_loaded_for_discovery(hermes_profile, monkeypatch):
    providers = ModuleType('providers')
    def forbidden(*args, **kwargs):
        pytest.fail('provider discovery could load plugins')
    providers.get_provider_profile = forbidden
    providers.list_providers = forbidden
    providers.bound_profile_resolver = forbidden
    monkeypatch.setitem(sys.modules, 'providers', providers)
    assert len(discover_models().candidates) == 2


def test_profile_inventory_is_the_candidate_source(hermes_profile):
    catalog = discover_models()
    assert catalog.current == Model('openrouter', 'future/aurora')
    assert catalog.candidates == [Model('openrouter', 'future/aurora'), Model('openrouter', 'future/ember')]
    assert catalog.require('openrouter', 'future/ember').model == 'future/ember'
    assert hermes_profile.calls == [False]


def test_discovery_does_not_use_the_picker_that_expands_credentials(hermes_profile):
    hermes_profile.inventory.load_picker_context = lambda: pytest.fail('picker read')
    assert len(discover_models().candidates) == 2


def test_discovery_never_reads_a_nonlocal_config_backend(hermes_profile, monkeypatch):
    monkeypatch.setenv('HERMES_CONFIG_BACKEND', 'remote')
    hermes_profile.inventory.load_picker_context = lambda: pytest.fail('remote config read')
    assert discover_models().failure == 'discovery_unavailable'


def test_explicit_local_file_backend_is_supported(hermes_profile, monkeypatch):
    monkeypatch.setenv('HERMES_CONFIG_BACKEND', 'file')
    assert len(discover_models().candidates) == 2


def test_model_without_credentials_is_refused(hermes_profile):
    with pytest.raises(ModelUnavailable) as error:
        discover_models().require('deepseek', 'deepseek-chat')
    assert error.value.code == 'credentials_unknown'
    assert 'credentials_unknown' in str(error.value)


def test_unknown_and_ambiguous_models_are_refused():
    catalog = ModelCatalog.from_models(Model('one', 'lab/a'), [Model('one', 'lab/a'), Model('two', 'lab/a')])
    with pytest.raises(ModelUnavailable) as error:
        catalog.resolve('lab/a')
    assert error.value.code == 'model_ambiguous'
    assert catalog.resolve('two::lab/a') == Model('two', 'lab/a')
    with pytest.raises(ModelUnavailable) as error:
        catalog.resolve('missing')
    assert error.value.code == 'model_not_configured'


def test_inline_custom_credential_is_not_used_as_presence(hermes_profile):
    class SecretConfig(dict):
        def get(self, key, default=None):
            assert key not in {'token', 'password'}
            return super().get(key, default)
    hermes_profile.configs['lab'] = SecretConfig(models=['future/ember'], api_key='private-exception-canary')
    with pytest.raises(ModelUnavailable) as error:
        discover_models().require('lab', 'future/ember')
    assert error.value.code == 'credentials_unknown'
    assert 'private-exception-canary' not in str(error.value)


def test_environment_credential_values_are_not_read(hermes_profile, monkeypatch):
    import os
    environment = dict(os.environ)
    class NamesOnly(dict):
        def __getitem__(self, key):
            pytest.fail('discovery read an environment value')
        def get(self, key, default=None):
            if key == 'HERMES_CONFIG_BACKEND':
                return dict.get(self, key, default)
            pytest.fail('discovery read a credential environment value')
    monkeypatch.setattr(os, 'environ', NamesOnly(environment))
    assert len(discover_models().candidates) == 2


@pytest.fixture
def codex_profile(hermes_profile):
    hermes_profile.configs['openai-codex'] = {'models': ['gpt-future']}
    return hermes_profile


def test_fresh_codex_login_is_available_from_hermes_auth_status(codex_profile):
    codex_profile.auth.get_auth_status = lambda provider: {'logged_in': provider == 'openai-codex'}
    model = discover_models().require('openai-codex', 'gpt-future')
    assert (model.provider, model.model, model.reachable) == ('openai-codex', 'gpt-future', True)
    assert model.plan


@pytest.mark.parametrize('status', [{'logged_in': False}, {}, None, {'logged_in': 'false'}])
def test_oauth_without_a_presence_signal_is_unknown(codex_profile, status):
    codex_profile.auth.get_auth_status = lambda provider: status
    with pytest.raises(ModelUnavailable) as error:
        discover_models().require('openai-codex', 'gpt-future')
    assert error.value.code == 'credentials_unknown'


@pytest.mark.parametrize('live', [False, True])
def test_oauth_status_failure_preserves_live_presence_fallback(codex_profile, live):
    def unavailable(provider):
        raise RuntimeError('private-exception-canary')
    codex_profile.auth.get_auth_status = unavailable
    catalog = discover_models(live_providers={'openai-codex'} if live else frozenset())
    if live:
        assert catalog.require('openai-codex', 'gpt-future').reachable
    else:
        with pytest.raises(ModelUnavailable) as error:
            catalog.require('openai-codex', 'gpt-future')
        assert error.value.code == 'credentials_unknown'
        assert 'private-exception-canary' not in str(error.value)


def test_oauth_status_api_missing_keeps_credentials_unknown(codex_profile):
    del codex_profile.auth.get_auth_status
    with pytest.raises(ModelUnavailable) as error:
        discover_models().require('openai-codex', 'gpt-future')
    assert error.value.code == 'credentials_unknown'
    assert discover_models(live_providers={'openai-codex'}).require('openai-codex', 'gpt-future').reachable


@pytest.mark.parametrize('provider', ['nous', 'anthropic', 'qwen-oauth', 'xai-oauth', 'minimax-oauth'])
def test_non_codex_discovery_never_calls_refresh_capable_auth_status(hermes_profile, provider):
    hermes_profile.configs.clear()
    hermes_profile.configs[provider] = {'models': ['future/model']}
    hermes_profile.context.current_provider = provider
    hermes_profile.context.current_model = 'future/model'
    def forbidden(provider):
        pytest.fail('discovery called a refresh-capable auth status')
    hermes_profile.auth.get_auth_status = forbidden
    with pytest.raises(ModelUnavailable) as error:
        discover_models().require(provider, 'future/model')
    assert error.value.code == 'credentials_unknown'
    assert discover_models(live_providers={provider}).require(provider, 'future/model').reachable


def test_oauth_presence_does_not_read_secret_values_or_files(codex_profile, monkeypatch):
    import builtins
    import os
    from pathlib import Path

    class NamesOnly(dict):
        def __getitem__(self, key):
            pytest.fail('discovery read an environment value')
        def get(self, key, default=None):
            if key == 'HERMES_CONFIG_BACKEND':
                return dict.get(self, key, default)
            pytest.fail('discovery read a credential environment value')
    class StatusOnly(dict):
        def get(self, key, default=None):
            assert key == 'logged_in', 'discovery read more than login presence'
            return super().get(key, default)
    def forbidden(*args, **kwargs):
        pytest.fail('discovery opened a file')
    codex_profile.auth.get_auth_status = lambda provider: StatusOnly(logged_in=provider == 'openai-codex')
    with monkeypatch.context() as boundary:
        boundary.setattr(os, 'environ', NamesOnly.fromkeys(os.environ, ''))
        boundary.setattr(builtins, 'open', forbidden)
        boundary.setattr(Path, 'open', forbidden)
        catalog = discover_models()
    assert catalog.require('openai-codex', 'gpt-future').reachable


def test_config_booleans_alone_do_not_prove_credentials(hermes_profile, monkeypatch):
    monkeypatch.delenv('SHADOW_TEST_ROUTER_KEY')
    hermes_profile.configs['openrouter'].update(logged_in=True, configured=True, authenticated=True)
    assert discover_models().candidates == []


def test_base_url_presence_does_not_authenticate_a_provider(hermes_profile, monkeypatch):
    monkeypatch.delenv('SHADOW_TEST_ROUTER_KEY')
    monkeypatch.setenv('SHADOW_ROUTER_BASE_URL', 'https://unused.invalid')
    assert discover_models().candidates == []


@pytest.mark.parametrize('signals', [{'source': 'local-runtime'}, {'auth_type': 'local'},
                                     {'authenticated': True, 'auth_verified': True}])
def test_config_inventory_fields_do_not_bypass_credentials(hermes_profile, signals):
    hermes_profile.configs['lab'] = {'models': ['future/ember'], **signals}
    with pytest.raises(ModelUnavailable) as error:
        discover_models().require('lab', 'future/ember')
    assert error.value.code == 'credentials_unknown'


def test_staged_hermes_models_are_available_without_auth(hermes_profile):
    hermes_profile.local.staged_model_ids = lambda: ['qwen-staged']
    assert discover_models().require('llamacpp', 'qwen-staged') == Model('llamacpp', 'qwen-staged')


def test_cached_metadata_adds_models_only_for_configured_providers(hermes_profile):
    hermes_profile.data.update({'openrouter': {'env': ['SHADOW_TEST_ROUTER_KEY'],
                                             'models': {'future/new': {}}},
                                'unconfigured': {'models': {'other/model': {}}}})
    catalog = discover_models()
    assert catalog.require('openrouter', 'future/new') == Model('openrouter', 'future/new')
    with pytest.raises(ModelUnavailable):
        catalog.resolve('other/model')


def test_custom_provider_models_and_aliases_are_kept(hermes_profile, monkeypatch):
    hermes_profile.context.custom_providers = [dict(name='Lab', provider_key='lab', model='future/a',
                                                   models={'future/a': {}, 'future/b': {}}, key_env='SHADOW_LAB_KEY')]
    hermes_profile.context.current_provider = 'lab'
    hermes_profile.context.current_model = 'future/a'
    monkeypatch.setenv('SHADOW_LAB_KEY', 'fixture-value')
    catalog = discover_models()
    assert catalog.current == Model('custom:lab', 'future/a')
    assert catalog.resolve('lab::future/b') == Model('custom:lab', 'future/b')


def test_qualified_model_does_not_match_another_namespace(hermes_profile):
    with pytest.raises(ModelUnavailable) as error:
        discover_models().resolve('different/ember')
    assert error.value.code == 'model_not_configured'


def test_current_provider_alias_uses_hermes_inventory_identity(hermes_profile):
    hermes_profile.configs['lab'] = hermes_profile.configs.pop('openrouter')
    hermes_profile.context.current_provider = 'custom:lab'
    catalog = discover_models()
    assert catalog.current == Model('lab', 'future/aurora')
    assert catalog.resolve('custom:lab::future/ember') == Model('lab', 'future/ember')


def test_disabled_and_excluded_providers_are_not_candidates(hermes_profile):
    hermes_profile.configs['openrouter']['enabled'] = False
    catalog = discover_models()
    assert catalog.candidates == []
    hermes_profile.configs['openrouter']['enabled'] = True
    hermes_profile.context.excluded_providers = ['openrouter']
    assert discover_models().candidates == []


def test_discovery_failure_is_reported_without_a_model_guess(hermes_profile, monkeypatch):
    def fail():
        raise RuntimeError('private-exception-canary')
    monkeypatch.setattr('shadow.common.discovery._local_context', fail)
    with pytest.raises(ModelUnavailable) as error:
        discover_models().resolve('future/ember')
    assert error.value.code == 'discovery_unavailable'
    assert 'private-exception-canary' not in str(error.value)


def test_discovery_copy_is_in_the_three_line_copy_table():
    from pathlib import Path
    directory = Path(__file__).resolve().parents[1] / 'shadow' / 'copy'
    assert not (directory / 'discovery.json').exists()
    import json
    assert 'discovery.credentials_unavailable' in json.loads((directory / 'common.json').read_text())
    assert 'credentials_unavailable' in load_copy()['discovery.credentials_unavailable']


def test_api_key_metadata_does_not_authenticate_an_oauth_route(hermes_profile, monkeypatch):
    hermes_profile.configs['openai-codex'] = {'models': ['gpt-future']}
    hermes_profile.data['openai'] = {'env': ['SHADOW_TEST_OPENAI_KEY'], 'models': {}}
    monkeypatch.setenv('SHADOW_TEST_OPENAI_KEY', 'fixture-value')
    with pytest.raises(ModelUnavailable) as error:
        discover_models().require('openai-codex', 'gpt-future')
    assert error.value.code == 'credentials_unknown'


@pytest.mark.parametrize('provider', ['openai-codex', 'nous', 'copilot', 'anthropic', 'deepseek'])
def test_completed_live_request_unlocks_provider_for_doctor_and_picker(hermes_profile, tmp_path, provider):
    from fixtures import FakeContext
    from shadow.common.copy import copy
    from shadow.common.storage import Store
    from shadow.trial.plugin import TrialPlugin, register

    hermes_profile.context.current_provider = provider
    hermes_profile.context.current_model = 'gpt-6.1-sol'
    hermes_profile.configs.clear()
    hermes_profile.configs[provider] = {'models': ['gpt-6.1-sol', 'gpt-6.1-terra']}
    ctx = FakeContext()
    plugin = TrialPlugin(ctx, store=Store(tmp_path / 'unused.db'), config_reader=lambda: {}, launcher=SimpleNamespace(isolation_verified=False))
    register(ctx, plugin=plugin)
    assert plugin.availability() == (False, False)
    assert copy('doctor.other_model') in plugin.command('doctor')
    with pytest.raises(ModelUnavailable) as error:
        plugin.model_catalog().require(provider, 'gpt-6.1-sol')
    assert error.value.code == 'credentials_unknown'
    ctx.hook('api_request_error', provider=provider, api_request_id='failed', session_id='live')
    assert plugin.availability() == (False, False)
    ctx.hook('post_api_request', provider=provider, api_request_id='completed', session_id='live')
    catalog = plugin.model_catalog()
    assert catalog.require(provider, 'gpt-6.1-sol') == Model(provider, 'gpt-6.1-sol', plan=provider == 'openai-codex')
    assert catalog.resolve('gpt-6.1-terra') == Model(provider, 'gpt-6.1-terra', plan=provider == 'openai-codex')
    assert plugin.availability() == (True, True)
    assert copy('doctor.other_model') not in plugin.command('doctor')
    from shadow.switch.plugin import SwitchPlugin
    from shadow.switch.domain import Route
    switch = SwitchPlugin(ctx, None, config_reader=lambda: {})
    switch.validate_routes(Route(provider, 'gpt-6.1-sol', 'https://unused.invalid/v1', 'chat_completions'),
                           Route(provider, 'gpt-6.1-terra', 'https://unused.invalid/v1', 'chat_completions'))
    assert not (tmp_path / 'unused.db').exists()


def test_live_presence_never_crosses_profiles_or_service_instances(hermes_profile, tmp_path):
    from fixtures import FakeContext
    from shadow.common.storage import Store
    from shadow.trial.plugin import TrialPlugin, register

    hermes_profile.context.current_provider = 'openai-codex'
    hermes_profile.context.current_model = 'gpt-6.1-sol'
    hermes_profile.configs['openai-codex'] = {'models': ['gpt-6.1-sol', 'gpt-6.1-terra']}
    ctx = FakeContext()
    plugin = TrialPlugin(ctx, store=Store(tmp_path / 'unused.db'))
    register(ctx, plugin=plugin)
    ctx.hook('post_api_request', provider='openai-codex', api_request_id='completed', session_id='live')
    plugin.model_catalog().require('openai-codex', 'gpt-6.1-terra')
    hermes_profile.constants.get_hermes_home = lambda: '/fixture/profile-two'
    with pytest.raises(ModelUnavailable):
        plugin.model_catalog().require('openai-codex', 'gpt-6.1-terra')
    hermes_profile.constants.get_hermes_home = lambda: '/fixture/profile-one'
    fresh = TrialPlugin(FakeContext(), store=Store(tmp_path / 'other.db'))
    with pytest.raises(ModelUnavailable):
        fresh.model_catalog().require('openai-codex', 'gpt-6.1-terra')


def test_doctor_refuses_an_unknown_current_provider_even_with_available_alternatives(hermes_profile, tmp_path):
    from fixtures import FakeContext
    from shadow.common.storage import Store
    from shadow.trial.plugin import TrialPlugin

    hermes_profile.context.current_provider = 'openai-codex'
    hermes_profile.context.current_model = 'gpt-6.1-sol'
    plugin = TrialPlugin(FakeContext(), store=Store(tmp_path / 'unused.db'))
    assert plugin.availability() == (False, False)


def test_routed_profile_uses_only_scoped_key_names(hermes_profile):
    class NamesOnly(dict):
        def get(self, *args):
            pytest.fail('discovery read a scoped secret value')
        def __getitem__(self, key):
            pytest.fail('discovery read a scoped secret value')
    hermes_profile.scope.serves_routed_profile = lambda: True
    hermes_profile.scope.current_secret_scope = lambda: NamesOnly()
    assert discover_models().candidates == []
    hermes_profile.scope.current_secret_scope = lambda: NamesOnly(SHADOW_TEST_ROUTER_KEY=None)
    assert len(discover_models().candidates) == 2


def test_launch_profile_combines_scope_and_environment_names(hermes_profile, monkeypatch):
    hermes_profile.scope.current_secret_scope = lambda: {'SHADOW_TEST_DEEPSEEK_KEY': None}
    assert len(discover_models().candidates) == 3


def test_child_or_incomplete_events_do_not_prove_profile_credentials(hermes_profile, tmp_path):
    from fixtures import FakeContext
    from shadow.common.storage import Store
    from shadow.trial.plugin import TrialPlugin, register

    hermes_profile.context.current_provider = 'openai-codex'
    hermes_profile.context.current_model = 'gpt-6.1-sol'
    ctx = FakeContext()
    plugin = TrialPlugin(ctx, store=Store(tmp_path / 'unused.db'))
    register(ctx, plugin=plugin)
    ctx.hook('subagent_start', child_session_id='child-with-override')
    ctx.hook('post_compaction', old_session_id='child-with-override', session_id='compacted-child')
    for event in [dict(session_id='child-with-override'), dict(session_id='compacted-child'), dict(session_id='')]:
        ctx.hook('post_api_request', provider='openai-codex', api_request_id='request', **event)
    with pytest.raises(ModelUnavailable):
        plugin.model_catalog().require('openai-codex', 'gpt-6.1-sol')


@pytest.mark.parametrize('compacted', [False, True])
def test_child_in_another_profile_does_not_prove_credentials(hermes_profile, tmp_path, compacted):
    from fixtures import FakeContext
    from shadow.common.storage import Store
    from shadow.trial.plugin import TrialPlugin, register

    hermes_profile.context.current_provider = 'openai-codex'
    hermes_profile.context.current_model = 'gpt-6.1-sol'
    ctx = FakeContext()
    plugin = TrialPlugin(ctx, store=Store(tmp_path / 'unused.db'))
    register(ctx, plugin=plugin)
    ctx.hook('subagent_start', child_session_id='routed-child')
    hermes_profile.constants.get_hermes_home = lambda: '/fixture/profile-two'
    session_id = 'routed-child'
    if compacted:
        ctx.hook('post_compaction', old_session_id=session_id, session_id='compacted-child')
        session_id = 'compacted-child'
    ctx.hook('post_api_request', provider='openai-codex', session_id=session_id, api_request_id='child')
    with pytest.raises(ModelUnavailable):
        plugin.model_catalog().require('openai-codex', 'gpt-6.1-sol')
    ctx.hook('post_api_request', provider='openai-codex', session_id='main', api_request_id='main')
    assert plugin.model_catalog().require('openai-codex', 'gpt-6.1-sol') == Model('openai-codex', 'gpt-6.1-sol', plan=True)


@pytest.mark.parametrize('reference', ['${SHADOW_LAB_KEY}', '${SHADOW_LAB_KEY}suffix', '$SHADOW_LAB_KEY'])
def test_raw_api_key_reference_checks_only_the_name(hermes_profile, monkeypatch, reference):
    hermes_profile.configs['lab'] = {'models': ['future/ember'], 'api_key': reference}
    monkeypatch.setenv('SHADOW_LAB_KEY', 'fixture-value')
    import os
    original = type(os.environ).__getitem__
    def no_secret_value(self, key):
        assert key != 'SHADOW_LAB_KEY', 'resolved a credential reference'
        return original(self, key)
    monkeypatch.setattr(type(os.environ), '__getitem__', no_secret_value)
    if reference == '${SHADOW_LAB_KEY}':
        assert discover_models().require('lab', 'future/ember') == Model('lab', 'future/ember')
    else:
        with pytest.raises(ModelUnavailable):
            discover_models().require('lab', 'future/ember')


@pytest.mark.parametrize('name', ['AZURE_RESOURCE_NAME', 'GOOGLE_VERTEX_PROJECT', 'GOOGLE_VERTEX_LOCATION',
                                 'SHADOW_LAB_API_KEY', 'SHADOW_LAB_TOKEN'])
def test_metadata_credential_names_exclude_resource_identifiers(hermes_profile, monkeypatch, name):
    hermes_profile.configs['lab'] = {'models': ['future/ember']}
    hermes_profile.data['lab'] = {'env': [name]}
    monkeypatch.setenv(name, 'fixture-value')
    catalog = discover_models()
    if name.endswith(('_KEY', '_TOKEN')):
        assert catalog.require('lab', 'future/ember') == Model('lab', 'future/ember')
    else:
        with pytest.raises(ModelUnavailable) as error:
            catalog.require('lab', 'future/ember')
        assert error.value.code == 'credentials_unknown'


def test_demo_try_reaches_the_consent_card_after_both_stored_login_providers_reply(hermes_profile, tmp_path):
    from fixtures import FakeContext, selection
    from shadow.common.copy import copy
    from shadow.common.storage import Store
    from shadow.trial.plugin import TrialPlugin, register

    hermes_profile.context.current_provider = 'openai-codex'
    hermes_profile.context.current_model = 'gpt-6.1-sol'
    hermes_profile.configs.clear()
    hermes_profile.configs.update({
        'openai-codex': {'models': ['gpt-6.1-sol', 'gpt-6.1-terra']},
        'anthropic': {'models': ['claude-sonnet-4-5']},
    })
    ctx = FakeContext()
    plugin = TrialPlugin(ctx, store=Store(tmp_path / 'trial.db'),
                         launcher=SimpleNamespace(isolation_verified=False, profile='demo'),
                         selector=lambda **kwargs: selection(tmp_path, kwargs['count']),
                         config_reader=lambda: {})
    register(ctx, plugin=plugin)
    command = 'try gpt-6.1-terra --judge claude-sonnet-4-5 --turns 20 --budget 5'
    assert '[credentials_unknown]' in ctx.commands['shadow'](command)
    for provider in ('openai-codex', 'anthropic'):
        ctx.hook('post_api_request', session_id='main-' + provider, provider=provider,
                 api_request_id='completed-' + provider)
    text = ctx.commands['shadow'](command)
    assert 'Try GPT-6.1 Terra against your GPT-6.1 Sol' in text
    assert 'Judged blind by Claude Sonnet 4.5, your pick.' in text
    assert copy('trial.isolation_required') in text
    assert '[credentials_unknown]' not in text
    assert not ctx.capture
    assert plugin.store.get()['state'] == 'prepared'


def test_fresh_cli_doctor_and_try_see_codex_login_without_warmup(codex_profile, tmp_path, capsys, monkeypatch):
    import argparse
    from fixtures import FakeContext, selection
    from shadow.common.copy import copy
    from shadow.common.storage import Store
    from shadow.trial.plugin import TrialPlugin, register

    codex_profile.context.current_provider = 'openai-codex'
    codex_profile.context.current_model = 'gpt-6.1-sol'
    codex_profile.configs.clear()
    codex_profile.configs.update({
        'openai-codex': {'models': ['gpt-6.1-sol', 'gpt-6.1-terra']},
        'anthropic': {'models': ['claude-sonnet-4-5'], 'key_env': 'SHADOW_TEST_ANTHROPIC_KEY'},
    })
    monkeypatch.setenv('SHADOW_TEST_ANTHROPIC_KEY', 'fixture-value')
    codex_profile.auth.get_auth_status = lambda provider: {'logged_in': provider == 'openai-codex'}
    ctx = FakeContext()
    plugin = TrialPlugin(ctx, store=Store(tmp_path / 'trial.db'),
                         launcher=SimpleNamespace(isolation_verified=False, profile='demo'),
                         selector=lambda **kwargs: selection(tmp_path, kwargs['count']),
                         config_reader=lambda: {})
    register(ctx, plugin=plugin)
    parser = argparse.ArgumentParser()
    setup, handler = ctx.cli['shadow']
    setup(parser)
    handler(parser.parse_args(['doctor']))
    doctor = capsys.readouterr().out
    assert copy('doctor.other_model') not in doctor
    assert copy('doctor.judge') not in doctor
    assert plugin.availability() == (True, True)
    handler(parser.parse_args(['try', 'gpt-6.1-terra', '--judge', 'claude-sonnet-4-5',
                              '--turns', '20', '--budget', '5']))
    text = capsys.readouterr().out
    assert 'Try GPT-6.1 Terra against your GPT-6.1 Sol' in text
    assert 'Judged blind by Claude Sonnet 4.5, your pick.' in text
    assert '[credentials_unknown]' not in text
    assert copy('trial.isolation_required') in ' '.join(text.split())
    assert not ctx.capture
    assert plugin.store.get()['state'] == 'prepared'
