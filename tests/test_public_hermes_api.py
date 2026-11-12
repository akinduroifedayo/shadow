"""Keep Shadow on public Hermes APIs without exceptions."""

import ast
from pathlib import Path


BANNED_MODULES = {'gateway.run', 'run_agent', 'hermes_cli.config_backend'}


def scan(source, filename='<snippet>'):
    tree = ast.parse(source)
    external = set()
    bindings = {}
    hits = set()

    def report(node, reason):
        hits.add(f'{filename}:{node.lineno}: {reason}')

    def banned(module):
        return any(module == name or module.startswith(name + '.') for name in BANNED_MODULES)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if banned(alias.name):
                    report(node, f'banned module {alias.name}')
                bindings[alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
                if not (alias.name == 'shadow' or alias.name.startswith('shadow.')):
                    external.add(alias.asname or alias.name.split('.')[0])
                    if alias.name.split('.')[0].startswith('_'):
                        report(node, f'private import {alias.name}')
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ''
            if banned(module):
                report(node, f'banned module {module}')
            for alias in node.names:
                if banned(module + '.' + alias.name):
                    report(node, f'banned module {module}.{alias.name}')
                bindings[alias.asname or alias.name] = module + '.' + alias.name
                if alias.name.startswith('_'):
                    report(node, f'private import {alias.name}')
                if not (module == 'shadow' or module.startswith('shadow.')):
                    external.add(alias.asname or alias.name)

    def imported_root(node):
        while isinstance(node, ast.Attribute):
            node = node.value
        return isinstance(node, ast.Name) and node.id in external

    def module_reference(node):
        if isinstance(node, ast.Name):
            return bindings.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return module_reference(node.value) + '.' + node.attr
        return ''

    def private(name):
        return name.startswith('_') and not (name.startswith('__') and name.endswith('__'))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and banned(node.value):
            report(node, f'banned module reference {node.value}')
        elif isinstance(node, ast.Attribute):
            if banned(module_reference(node)):
                report(node, f'banned module reference {module_reference(node)}')
            if private(node.attr) and imported_root(node.value):
                report(node, f'private attribute {node.attr}')
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == 'getattr' and len(node.args) >= 2
              and imported_root(node.args[0]) and isinstance(node.args[1], ast.Constant)
              and isinstance(node.args[1].value, str) and private(node.args[1].value)):
            report(node, f'private getattr {node.args[1].value}')
    return sorted(hits)


def test_shadow_uses_only_public_hermes_apis():
    root = Path(__file__).resolve().parents[1]
    hits = [hit for path in sorted((root / 'shadow').rglob('*.py'))
            for hit in scan(path.read_text(), str(path.relative_to(root)))]
    assert not hits, '\n' + '\n'.join(hits)


def test_scanner_catches_banned_shapes():
    source = '''import sys
import gateway.run
from run_agent import AIAgent
from hermes_cli.config_backend import read_config_doc
from agent import _private
import providers as registry
registry._REGISTRY
getattr(registry, '_ALIASES')
sys.modules.get('gateway.run')
import gateway as gw
gw.run
from shadow.internal import _private
'''
    hits = scan(source)
    assert {int(hit.split(':')[1]) for hit in hits} == {2, 3, 4, 5, 7, 8, 9, 11, 12}


def test_scanner_allows_shadow_private_names_and_dunders():
    assert scan('''from .internal import _helper
import os
class Example:
    def run(self):
        return self._x, _helper(), os.__name__
''') == []
