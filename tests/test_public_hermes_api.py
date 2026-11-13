"""Keep Shadow on public Hermes APIs without exceptions."""

import ast
from pathlib import Path


BANNED_NAMES = {'read_config_doc', 'write_config_key'}
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
                    if any(part.startswith('_') for part in alias.name.split('.')):
                        report(node, f'private import {alias.name}')
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module != '__future__':
            module = node.module or ''
            if banned(module):
                report(node, f'banned module {module}')
            if not (module == 'shadow' or module.startswith('shadow.')):
                if any(part.startswith('_') for part in module.split('.')):
                    report(node, f'private import {module}')
            for alias in node.names:
                if alias.name in BANNED_NAMES:
                    report(node, f'banned name {alias.name}')
                if banned(module + '.' + alias.name):
                    report(node, f'banned module {module}.{alias.name}')
                bindings[alias.asname or alias.name] = module + '.' + alias.name
                if alias.name.startswith('_'):
                    report(node, f'private import {alias.name}')
                if not (module == 'shadow' or module.startswith('shadow.')):
                    external.add(alias.asname or alias.name)

    def imported_root(node):
        if module_reference(node) in external:
            return True
        if isinstance(node, ast.Attribute):
            return node.attr == 'ctx' or imported_root(node.value)
        if isinstance(node, ast.Call):
            if (isinstance(node.func, ast.Name)
                    and node.func.id in {'enumerate', 'zip', 'sorted', 'reversed', 'list', 'tuple', 'iter'}
                    and any(imported_root(argument) for argument in node.args)):
                return True
            return imported_root(node.func)
        if isinstance(node, (ast.Subscript, ast.NamedExpr)):
            return imported_root(node.value)
        if isinstance(node, (ast.Tuple, ast.List)):
            return any(imported_root(element) for element in node.elts)
        return isinstance(node, ast.Name) and (node.id in external or node.id in {'ctx', '__import__'})

    def module_reference(node):
        if isinstance(node, ast.Name):
            return bindings.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return module_reference(node.value) + '.' + node.attr
        return ''

    def private(name):
        return name.startswith('_') and not (name.startswith('__') and name.endswith('__'))

    def target_references(node):
        if isinstance(node, ast.Starred):
            return target_references(node.value)
        if isinstance(node, (ast.Tuple, ast.List)):
            return {reference for element in node.elts for reference in target_references(element)}
        if isinstance(node, (ast.Name, ast.Attribute)):
            return {module_reference(node)}
        return set()

    while True:
        inferred = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
                targets, value = [node.target], node.value
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                targets, value = [node.target], node.iter
            elif isinstance(node, ast.withitem):
                targets, value = [node.optional_vars], node.context_expr
            else:
                continue
            if imported_root(value):
                inferred.update(reference for target in targets for reference in target_references(target))
        if inferred <= external:
            break
        external.update(inferred)

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and banned(node.value):
            report(node, f'banned module reference {node.value}')
        elif isinstance(node, ast.Attribute):
            if module_reference(node) == 'sys.modules':
                report(node, 'banned module registry read')
            if node.attr in BANNED_NAMES:
                report(node, f'banned name {node.attr}')
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
    assert scan('''from __future__ import annotations
from .internal import _helper
from hermes_cli.plugins import get_plugin_manager
import os
class Example:
    def run(self):
        self.pm = get_plugin_manager()
        return self._x, _helper(), os.__name__
''') == []


def test_scanner_bans_backend_reexports_from_any_module():
    for name in BANNED_NAMES:
        assert scan(f'from hermes_cli.config import {name} as public_name')
        assert scan(f'import another_module as config; config.{name}()')


def test_scanner_catches_private_dotted_module_segments():
    for source in [
        'from hermes_cli._parser import build_parser',
        'import hermes_cli._parser as parser',
        'from hermes_cli._internal.parser import build_parser',
    ]:
        assert scan(source), source
    assert scan('import shadow._internal\nfrom shadow._internal import helper') == []


def test_scanner_catches_call_subscript_and_context_roots():
    expressions = [
        "get_plugin_manager()._hooks",
        "pm = get_plugin_manager(); pm._hooks",
        "pm: object = registry.profile; pm._hooks",
        "profiles = registry.profiles; profile = profiles['x']; profile._secret",
        "pm = get_plugin_manager(); alias = pm; getattr(alias, '_hooks')",
        "self.pm = get_plugin_manager(); self.pm._hooks",
        "self.pm = get_plugin_manager(); getattr(self.pm, '_hooks')",
        "self.pm: object = registry.profile; self.pm._hooks",
        "a, b = registry.x, registry.y; a._z",
        "[a, b] = [registry.x, registry.y]; b._z",
        "a, *rest = registry.x, registry.y; rest[0]._z",
        "for p in registry.profiles: p._secret",
        "[p._secret for p in registry.profiles]",
        "for i, p in enumerate(registry.profiles): p._secret",
        "for p, local in zip(registry.profiles, []): p._secret",
        "for local, p in zip([], registry.profiles): p._secret",
        "for p in sorted(registry.profiles): p._secret",
        "for p in reversed(registry.profiles): p._secret",
        "for p in list(registry.profiles): p._secret",
        "for p in tuple(registry.profiles): p._secret",
        "for p in iter(registry.profiles): p._secret",
        "with get_plugin_manager() as pm: pm._hooks",
        "(pm := get_plugin_manager())._hooks",
        "(pm := get_plugin_manager()); pm._hooks",
        "sys.modules['x']._y",
        "sys.modules.get('x')._y",
        "importlib.import_module('agent.model_metadata')._z",
        "import_module('agent.model_metadata')._z",
        "__import__('agent.model_metadata')._z",
        "registry.profiles['x']._secret",
        "ctx._manager",
        "self.ctx._manager",
        "service.ctx._manager._plugins",
        "service.ctx()._manager",
        "getattr(service.ctx, '_manager')",
    ]
    imports = ('import sys\nimport importlib\nfrom importlib import import_module\n'
               'from hermes_cli.plugins import get_plugin_manager\nimport providers as registry\n')
    for expression in expressions:
        assert scan(imports + expression), expression


def test_scanner_bans_module_registry_reads_without_private_attributes():
    for expression in ["sys.modules['x']", "sys.modules.get('x')"]:
        assert scan('import sys\n' + expression)
