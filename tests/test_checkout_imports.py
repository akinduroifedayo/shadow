import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_collection_from_checkout_named_shadow(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    checkout = tmp_path / 'shadow'
    shutil.copytree(repo, checkout, symlinks=True, ignore=shutil.ignore_patterns(
        '.git', '__pycache__', '*.pyc', '.pytest_cache', '.venv', 'venv', 'node_modules'))
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider'],
        cwd=checkout, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'acceptance/test_ac04_judge.py::' in result.stdout
    assert 'tests/test_switch_round3.py::' in result.stdout
