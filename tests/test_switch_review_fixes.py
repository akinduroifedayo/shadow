import sqlite3

import pytest

from shadow.switch import Engine, Store
from test_switch_safety import OLD, NEW, admit, identity, request, start
from test_switch_states import turn, CROSS


@pytest.fixture
def engine():
    return Engine(Store(sqlite3.connect(":memory:")), verified_pair=lambda *_: True,
                  set_default=lambda route: None)


@pytest.mark.parametrize("ending", ["done", "undo", "stop"])
def test_ended_switch_only_routes_conversations_open_before_ending(engine, ending):
    start(engine, step_turns=1)
    engine.advance()
    engine.advance()
    admit(engine, "already-open")
    assert request(engine, "already-open")["request"]["model"] == "new"
    if ending == "done":
        turn(engine, "complete")
    else:
        engine.undo(stop=ending == "stop")
    expected_phase = {"done": "done", "undo": "undone", "stop": "stopped"}[ending]
    assert engine.record.phase == expected_phase
    admit(engine, "later-chat")
    assert request(engine, "later-chat") is None
    engine.session_started(session_id="later-new", platform="cli")
    engine.admit(**identity("later-new"), model="new", platform="cli")
    assert request(engine, "later-new", model="new", request={"model": "new"}) is None
    model = "old" if ending == "done" else "new"
    engine.admit(**identity("already-open", "next"), platform="cli", model=model)
    assert request(engine, "already-open", "next", model=model,
                   request={"model": model})["request"]["model"] == ("new" if ending == "done" else "old")


def test_trial_guard_uses_exported_phase_contract(engine):
    from shadow.switch import Proposal, is_active

    assert not is_active(engine.record)
    engine.start(Proposal("cross", OLD, CROSS, "better"))
    assert is_active(engine.record)
    engine.undo()
    assert not is_active(engine.record)


def test_skipping_old_model_sampling_still_rolls_back_trouble(engine):
    start(engine)
    engine.advance()
    engine.advance()
    for i in range(10):
        turn(engine, i, trouble=i < 4)
    assert engine.record.phase == "paused"
    assert engine.record.trips[-1]["reason"] == "trouble"


@pytest.mark.parametrize("model", ["third-model", "new"])
def test_preexisting_model_choices_are_ineligible_not_manual_changes(engine, model):
    start(engine)
    engine.session_started(session_id="custom-chat", platform="cli")
    engine.admit(**identity("custom-chat"), model=model, platform="cli")
    assert engine.record.phase == "running"
    assert request(engine, "custom-chat", model=model, request={"model": model}) is None
    engine.advance()
    engine.advance()
    admit(engine, "eligible-chat")
    assert request(engine, "eligible-chat")["request"]["model"] == "new"


def test_mid_conversation_model_change_still_stops_switch(engine):
    start(engine)
    admit(engine)
    request(engine)
    engine.admit(**identity(turn="next"), model="third-model", platform="cli")
    assert engine.record.phase == "manual"


def test_snapshot_response_models_count_toward_step_progress(engine):
    start(engine, step_turns=2)
    engine.advance()
    engine.advance()
    for n in range(2):
        admit(engine, turn=f"dated-{n}")
        request(engine, turn=f"dated-{n}")
        engine.response(**identity(turn=f"dated-{n}"), provider=NEW.provider, base_url=NEW.base_url,
                        api_mode=NEW.api_mode, api_request_id=f"dated-{n}:api:1",
                        response_model="new-2026-08-01")
        engine.finish(**identity(turn=f"dated-{n}"), completed=True)
    assert engine.record.phase == "done"
    assert engine.record.counts("new").turns == 2


def test_unknown_response_model_keeps_evidence_without_claiming_attribution(engine):
    start(engine)
    engine.advance()
    engine.advance()
    admit(engine)
    request(engine)
    engine.response(**identity(), provider=NEW.provider, base_url=NEW.base_url, api_mode=NEW.api_mode,
                    api_request_id="turn-1:api:1", response_model="unknown-alias")
    assert engine.record.last_response_model == "unknown-alias"
    assert any("unknown-alias" in o for t in engine.record.turns.values() for o in t.observations)


@pytest.mark.parametrize("args", ["", "--session", "--global"])
def test_model_picker_without_choice_does_not_stop_switch(engine, args):
    start(engine)
    engine.command(command="model", args_raw=args, session_key="conversation")
    assert engine.record.phase == "running"


@pytest.mark.parametrize("args", ["different --provider other", "--provider other different --session",
                                  "different --provider other --global"])
def test_provider_qualified_instructed_move_preserves_watch(engine, args):
    from shadow.switch import Proposal

    engine.start(Proposal("cross", OLD, CROSS, "better"))
    engine.command(command="model", args_raw=args, session_key="conversation")
    assert engine.record.phase == "watch"


