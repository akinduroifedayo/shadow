import argparse
import copy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from shadow.switch import Engine, Proposal, Store, register
from shadow.switch.copy import render_notice, short_name, status
from shadow.switch.defaults import DefaultWriter
from shadow.switch.doctor import REQUIRED_SETTINGS, doctor, inspect_setup
from shadow.switch.plugin import SwitchPlugin, verified_pair_from_config
from test_switch_safety import OLD, NEW, admit, identity, request, start
from test_switch_states import CROSS, turn, watch_turn
from test_pair_probe import hermes_agent_context


@pytest.fixture(autouse=True)
def isolated_runner_environment(monkeypatch):
    monkeypatch.setattr("shadow.switch.defaults.os", SimpleNamespace(environ={}))


def config():
    return {"plugins": {"entries": {"shadow": {"allow_gateway_injection": True, "llm": {
        "allow_provider_override": True, "allow_model_override": True}}}}}


@pytest.mark.parametrize("setting", REQUIRED_SETTINGS)
@pytest.mark.parametrize("value", [None, False, "true", 1])
def test_doctor_names_each_missing_setting_and_exact_fix_without_changes(setting, value):
    cfg = config()
    parent = cfg
    parts = setting.split(".")
    for part in parts[:-1]:
        parent = parent[part]
    if value is None:
        parent.pop(parts[-1])
    else:
        parent[parts[-1]] = value
    if value in ("true", 1):
        assert inspect_setup(cfg, other_model=True, judge=True) == []
        return
    before = copy.deepcopy(cfg)
    missing = inspect_setup(cfg, other_model=True, judge=True)
    assert len(missing) == 1
    assert missing[0].setting == setting
    text = doctor(cfg)
    assert setting in text
    fix = "hermes plugins enable shadow"
    assert fix in text
    assert "Nothing changed." in text
    assert cfg == before


def test_doctor_checks_other_model_and_judge_separately():
    text = doctor(config())
    assert "at least one other model to try" in text
    assert "a model that can act as judge" in text
    assert inspect_setup(config(), other_model=True, judge=True) == []


