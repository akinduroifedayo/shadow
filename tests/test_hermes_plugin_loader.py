"""Load the plugin the way Hermes does, under a name other than ``shadow``."""
import importlib
import importlib.util
import shutil
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

# hermes-agent 7f3ac61295. User plugins are scanned with an empty prefix
# (hermes_cli/plugins_discovery.py:210-212). The manifest key is then the
# yaml name, not the directory name (hermes_cli/plugins_manifest.py:564-565).
# The loader imports that key as hermes_plugins.<slug>
# (hermes_cli/plugins_loader.py:36 and :795-804; the first scope keeps the
# bare name, :801-802). Execution is spec_from_file_location plus
# __package__/__path__ (hermes_cli/plugins_loader.py:834-843).
_NS_PARENT = "hermes_plugins"
_USER_PREFIX = ""


def _manifest_module_name(plugin_dir: Path) -> str:
    name = plugin_dir.name
    for line in (plugin_dir / "plugin.yaml").read_text().splitlines():
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("'\"")
            break
    key = f"{_USER_PREFIX}/{plugin_dir.name}" if _USER_PREFIX else name
    slug = key.replace("/", "__").replace("-", "_")
    return f"{_NS_PARENT}.{slug}"


def _load_directory_module(plugin_dir: Path, module_name: str):
    if _NS_PARENT not in sys.modules:
        namespace = ModuleType(_NS_PARENT)
        namespace.__path__ = []
        namespace.__package__ = _NS_PARENT
        sys.modules[_NS_PARENT] = namespace
    init_file = plugin_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        module_name, init_file, submodule_search_locations=[str(plugin_dir)])
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(plugin_dir)]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _matches(prefix: str, name: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _snapshot(prefix: str) -> dict:
    return {name: sys.modules[name] for name in list(sys.modules) if _matches(prefix, name)}


def _drop(prefix: str) -> None:
    for name in list(sys.modules):
        if _matches(prefix, name):
            del sys.modules[name]


class _Context:
    def __init__(self):
        self.commands = {}
        self.cli = {}
        self.hooks = {}
        self.middleware = {}
        self.tools = {}
        self.rpc = {}

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, **kwargs):
        self.cli[name] = (setup_fn, handler_fn)

    def register_hook(self, name, callback, **kwargs):
        self.hooks.setdefault(name, []).append(callback)

    def register_middleware(self, name, callback):
        self.middleware[name] = callback

    def register_tool(self, *, name, handler, **kwargs):
        self.tools[name] = handler

    def register_rpc(self, name, handler):
        self.rpc[name] = handler


def _install_host(monkeypatch, home: Path) -> None:
    names = (
        "hermes_constants", "hermes_cli", "hermes_cli.config", "plugins",
        "plugins.plugin_storage", "gateway", "gateway.session_context",
    )
    modules = {name: ModuleType(name) for name in names}
    modules["hermes_constants"].get_hermes_home = lambda: str(home)
    modules["hermes_cli.config"].read_raw_config_readonly = lambda: {}
    modules["hermes_cli.config"].load_config_readonly = lambda: {}
    modules["plugins.plugin_storage"].plugin_db = lambda *_args: sqlite3.connect(":memory:")
    modules["plugins.plugin_storage"].plugin_data_dir = lambda _name: home
    modules["gateway.session_context"].get_session_env = lambda _key, default="": default
    for name, module in modules.items():
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(modules[parent], child, module)
        monkeypatch.setitem(sys.modules, name, module)


def test_hermes_loader_alias_registers_shadow_command(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    plugin_dir = tmp_path / "hermes_plugins_xyz" / "shadow_alias"
    plugin_dir.mkdir(parents=True)
    shutil.copy2(repo / "plugin.yaml", plugin_dir / "plugin.yaml")
    shutil.copy2(repo / "__init__.py", plugin_dir / "__init__.py")
    shutil.copytree(repo / "shadow", plugin_dir / "shadow", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(repo / "desktop", plugin_dir / "desktop", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    assert (plugin_dir / "__init__.py").is_file()

    module_name = _manifest_module_name(plugin_dir)
    assert module_name == "hermes_plugins.shadow_trial"
    assert plugin_dir.name != "shadow"

    saved_path = list(sys.path)
    saved_shadow = _snapshot("shadow")
    saved_namespace = _snapshot(_NS_PARENT)
    try:
        sys.path[:] = [entry for entry in sys.path if not _exposes_this_plugin(entry, repo)]
        assert all(not _is_repo_root(entry, repo) for entry in sys.path)
        assert str(plugin_dir) not in sys.path and str(plugin_dir.parent) not in sys.path
        _drop("shadow")
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shadow")
        _install_host(monkeypatch, tmp_path)
        module = _load_directory_module(plugin_dir, module_name)
        assert module.__name__ == module_name
        assert "shadow" not in sys.modules
        ctx = _Context()
        module.register(ctx)
        assert callable(ctx.commands["shadow"])
        assert "shadow" in ctx.cli
        desktop = importlib.import_module(f"{module_name}.shadow.desktop_rpc")
        switch_copy = importlib.import_module(f"{module_name}.shadow.switch.copy")
        assert desktop.display_name("gpt-6.1-sol").startswith("GPT-")
        assert switch_copy.short_name("gpt-6.1-sol").startswith("GPT-")
        assert "shadow" not in sys.modules
    finally:
        sys.path[:] = saved_path
        _drop("shadow")
        _drop(_NS_PARENT)
        sys.modules.update(saved_shadow)
        sys.modules.update(saved_namespace)


def _path_entry(entry: str) -> Path:
    return Path.cwd() if entry == "" else Path(entry)


def _is_repo_root(entry: str, repo: Path) -> bool:
    try:
        return _path_entry(entry).resolve() == repo.resolve()
    except OSError:
        return False


def _exposes_this_plugin(entry: str, repo: Path) -> bool:
    """True when this path entry would import the checkout as ``shadow``.

    The checkout directory is itself named ``shadow`` and contains ``__init__.py``,
    so its parent imports the plugin entry. The repo root imports the inner package.
    """
    try:
        resolved = _path_entry(entry).resolve()
        repo_resolved = repo.resolve()
    except OSError:
        return False
    if resolved == repo_resolved:
        return True
    init = resolved / "shadow" / "__init__.py"
    if not init.is_file():
        return False
    try:
        init_resolved = init.resolve()
    except OSError:
        return False
    return init_resolved == repo_resolved / "__init__.py" or repo_resolved in init_resolved.parents