def test_config_writer_runs_after_intent_commits_and_without_store_lock(engine):
    from shadow.switch import Proposal
    from concurrent.futures import ThreadPoolExecutor

    observed = []
    def unlocked_writer(route):
        assert not engine.store.connection.in_transaction
        def can_read():
            acquired = engine.store.lock.acquire(timeout=0.1)
            if acquired:
                engine.store.lock.release()
            return acquired
        with ThreadPoolExecutor(max_workers=1) as pool:
            observed.append(pool.submit(can_read).result(timeout=0.5))

    engine.set_default = unlocked_writer
    engine.start(Proposal("cross", OLD, CROSS, "better"))
    assert observed == [True]


def test_default_writer_restores_both_names_after_second_key_failure(tmp_path):
    from types import SimpleNamespace
    from shadow.switch.defaults import DefaultWriter

    saved = {"provider": OLD.provider, "default": OLD.model}
    calls = []
    def runner(argv, **kwargs):
        key, value = argv[-2:]
        calls.append((key, value))
        saved[key.split(".")[-1]] = value
        return SimpleNamespace(returncode=1 if len(calls) == 2 else 0)

    writer = DefaultWriter(tmp_path, runner=runner, config_reader=lambda: {"model": dict(saved)})
    with pytest.raises(RuntimeError):
        writer(CROSS)
    assert saved == {"provider": OLD.provider, "default": OLD.model}
    assert len(calls) == 4


def test_saved_routing_identities_are_private_and_survive_restart(tmp_path):
    path = tmp_path / "switch.db"
    connection = sqlite3.connect(path)
    engine = Engine(Store(connection), verified_pair=lambda *_: True)
    start(engine)
    engine.advance()
    engine.advance()
    session = "whatsapp:+15551234567:private-chat"
    engine.session_started(session_id=session, session_key=session, platform="whatsapp")
    engine.admit(**identity(session), model="old", platform="whatsapp")
    request(engine, session)
    serialized = connection.execute("SELECT data FROM shadow_switch_records").fetchone()[0]
    assert session not in serialized
    assert "task-1" not in serialized
    assert "turn-1" not in serialized
    restarted = Engine(Store(sqlite3.connect(path)))
    restarted.admit(**identity(session, "next"), model="old", platform="whatsapp")
    assert request(restarted, session, "next")["request"]["model"] == "new"


def test_panel_exposes_only_switch_summary(engine):
    from shadow.switch.plugin import SwitchPlugin

    start(engine)
    admit(engine)
    service = SwitchPlugin(None, engine, config_reader=lambda: {})
    panel = service.panel()
    assert set(panel) <= {"id", "test_id", "old", "new", "mode", "phase", "step", "step_turns",
                          "counts", "trips", "resumes", "undo", "completed_on", "assignment_unit", "status", "notices", "history"}
    assert panel["old"] == {"provider": OLD.provider, "model": OLD.model}


@pytest.mark.parametrize("ended", [False, True])
def test_compaction_preserves_assignment_and_inflight_model(engine, ended):
    from shadow.switch import choose_conversation

    start(engine, step_turns=1)
    before = next(f"before-{n}" for n in range(1000) if choose_conversation(f"before-{n}", engine.record.id, 10))
    after = next(f"after-{n}" for n in range(1000) if not choose_conversation(f"after-{n}", engine.record.id, 10))
    admit(engine, before)
    assert request(engine, before)["request"]["model"] == "new"
    if ended:
        engine.undo()
    engine.compacted(session_id=after, old_session_id=before)
    assert request(engine, after, api_request_id="continued")["request"]["model"] == "new"
    engine.admit(**identity(after, "next"), model="old" if not ended else "new", platform="cli")
    result = request(engine, after, "next", model="old" if not ended else "new",
                     request={"model": "old" if not ended else "new"})
    assert result["request"]["model"] == ("new" if not ended else "old")


def test_compaction_cannot_enroll_unknown_or_excluded_sessions(engine):
    start(engine)
    engine.advance()
    engine.advance()
    engine.compacted(session_id="unknown-child", old_session_id="unknown-parent")
    engine.admit(**identity("unknown-child"), model="old", platform="cli")
    assert request(engine, "unknown-child") is None
    admit(engine, "helper")
    engine.exclude_session("helper")
    engine.compacted(session_id="helper-child", old_session_id="helper")
    admit(engine, "helper-child")
    assert request(engine, "helper-child") is None


def test_three_final_failed_turns_trip_once_and_recovered_request_resets_streak(engine):
    start(engine)
    engine.advance()
    engine.advance()
    turn(engine, "fail-1", failed=True)
    turn(engine, "fail-2", failed=True)
    assert engine.record.failure_streak == 2
    turn(engine, "recovery")
    assert engine.record.failure_streak == 0
    for i in range(3):
        turn(engine, f"final-{i}", failed=True)
    assert engine.record.phase == "paused"
    assert len(engine.record.trips) == 1
    assert engine.record.trips[-1]["reason"] == "requests"


def test_resume_discards_old_epoch_turn_details_but_retains_counts_in_trip(engine):
    start(engine)
    engine.advance()
    engine.advance()
    for i in range(3):
        turn(engine, i, failed=True)
    trip = engine.record.trips[-1]
    engine.resume()
    assert engine.record.turns == {}
    assert engine.record.trips[-1] == trip