def test_defaults_writer_is_profile_scoped_and_uses_only_model_names(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    writer = DefaultWriter(tmp_path / "profiles" / "demo", runner=runner)
    writer(CROSS)
    assert [call[0] for call in calls] == [
        [sys.executable, "-m", "hermes_cli.main", "-p", "demo", "config", "set", "model.provider", "other"],
        [sys.executable, "-m", "hermes_cli.main", "-p", "demo", "config", "set", "model.default", "different"],
    ]
    for _, kwargs in calls:
        assert kwargs["env"]["HERMES_HOME"] == str(writer.profile_home)
        assert kwargs["timeout"] == 30
        assert kwargs["stdout"] == kwargs["stderr"] == -3


def test_defaults_writer_pins_an_isolated_root_to_default(tmp_path):
    calls = []
    writer = DefaultWriter(tmp_path / "isolated", runner=lambda argv, **kw: (
        calls.append(argv) or SimpleNamespace(returncode=0)))
    writer(NEW)
    assert all(argv[3:5] == ["-p", "default"] for argv in calls)


def test_defaults_writer_reports_no_subprocess_output_on_failure(tmp_path):
    writer = DefaultWriter(tmp_path, runner=lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="model defaults were not saved"):
        writer(NEW)


def test_config_timeout_remains_a_recoverable_pending_switch(plugin):
    _, service = plugin

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    service.engine.set_default = DefaultWriter(Path("/tmp/shadow-fixture-profile"), runner=timeout)
    service.proposal_lookup = lambda test_id: Proposal("cross", OLD, CROSS, "better")
    assert "pending change" in service.command("switch cross")
    assert service.engine.record.config_pending == "new"


def test_exact_pair_proof_checks_both_routes_and_evidence(hermes_agent_context):
    cfg = config()
    proof = {"old": asdict(OLD), "new": asdict(NEW), "probe_passed": True, "evidence": "fixture-probe",
             "reasoning": {"old": None, "new": None}, "sealed_reasoning_observed": False}
    cfg["plugins"]["entries"]["shadow"]["settings"] = {"verified_gradual_pairs": [proof]}
    assert verified_pair_from_config(lambda: cfg, OLD, NEW)
    assert not verified_pair_from_config(lambda: cfg, NEW, OLD)
    proof["new"]["api_mode"] = "responses"
    assert not verified_pair_from_config(lambda: cfg, OLD, NEW)


class Context:
    def __init__(self):
        self.hooks = {}
        self.middleware = {}
        self.commands = {}
        self.cli = {}
        self.messages = []

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_middleware(self, name, handler):
        self.middleware[name] = handler

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup, handler):
        self.cli[name] = setup, handler

    def post_message(self, text):
        self.messages.append(text)
        return SimpleNamespace(ok=True)


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    modules = {name: ModuleType(name) for name in ("hermes_constants", "hermes_cli", "hermes_cli.config",
                                                  "plugins", "plugins.plugin_storage", "gateway", "gateway.session_context")}
    session_env = {}
    modules["gateway.session_context"].get_session_env = lambda key, default="": session_env.get(key, default)
    modules["hermes_constants"].get_hermes_home = lambda: tmp_path
    modules["hermes_cli.config"].read_raw_config_readonly = lambda: config()
    modules["plugins.plugin_storage"].plugin_db = lambda *args: sqlite3.connect(":memory:")
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    ctx = Context()
    ctx.session_env = session_env
    service = register(ctx)
    service.engine.verified_pair = lambda old, new: True
    service.engine.set_default = lambda route: None
    service.proposal_lookup = lambda test_id: Proposal(test_id or "test", OLD, NEW, "better")
    from shadow.common.discovery import ModelCatalog
    from shadow.common.models import Model
    models = [Model(route.provider, route.model) for route in (OLD, NEW, CROSS)]
    service.catalog_reader = lambda: ModelCatalog.from_models(models[0], models)
    service.availability = lambda: (True, True)
    return ctx, service


def test_registered_hooks_remain_idle_across_a_full_turn(plugin):
    ctx, service = plugin
    ctx.hooks["on_session_start"](session_id="conversation", platform="cli")
    ctx.hooks["pre_llm_call"](**identity(), model="old", platform="cli")
    assert request(service.engine) is None
    ctx.hooks["post_api_request"](**identity(), response_model="old", api_request_id="a")
    ctx.hooks["post_tool_call"](**identity(), status="error")
    ctx.hooks["api_request_error"](**identity(), api_request_id="a")
    ctx.hooks["on_session_end"](**identity(), completed=True, failed=False, interrupted=False)
    ctx.hooks["pre_command"](command="retry", session_key="conversation", args_raw="")
    assert service.engine.store.row_count() == 0
    assert service.engine.sessions == {}
    assert ctx.messages == []


def test_cli_and_slash_doctor_use_same_read_only_checks(plugin, capsys):
    ctx, service = plugin
    slash = ctx.commands["shadow"]("doctor")
    setup, handler = ctx.cli["shadow"]
    parser = argparse.ArgumentParser()
    setup(parser)
    handler(parser.parse_args(["doctor"]))
    assert capsys.readouterr().out.strip() == slash
    assert service.engine.store.row_count() == 0
    assert ctx.messages == []


def test_command_start_status_and_undo_go_through_registered_router(plugin):
    ctx, service = plugin
    text = ctx.commands["shadow"]("switch test-1")
    assert "Step 1 of 3" in text
    assert "new conversations" in text
    assert "Progress counts turns" in text
    assert "Shadow watches your turns only until this switch ends." in text
    assert "observed tool failures" in text
    assert "step 1 of 3" in ctx.commands["shadow"]("status")
    assert "switch is over" in ctx.commands["shadow"]("undo")
    assert service.engine.record.phase == "undone"


