"""AC12. The setup check."""

from __future__ import annotations

import sqlite3
import tempfile
from copy import deepcopy
from pathlib import Path

from acceptance.support import Check, Context, _STATE, install_stubs, restore_stubs
from shadow import register

SETTINGS = (
    "plugins.entries.shadow-trial.llm.allow_provider_override",
    "plugins.entries.shadow-trial.llm.allow_model_override",
)


def _config(missing=None):
    shadow = {
        "allow_gateway_injection": True,
        "llm": {"allow_provider_override": True, "allow_model_override": True},
        "settings": {"models": [
            {"provider": "openai-codex", "model": "gpt-6.1-sol", "plan": True, "reachable": True},
            {"provider": "openai-codex", "model": "gpt-6-luna", "plan": True, "reachable": True},
            {"provider": "anthropic", "model": "claude-sonnet-4-5", "reachable": True},
        ]},
    }
    if missing == "plugins.entries.shadow-trial.allow_gateway_injection":
        shadow["allow_gateway_injection"] = False
    elif missing == "plugins.entries.shadow-trial.llm.allow_provider_override":
        shadow["llm"]["allow_provider_override"] = False
    elif missing == "plugins.entries.shadow-trial.llm.allow_model_override":
        shadow["llm"]["allow_model_override"] = False
    return {
        "model": {"provider": "openai-codex", "default": "gpt-6.1-sol"},
        "plugins": {"entries": {"shadow-trial": shadow}},
    }


def check(mac: bool = False):
    del mac
    probe = Check(12)
    home = Path(tempfile.mkdtemp(prefix="shadow-doctor-"))
    install_stubs(home)
    try:
        return _doctor(probe, home)
    finally:
        restore_stubs()


def _doctor(probe, home):
    ctx = Context()
    register(ctx)
    for setting in SETTINGS:
        _STATE["config"] = _config(setting)
        before = deepcopy(_STATE["config"])
        text = ctx.commands["shadow"]("doctor")
        probe.expect(setting in text, f"doctor did not name {setting}")
        fix = "hermes plugins enable shadow-trial"
        probe.expect(fix in text, f"doctor did not give the fix line for {setting}")
        for other in SETTINGS:
            if other != setting:
                label = "llm." + other.rsplit("allow_", 1)[1]
                probe.expect(f"{label}: granted" in text, f"doctor did not report granted {other}")
        probe.expect(_STATE["config"] == before, f"doctor changed the config while reporting {setting}")
        database = home / "plugin-data" / "shadow" / "state.db"
        rows = 0
        if database.exists():
            connection = sqlite3.connect(database)
            try:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trial_records'"
                ).fetchone()
                if exists:
                    rows = connection.execute("SELECT COUNT(*) FROM trial_records").fetchone()[0]
            finally:
                connection.close()
        probe.expect(rows == 0, f"doctor stored {rows} trial rows while reporting {setting}")
    return probe.finish("each missing permission was named with its exact consent or config command; config and stored rows were unchanged")


def test_ac12_doctor_names_the_missing_setting():
    result = check(False)
    assert result.status == "PASS", result.evidence
