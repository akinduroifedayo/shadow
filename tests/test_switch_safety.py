import copy
import sqlite3

import pytest

from shadow.switch import Engine, Proposal, Route, Store, choose_conversation


OLD = Route("fixture", "old", "https://fixture.invalid/v1", "chat_completions")
NEW = Route("fixture", "new", "https://fixture.invalid/v1", "chat_completions")


@pytest.fixture
def engine():
    return Engine(Store(sqlite3.connect(":memory:")), verified_pair=lambda old, new: True)


def start(engine, **kwargs):
    return engine.start(Proposal("test-1", OLD, NEW, "better"), switch_id="switch-1", **kwargs)


def identity(session="conversation", turn="turn-1"):
    return dict(session_id=session, task_id="task-1", turn_id=turn)


def admit(engine, session="conversation", turn="turn-1"):
    engine.session_started(session_id=session, platform="cli")
    engine.admit(**identity(session, turn), model="old", platform="cli", parent_session_id="")


def request(engine, session="conversation", turn="turn-1", **changes):
    event = dict(**identity(session, turn), provider=OLD.provider, base_url=OLD.base_url,
                 api_mode=OLD.api_mode, model="old", api_request_id=f"{turn}:api:1",
                 request={"model": "old", "messages": [{"role": "user", "content": "fixture"}],
                          "extra_body": {"fixture": True}})
    event.update(changes)
    before = copy.deepcopy(event)
    result = engine.rewrite(**event)
    assert event == before
    return result


def test_idle_full_turn_has_no_calls_messages_rows_or_retained_identities(engine):
    admit(engine)
    assert request(engine) is None
    engine.response(**identity(), api_request_id="a", response_model="old", provider=OLD.provider,
                    base_url=OLD.base_url, api_mode=OLD.api_mode)
    engine.request_error(**identity(), api_request_id="b")
    engine.tool_result(**identity(), status="error")
    engine.finish(**identity(), completed=True, failed=False, interrupted=False)
    engine.command(command="retry", session_key="conversation", args_raw="")
    assert engine.record is None
    assert engine.store.row_count() == 0
    assert engine.drain_notices() == []


def test_only_model_changes_for_admitted_main_turn(engine):
    start(engine)
    engine.advance()
    engine.advance()
    admit(engine)
    result = request(engine)
    assert result["request"] == {"model": "new", "messages": [{"role": "user", "content": "fixture"}],
                                 "extra_body": {"fixture": True}}
    assert request(engine)["request"]["model"] == "new"


@pytest.mark.parametrize("changes", [
    {"session_id": "helper"}, {"task_id": "side-question-task"}, {"turn_id": "turn-1:side_question"},
    {"provider": "other"}, {"base_url": "https://other.invalid/v1"},
    {"api_mode": "responses"}, {"model": "fallback"}, {"request": {"model": "fallback"}},
    {"task_id": ""}, {"turn_id": ""}, {"session_id": ""},
])
def test_unadmitted_or_changed_routes_are_untouched(engine, changes):
    start(engine)
    engine.advance()
    engine.advance()
    admit(engine)
    assert request(engine, **changes) is None


@pytest.mark.parametrize("case", ["parent", "subagent", "unknown", "controller", "replay", "registered"])
def test_helpers_controllers_and_unknown_sessions_cannot_gain_admission(engine, case):
    start(engine)
    engine.advance()
    engine.advance()
    if case != "unknown":
        engine.session_started(session_id="conversation", platform="subagent" if case == "subagent" else "cli")
    if case == "registered":
        engine.exclude_session("conversation")
    engine.admit(**identity(), platform="cli", model="old",
                 parent_session_id="parent" if case == "parent" else "",
                 user_message=f"[shadow:{case}]" if case in {"controller", "replay"} else "fixture")
    assert request(engine) is None


@pytest.mark.parametrize("share", [10, 50, 100])
def test_chooser_is_stable_and_within_two_percentage_points(share):
    choices = [choose_conversation(f"conversation-{i}", "switch", share) for i in range(10_000)]
    assert choices == [choose_conversation(f"conversation-{i}", "switch", share) for i in range(10_000)]
    assert abs(sum(choices) / 100 - share) < 2


def test_conversation_assignment_never_changes_as_steps_advance(engine):
    start(engine)
    decisions = {}
    for i in range(100):
        session = f"conversation-{i}"
        admit(engine, session)
        decisions[session] = request(engine, session) is not None
    assignments = dict(engine.record.assignments)
    engine.advance()
    engine.advance()
    for session in decisions:
        admit(engine, session, "turn-2")
        result = request(engine, session, "turn-2")
        assert (result is not None) == decisions[session]
    assert engine.record.assignments == assignments


def test_gradual_requires_exact_pair_compatibility_evidence():
    engine = Engine(Store(sqlite3.connect(":memory:")))
    with pytest.raises(ValueError, match="compatibility"):
        start(engine)
    assert engine.store.row_count() == 0
