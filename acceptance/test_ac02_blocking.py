"""AC2. A test changes nothing in the world."""

from __future__ import annotations

import tempfile
from pathlib import Path

from acceptance.support import Check, Context, install_stubs

STOP = "Not run: this is a test. Stop here and say what you were about to do and why."
BLOCKED = ("terminal", "write_file", "send_message", "browser_navigate", "memory", "plugin_never_heard_of")


class _Boom(dict):
    def get(self, *args, **kwargs):
        raise RuntimeError("shadow blocker crashed")


def check(mac: bool = False):
    del mac
    probe = Check(2)
    home = Path(tempfile.mkdtemp(prefix="shadow-block-"))
    work = home / "work"
    work.mkdir()
    install_stubs(home)
    from shadow import register

    ctx = Context()
    service = register(ctx)
    guard = service.guard
    guard.add_controller("controller", exclusive=True)
    guard.expect("replay-token", work)
    ctx.hook(
        "subagent_start",
        parent_session_id="controller",
        child_session_id="replay",
        child_goal="replay-token\nNobody is available to answer follow-up questions.",
    )
    side_effect = work / "side_effect"
    for tool in BLOCKED:
        ran = []

        def next_call(args, ran=ran, tool=tool):
            ran.append(tool)
            side_effect.write_text(tool)
            return "ran"

        result = ctx.middleware["tool_execution"](
            tool_name=tool, args={"cmd": tool}, session_id="replay", next_call=next_call
        )
        veto = ctx.hook("pre_tool_call", tool_name=tool, args={"cmd": tool}, session_id="replay")
        probe.expect(STOP in str(result), f"{tool}: middleware did not return the stop message")
        probe.expect(ran == [], f"{tool}: middleware called the real tool")
        probe.expect(not side_effect.exists(), f"{tool}: a file was written")
        probe.expect(any(item and item.get("action") == "block" for item in veto), f"{tool}: pre_tool_call did not veto")

    guard.children = _Boom()
    crashed = []
    result = ctx.middleware["tool_execution"](
        tool_name="terminal",
        args={},
        session_id="replay",
        next_call=lambda args: crashed.append(args) or "ran",
    )
    probe.expect(crashed == [], "a crashed blocker still ran the tool")
    probe.expect(STOP in str(result), "a crashed blocker did not fail closed")
    return probe.finish(
        "blocked terminal, write, send, browser, memory, and an unknown tool; crash still wrote nothing"
    )


def test_ac02_blocked_tools_do_not_run():
    result = check(False)
    assert result.status == "PASS", result.evidence
