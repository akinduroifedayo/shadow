#!/usr/bin/env python3
"""Check Desktop's preview at the history-export boundary, without Hermes credentials or provider calls.

The control feeds synthetic export lines through the real export wrapper, selection and RPC handler and
expects a consent card with every planted secret removed. Each failure (timeout, nonzero exit, malformed
output) must return the named history reason and queue nothing.
"""
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from shadow.common.copy import load_copy
from shadow.desktop_rpc import DesktopRpc, FixtureSource
from shadow.trial.picking import export_sessions, pick_turns

CANARY = "PREVIEWCANARY"


def redact(text):
    """Stands in for Hermes's force redaction: masks every planted canary."""
    return re.sub(CANARY + r"\w*", "***", text)


def export_line(cwd):
    call = {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"env": {"API_KEY": f"{CANARY}_NESTED"}})}}
    return json.dumps({"id": "s1", "source": "cli", "cwd": cwd, "title": f"{CANARY}_TITLE", "messages": [
        {"id": "m1", "role": "user", "content": f"Please fix the deploy script, my key is {CANARY}_USER"},
        {"id": "m2", "role": "assistant", "content": "Running it.", "tool_calls": [call]},
        {"id": "m3", "role": "tool", "content": f"{CANARY}_TOOL_OUTPUT", "tool_call_id": "c1"},
        {"id": "m4", "role": "assistant", "content": "Deployed. " * 30},
        {"id": "m5", "role": "user", "content": "Now please add a rollback step to the deploy script."},
        {"id": "m6", "role": "assistant", "content": "Added the rollback step. " * 20},
    ]})


class PreviewSource(FixtureSource):
    def __init__(self, failure, cwd):
        super().__init__(ROOT / "desktop/fixtures/empty.json")
        self.failure, self.cwd, self.calls, self.selection = failure, cwd, [], None

    def start_facts(self, model):
        def runner(command, **kwargs):
            self.calls.append((command, kwargs["timeout"]))
            if self.failure == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=export_line(self.cwd))
            if self.failure == "exit":
                raise subprocess.CalledProcessError(1, command, output=export_line(self.cwd))
            stdout = export_line(self.cwd)[:-9] if self.failure == "malformed" else export_line(self.cwd)
            return subprocess.CompletedProcess(command, 0, stdout=stdout + "\n")

        self.selection = pick_turns(export_sessions("fixture", runner=runner, redact=redact), seed=1)
        return super().start_facts(model)

    def act(self, action, params):
        raise AssertionError("a preview must not act or start a trial")


def main():
    table = load_copy()
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory() as cwd:
        for failure in (None, "timeout", "exit", "malformed"):
            source = PreviewSource(failure, cwd)
            start = DesktopRpc(source, table).methods()["start.card"]
            before = json.dumps(source.tests(), sort_keys=True)
            result = start({"model": source.world["start"]["try"]})
            assert source.calls == [(["hermes", "-p", "fixture", "sessions", "export", "-", "--newer-than", "7d"], 60)], source.calls
            assert json.dumps(source.tests(), sort_keys=True) == before
            assert CANARY not in json.dumps(result)
            if failure:
                assert result == {"error": {"message": table["trial.history_unavailable"]}}, result
                print(f"PASS {failure}: named history reason, no consent card, no queued trial")
            else:
                assert "rows" in result and "error" not in result, result
                turns = json.dumps([t.record() for t in source.selection.turns])
                assert source.selection.turns and CANARY not in turns, turns
                print(f"PASS control: consent card, {len(source.selection.turns)} turns picked, planted secrets removed, no trial queued")


if __name__ == "__main__":
    main()