def test_shared_router_can_register_trial_handlers_without_overwriting_switch(plugin):
    ctx, _ = plugin
    ctx.shadow_command_routes["try"] = lambda raw_args, **context: "fixture-trial"
    assert ctx.commands["shadow"]("try new") == "fixture-trial"
    assert "Models Hermes exposes with usable credentials:" in ctx.commands["shadow"]("doctor")


def test_notices_are_transition_only_and_status_uses_actual_model(plugin):
    ctx, service = plugin
    ctx.commands["shadow"]("switch test-1")
    key = turn(service.engine, 1)
    assert ctx.messages == []
    assert f"Last: {short_name(NEW.model)}." in ctx.commands["shadow"]("status")
    ctx.hooks["pre_command"](command="retry", session_key=key["session_id"], args_raw="")
    assert ctx.messages == []
    service.engine.advance()
    ctx.hooks["on_session_start"](session_id="another", platform="desktop")
    assert ctx.messages == []
    assert "Step 2 of 3" in ctx.commands["shadow"]("status")


def test_terminal_notices_wrap_to_72_columns_and_use_copy_table(plugin):
    ctx, service = plugin
    service.engine.start(Proposal("test", OLD, NEW, "better"))
    notices = service.engine.drain_notices()
    for notice in notices:
        text = render_notice(service.engine.record, notice, terminal=True)
        assert all(len(line) <= 72 for line in text.splitlines())
    table = json.loads((Path(__file__).parents[1] / "shadow/copy/switch.json").read_text())
    assert all(key.startswith("switch.") for key in table)


def test_all_at_once_copy_includes_current_chat_command_and_restoration(plugin):
    _, service = plugin
    service.engine.start(Proposal("cross", OLD, CROSS, "better"))
    text = service.notice_text()
    assert "/model different" in text
    assert "next 20 turns" in text
    assert "not a small drop" in text
    for i in range(5):
        watch_turn(service.engine, i, trouble=True)
    text = service.notice_text()
    assert "5 hit trouble" in text
    assert "observed tool failures" in text
    assert "/model old" in text


def test_doctor_never_reconciles_pending_writes(plugin):
    _, service = plugin
    calls = []
    service.engine.set_default = lambda route: calls.append(route)
    with service.engine.store.transaction(create=True) as holder:
        from shadow.switch.domain import SwitchRecord
        holder[0] = SwitchRecord("s", "t", OLD, CROSS, "all_at_once", config_pending="new")
    before = service.engine.record.to_dict()
    assert "Nothing changed." in service.command("doctor")
    assert calls == []
    assert service.engine.record.to_dict() == before


def test_refused_transition_notice_is_kept_until_status_can_show_it(plugin):
    ctx, service = plugin
    ctx.commands["shadow"]("switch test-1")
    service.engine.advance()
    ctx.post_message = lambda text: SimpleNamespace(ok=False, pending=False, error="no_conversation")
    ctx.hooks["on_session_start"](session_id="another", platform="desktop")
    assert service.engine.record.notices
    text = ctx.commands["shadow"]("status")
    assert "Step 2 of 3" in text
    assert service.engine.record.notices == []
    assert "Step 2 of 3" not in ctx.commands["shadow"]("status")


def test_classic_cli_transition_notice_prints_without_a_gateway(plugin, capsys):
    ctx, service = plugin
    ctx.commands["shadow"]("switch test-1")
    service.engine.advance()
    ctx.post_message = lambda text: SimpleNamespace(ok=False, pending=False, error="gateway_unavailable")
    ctx.hooks["on_session_start"](session_id="another", platform="cli")
    assert capsys.readouterr().out == ""
    ctx.hooks["on_session_end"](**identity("another"), platform="cli", completed=True)
    text = capsys.readouterr().out
    assert "Step 2 of 3" in text
    assert all(len(line) <= 72 for line in text.splitlines())
    assert service.engine.record.notices == []


