import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


HERMES_SOURCE = os.environ.get('SHADOW_HERMES_SOURCE')


def test_real_hermes_discovers_gemini_before_any_provider_lookup(tmp_path):
    from test_real_tools import require_hermes_reference

    source = require_hermes_reference().resolve()
    profile = tmp_path / 'profile'
    profile.mkdir()
    (profile / 'config.yaml').write_text('model:\n  provider: gemini\n  default: gemini-test\n')
    script = textwrap.dedent('''\
        import sys
        import requests
        from hermes_cli.config import read_raw_config_readonly
        read_raw_config_readonly()

        class ForbiddenDiscoveryEffect(BaseException):
            pass

        def audit(event, args):
            if event.startswith('socket.'):
                raise ForbiddenDiscoveryEffect('provider initialization used a socket')
        sys.addaudithook(audit)
        from shadow.common.discovery import discover_models
        from shadow.common.models import Model

        catalog = discover_models()
        assert catalog.failure is None, catalog.failure
        assert catalog.require('gemini', 'gemini-test') == Model('gemini', 'gemini-test')
        ''')
    repo = Path(__file__).resolve().parents[1]
    interpreter = os.environ.get('SHADOW_HERMES_PYTHON', sys.executable)
    result = subprocess.run([interpreter, '-c', script], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30,
                            env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'HERMES_HOME': str(profile),
                                 'PYTHONPATH': ':'.join([str(repo), str(source)]),
                                 'PYTHONDONTWRITEBYTECODE': '1', 'GEMINI_API_KEY': 'fixture-value'})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('warm_cache', [False, True])
