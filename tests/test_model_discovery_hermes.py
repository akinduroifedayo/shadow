import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


HERMES_SOURCE = os.environ.get('SHADOW_HERMES_SOURCE')


@pytest.mark.parametrize('warm_cache', [False, True])
@pytest.mark.parametrize('initialized, registered', [(False, False), (True, False), (True, True)])
def test_real_hermes_discovery_is_local_only(tmp_path, warm_cache, initialized, registered):
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
          provider: openai-codex
          default: gpt-future
        providers:
          openai-codex:
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
            'openai': {'env': ['OPENAI_API_KEY'], 'models': {'gpt-cached': {}}},
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
        from hermes_cli import config_backend
        if sys.argv[2] == 'initialized':
            config_backend.get_config_backend()
        if sys.argv[3] == 'registered':
            from providers import list_providers
            profiles = {profile.name: profile for profile in list_providers()}
            assert profiles['openrouter'].auth_type == 'api_key'
        (Path(os.environ['HERMES_HOME']) / '.env').write_text('SHADOW_DOTENV_CANARY=fixture')
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
        if sys.argv[2] != 'initialized':
            assert catalog.failure == 'discovery_unavailable'
            print('real Hermes discovery refused before dotenv bootstrap')
            raise SystemExit(0)
        assert catalog.failure is None, catalog.failure
        assert catalog.current == Model('openai-codex', 'gpt-future', reachable=False)
        assert catalog.require('lab', 'future/ember') == Model('lab', 'future/ember')
        assert catalog.require('custom:lab', 'future/ember') == Model('lab', 'future/ember')
        assert catalog.require('referenced', 'future/reference') == Model('referenced', 'future/reference')
        if sys.argv[3] == 'registered':
            assert catalog.require('openrouter', 'future/router') == Model('openrouter', 'future/router')
        assert catalog.require('llamacpp', 'qwen-local') == Model('llamacpp', 'qwen-local')
        assert catalog.require('saved-lab', 'future/custom') == Model('custom:saved-lab', 'future/custom')
        try:
            catalog.require('openai-codex', 'gpt-future')
        except ModelUnavailable as error:
            assert error.code == 'credentials_unknown', error.code
        else:
            raise AssertionError('Codex was authenticated without a presence-only signal')
        presence = ProviderPresence()
        presence.observe(provider='openai-codex', session_id='offline-completed')
        catalog = discover_models(live_providers=presence.providers())
        assert catalog.current == Model('openai-codex', 'gpt-future')
        for name in ('gpt-future', 'gpt-next', 'gpt-6.1-sol', 'gpt-6.1-terra'):
            assert catalog.require('openai-codex', name) == Model('openai-codex', name)
        assert not any(model.provider == 'unconfigured' for model in catalog.models)
        if sys.argv[1] == 'warm':
            assert any(model.model == 'gpt-cached' for model in catalog.models)
        print('real Hermes local-only discovery passed')
        ''')
    repo = Path(__file__).resolve().parents[1]
    import_paths = [str(repo), str(source)]
    interpreter = os.environ.get('SHADOW_HERMES_PYTHON', sys.executable)
    result = subprocess.run([interpreter, '-c', script, 'warm' if warm_cache else 'cold',
                             'initialized' if initialized else 'uninitialized',
                             'registered' if registered else 'unregistered'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30,
                            env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'HERMES_HOME': str(profile),
                                 'PYTHONPATH': ':'.join(import_paths), 'PYTHONDONTWRITEBYTECODE': '1',
                                 'SHADOW_TEST_PRESENT_KEY': 'fixture-value', 'OPENROUTER_API_KEY': 'fixture-value'})
    assert result.returncode == 0, result.stdout + result.stderr
    expected = ('real Hermes local-only discovery passed' if initialized else
                'real Hermes discovery refused before dotenv bootstrap')
    assert result.stdout.strip() == expected