def test_pending_gateway_delivery_does_not_duplicate_or_drop_later_notices(plugin):
    ctx, service = plugin
    ctx.commands["shadow"]("switch test-1")
    ctx.session_env.update(HERMES_SESSION_ID="another", HERMES_SESSION_KEY="telegram:another")
    service.engine.advance()
    handle = SimpleNamespace(ok=False, pending=True)
    posts = []
    ctx.post_message = lambda text: posts.append(text) or handle
    ctx.hooks["on_session_start"](session_id="another", session_key="telegram:another", platform="telegram")
    service.engine.advance()
    ctx.hooks["on_session_start"](session_id="another", session_key="telegram:another", platform="telegram")
    assert len(posts) == 1
    assert len(service.engine.record.notices) == 2
    handle.ok = True
    handle.pending = False
    text = ctx.commands["shadow"]("status")
    assert "Step 2 of 3" not in text
    assert "Step 3 of 3" in text
    assert service.engine.record.notices == []


def test_bare_shadow_shows_help_and_runs_read_only_setup_check(plugin):
    ctx, service = plugin
    service.config_reader = lambda: {}
    text = ctx.commands["shadow"]("")
    assert "/shadow status" in text
    assert "hermes plugins enable shadow" in text
    assert "allow_gateway_injection" not in text
    assert "Nothing changed." in text
    assert service.engine.record is None


def test_stop_action_uses_stop_copy_and_leaves_switch_inactive(plugin):
    ctx, service = plugin
    ctx.commands["shadow"]("switch test-1")
    text = service.action("stop")
    assert "Switch stopped." in text
    assert "The test result is kept." in text
    assert not service.engine.record.watching


def test_switch_command_binds_an_existing_chat_without_another_start_hook(plugin):
    ctx, service = plugin
    ctx.hooks["on_session_start"](session_id="conversation", platform="cli")
    ctx.commands["shadow"]("switch test-1", session_id="conversation", session_key="conversation", platform="cli")
    service.engine.advance()
    service.engine.advance()
    ctx.hooks["pre_llm_call"](**identity(), platform="cli", model="old")
    assert request(service.engine)["request"]["model"] == "new"


@pytest.mark.parametrize("command", ["retry", "undo"])
def test_gateway_command_route_maps_to_its_actual_turn_session(plugin, command):
    ctx, service = plugin
    session = "conversation"
    ctx.commands["shadow"]("switch test-1", session_id=session, session_key="telegram:route", platform="telegram")
    service.engine.advance()
    service.engine.advance()
    ctx.hooks["pre_llm_call"](**identity(), platform="telegram", model="old")
    request(service.engine)
    ctx.hooks["post_api_request"](**identity(), api_request_id="turn-1:api:1", response_model="new",
                                  provider=NEW.provider, base_url=NEW.base_url, api_mode=NEW.api_mode)
    ctx.hooks["on_session_end"](**identity(), completed=True)
    ctx.hooks["pre_command"](command=command, session_key="telegram:route", platform="telegram")
    assert service.engine.record.counts("new").trouble == 1


def test_status_can_bind_an_existing_chat_after_a_terminal_started_switch(plugin):
    ctx, service = plugin
    ctx.commands["shadow"]("switch test-1")
    ctx.commands["shadow"]("status", session_id="conversation", session_key="conversation", platform="cli")
    service.engine.advance()
    service.engine.advance()
    ctx.hooks["pre_llm_call"](**identity(), platform="cli", model="old")
    assert request(service.engine)["request"]["model"] == "new"


