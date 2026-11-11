"""Shared result type and throwaway Hermes stubs for the acceptance harness."""

from __future__ import annotations

import sqlite3
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace


MAC = False
MAC_REPORT = ""

FORBIDDEN = (
    Path.home() / ".hermes" / ".env",
    Path.home() / ".hermes" / "auth.json",
    Path.home() / ".codex" / "auth.json",
    Path.home() / ".claude" / ".credentials.json",
)


@dataclass
class Result:
    ac: int
    status: str
    evidence: str
    skip_reason: str = ""

    def label(self) -> str:
        if self.status == "SKIP":
            return f"SKIP({self.skip_reason})"
        return self.status


@dataclass
class Check:
    ac: int
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def expect(self, condition: bool, message: str) -> None:
        if not condition:
            self.failures.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def finish(self, evidence: str, *, skip: str = "") -> Result:
        if self.failures:
            detail = "; ".join(self.failures)
            return Result(self.ac, "FAIL", detail)
        if skip:
            extra = "; ".join(self.notes)
            text = evidence if not extra else f"{evidence}; {extra}"
            return Result(self.ac, "SKIP", text, skip)
        extra = "; ".join(self.notes)
        text = evidence if not extra else f"{evidence}; {extra}"
        return Result(self.ac, "PASS", text)


_STATE = {"home": None, "config": {}}
_STUBBED = (
    "hermes_constants",
    "hermes_cli",
    "hermes_cli.config",
    "plugins",
    "plugins.plugin_storage",
    "gateway",
    "gateway.session_context",
)
_PREVIOUS_MODULES: dict[str, object] = {}


def install_stubs(home: Path) -> None:
    """Point Hermes imports at a throwaway directory. Never reads the user's profile."""
    home = home.resolve()
    if home == Path.home() or Path.home() in home.parents and ".hermes" in home.parts:
        raise RuntimeError("throwaway home must not be the user Hermes profile")
    for forbidden in FORBIDDEN:
        if home == forbidden or forbidden in home.parents:
            raise RuntimeError("throwaway home overlaps a forbidden secret path")
    home.mkdir(parents=True, exist_ok=True)
    (home / "profiles" / "shadow-demo").mkdir(parents=True, exist_ok=True)
    _STATE["home"] = home

    def get_hermes_home() -> str:
        return str(_STATE["home"])

    def read_config(path=None):
        if path is not None:
            resolved = Path(path).resolve()
            root = Path(_STATE["home"]).resolve()
            if resolved != root and root not in resolved.parents:
                raise RuntimeError("refusing to read config outside the throwaway home")
        return _STATE["config"]

    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = get_hermes_home
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.read_config_doc = read_config
    config_mod.load_config_readonly = read_config
    plugins = types.ModuleType("plugins")
    plugins.__path__ = []
    storage = types.ModuleType("plugins.plugin_storage")

    def plugin_db(name, filename="state.db"):
        target = home / "plugin-data" / name / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(target)

    def plugin_data_dir(name):
        target = home / "plugin-data" / name
        target.mkdir(parents=True, exist_ok=True)
        return target

    storage.plugin_db = plugin_db
    storage.plugin_data_dir = plugin_data_dir
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    session_context = types.ModuleType("gateway.session_context")
    session_context.get_session_env = lambda key, default="": default
    replacements = {
        "hermes_constants": constants,
        "hermes_cli": hermes_cli,
        "hermes_cli.config": config_mod,
        "plugins": plugins,
        "plugins.plugin_storage": storage,
        "gateway": gateway,
        "gateway.session_context": session_context,
    }
    for name, module in replacements.items():
        if name not in _PREVIOUS_MODULES:
            _PREVIOUS_MODULES[name] = sys.modules.get(name)
        sys.modules[name] = module


def restore_stubs() -> None:
    """Put the process module table back so later tests do not see the throwaway Hermes."""
    for name in _STUBBED:
        if name not in _PREVIOUS_MODULES:
            continue
        previous = _PREVIOUS_MODULES[name]
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    _PREVIOUS_MODULES.clear()


class Context:
    """In-memory Hermes plugin context. Records calls, messages, and hooks."""

    def __init__(self):
        self.hooks = {}
        self.middleware = {}
        self.tools = {}
        self.commands = {}
        self.cli = {}
        self.rpc = {}
        self.messages = []
        self.llm_calls = []
        self.launches = []
        self.config = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_hook(self, name, callback, **kwargs):
        self.hooks.setdefault(name, []).append(callback)

    def hook(self, name, **kwargs):
        return [callback(**kwargs) for callback in self.hooks.get(name, [])]

    def register_middleware(self, name, callback):
        self.middleware[name] = callback

    def register_tool(self, *, name, handler, **kwargs):
        self.tools[name] = handler

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_rpc(self, name, handler):
        self.rpc[name] = handler

    def emit_to_session(self, session_id, event, payload):
        return True

    def register_cli_command(self, name, help_text, setup_fn, handler_fn=None, **kwargs):
        self.cli[name] = (setup_fn, handler_fn)

    def ask_choice(self, question, choices):
        return None

    def post_message(self, text):
        self.messages.append(text)
        return SimpleNamespace(edit=lambda value: self.messages.__setitem__(-1, value), pending=False, ok=True)

    def dispatch_tool(self, name, args):
        raise AssertionError(f"live tool dispatch is not allowed: {name}")


def print_table(results: list[Result]) -> None:
    print(f"{'AC':<4} {'STATUS':<18} EVIDENCE")
    for result in results:
        evidence = " ".join(result.evidence.split())
        print(f"{result.ac:<4} {result.label():<18} {evidence}")
