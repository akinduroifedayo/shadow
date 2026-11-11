"""AC10. The all-at-once switch. The fixture watches names; the profile needs a Mac."""

from __future__ import annotations

import sqlite3

from acceptance.support import MAC_REPORT, Check
from shadow.switch import Engine, Proposal, Route, Store

OLD = Route("openai-codex", "gpt-6.1-sol", "https://fixture.invalid/v1", "codex_responses")
NEW = Route("anthropic", "claude-sonnet-4-5", "https://other.invalid/v1", "messages")


def check(mac: bool = False):
    probe = Check(10)
    names = []
    engine = Engine(
        Store(sqlite3.connect(":memory:")),
        verified_pair=lambda old, new: True,
        set_default=lambda route: names.append((route.provider, route.model)),
    )
    before = (OLD.provider, OLD.model)
    engine.start(Proposal("test", OLD, NEW, "better"), switch_id="watch")
    probe.expect(engine.record.mode == "all_at_once", "a different provider did not use the all-at-once switch")
    probe.expect((engine.record.old.provider, engine.record.old.model) == before, "the old names were not saved")
    probe.expect(names == [(NEW.provider, NEW.model)], f"the default did not change to the new names: {names}")
    route = {"provider": NEW.provider, "base_url": NEW.base_url, "api_mode": NEW.api_mode}
    engine.session_started(session_id="chat", platform="cli")
    for index in range(3):
        key = {"session_id": "chat", "task_id": "task", "turn_id": f"t{index}"}
        engine.admit(**key, platform="cli", model=NEW.model)
        engine.rewrite(**key, **route, model=NEW.model, api_request_id=f"api-{index}", request={"model": NEW.model})
        engine.request_error(**key, **route, api_request_id=f"api-{index}", retry_count=0)
        engine.finish(**key, completed=False, failed=True, interrupted=False)
    probe.expect(engine.record.phase == "paused", "staged trouble did not trip the watch")
    probe.expect(names[-1] == before, f"the watch did not restore the saved names: {names}")
    engine.undo()
    probe.expect(engine.record.undo and names[-1] == before, f"undo did not restore the saved names: {names}")
    if probe.failures:
        return probe.finish("fixture watch failed")
    if mac and MAC_REPORT:
        probe.note("mac log printed; Hermes settings were not read")
    return probe.finish(
        f"fixture names before {before[0]}/{before[1]}, then {NEW.provider}/{NEW.model}, "
        f"then restored to {before[0]}/{before[1]} on trip and undo. "
        "shadow-demo settings were not read",
        skip="needs-mac",
    )


def test_ac10_does_not_pass_without_a_mac():
    result = check(False)
    assert result.status != "PASS"
    if result.status == "FAIL":
        raise AssertionError(result.evidence)
    assert result.skip_reason == "needs-mac"