def test_bound_turn_context_enrolls_an_existing_gateway_chat_and_maps_retry(plugin):
    ctx, service = plugin
    ctx.commands["shadow"]("switch test-1")
    service.engine.advance()
    service.engine.advance()
    ctx.session_env.update(HERMES_SESSION_ID="conversation", HERMES_SESSION_KEY="telegram:route")
    ctx.hooks["pre_llm_call"](**identity(), platform="telegram", model="old")
    assert request(service.engine)["request"]["model"] == "new"
    ctx.hooks["post_api_request"](**identity(), api_request_id="turn-1:api:1", response_model="new",
                                  provider=NEW.provider, base_url=NEW.base_url, api_mode=NEW.api_mode)
    ctx.hooks["on_session_end"](**identity(), completed=True)
    ctx.hooks["pre_command"](command="retry", session_key="telegram:route", platform="telegram")
    assert service.engine.record.counts("new").trouble == 1


@pytest.mark.parametrize("changes", [{"parent_session_id": "parent"}, {"user_message": "[shadow:controller]"},
                                    {"user_message": "[shadow:replay]"}, {"platform": "subagent"}])
def test_bound_session_context_cannot_admit_helpers_or_shadow_runs(plugin, changes):
    ctx, service = plugin
    ctx.commands["shadow"]("switch test-1")
    service.engine.advance()
    service.engine.advance()
    ctx.session_env.update(HERMES_SESSION_ID="conversation", HERMES_SESSION_KEY="telegram:route")
    event = dict(**identity(), platform="telegram", model="old")
    event.update(changes)
    ctx.hooks["pre_llm_call"](**event)
    assert request(service.engine) is None


def test_mismatched_bound_session_cannot_enroll_an_unknown_chat(plugin):
    ctx, service = plugin
    ctx.commands["shadow"]("switch test-1")
    service.engine.advance()
    service.engine.advance()
    ctx.session_env.update(HERMES_SESSION_ID="different-conversation", HERMES_SESSION_KEY="telegram:route")
    ctx.hooks["pre_llm_call"](**identity(), platform="telegram", model="old")
    assert request(service.engine) is None


def test_manual_change_reports_retained_partial_default_write_and_status_never_retries(plugin):
    ctx, service = plugin

    def partial_write(route):
        raise RuntimeError("fixture unfinished config write")

    service.engine.set_default = partial_write
    service.proposal_lookup = lambda _: Proposal("cross", OLD, CROSS, "better")
    service.command("switch cross")
    service.engine.session_started(session_id="conversation", session_key="telegram:conversation", platform="telegram")
    ctx.session_env.update(HERMES_SESSION_ID="conversation", HERMES_SESSION_KEY="telegram:conversation")
    ctx.hooks["pre_command"](command="model", args_raw="choice", session_key="telegram:conversation", platform="telegram")
    assert any("hermes model" in message and "other" in message and "different" in message
               for message in ctx.messages)
    assert all("Your default model is now" not in message for message in ctx.messages)
    writes = []
    service.engine.set_default = writes.append
    text = service.command("status")
    assert "hermes model" in text
    assert writes == []


def test_cross_provider_mixed_response_models_restore_defaults_and_render_notice(plugin):
    ctx, service = plugin
    writes = []
    service.engine.set_default = writes.append
    service.engine.start(Proposal("cross", OLD, CROSS, "better"))
    service.notice_text()
    ctx.session_env.update(HERMES_SESSION_ID="conversation", HERMES_SESSION_KEY="telegram:conversation")
    ctx.hooks["on_session_start"](session_id="conversation", session_key="telegram:conversation", platform="telegram")
    ctx.hooks["pre_llm_call"](**identity(), platform="telegram", model=CROSS.model)
    route = dict(provider=CROSS.provider, base_url=CROSS.base_url, api_mode=CROSS.api_mode)
    for request_id, response_model in (("first", CROSS.model), ("second", OLD.model)):
        service.engine.rewrite(**identity(), **route, api_request_id=request_id, model=CROSS.model,
                               request={"model": CROSS.model})
        ctx.hooks["post_api_request"](**identity(), **route, api_request_id=request_id,
                                      response_model=response_model)
    assert service.engine.record.phase == "paused"
    assert writes == [CROSS, OLD]
    assert "different models within one turn" in ctx.messages[-1]
    assert "/model old" in ctx.messages[-1]


