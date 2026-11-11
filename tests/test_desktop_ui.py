"""Exercise Desktop control behavior with finite SDK responses and React hook stubs."""
from pathlib import Path
import subprocess


def test_desktop_ui_behaviors():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ['node', '--test', 'desktop/dev/test-ui.mjs'], cwd=root,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
