import argparse
import copy
import re
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from shadow.switch import Engine, Proposal, Store
from shadow.switch.copy import copy as switch_copy
from shadow.switch.copy import render_notice, short_name, status
from shadow.switch.doctor import doctor
from shadow.switch.domain import SwitchRecord, Turn
from shadow.switch.plugin import SwitchPlugin
from test_switch_plugin import plugin
from test_switch_safety import OLD, NEW
from test_switch_states import CROSS
from test_pair_probe import hermes_agent_context


def record(**values):
    return SwitchRecord("switch", "test", OLD, NEW, "gradual", **values)


def ready_config():
    return {"plugins": {"entries": {"shadow-trial": {"allow_gateway_injection": True,
            "llm": {"allow_provider_override": True, "allow_model_override": True}}}}}


@pytest.mark.parametrize("threshold", [1, 20])
def test_step_one_omits_unsupported_pace_and_preserves_turn_threshold(threshold):
    text = render_notice(record(step_turns=threshold), {"kind": "step1"})
    assert f"After {threshold} of its turns without trouble, it answers half the time. Shadow watches your turns only until this switch ends." in text
    assert "At your pace" not in text
    assert re.search(r"about \d+ days?", text) is None


@pytest.mark.parametrize("surface", ["cli", "chat", "desktop"])
def test_step_one_omits_unsupported_pace_on_every_surface(plugin, capsys, surface):
    ctx, service = plugin
    if surface == "cli":
        setup, handler = ctx.cli["shadow"]
        parser = argparse.ArgumentParser()
        setup(parser)
        handler(parser.parse_args(["switch", "saved"]))
        text = capsys.readouterr().out
    elif surface == "chat":
        text = ctx.commands["shadow"]("switch saved")
    else:
        service.engine.start(Proposal("saved", OLD, NEW, "better"))
        text = service.panel()["notices"][0]["text"]
    text = " ".join(text.split())
    assert "Switching to New a little at a time. Step 1 of 3" in text
    assert "After 20 of its turns without trouble, it answers half the time. Shadow watches your turns only until this switch ends." in text
    assert "At your pace" not in text
    assert re.search(r"about \d+ days?", text) is None


@pytest.mark.parametrize("phase,step,expected", [
    ("running", 3, "Shadow: step 3 of 3, {new} all the time. 20 of 20 to go."),
    ("paused", 1, "Shadow: switch paused after a rollback. You're on {old}."),
    ("watch", 1, "Shadow: watching {new}, 0 of 20 turns, no trouble yet."),
    ("done", 3, "Shadow: nothing running. Last switch: {new}, done Nov 9."),
])
def test_status_matches_design_without_extra_last_answer(phase, step, expected):
    saved = record(phase=phase, step=step, completed_on="2026-11-09", last_response_model="new")
    assert status(saved) == expected.format(new=short_name(NEW.model), old=short_name(OLD.model))


@pytest.mark.parametrize("phase", ["undone", "stopped", "manual", "done"])
def test_pending_restore_status_does_not_say_nothing_is_running(phase):
    saved = record(phase=phase, config_pending="old")
    text = status(saved)
    assert text == switch_copy("switch.status.restore_pending", old=short_name(OLD.model))
    assert "nothing running" not in text


def test_undo_during_a_pending_restore_says_there_is_nothing_else_to_undo(tmp_path):
    writes = []

    def write(route):
        writes.append(route)
        if route == OLD:
            raise RuntimeError("fixture config unavailable")

    engine = Engine(Store(path=tmp_path / "switch.db"), set_default=write)
    engine.start(Proposal("saved", OLD, CROSS, "better"))
    with pytest.raises(RuntimeError, match="fixture config unavailable"):
        engine.undo()
    assert engine.record.phase == "undone" and engine.record.undo and engine.record.config_pending == "old"
    pending = status(engine.record)
    assert pending == switch_copy("switch.status.restore_pending", old=short_name(OLD.model))
    assert "nothing running" not in pending
    service = SwitchPlugin(SimpleNamespace(), engine, config_reader=lambda: {})
    before = engine.record.to_dict()
    expected = switch_copy("switch.undo_pending", old=short_name(OLD.model))
    assert service.command("undo") == expected
    assert service.action("undo") == expected
    assert engine.record.to_dict() == before
    assert writes == [CROSS, OLD, OLD, OLD]


def test_rollback_retry_signal_uses_design_words():
    saved = record(turns={"one": Turn("s", "t", "one", "new", 0, signals=["retry_requested"])})
    text = render_notice(saved, {"kind": "rollback", "reason": "trouble"})
    assert "(1 you retried)" in text


@pytest.mark.parametrize("kind,reason,route", [("watch_start", None, CROSS),
                                              ("watch_trip", "requests", OLD),
                                              ("watch_undo", None, OLD)])
def test_cross_provider_notice_preserves_design_and_adds_unambiguous_command(kind, reason, route):
    saved = SwitchRecord("switch", "test", OLD, CROSS, "all_at_once")
    notice = {"kind": kind}
    if reason:
        notice["reason"] = reason
    text = render_notice(saved, notice)
    assert f"\n/model {route.model} --provider {route.provider}\n" in text + "\n"
    if kind == "watch_start":
        assert "To move this chat now, send /model different." in text


def test_doctor_explains_missing_runtime_registry_without_writing_config():
    config = ready_config()
    before = copy.deepcopy(config)
    text = doctor(config)
    assert "plugins.entries.shadow-trial.settings.verified_gradual_pairs" in text
    assert "completion proof" in text
    assert "fails closed when sealed reasoning is required" in text
    assert config == before


def test_doctor_prints_exact_pair_entry_only_after_runtime_probe():
    config = ready_config()
    before = copy.deepcopy(config)
    text = doctor(config, old=OLD, new=NEW)
    assert "plugins.entries.shadow-trial.settings.verified_gradual_pairs" in text
    assert '"old": ' in text and '"new": ' in text
    assert '"probe_passed": false' in text
    assert '"evidence": ""' in text
    assert "Fixture tests do not authorize" in text
    assert config == before


def test_doctor_reports_exact_pair_proof_instead_of_missing_registry(hermes_agent_context):
    config = ready_config()
    config["plugins"]["entries"]["shadow-trial"]["settings"] = {"verified_gradual_pairs": [
        {"old": asdict(OLD), "new": asdict(NEW), "probe_passed": True, "evidence": "runtime-run-42",
         "reasoning": {"old": None, "new": None}, "sealed_reasoning_observed": False}]}
    text = doctor(config, old=OLD, new=NEW)
    assert "Missing runtime compatibility proof" not in text
    assert "Exact-pair runtime compatibility proof is recorded" in text
