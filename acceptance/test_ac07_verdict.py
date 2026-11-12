"""AC7. The verdict follows the rules.

Single-judge runs use the spec table. The panel rule applies only when
judge_mode is backup_both, so a stored judge_policy must not change a
third-maker card. Every number on the card has to match the saved record.
"""

from __future__ import annotations

import math

from acceptance.support import Check
from shadow.trial.cards import receipt_columns, render_verdict
from shadow.trial.presentation import verdict_presentation
from shadow.trial.verdict import finalize, statistics_for

TABLE = (
    (15, 11, 4, "better"),
    (10, 18, 2, "better"),
    (14, 11, 5, "better"),
    (9, 19, 2, "better"),
    (6, 19, 5, "holds_up"),
    (3, 24, 3, "holds_up"),
    (0, 30, 0, "holds_up"),
    (4, 22, 4, "too_close"),
    (4, 20, 6, "too_close"),
    (2, 19, 9, "worse"),
    (3, 9, 18, "worse"),
    (2, 9, 9, "worse"),
    (7, 8, 4, "not_enough"),
)


def _turn(index, result):
    pick = {"won": "1", "lost": "2", "same": "same"}[result]
    forward = {"pick": pick, "fixes_1": "none", "fixes_2": "none", "reason_kind": "missed_request",
               "reason": "covered", "normalized_pick": pick, "reverse": False}
    backward = {**forward, "reverse": True, "pick": {"1": "2", "2": "1", "same": "same"}[pick]}
    run = {"cost": 0.014 if result == "won" else 0.036, "usage": {"total_tokens": 100, "estimated": False},
           "duration_s": 1.5, "tool_calls": [{}], "broken_calls": 0}
    return {
        "id": f"t{index}", "usable": True, "state": "done", "first_line": f"turn {index}",
        "runs": {"new": [dict(run, cost=0.014)], "yours": [dict(run, cost=0.036)]},
        "judgment": {
            "result": result, "agree": True, "disagreement": False,
            "decisions": [{"orders": [forward, backward], "agree": True, "pick": pick}],
            "reason_kind": "missed_request", "reason": "covered", "fixes_new": "none", "fixes_yours": "none",
        },
    }


def _record(won, same, lost):
    turns = []
    index = 0
    for result, count in (("won", won), ("same", same), ("lost", lost)):
        for _ in range(count):
            turns.append(_turn(index, result))
            index += 1
    return {
        "id": "verdict", "intent": "switch", "picked": won + same + lost, "cap": 5, "web_lookups": True, "web_turns": 0,
        "current": {"provider": "openai-codex", "model": "gpt-6.1-sol", "plan": False},
        "trial": {"provider": "openai-codex", "model": "gpt-6-luna", "plan": False},
        "judge": {"models": [{"provider": "anthropic", "model": "claude-sonnet-4-5"}], "source": "auto", "notice": ""},
        "judge_mode": "third_maker", "judge_source": "auto", "judge_disagreements": 0,
        "judge_policy": {"version": 2, "swap_consistency_min": 70, "bootstrap_seed": 1, "bootstrap_samples": 10000},
        "second_look": False, "reruns": 0, "varied": 0, "stop_reason": None, "routes": {},
        "estimate": {"total": 1.5},
        "turns": turns,
    }


def check(mac: bool = False):
    del mac
    probe = Check(7)
    mismatches = []
    for won, same, lost, spec_word in TABLE:
        maths = statistics_for(won, same, lost)
        probe.expect(maths["word"] == spec_word, f"statistics_for {won}/{same}/{lost} was {maths['word']}, spec table says {spec_word} (shadow/trial/verdict.py:15)")
        record = _record(won, same, lost)
        probe.expect(record["judge_mode"] == "third_maker", "the single-judge fixture was not third_maker")
        finalize(record)
        actual = record["totals"]["word"]
        if actual != spec_word:
            mismatches.append(f"{won}/{same}/{lost} card={actual} spec={spec_word}")
        probe.expect(actual == spec_word, f"single-judge card {won}/{same}/{lost} was {actual}, spec table says {spec_word} (shadow/trial/verdict.py:84)")
        card = render_verdict(record, stats=True)
        totals = record["totals"]
        shown = verdict_presentation(record)["headline"]
        advice = {"better": "Safe to switch", "holds_up": "Switch saves money", "worse": "Stay on your current model",
                  "too_close": "Too close to call", "not_enough": "Too close to call"}[actual]
        probe.expect("[" + advice + "]" in card, "advice tag differs from the saved verdict")
        probe.expect(card.splitlines()[0] == shown, f"card heading for {won}/{same}/{lost} was not {shown}")
        probe.expect(
            f"Won {totals['won']} · Same {totals['same']} · Lost {totals['lost']} of {totals['pairs']}" in card,
            f"card W/S/L differs from the record for {won}/{same}/{lost}",
        )
        for column in receipt_columns(record):
            if column["key"] == "result":
                probe.expect(column["value"] == f"{totals['won']} · {totals['same']} · {totals['lost']}", "receipt strip disagrees with the record")
                continue
            probe.expect(column["value"] in card, f"card {column['key']} {column['value']!r} is not the record")
        if totals["word"] in {"better", "worse"}:
            luck = math.ceil(100 * totals["p"]) if totals["p"] >= 0.01 else math.ceil(1000 * totals["p"])
            probe.expect(str(luck) in card, f"luck figure {luck} from the record is missing on the card")
        if totals["word"] in {"holds_up", "too_close"}:
            if totals["worst_per_100"] > 0:
                probe.expect(str(totals["worst_per_100"]) in card, f"ruled-out gap {totals['worst_per_100']} is missing on the card")
            else:
                probe.expect("about even with yours" in card, "a non-positive gap was not shown as about even")
    first = statistics_for(9, 19, 2)
    probe.expect(first["word"] == "better", "first look did not clear the interval bar")
    second = statistics_for(9, 19, 2, second_look=True)
    probe.expect(second["word"] != "better", "second look did not raise the bar (shadow/trial/verdict.py:27)")
    note = "mismatched rows: " + ", ".join(mismatches) if mismatches else "single-judge card words match the spec table on every row"
    return probe.finish(
        "W/S/L, luck, gap, and costs match the record; " + note
    )


def test_ac07_verdict_follows_the_rules():
    result = check(False)
    assert result.status == "PASS", result.evidence
