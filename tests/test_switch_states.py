import random
import sqlite3

import pytest

from shadow.switch import Counts, Engine, Proposal, Route, Store, choose_conversation, rollback_reason
from test_switch_safety import OLD, NEW, admit, identity, request, start


@pytest.fixture
def writes():
    return []


@pytest.fixture
def engine(writes):
    return Engine(Store(sqlite3.connect(":memory:")), verified_pair=lambda old, new: True,
                  set_default=writes.append)


def turn(engine, n, *, side="new", trouble=False, interrupted=False, failed=False):
    if side == "new":
        session = next((f"s-{i}" for i in range(1000)
                        if choose_conversation(f"s-{i}", engine.record.id, 10)), None)
    else:
        session = next((f"s-{i}" for i in range(1000)
                        if not choose_conversation(f"s-{i}", engine.record.id, 50)), None)
    key = identity(session, f"turn-{n}")
    admit(engine, session, key["turn_id"])
    request(engine, session, key["turn_id"])
    model = NEW.model if side == "new" else OLD.model
    route = dict(provider=OLD.provider, base_url=OLD.base_url, api_mode=OLD.api_mode)
    if failed:
        engine.request_error(**key, **route, api_request_id=f"turn-{n}:api:1", retry_count=0)
    else:
        engine.response(**key, **route, api_request_id=f"turn-{n}:api:1", response_model=model)
    if trouble:
        engine.tool_result(**key, status="error", tool_call_id=f"tool-{n}")
    engine.finish(**key, completed=not (failed or interrupted), failed=failed, interrupted=interrupted)
    return key


