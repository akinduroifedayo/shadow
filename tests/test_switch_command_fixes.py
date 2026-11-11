import argparse

import pytest

from shadow.switch import Proposal, Route
from test_switch_plugin import plugin
from test_switch_safety import NEW, OLD


@pytest.mark.parametrize("terminal", [False, True])
def test_registered_doctor_checks_the_saved_exact_pair(plugin, capsys, terminal):
    ctx, service = plugin
    looked_up = []

    def lookup(test_id):
        looked_up.append(test_id)
        return Proposal("saved", OLD, NEW, "better")

    service.proposal_lookup = lookup
    if terminal:
        setup, handler = ctx.cli["shadow"]
        parser = argparse.ArgumentParser()
        setup(parser)
        handler(parser.parse_args(["doctor"]))
        text = capsys.readouterr().out
    else:
        text = ctx.commands["shadow"]("doctor")
    assert '"old": ' in text and '"new": ' in text
    assert '"probe_passed": false' in text
    assert looked_up == [None]
    assert service.engine.record is None


@pytest.mark.parametrize("command", ["switch saved", "doctor"])
def test_invalid_trial_route_metadata_is_refused_without_a_traceback(plugin, command):
    ctx, service = plugin

    def lookup(test_id):
        return Proposal("saved", Route(**{**vars(OLD), "extra": "metadata"}), NEW, "better")

    service.proposal_lookup = lookup
    text = ctx.commands["shadow"](command)
    assert text == "This switch action isn't available in the current state."
    assert service.engine.record is None
    assert ctx.messages == []


def test_doctor_without_an_eligible_saved_test_still_reports_the_registry(plugin):
    _, service = plugin
    service.proposal_lookup = lambda test_id: None
    text = service.command("doctor")
    assert "plugins.entries.shadow.settings.verified_gradual_pairs" in text
    assert "Entry template" not in text
