"""AC1. Idle means idle."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from acceptance.support import Check, Context, install_stubs, restore_stubs
from shadow import register


USER_TEXT = "Please summarize the fixture notes from yesterday morning."


def _rows(home: Path) -> int:
    total = 0
    for path in (home / "plugin-data").rglob("*.db"):
        connection = sqlite3.connect(path)
        try:
            for table in ("trial_records", "shadow_switch_records"):
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if exists:
                    total += connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            connection.close()
    return total


def _stored_text(home: Path) -> bool:
    for path in home.rglob("*"):
        if path.is_file() and USER_TEXT in path.read_text(errors="ignore"):
            return True
    return False


def check(mac: bool = False):
    del mac
    probe = Check(1)
    home = Path(tempfile.mkdtemp(prefix="shadow-idle-"))
    install_stubs(home)
    try:
        ctx = Context()
        register(ctx)
        before = _rows(home)
        ctx.hook("on_session_start", session_id="ordinary", platform="cli", session_key="ordinary-key")
        sparse = {"session_id": "ordinary", "platform": "cli", "user_message": USER_TEXT, "parent_session_id": ""}
        try:
            ctx.hook("pre_llm_call", **sparse, messages=[{"role": "user", "content": USER_TEXT}])
        except TypeError as exc:
            probe.expect(
                False,
                "ordinary pre_llm_call with only a session and user message raised "
                f"{exc} (shadow/switch/plugin.py:234, switch line). "
                "A turn that also carried task_id, turn_id, and model was checked next",
            )
        ctx.hook(
            "pre_llm_call",
            session_id="ordinary",
            task_id="task-1",
            turn_id="turn-1",
            model="gpt-6.1-sol",
            platform="cli",
            user_message=USER_TEXT,
            parent_session_id="",
            messages=[{"role": "user", "content": USER_TEXT}],
        )
        rewritten = ctx.middleware["llm_request"](
            session_id="ordinary",
            task_id="task-1",
            turn_id="turn-1",
            platform="cli",
            provider="openai-codex",
            base_url="https://api.openai.com/v1",
            api_mode="codex_responses",
            model="gpt-6.1-sol",
            api_request_id="api-1",
            request={"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": USER_TEXT}]},
        )
        ctx.hook(
            "post_api_request",
            session_id="ordinary",
            task_id="task-1",
            turn_id="turn-1",
            api_request_id="api-1",
            response_model="gpt-6.1-sol",
            provider="openai-codex",
            base_url="https://api.openai.com/v1",
            api_mode="codex_responses",
            usage={"prompt_tokens": 10, "completion_tokens": 10},
        )
        ran = []
        ctx.middleware["tool_execution"](
            tool_name="terminal",
            args={"cmd": "true"},
            session_id="ordinary",
            next_call=lambda args: ran.append(args) or "ok",
        )
        ctx.hook("post_tool_call", session_id="ordinary", tool_name="terminal", status="ok")
        ctx.hook("on_session_end", session_id="ordinary", task_id="task-1", turn_id="turn-1", completed=True, failed=False)
        probe.expect(rewritten is None, "idle llm_request middleware rewrote an ordinary turn")
        probe.expect(ctx.llm_calls == [], "idle turn made a Shadow model call")
        probe.expect(ctx.launches == [], "idle turn launched a Shadow helper")
        probe.expect(ctx.messages == [], "idle turn posted a Shadow message")
        probe.expect(_rows(home) == before, f"idle turn stored rows (before {before}, after {_rows(home)})")
        probe.expect(not _stored_text(home), "idle turn stored the user's message")
        probe.expect(ran == [{"cmd": "true"}], "idle tool middleware swallowed an ordinary tool")
        return probe.finish(
            "ordinary turn: llm_request unchanged, 0 model calls, 0 messages, 0 new trial or switch rows"
        )
    finally:
        restore_stubs()


def test_ac01_idle_means_idle():
    result = check(False)
    assert result.status == "PASS", result.evidence