@pytest.mark.parametrize('preload', ['registry', 'listing', 'lookup'])
def test_real_hermes_discovery_is_local_only(tmp_path, warm_cache, preload):
    """Preload requests' IPv6 capability check, then trap all Hermes discovery effects."""
    if not HERMES_SOURCE:
        pytest.skip('set SHADOW_HERMES_SOURCE to a Hermes source checkout for the offline safety test')
    source = Path(HERMES_SOURCE).resolve()
    if not (source / 'hermes_cli' / 'inventory.py').is_file():
        pytest.skip('SHADOW_HERMES_SOURCE does not contain hermes_cli/inventory.py')
    profile = tmp_path / 'profile'
    profile.mkdir()
    (profile / 'config.yaml').write_text(textwrap.dedent('''\
        model:
          provider: nous
          default: gpt-future
        providers:
          nous:
            models: [gpt-future, gpt-next, gpt-6.1-sol, gpt-6.1-terra]
          openrouter:
            models: [future/router]
          lab:
            models: [future/aurora, future/ember]
            key_env: SHADOW_TEST_PRESENT_KEY
            api_key: ${SHADOW_FORBIDDEN_SECRET}
          referenced:
            models: [future/reference]
            api_key: ${SHADOW_TEST_PRESENT_KEY}
          llamacpp:
            models: [qwen-local]
        custom_providers:
          - name: Saved Lab
            provider_key: saved-lab
            base_url: https://unreachable.invalid/v1
            key_env: SHADOW_TEST_PRESENT_KEY
            models: [future/custom]
        '''))
    models = profile / 'models'
    models.mkdir(parents=True)
    (models / 'qwen-local.gguf').touch()
    if warm_cache:
        cache = profile / 'models_dev_cache.json'
        cache.write_text(json.dumps({
            'nous': {'env': ['NOUS_API_KEY'], 'models': {'gpt-cached': {}}},
            'unconfigured': {'env': [], 'models': {'unconfigured/model': {}}},
        }))
        os.utime(cache, (1, 1))
    script = textwrap.dedent('''\
        import os
        from pathlib import Path
        import sys
        import threading

        class ForbiddenDiscoveryEffect(BaseException):
            pass

        def audit(event, args):
            if event.startswith('socket.'):
                raise ForbiddenDiscoveryEffect('discovery used a socket')
            if event == 'open' and isinstance(args[0], (str, bytes)):
                path = Path(os.fsdecode(args[0]))
                if path.name in {'.env', 'auth.json'} or '.codex' in path.parts:
                    raise ForbiddenDiscoveryEffect('discovery opened a credential file')

        import requests
        from hermes_cli.env_loader import load_hermes_dotenv
        dotenv = Path(os.environ['HERMES_HOME']) / '.env'
        dotenv.write_text('SHADOW_DOTENV_CANARY=fixture\\nOPENROUTER_API_KEY=fixture-value\\n')
        load_hermes_dotenv(load_external_secrets=False)
        assert os.environ['SHADOW_DOTENV_CANARY'] == 'fixture'
        from providers import bound_registry_key, get_provider_profile, list_providers
        if sys.argv[2] == 'registry':
            bound_registry_key()
        elif sys.argv[2] == 'listing':
            profiles = {profile.name: profile for profile in list_providers()}
            assert profiles['openrouter'].auth_type == 'api_key'
        else:
            assert get_provider_profile('openrouter').auth_type == 'api_key'
        sys.addaudithook(audit)
        original_getitem = type(os.environ).__getitem__
        def environment_value(self, key):
            if key in {'SHADOW_FORBIDDEN_SECRET', 'SHADOW_TEST_PRESENT_KEY', 'OPENROUTER_API_KEY'}:
                raise ForbiddenDiscoveryEffect('discovery resolved a secret config reference')
            return original_getitem(self, key)
        type(os.environ).__getitem__ = environment_value
        def no_thread(self):
            raise ForbiddenDiscoveryEffect('discovery started a thread')
        threading.Thread.start = no_thread
        from shadow.common.discovery import ModelUnavailable, ProviderPresence, discover_models
        from shadow.common.models import Model

        catalog = discover_models()
        assert catalog.failure is None, catalog.failure
        assert catalog.current == Model('nous', 'gpt-future', reachable=False)
        assert catalog.require('lab', 'future/ember') == Model('lab', 'future/ember')
        assert catalog.require('custom:lab', 'future/ember') == Model('lab', 'future/ember')
        assert catalog.require('referenced', 'future/reference') == Model('referenced', 'future/reference')
        assert catalog.require('openrouter', 'future/router') == Model('openrouter', 'future/router')
        assert catalog.require('llamacpp', 'qwen-local') == Model('llamacpp', 'qwen-local')
        assert catalog.require('saved-lab', 'future/custom') == Model('custom:saved-lab', 'future/custom')
        try:
            catalog.require('nous', 'gpt-future')
        except ModelUnavailable as error:
            assert error.code == 'credentials_unknown', error.code
        else:
            raise AssertionError('OAuth provider was authenticated without a presence-only signal')
        presence = ProviderPresence()
        presence.observe(provider='nous', session_id='offline-completed')
        catalog = discover_models(live_providers=presence.providers())
        assert catalog.current == Model('nous', 'gpt-future')
        for name in ('gpt-future', 'gpt-next', 'gpt-6.1-sol', 'gpt-6.1-terra'):
            assert catalog.require('nous', name) == Model('nous', name)
        assert not any(model.provider == 'unconfigured' for model in catalog.models)
        if sys.argv[1] == 'warm':
            assert any(model.model == 'gpt-cached' for model in catalog.models)
        print('real Hermes local-only discovery passed')
        ''')
    repo = Path(__file__).resolve().parents[1]
    import_paths = [str(repo), str(source)]
    interpreter = os.environ.get('SHADOW_HERMES_PYTHON', sys.executable)
    result = subprocess.run([interpreter, '-c', script, 'warm' if warm_cache else 'cold', preload],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30,
                            env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'HERMES_HOME': str(profile),
                                 'PYTHONPATH': ':'.join(import_paths), 'PYTHONDONTWRITEBYTECODE': '1',
                                 'SHADOW_TEST_PRESENT_KEY': 'fixture-value', 'OPENROUTER_API_KEY': 'fixture-value'})
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'real Hermes local-only discovery passed'