def test_notices_do_not_post_from_unbound_or_unrelated_hooks(plugin):
    ctx, service = plugin
    service.command("switch test", session_id="owner", session_key="telegram:owner", platform="telegram")
    service.engine.advance()
    ctx.hooks["post_tool_call"](**identity("stranger"), platform="telegram", status="error")
    assert ctx.messages == []
    ctx.hooks["on_session_start"](session_id="desktop-chat", platform="desktop")
    assert ctx.messages == []
    assert service.engine.record.notices


def test_failed_gateway_delivery_retries_are_bounded_and_status_keeps_notice(plugin):
    ctx, service = plugin
    service.command("switch test", session_id="owner", session_key="telegram:owner", platform="telegram")
    ctx.session_env.update(HERMES_SESSION_ID="owner", HERMES_SESSION_KEY="telegram:owner")
    service.engine.advance()
    attempts = []
    ctx.post_message = lambda text: attempts.append(text) or SimpleNamespace(ok=False, pending=False)
    for i in range(5):
        ctx.hooks["post_tool_call"](**identity("owner"), platform="telegram", status="success")
    assert len(attempts) == 1
    assert "Step 2 of 3" in service.command("status")


def test_cli_transition_waits_until_turn_end(plugin, capsys):
    ctx, service = plugin
    service.command("switch test", session_id="owner", platform="cli")
    service.engine.advance()
    ctx.hooks["pre_llm_call"](**identity("owner"), model="old", platform="cli")
    assert capsys.readouterr().out == ""
    ctx.hooks["on_session_end"](**identity("owner"), platform="cli", completed=True)
    assert "Step 2 of 3" in capsys.readouterr().out


def test_panel_exposes_rollback_notices_until_explicitly_acknowledged(plugin):
    ctx, service = plugin
    service.engine.start(Proposal('saved', OLD, CROSS, 'better'))
    service.notice_text()
    for index in range(5):
        watch_turn(service.engine, index, trouble=True)
    panel = service.panel()
    assert panel['phase'] == 'paused'
    assert ctx.messages == []
    assert len(panel['notices']) == 1
    notice = panel['notices'][0]
    assert set(notice) == {'id', 'text'}
    assert f"You're back on {short_name(OLD.model)}, and it's your default again." in notice['text']
    assert service.panel()['notices'] == panel['notices']
    service.action('acknowledge', switch_id='stale-switch', notice_ids=[notice['id']])
    assert service.panel()['notices'] == panel['notices']
    service.action('acknowledge', switch_id=panel['id'], notice_ids=[notice['id']])
    assert service.panel()['notices'] == []
    assert service.panel()['phase'] == 'paused'


def test_panel_acknowledges_only_the_rendered_notice_ids(plugin):
    _, service = plugin
    service.engine.start(Proposal('saved', OLD, NEW, 'better'))
    initial = service.panel()
    service.engine.advance()
    assert len(service.panel()['notices']) == 2
    service.action('acknowledge', switch_id=initial['id'],
                   notice_ids=[notice['id'] for notice in initial['notices']])
    remaining = service.panel()['notices']
    assert len(remaining) == 1
    assert 'Step 2 of 3' in remaining[0]['text']


def test_panel_defers_success_notices_until_config_write_finishes(plugin, monkeypatch):
    _, service = plugin
    now = [100.0]
    monkeypatch.setattr("shadow.switch.engine.monotonic", lambda: now[0])
    def broken(_route):
        raise RuntimeError('fixture config failure')
    service.engine.set_default = broken
    with pytest.raises(RuntimeError):
        service.engine.start(Proposal('saved', OLD, CROSS, 'better'))
    assert service.panel()['notices'] == []
    service.engine.set_default = lambda _: None
    service.engine.reconcile_config()
    assert service.panel()['notices'] == []
    now[0] += 30
    service.engine.reconcile_config()
    assert 'Your default model is now' in service.panel()['notices'][0]['text']