def test_steps_count_new_model_turns_and_stop_watching_on_done(engine, writes):
    start(engine, step_turns=2)
    for i in range(10):
        turn(engine, f"old-{i}", side="old")
    assert engine.record.step == 1
    for i in range(6):
        turn(engine, i)
        assert engine.record.step == min(3, 1 + (i + 1) // 2)
    assert engine.record.phase == "done"
    assert engine.record.counts("new") == Counts(6, 0)
    assert engine.record.last_response_model == "new"
    assert writes == [NEW]
    record = engine.record
    engine.tool_result(**identity(), status="error")
    assert engine.record.to_dict() == record.to_dict()


def test_observers_deduplicate_turns_and_use_actual_response_model(engine):
    start(engine)
    key = turn(engine, 1, trouble=True)
    engine.tool_result(**key, status="error", tool_call_id="tool-1")
    engine.finish(**key, completed=True, failed=False, interrupted=False)
    engine.command(command="retry", session_key=key["session_id"], args_raw="")
    engine.command(command="undo", session_key=key["session_id"], args_raw="")
    assert engine.record.counts("new") == Counts(1, 1)
    assert engine.record.counts("old") == Counts(0, 0)
    assert engine.record.last_response_model == "new"


def test_request_attempt_failure_does_not_make_recovered_turn_trouble(engine):
    start(engine)
    admit(engine)
    request(engine)
    event = dict(**identity(), provider=OLD.provider, base_url=OLD.base_url, api_mode=OLD.api_mode,
                 api_request_id="turn-1:api:1", retry_count=0)
    engine.request_error(**event)
    engine.response(**{k: v for k, v in event.items() if k != "retry_count"}, response_model=engine.record.turns[next(iter(engine.record.turns))].target)
    engine.finish(**identity(), completed=True, failed=False, interrupted=False)
    assert engine.record.counts("new").trouble + engine.record.counts("old").trouble == 0
    assert engine.record.failure_streak == 0


def test_rate_trip_restores_old_next_turn_and_resume_resets_counts(engine):
    start(engine)
    for i in range(20):
        turn(engine, f"old-{i}", side="old", trouble=i < 2)
    for i in range(10):
        key = turn(engine, f"new-{i}", trouble=i < 5)
    assert engine.record.phase == "paused"
    assert engine.record.trips[-1]["new"] == {"turns": 10, "trouble": 5}
    assert engine.record.trips[-1]["old"] == {"turns": 20, "trouble": 2}
    admit(engine, key["session_id"], "after-trip")
    assert request(engine, key["session_id"], "after-trip") is None
    engine.resume()
    assert engine.record.phase == "running"
    assert engine.record.step == 1
    assert engine.record.resumes == 1
    assert engine.record.counts("new") == Counts()
    assert engine.record.counts("old") == Counts()


@pytest.mark.parametrize("lower_share,share,expected_model", [(0, 10, "new"), (10, 50, "old"), (50, 100, "old")])
def test_gradual_resume_rechooses_existing_conversation_at_ten_percent(engine, lower_share, share, expected_model):
    start(engine)
    engine.advance()
    engine.advance()
    session = next(f"resume-{i}" for i in range(1000)
                   if choose_conversation(f"resume-{i}", engine.record.id, share)
                   and (not lower_share or not choose_conversation(f"resume-{i}", engine.record.id, lower_share)))
    admit(engine, session)
    assert request(engine, session)["request"]["model"] == "new"
    for i in range(3):
        turn(engine, f"failure-{i}", failed=True)
    assert engine.record.phase == "paused"

    engine.resume()
    admit(engine, session, "after-resume")
    result = request(engine, session, "after-resume")
    assert (result["request"]["model"] if result else OLD.model) == expected_model


def test_retried_request_that_recovers_does_not_trip_or_count_as_trouble(engine):
    start(engine)
    engine.advance()
    engine.advance()
    admit(engine)
    request(engine)
    event = dict(**identity(), provider=OLD.provider, base_url=OLD.base_url, api_mode=OLD.api_mode,
                 api_request_id="turn-1:api:1")
    for retry in range(3):
        engine.request_error(**event, retry_count=retry)
        engine.request_error(**event, retry_count=retry)
    assert engine.record.phase == "running"
    assert engine.record.failure_streak == 0
    engine.response(**event, response_model="new")
    engine.finish(**identity(), completed=True)
    assert engine.record.counts("new") == Counts(1, 0)
    assert engine.record.phase == "running"


@pytest.mark.parametrize("step", [1, 2, 3, 4])
def test_undo_each_step_and_after_done(engine, writes, step):
    start(engine, step_turns=1)
    for _ in range(min(step - 1, 2)):
        engine.advance()
    if step == 4:
        turn(engine, "done")
    engine.undo()
    assert engine.record.phase == "undone"
    assert engine.record.undo is True
    assert writes == ([NEW, OLD] if step == 4 else [])
    if step == 4:
        engine.session_started(session_id="open-chat", platform="cli")
        engine.admit(**identity("open-chat"), model="new", platform="cli")
        event = dict(**identity("open-chat"), model="new", provider=NEW.provider, base_url=NEW.base_url,
                     api_mode=NEW.api_mode, api_request_id="a", request={"model": "new"})
        assert engine.rewrite(**event) is None


def test_manual_model_command_preserves_user_choice_and_disarms_all_rewrites(engine):
    start(engine)
    engine.command(command="model", args_raw="chosen", session_key="conversation")
    assert engine.record.phase == "manual"
    admit(engine)
    assert request(engine) is None


def test_rollback_threshold_boundaries_and_no_old_comparison():
    assert rollback_reason(Counts(10, 4), Counts(10, 2), 0) is None
    assert rollback_reason(Counts(10, 4), Counts(10, 1), 0) == "trouble"
    assert rollback_reason(Counts(9, 9), Counts(10, 0), 0) is None
    assert rollback_reason(Counts(20, 3), Counts(10, 0), 0) is None
    assert rollback_reason(Counts(10, 10), Counts(), 0) == "trouble"
    assert rollback_reason(Counts(), Counts(), 3) == "requests"


@pytest.mark.parametrize("new_rate,minimum,maximum", [(0.1, 0, 10), (0.5, 90, 101)])
def test_rollback_simulation(new_rate, minimum, maximum):
    rng = random.Random(20161110)
    trips = 0
    for _ in range(100):
        old_bad = new_bad = 0
        for n in range(1, 21):
            old_bad += sum(rng.random() < .1 for _ in range(10))
            new_bad += rng.random() < new_rate
            if rollback_reason(Counts(n, new_bad), Counts(n * 10, old_bad), 0):
                trips += 1
                break
    assert minimum < trips < maximum if minimum else trips < maximum


def test_restart_keeps_step_pins_and_duplicate_observations(tmp_path, writes):
    path = tmp_path / "switch.db"
    engine = Engine(Store(sqlite3.connect(path)), verified_pair=lambda old, new: True, set_default=writes.append)
    start(engine)
    turn(engine, 1)
    engine.advance()
    before = engine.record.to_dict()
    restarted = Engine(Store(sqlite3.connect(path)), set_default=writes.append)
    assert restarted.record.to_dict() == before
    turn(restarted, 2)
    assert restarted.record.step == 2
    assert restarted.record.counts("new").turns == 2


CROSS = Route("other", "different", "https://other.invalid/v1", "responses")


def watch_turn(engine, i, *, trouble=False, failed=False):
    key = identity("watch-chat", f"watch-{i}")
    engine.session_started(session_id=key["session_id"], platform="cli")
    engine.admit(**key, platform="cli", model=CROSS.model)
    route = dict(provider=CROSS.provider, base_url=CROSS.base_url, api_mode=CROSS.api_mode)
    engine.rewrite(**key, **route, model=CROSS.model, api_request_id=f"api-{i}", request={"model": CROSS.model})
    if failed:
        engine.request_error(**key, **route, api_request_id=f"api-{i}", retry_count=0)
    else:
        engine.response(**key, **route, api_request_id=f"api-{i}", response_model=CROSS.model)
    if trouble:
        engine.tool_result(**key, status="error", tool_call_id=f"tool-{i}")
    engine.finish(**key, completed=not failed, failed=failed, interrupted=False)


def test_cross_provider_watch_and_undo(engine, writes):
    engine.start(Proposal("test-cross", OLD, CROSS, "better"))
    assert writes == [CROSS]
    assert engine.record.old == OLD
    for i in range(20):
        watch_turn(engine, i, trouble=i == 0)
    assert engine.record.phase == "done"
    assert engine.record.counts("new") == Counts(20, 1)
    engine.undo()
    assert writes == [CROSS, OLD]


@pytest.mark.parametrize("failure", [False, True])
def test_cross_provider_watch_trips_restore_saved_names(engine, writes, failure):
    engine.start(Proposal("test-cross", OLD, CROSS, "better"))
    for i in range(3 if failure else 5):
        watch_turn(engine, i, trouble=not failure, failed=failure)
    assert engine.record.phase == "paused"
    assert writes == [CROSS, OLD]
    engine.resume()
    assert writes == [CROSS, OLD, CROSS]
    assert engine.record.counts("new") == Counts()


def test_cross_provider_anyway_is_refused_without_changes(engine, writes):
    with pytest.raises(ValueError):
        engine.start(Proposal("test-cross", OLD, CROSS, "too_close"))
    assert writes == []
    assert engine.store.row_count() == 0
