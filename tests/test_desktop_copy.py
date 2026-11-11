"""Desktop copy snapshots are fallbacks for independent line checkouts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from shadow import desktop_rpc as d

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'tests' / 'fixtures' / 'desktop' / 'copy'


def copy_layout(tmp_path, monkeypatch):
    own = tmp_path / 'shadow' / 'copy'
    own.mkdir(parents=True)
    shutil.copy(ROOT / 'shadow' / 'copy' / 'desktop.json', own)
    monkeypatch.setattr(d, '__file__', str(own.parent / 'desktop_rpc.py'))
    return own


@pytest.mark.parametrize('extra_owner', [False, True])
def test_live_copy_tables_replace_snapshot_tables(tmp_path, monkeypatch, extra_owner):
    own = copy_layout(tmp_path, monkeypatch)
    live = tmp_path / 'trial-copy' if extra_owner else own
    live.mkdir(exist_ok=True)
    for name in ('trial.json', 'common.json'):
        shutil.copy(SNAPSHOT / name, live)
    trial = json.loads((live / 'trial.json').read_text())
    trial['trial.stopped'] = 'Live stopped copy'
    del trial['start.preview']
    (live / 'trial.json').write_text(json.dumps(trial))
    table = d.fixture_copy(((live,) if extra_owner else ()) + (SNAPSHOT,))
    assert table['trial.stopped'] == 'Live stopped copy'
    assert 'start.preview' not in table
    assert table['desktop.title'] == 'Shadow'


def test_real_copy_ownership_conflicts_still_fail(tmp_path, monkeypatch):
    own = copy_layout(tmp_path, monkeypatch)
    duplicate = tmp_path / 'other-owner'
    duplicate.mkdir()
    title = json.loads((own / 'desktop.json').read_text())['desktop.title']
    (duplicate / 'another.json').write_text(json.dumps({'desktop.title': title}))
    with pytest.raises(ValueError, match='duplicate copy key: desktop.title'):
        d.fixture_copy((duplicate, SNAPSHOT))


@pytest.mark.parametrize('merged', [False, True])
def test_desktop_rpc_collection_in_isolated_and_merged_layouts(tmp_path, merged):
    layout = tmp_path / ('merged' if merged else 'isolated')
    shutil.copytree(ROOT / 'shadow', layout / 'shadow', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'desktop' / 'fixtures', layout / 'desktop' / 'fixtures')
    shutil.copytree(SNAPSHOT, layout / 'tests' / 'fixtures' / 'desktop' / 'copy')
    shutil.copy(ROOT / 'tests' / 'test_desktop_rpc.py', layout / 'tests')
    if merged:
        for path in SNAPSHOT.glob('*.json'):
            shutil.copy(path, layout / 'shadow' / 'copy')
        common = layout / 'shadow' / 'common'
        common.mkdir(exist_ok=True)
        (common / '__init__.py').touch()
        (common / 'copy.py').write_text(
            'from shadow.desktop_rpc import fixture_copy\n'
            'def load_copy():\n'
            '    return fixture_copy()\n'
        )
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q', 'tests/test_desktop_rpc.py'],
        cwd=layout, env={**os.environ, 'PYTHONPATH': str(layout)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'tests collected' in result.stdout
