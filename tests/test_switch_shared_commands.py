from types import SimpleNamespace

import pytest

from shadow.switch import Proposal, register
from shadow.switch.copy import short_name
from test_switch_plugin import config, plugin
from test_switch_safety import NEW, OLD
from test_switch_states import CROSS


def register_after_trial(ctx, *, trial_active=False):
    calls = []

    def trial_command(raw_args, **context):
        calls.append((raw_args, context))
        return "trial " + raw_args

    ctx.shadow_trial = SimpleNamespace(active_record=lambda: {"id": "trial"} if trial_active else None)
    ctx.shadow_command_routes.update({verb: trial_command for verb in ("doctor", "status", "undo")})
    service = register(ctx)
    from shadow.common.discovery import ModelCatalog
    from shadow.common.models import Model
    models = [Model(route.provider, route.model) for route in (OLD, NEW, CROSS)]
    service.catalog_reader = lambda: ModelCatalog.from_models(models[0], models)
    service.engine.verified_pair = lambda *_: True
    service.engine.set_default = lambda _: None
    service.proposal_lookup = lambda _: Proposal("saved", OLD, NEW, "better")
    return service, calls


@pytest.mark.parametrize("verb", ["status", "undo"])
def test_shared_commands_preserve_trial_handler_without_a_switch(plugin, verb):
    ctx, _ = plugin
    _, calls = register_after_trial(ctx, trial_active=True)
    context = {"session_id": "existing", "session_key": "chat", "platform": "desktop"}
    assert ctx.commands["shadow"](verb, **context) == "trial " + verb
    assert calls == [(verb, context)]


def test_shared_doctor_keeps_trial_and_switch_checks(plugin):
    ctx, _ = plugin
    _, calls = register_after_trial(ctx)
    text = ctx.commands["shadow"]("doctor", terminal=True)
    assert "trial doctor" in text
    assert "verified_gradual_pairs" in text
    assert calls == [("doctor", {"terminal": True})]


@pytest.mark.parametrize("phase", ["running", "watch", "paused"])
def test_active_switch_owns_status_and_undo(plugin, phase):
    ctx, _ = plugin
    service, calls = register_after_trial(ctx)
    service.engine.start(Proposal("saved", OLD, CROSS if phase == "watch" else NEW, "better"))
    if phase == "paused":
        with service.engine.store.transaction() as holder:
            holder[0].phase = "paused"
    text = ctx.commands["shadow"]("status")
    assert "trial status" not in text
    assert {"running": "step 1", "watch": "watching", "paused": "switch paused"}[phase] in text
    assert f"back on {short_name(OLD.model)}" in ctx.commands["shadow"]("undo")
    assert service.engine.record.phase == "undone"
    assert calls == []


def test_completed_switch_still_has_status_and_undo_when_trial_is_idle(plugin):
    ctx, _ = plugin
    service, calls = register_after_trial(ctx)
    service.engine.start(Proposal("saved", OLD, NEW, "better"))
    with service.engine.store.transaction() as holder:
        holder[0].phase = "done"
        holder[0].default_changed = True
        holder[0].completed_on = "2026-11-09"
    assert "done Nov 9" in ctx.commands["shadow"]("status")
    assert "default again" in ctx.commands["shadow"]("undo")
    assert calls == []


def test_active_trial_owns_status_after_an_older_completed_switch(plugin):
    ctx, _ = plugin
    service, calls = register_after_trial(ctx, trial_active=True)
    service.engine.start(Proposal("saved", OLD, NEW, "better"))
    with service.engine.store.transaction() as holder:
        holder[0].phase = "done"
    assert ctx.commands["shadow"]("status") == "trial status"
    assert calls == [("status", {})]


@pytest.mark.parametrize('terminal', [False, True])
def test_registered_switch_refuses_while_trial_is_active(plugin, terminal):
    ctx, _ = plugin
    service, _ = register_after_trial(ctx, trial_active=True)
    writes = []
    service.engine.set_default = writes.append
    service.proposal_lookup = lambda _: Proposal('saved', OLD, CROSS, 'better')
    text = ctx.commands['shadow']('switch saved', terminal=terminal)
    assert text == 'Test trial is still running.'
    assert service.engine.record is None
    assert writes == []
    ctx.shadow_trial.active_record = lambda: None
    assert 'Your default model is now' in ctx.commands['shadow']('switch saved', terminal=terminal)
    assert writes == [CROSS]


@pytest.mark.parametrize('missing_permissions', [False, True])
def test_combined_doctor_reports_shared_setup_only_once(plugin, missing_permissions):
    from shadow.switch.doctor import REQUIRED_SETTINGS

    ctx, _ = plugin
    setup = ('\n'.join(f'Enable {key} with hermes config set {key} true' for key in REQUIRED_SETTINGS)
             if missing_permissions else 'Isolation is required before Shadow can run a test.')
    ctx.shadow_command_routes['doctor'] = lambda *_args, **_kwargs: setup
    service = register(ctx)
    service.config_reader = lambda: {} if missing_permissions else config()
    service.availability = lambda: (True, True)
    service.proposal_lookup = lambda _: Proposal('saved', OLD, NEW, 'better')
    text = ctx.commands['shadow']('doctor')
    assert text.count(setup) == 1
    for key in REQUIRED_SETTINGS:
        assert text.count(f'hermes config set {key} true') == int(missing_permissions)
    assert 'Shadow setup is ready' not in text
    assert 'verified_gradual_pairs' in text
