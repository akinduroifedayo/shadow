import sqlite3

import pytest

from shadow.switch import Counts, Engine, Proposal, Route, Store, choose_conversation
from test_switch_safety import OLD, NEW, admit, identity, request, start
from test_switch_states import CROSS, turn, watch_turn


@pytest.fixture
def engine():
    return Engine(Store(sqlite3.connect(":memory:")), verified_pair=lambda old, new: True,
                  set_default=lambda route: None)


def test_done_and_undone_keep_routing_without_storing_new_turn_observations(engine):
    start(engine, step_turns=1)
    engine.advance()
    engine.advance()
    session = turn(engine, "initial")["session_id"]
    engine.drain_notices()
    before = engine.record.to_dict()
    admit(engine, session, "after-done")
    assert request(engine, session, "after-done")["request"]["model"] == "new"
    engine.response(**identity(session, "after-done"), api_request_id="after-done:api:1", response_model="new",
                    provider=OLD.provider, base_url=OLD.base_url, api_mode=OLD.api_mode)
    engine.finish(**identity(session, "after-done"), completed=True)
    assert engine.record.to_dict() == before
    engine.undo()
    engine.drain_notices()
    before = engine.record.to_dict()
    engine.session_started(session_id=session, platform="cli")
    engine.admit(**identity(session, "after-undo"), platform="cli", model="new")
    assert request(engine, session, "after-undo", model="new", request={"model": "new"})["request"]["model"] == "old"
    assert engine.record.to_dict() == before


@pytest.mark.parametrize("phase", ["done", "undone", "manual", "paused"])
def test_excluding_helpers_after_watching_ends_does_not_store_identities(engine, phase):
    start(engine)
    with engine.store.transaction() as holder:
        holder[0].phase = phase
    before = engine.record.to_dict()
    engine.exclude_session("later-helper")
    assert engine.record.to_dict() == before


@pytest.mark.parametrize("phase", ["done", "undone"])
def test_late_helper_exclusion_disarms_already_admitted_routing(engine, phase):
    start(engine)
    with engine.store.transaction() as holder:
        holder[0].phase = phase
    admit(engine, "later-helper")
    engine.exclude_session("later-helper")
    assert request(engine, "later-helper", model="new" if phase == "undone" else "old",
                   request={"model": "new" if phase == "undone" else "old"}) is None


@pytest.mark.parametrize("phase", ["done", "undone", "paused"])
def test_late_helper_exclusion_disarms_turns_admitted_while_watching(engine, phase):
    start(engine)
    engine.advance()
    engine.advance()
    admit(engine, "later-helper")
    assert request(engine, "later-helper")["request"]["model"] == "new"
    with engine.store.transaction() as holder:
        holder[0].phase = phase
    before = engine.record.to_dict()
    engine.exclude_session("later-helper")
    assert request(engine, "later-helper", api_request_id="next-request") is None
    assert engine.record.to_dict() == before


def test_original_model_mismatch_can_never_rewrite_to_new(engine):
    start(engine)
    engine.advance()
    engine.advance()
    engine.session_started(session_id="conversation", platform="cli")
    engine.admit(**identity(), platform="cli", model="new")
    assert request(engine, model="new", request={"model": "new"}) is None


def test_unrelated_observers_and_fallbacks_never_affect_counts_or_streak(engine):
    start(engine)
    engine.advance()
    engine.advance()
    admit(engine)
    request(engine)
    for changes in ({"provider": "other"}, {"base_url": "https://other.invalid/v1"}, {"api_mode": "responses"},
                    {"api_request_id": "unadmitted"}, {"task_id": "helper-task"}):
        event = dict(**identity(), api_request_id="turn-1:api:1", provider=OLD.provider,
                     base_url=OLD.base_url, api_mode=OLD.api_mode)
        event.update(changes)
        engine.request_error(**event)
        engine.response(**event, response_model="new")
    assert engine.record.failure_streak == 0
    assert engine.record.last_response_model is None


def test_interrupted_and_failed_turns_count_once_and_only_failed_turns_count_request_failures(engine):
    start(engine)
    turn(engine, 1, interrupted=True)
    turn(engine, 2, failed=True)
    assert engine.record.counts("new").trouble == 2
    signals = [t.signals for t in engine.record.turns.values()]
    assert ["stopped_turn"] in signals
    assert ["failed_turn", "failed_requests"] in signals


