"""AC8. The gradual switch. Fixture parts run here; a real profile stays on the Mac."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from acceptance.support import MAC_REPORT, Check
from shadow.switch import Engine, Proposal, Route, Store, choose_conversation
from shadow.trial.cards import result_actions

OLD = Route("fixture", "gpt-6.1-sol", "https://fixture.invalid/v1", "chat_completions")
NEW = Route("fixture", "gpt-6-luna", "https://fixture.invalid/v1", "chat_completions")
CROSS = Route("other", "claude-sonnet-4-5", "https://other.invalid/v1", "messages")


def _engine(path=None):
    connection = sqlite3.connect(path or ":memory:")
    return Engine(Store(connection), verified_pair=lambda old, new: True, set_default=lambda route: None)


def _identity(session, turn):
    return {"session_id": session, "task_id": "task", "turn_id": turn}


def _rewrite(engine, session, turn, **changes):
    event = {
        **_identity(session, turn),
        "provider": OLD.provider,
        "base_url": OLD.base_url,
        "api_mode": OLD.api_mode,
        "model": OLD.model,
        "api_request_id": f"{turn}-api",
        "request": {"model": OLD.model},
    }
    event.update(changes)
    return engine.rewrite(**event)


def check(mac: bool = False):
    probe = Check(8)
    switch_id = "switch-share"
    for share in (10, 50, 100):
        portion = sum(choose_conversation(f"id-{i}", switch_id, share) for i in range(10000)) / 10000
        probe.expect(abs(portion - share / 100) <= 0.02, f"chooser share {share}% landed at {portion:.3f}")
    probe.expect(
        choose_conversation("id-7", switch_id, 10) == choose_conversation("id-7", switch_id, 10),
        "the same id changed its answer",
    )

    engine = _engine()
    engine.start(Proposal("test", OLD, NEW, "better"), switch_id="switch-1")
    engine.advance()
    engine.advance()
    engine.session_started(session_id="conversation", platform="cli")
    for turn in ("turn-a", "turn-b", "turn-c"):
        engine.admit(**_identity("conversation", turn), model=OLD.model, platform="cli")
    first = _rewrite(engine, "conversation", "turn-a")
    second = _rewrite(engine, "conversation", "turn-b")
    probe.expect(
        first and second and first["request"]["model"] == second["request"]["model"] == NEW.model,
        "requests in one conversation did not stay on one model",
    )
    helper = _rewrite(engine, "helper", "turn-h", parent_session_id="conversation")
    other = _rewrite(engine, "conversation", "turn-c", provider="other", base_url=CROSS.base_url, api_mode=CROSS.api_mode)
    fallback = _rewrite(engine, "conversation", "turn-c", model="fallback-model", request={"model": "fallback-model"})
    probe.expect(helper is None and other is None and fallback is None, "an excluded request was rewritten")
    engine.admit(
        **_identity("shadow-session", "turn-s"), model=OLD.model, platform="cli", user_message="[shadow:replay] fixture"
    )
    probe.expect(_rewrite(engine, "shadow-session", "turn-s") is None, "a Shadow replay request was rewritten")

    mixed = _engine()
    mixed.start(Proposal("test", OLD, NEW, "holds_up"), switch_id="mixed")
    mixed.advance()
    mixed.advance()
    mixed.session_started(session_id="mix", platform="cli")
    mixed.admit(**_identity("mix", "turn-m"), model=OLD.model, platform="cli")
    route = {"provider": OLD.provider, "base_url": OLD.base_url, "api_mode": OLD.api_mode}
    _rewrite(mixed, "mix", "turn-m", api_request_id="m1")
    mixed.response(**_identity("mix", "turn-m"), **route, api_request_id="m1", response_model=NEW.model)
    _rewrite(mixed, "mix", "turn-m", api_request_id="m2")
    mixed.response(**_identity("mix", "turn-m"), **route, api_request_id="m2", response_model=OLD.model)
    probe.expect(mixed.record.phase == "paused", "a turn that mixed models continued (shadow/switch/engine.py:335)")

    cross = Proposal("test", OLD, CROSS, "better")
    probe.expect(cross.gradual is False and cross.allowed, "a different provider was not limited to all-at-once")
    mismatched = Proposal("test", OLD, Route(OLD.provider, "gpt-4.1", OLD.base_url, "responses"), "better")
    probe.expect(not mismatched.allowed, "a same-provider pair with a different request format was offered")
    offered = _engine()
    offered.start(cross, switch_id="cross")
    probe.expect(offered.record.mode == "all_at_once", "a different provider started a gradual switch")
    actions = result_actions({
        "intent": "switch",
        "current": {"provider": OLD.provider, "model": OLD.model},
        "trial": {"provider": CROSS.provider, "model": CROSS.model},
        "routes": {
            "current": {"provider": OLD.provider, "model": OLD.model, "base_url": OLD.base_url, "api_mode": OLD.api_mode},
            "trial": {"provider": CROSS.provider, "model": CROSS.model, "base_url": CROSS.base_url, "api_mode": CROSS.api_mode},
        },
        "totals": {
            "word": "better", "lost": 1, "second_look": False,
            "receipt": {"saved_pct": 10, "speed_new_s": 1.0, "speed_yours_s": 2.0},
        },
    })
    labels = [item["label"] for item in actions]
    probe.expect("Switch all at once" in labels and "Switch gradually" not in labels, f"verdict offered {labels}")

    file_path = Path(tempfile.mkdtemp(prefix="shadow-switch-")) / "switch.db"
    durable = _engine(file_path)
    durable.start(Proposal("test", OLD, NEW, "better"), switch_id="durable")
    restarted = _engine(file_path)
    probe.expect(
        restarted.record is not None and restarted.record.id == "durable" and restarted.record.step == 1,
        "the switch record did not survive a restart",
    )
    if probe.failures:
        return probe.finish("fixture portion failed")
    if mac and MAC_REPORT:
        probe.note("mac log printed; live response_model samples were not collected")
    return probe.finish(
        "chooser within 2 points of 10/50/100 and stable; exclusions hold; mixed models pause; "
        "cross-provider is all-at-once only; record reloaded after restart. "
        "Live post_api_request.response_model samples on shadow-demo were not run",
        skip="needs-mac",
    )


def test_ac08_does_not_pass_without_a_mac():
    result = check(False)
    assert result.status != "PASS"
    if result.status == "FAIL":
        raise AssertionError(result.evidence)
    assert result.skip_reason == "needs-mac"
