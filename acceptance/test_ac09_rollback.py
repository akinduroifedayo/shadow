"""AC9. Rollback and undo."""

from __future__ import annotations

import random
import sqlite3

from acceptance.support import Check
from shadow.switch import Counts, Engine, Proposal, Route, Store, choose_conversation, rollback_reason
from shadow.switch.copy import render_notice

OLD = Route("fixture", "gpt-6.1-sol", "https://fixture.invalid/v1", "chat_completions")
NEW = Route("fixture", "gpt-6-luna", "https://fixture.invalid/v1", "chat_completions")
ROUTE = {"provider": OLD.provider, "base_url": OLD.base_url, "api_mode": OLD.api_mode}


def _engine(writes=None):
    sink = writes if writes is not None else []
    return Engine(Store(sqlite3.connect(":memory:")), verified_pair=lambda old, new: True, set_default=sink.append)


def _ids(session, turn):
    return {"session_id": session, "task_id": "task", "turn_id": turn}


def _session(engine, side):
    share = 10 if side == "new" else 50
    want = side == "new"
    for index in range(1000):
        name = f"s-{index}"
        if choose_conversation(name, engine.record.id, share) is want:
            return name
    raise RuntimeError("chooser produced no session")


def _turn(engine, number, *, side="new", trouble=False, failed=False):
    session = _session(engine, side)
    key = _ids(session, f"turn-{number}")
    engine.session_started(session_id=session, platform="cli")
    engine.admit(**key, model=OLD.model, platform="cli")
    engine.rewrite(**key, **ROUTE, model=OLD.model, api_request_id=f"turn-{number}:api", request={"model": OLD.model})
    if failed:
        engine.request_error(**key, **ROUTE, api_request_id=f"turn-{number}:api", retry_count=0)
    else:
        model = NEW.model if side == "new" else OLD.model
        engine.response(**key, **ROUTE, api_request_id=f"turn-{number}:api", response_model=model)
    if trouble:
        engine.tool_result(**key, status="error", tool_call_id=f"tool-{number}")
    engine.finish(**key, completed=not failed, failed=failed, interrupted=False)
    return key


def _simulate(new_rate):
    rng = random.Random(20161110)
    trips = 0
    for _ in range(100):
        old_bad = new_bad = 0
        for n in range(1, 21):
            old_bad += sum(rng.random() < 0.1 for _ in range(10))
            new_bad += rng.random() < new_rate
            if rollback_reason(Counts(n, new_bad), Counts(n * 10, old_bad), 0):
                trips += 1
                break
    return trips


def check(mac: bool = False):
    del mac
    probe = Check(9)
    engine = _engine()
    engine.start(Proposal("test", OLD, NEW, "better"), switch_id="rollback")
    for index in range(20):
        _turn(engine, f"old-{index}", side="old", trouble=index < 2)
    for index in range(10):
        _turn(engine, f"new-{index}", trouble=index < 5)
    probe.expect(engine.record.phase == "paused", "staged trouble did not roll back")
    notice = render_notice(engine.record, engine.record.notices[-1])
    probe.expect("5 of 10" in notice and "2 of 20" in notice, f"rollback notice did not give the counts: {notice}")
    engine.session_started(session_id="after", platform="cli")
    engine.admit(**_ids("after", "next"), model=OLD.model, platform="cli")
    rewritten = engine.rewrite(**_ids("after", "next"), **ROUTE, model=OLD.model, api_request_id="next",
                               request={"model": OLD.model})
    probe.expect(rewritten is None, "the turn after rollback was not left on the old model")
    engine.resume()
    probe.expect(engine.record.phase == "running" and engine.record.step == 1 and engine.record.resumes == 1,
                 "resume did not return to step 1")
    probe.expect(engine.record.counts("new") == Counts() and engine.record.counts("old") == Counts(),
                 "resume did not clear the trouble counts")

    for step in (1, 2, 3, 4):
        writes = []
        sample = _engine(writes)
        sample.start(Proposal("test", OLD, NEW, "better"), switch_id=f"undo-{step}", step_turns=1)
        for _ in range(min(step - 1, 2)):
            sample.advance()
        if step == 4:
            _turn(sample, "done")
        sample.undo()
        probe.expect(sample.record.phase == "undone" and sample.record.undo, f"undo at step {step} did not restore")
        if step == 4:
            probe.expect(writes == [NEW, OLD], f"undo after done did not restore the saved default: {writes}")

    manual = _engine()
    manual.start(Proposal("test", OLD, NEW, "better"), switch_id="manual")
    manual.command(command="model", args_raw="chosen-model", session_key="conversation")
    probe.expect(manual.record.phase == "manual", "a manual /model did not end the switch")
    manual.session_started(session_id="conversation", platform="cli")
    manual.admit(**_ids("conversation", "later"), model=OLD.model, platform="cli")
    probe.expect(
        manual.rewrite(**_ids("conversation", "later"), **ROUTE, model=OLD.model, api_request_id="later",
                       request={"model": OLD.model}) is None,
        "Shadow kept rewriting after a manual /model",
    )

    streak = _engine()
    streak.start(Proposal("test", OLD, NEW, "better"), switch_id="streak")
    streak.advance()
    streak.advance()
    for index in range(3):
        _turn(streak, f"fail-{index}", failed=True)
    probe.expect(streak.record.phase == "paused" and streak.record.trips[-1]["reason"] == "requests",
                 "three failed requests in a row did not trip")
    even = _simulate(0.1)
    worse = _simulate(0.5)
    probe.expect(even < 10, f"equal 10% trouble tripped {even}/100 steps (shadow/switch/domain.py:88)")
    probe.expect(worse > 90, f"50% against 10% tripped only {worse}/100 (shadow/switch/domain.py:88)")
    return probe.finish(
        f"rollback notice gave 5 of 10 and 2 of 20; resume cleared counts; undo at steps 1-3 and after done; "
        f"manual /model stopped rewriting; simulation {even}/100 and {worse}/100"
    )


def test_ac09_rollback_and_undo():
    result = check(False)
    assert result.status == "PASS", result.evidence
