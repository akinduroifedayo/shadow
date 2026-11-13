"""Keep desktop wire names aligned with the Hermes manifest identity."""
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap

import pytest
import yaml

from shadow import desktop_rpc
from test_real_tools import require_hermes_reference


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = yaml.safe_load((ROOT / 'plugin.yaml').read_text())['name']
PREFIX = f'plugin.{MANIFEST_NAME}.'


@pytest.mark.parametrize('filename', [
    'desktop/plugin.js',
    'desktop/dev/serve.py',
    'desktop/dev/shoot.mjs',
    'desktop/dev/test-ui.mjs',
    'shadow/desktop_rpc.py',
])
def test_desktop_wire_prefix_matches_manifest(filename):
    prefixes = set(re.findall(r'plugin\.[a-z0-9_-]+\.', (ROOT / filename).read_text()))
    assert prefixes == {PREFIX}


def test_hermes_wire_identity_matches_manifest(tmp_path):
    reference = require_hermes_reference().resolve()
    script = textwrap.dedent('''\
        import sys
        from types import SimpleNamespace
        sys.path.insert(0, sys.argv[1])
        from hermes_cli.plugin_events import plugin_event_name
        from hermes_cli.plugins import PluginContext, PluginManifest

        name, *local_names = sys.argv[2:]
        for key in ('', name):
            ctx = PluginContext(PluginManifest(name=name, key=key), SimpleNamespace())
            assert ctx.plugin_id == name
            for local in local_names:
                assert plugin_event_name(ctx.plugin_id, local) == f'plugin.{name}.{local}'
        ''')
    methods = desktop_rpc.DesktopRpc(
        desktop_rpc.FixtureSource('empty'), desktop_rpc.fixture_copy()).methods()
    result = subprocess.run(
        [os.environ.get('SHADOW_HERMES_PYTHON', sys.executable), '-c', script,
         str(reference), MANIFEST_NAME, *methods, desktop_rpc.EVENT_CHANGED],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp_path),
             'HERMES_HOME': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1'},
    )
    assert result.returncode == 0, result.stdout + result.stderr
