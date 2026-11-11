"""AC6. A real test, end to end, on the box profile. Needs a Mac."""

from acceptance.support import MAC_REPORT, Check


def check(mac: bool = False):
    probe = Check(6)
    if mac and MAC_REPORT:
        probe.note("mac log printed by acceptance/mac.py")
    return probe.finish(
        "needs profile shadow-demo on a Mac: chat start, at least 20 usable turns, "
        "third-maker judge, both runs per turn, picked and left-out reasons, "
        "start-card busy line, screenshots of the start card and verdict card. No provider was called",
        skip="needs-mac",
    )


def test_ac06_reports_skip_without_a_mac():
    result = check(False)
    assert result.status == "SKIP", result.evidence
    assert result.skip_reason == "needs-mac"
