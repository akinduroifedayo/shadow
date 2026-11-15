"""Runtime modules must import siblings relatively.

Hermes imports the plugin as ``hermes_plugins.shadow_trial``, so a top-level
``shadow`` package does not exist in a real install. Pytest preloads one
(pytest.ini ``-p shadow``), which hides absolute self-imports from every other test.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNTIME = [REPO / '__init__.py', *sorted((REPO / 'shadow').rglob('*.py'))]


def _absolute_self_imports(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        for name in names:
            if name == 'shadow' or name.startswith('shadow.'):
                yield f'{path.relative_to(REPO)}:{node.lineno}: {name}'


def test_runtime_modules_have_no_absolute_shadow_imports():
    assert len(RUNTIME) > 10
    found = [hit for path in RUNTIME for hit in _absolute_self_imports(path)]
    assert found == []