def test_defaults_failure_is_durable_and_replayed_after_restart(tmp_path):
    db = tmp_path / "switch.db"

    def broken(route):
        raise RuntimeError("fixture failure")

    engine = Engine(Store(sqlite3.connect(db)), set_default=broken)
    with pytest.raises(RuntimeError):
        engine.start(Proposal("cross", OLD, CROSS, "better"))
    assert engine.record.config_pending == "new"
    writes = []
    restarted = Engine(Store(sqlite3.connect(db)), set_default=writes.append)
    restarted.reconcile_config()
    restarted.reconcile_config()
    assert writes == [CROSS]
    assert restarted.record.default_changed


def test_manual_change_then_undo_cannot_override_user_choice(engine):
    start(engine)
    engine.command(command="model", args_raw="choice", session_key="conversation")
    with pytest.raises(ValueError):
        engine.undo()


def test_controller_excluded_before_switch_starts_stays_excluded(engine):
    engine.exclude_session("controller")
    start(engine)
    engine.advance()
    engine.advance()
    admit(engine, "controller")
    assert request(engine, "controller") is None


def test_no_default_completion_until_user_allows_twenty_more_turns(engine):
    start(engine)
    engine.advance()
    engine.advance()
    with pytest.raises(ValueError, match="20"):
        engine.advance()


def test_cross_watch_undo_during_watch_and_after_trip(engine):
    engine.start(Proposal("cross", OLD, CROSS, "holds_up"))
    watch_turn(engine, 1)
    engine.undo()
    assert engine.record.phase == "undone"
    assert not engine.record.default_changed


def test_stopped_request_without_response_counts_as_one_troubled_turn(engine):
    start(engine)
    engine.advance()
    engine.advance()
    admit(engine)
    request(engine)
    engine.finish(**identity(), completed=False, failed=False, interrupted=True)
    assert engine.record.counts("new") == Counts(1, 1)


def test_manual_model_change_disarms_paused_routing(engine):
    start(engine)
    with engine.store.transaction() as holder:
        holder[0].phase = "paused"
    engine.command(command="model", args_raw="choice", session_key="conversation")
    assert engine.record.phase == "manual"
    with pytest.raises(ValueError):
        engine.undo()


@pytest.mark.parametrize("phase", ["done", "undone", "stopped"])
@pytest.mark.parametrize("command_chat", ["retained", "later"])
def test_model_command_after_ending_preserves_phase_undo_and_other_chats(engine, phase, command_chat):
    start(engine, step_turns=1)
    engine.advance()
    engine.advance()
    admit(engine, "retained")
    admit(engine, "other")
    turn(engine, "complete")
    if phase != "done":
        engine.undo(stop=phase == "stopped")
    engine.drain_notices()
    engine.command(command="model", args_raw="choice", session_key=command_chat)
    assert engine.record.phase == phase
    assert engine.record.notices == []
    admit(engine, "other", "next")
    original = "old" if phase == "done" else "new"
    result = request(engine, "other", "next", model=original, request={"model": original})
    assert result["request"]["model"] == ("new" if phase == "done" else "old")
    admit(engine, command_chat, "next")
    assert request(engine, command_chat, "next") is None
    engine.undo()
    assert engine.record.phase in {"undone", "stopped"}


def test_retry_is_only_trouble_when_requested_immediately_after_a_turn(engine):
    start(engine)
    previous = turn(engine, "previous")
    admit(engine, previous["session_id"], "in-progress")
    engine.command(command="retry", session_key=previous["session_id"], args_raw="")
    assert engine.record.counts("new").trouble == 0


def test_cross_provider_same_model_name_uses_the_wire_to_attribute_turns(engine):
    cross = Route(CROSS.provider, OLD.model, CROSS.base_url, CROSS.api_mode)
    engine.start(Proposal("same-name", OLD, cross, "better"))
    admit(engine)
    engine.rewrite(**identity(), provider=cross.provider, base_url=cross.base_url, api_mode=cross.api_mode,
                   model=cross.model, api_request_id="api", request={"model": cross.model})
    engine.response(**identity(), provider=cross.provider, base_url=cross.base_url, api_mode=cross.api_mode,
                    response_model=cross.model, api_request_id="api")
    engine.finish(**identity(), completed=True)
    assert engine.record.counts("new") == Counts(1, 0)
    assert engine.record.counts("old") == Counts()


def test_same_provider_fallback_to_new_model_is_not_rewritten_or_observed(engine):
    start(engine)
    session = next(f"old-{i}" for i in range(1000)
                   if not choose_conversation(f"old-{i}", engine.record.id, 10))
    admit(engine, session)
    assert request(engine, session, model="new", request={"model": "new"}) is None
    engine.response(**identity(session), provider=NEW.provider, base_url=NEW.base_url,
                    api_mode=NEW.api_mode, response_model="new", api_request_id="turn-1:api:1")
    engine.finish(**identity(session), completed=True)
    assert engine.record.counts("new") == Counts()
    assert engine.record.counts("old") == Counts()


