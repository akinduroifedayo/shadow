"""AC11. The Desktop page. Numbers can be checked from records; screenshots need a Mac."""

from __future__ import annotations

from acceptance.support import MAC_REPORT, Check
from shadow.common.copy import load_copy
from shadow.desktop_rpc import Copy, FixtureSource, build_page, build_turn, trial_view


def _numbers(value, found):
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        found.append(value)
    elif isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _numbers(item, found)
    elif isinstance(value, list):
        for item in value:
            _numbers(item, found)


def check(mac: bool = False):
    probe = Check(11)
    table = Copy(load_copy())
    source = FixtureSource("result-better")
    test = trial_view(source.test(source.tests()[0]["id"]))
    page = build_page(source, table, test["id"])
    totals = test["totals"]
    bar = page["verdict"]["bar"]
    probe.expect(
        (bar["won"], bar["same"], bar["lost"]) == (totals["won"], totals["same"], totals["lost"]),
        f"verdict strip {bar['won']}/{bar['same']}/{bar['lost']} != record {totals['won']}/{totals['same']}/{totals['lost']} (shadow/desktop_rpc.py:531)",
    )
    shown = []
    _numbers(page["verdict"]["receipt"], shown)
    flat = " ".join(str(item) for item in shown)
    for label, number in (("won", totals["won"]), ("same", totals["same"]), ("lost", totals["lost"]), ("pairs", totals["pairs"])):
        probe.expect(str(number) in flat, f"receipt omitted record {label} {number}")
    lost = next(turn for turn in test["turns"] if turn.get("usable") and turn["judgment"]["result"] == "lost")
    hidden = build_turn(table, test, lost["id"])
    probe.expect(hidden["revealed"] is False, "side-by-side revealed names before a pick")
    probe.expect(all(answer["name"] is None for answer in hidden["answers"]), "hidden side-by-side showed a model name")
    source.save_pick(test["id"], lost["id"], 1)
    revealed = build_turn(table, trial_view(source.test(test["id"])), lost["id"])
    probe.expect(revealed["revealed"] is True and any(answer["name"] for answer in revealed["answers"]),
                 "reveal did not show the saved model names")

    switch_source = FixtureSource("switch")
    switch_page = build_page(switch_source, table)
    raw = switch_source.world["switch"]["counts"]
    rows = switch_page["switch"]["trouble"]["rows"]
    probe.expect(rows[0][1] == str(raw["new"]["turns"]) and rows[1][1] == str(raw["old"]["turns"]),
                 f"switch panel turns {rows} != record {raw} (shadow/desktop_rpc.py:894)")
    if probe.failures:
        return probe.finish("desktop numbers did not match the record")
    if mac and MAC_REPORT:
        probe.note("mac log printed; screenshots were not taken")
    return probe.finish(
        f"verdict strip {bar['won']}/{bar['same']}/{bar['lost']} matches the record; names stay hidden until reveal; "
        f"switch panel turns match {raw['new']['turns']}/{raw['old']['turns']}. Screenshots were not taken",
        skip="needs-mac",
    )


def test_ac11_does_not_pass_without_a_mac():
    result = check(False)
    assert result.status != "PASS"
    if result.status == "FAIL":
        raise AssertionError(result.evidence)
    assert result.skip_reason == "needs-mac"