def test_preexisting_new_model_chat_does_not_end_gradual_switch(engine):
    start(engine)
    engine.session_started(session_id="conversation", platform="cli")
    engine.admit(**identity(), platform="cli", model="new")
    assert engine.record.phase == "running"
    assert request(engine, model="new", request={"model": "new"}) is None


def test_instructed_cross_provider_chat_move_keeps_watch_and_undo_armed(engine):
    engine.start(Proposal("cross", OLD, CROSS, "better"))
    engine.command(command="model", args_raw=CROSS.model, session_key="watch-chat")
    assert engine.record.phase == "watch"
    watch_turn(engine, 1)
    engine.undo()
    assert engine.record.phase == "undone"
    engine.command(command="model", args_raw=OLD.model, session_key="watch-chat")
    assert engine.record.phase == "undone"


def test_later_manual_model_change_still_ends_cross_provider_watch(engine):
    engine.start(Proposal("cross", OLD, CROSS, "better"))
    engine.command(command="model", args_raw=CROSS.model, session_key="watch-chat")
    watch_turn(engine, 1)
    engine.command(command="model", args_raw=CROSS.model, session_key="watch-chat")
    assert engine.record.phase == "manual"
    with pytest.raises(ValueError):
        engine.undo()


def test_unrelated_model_command_during_cross_provider_watch_ends_it(engine):
    engine.start(Proposal("cross", OLD, CROSS, "better"))
    engine.command(command="model", args_raw="user-choice", session_key="watch-chat")
    assert engine.record.phase == "manual"


def test_cross_provider_restore_instruction_keeps_trip_resumable(engine):
    engine.start(Proposal("cross", OLD, CROSS, "better"))
    for i in range(5):
        watch_turn(engine, i, trouble=True)
    engine.command(command="model", args_raw=OLD.model, session_key="watch-chat")
    assert engine.record.phase == "paused"
    engine.resume()
    engine.command(command="model", args_raw=CROSS.model, session_key="watch-chat")
    assert engine.record.phase == "watch"


@pytest.mark.parametrize("field,value", [("api_mode", "responses"), ("base_url", "https://second.invalid/v1")])
def test_same_provider_incompatible_wire_cannot_bypass_pair_gate(engine, field, value):
    route = dict(provider=NEW.provider, model=NEW.model, base_url=NEW.base_url, api_mode=NEW.api_mode)
    route[field] = value
    with pytest.raises(ValueError):
        engine.start(Proposal("wire-change", OLD, Route(**route), "better"))
    assert engine.record is None


@pytest.mark.parametrize("action", ["advance", "undo"])
def test_inflight_turn_keeps_one_model_across_step_or_undo(engine, action):
    start(engine)
    session = next(f"new-{i}" for i in range(1000)
                   if choose_conversation(f"new-{i}", engine.record.id, 10))
    admit(engine, session)
    assert request(engine, session)["request"]["model"] == NEW.model
    getattr(engine, action)()
    assert request(engine, session, api_request_id="tool-loop")["request"]["model"] == NEW.model
    admit(engine, session, "next-turn")
    result = request(engine, session, "next-turn")
    assert (result is None) == (action == "undo")


def test_gateway_route_mapping_survives_restart(tmp_path):
    path = tmp_path / "switch.db"
    engine = Engine(Store(sqlite3.connect(path)), verified_pair=lambda old, new: True)
    start(engine)
    key = turn(engine, "gateway")
    engine.session_started(session_id=key["session_id"], session_key="telegram:route", platform="telegram")
    restarted = Engine(Store(sqlite3.connect(path)))
    restarted.command(command="retry", session_key="telegram:route")
    assert restarted.record.counts("new") == Counts(1, 1)


def test_manual_change_keeps_partial_config_recovery_without_overriding_choice(tmp_path):
    path = tmp_path / "switch.db"
    saved = {"provider": OLD.provider, "model": OLD.model}

    def partial_write(route):
        saved["provider"] = route.provider
        raise RuntimeError("fixture second write failed")

    engine = Engine(Store(sqlite3.connect(path)), set_default=partial_write)
    with pytest.raises(RuntimeError):
        engine.start(Proposal("cross", OLD, CROSS, "better"))
    engine.command(command="model", args_raw="choice", session_key="conversation")
    saved.update(provider="chosen-provider", model="choice")
    writes = []
    restarted = Engine(Store(sqlite3.connect(path)), set_default=writes.append)
    restarted.reconcile_config()
    assert restarted.record.phase == "manual"
    assert restarted.record.config_pending == "new"
    assert writes == []
    assert saved == {"provider": "chosen-provider", "model": "choice"}
    with pytest.raises(ValueError):
        restarted.undo()
