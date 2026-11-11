"""AC4. The judge is not a contestant."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from acceptance.support import Check
from shadow.common.models import Model
from shadow.trial.cards import render_start, render_verdict
from shadow.trial.engine import TrialEngine
from shadow.trial.judge import Judge, choose_judge, judge_options
from shadow.trial.picking import Selection, Turn
from shadow.trial.safety import ReplayGuard
from shadow.common.storage import Store


CURRENT = Model("openai-codex", "gpt-6.1-sol")
TRIAL = Model("openai-codex", "gpt-6-luna")
THIRD = Model("anthropic", "claude-sonnet-4-5", reachable=True)
SAME_MAKER_OTHER_PROVIDER = Model("nous", "gpt-4.1", reachable=True)
OTHER_MAKER_SAME_PROVIDER = Model("nous", "gemini-2.5-pro", reachable=True)
UNREACHABLE = Model("anthropic", "claude-opus-4-6", reachable=False)
CATALOG = [CURRENT, TRIAL, SAME_MAKER_OTHER_PROVIDER, OTHER_MAKER_SAME_PROVIDER, THIRD, UNREACHABLE]


class CapturingLLM:
    def __init__(self):
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            provider=kwargs["provider"], model=kwargs["model"], text="same",
            parsed={"pick": "same", "fixes_1": "none", "fixes_2": "none",
                    "reason_kind": "missed_request", "reason": "Both covered the request."},
            usage={"input_tokens": 20, "output_tokens": 10},
        )


def _refuse(probe: Check, requested: str, snippet: str) -> None:
    try:
        choose_judge(CURRENT, TRIAL, CATALOG, requested)
    except ValueError as exc:
        probe.expect(snippet in str(exc), f"refusal for {requested!r} was {exc}")
        return
    probe.expect(False, f"{requested!r} was accepted as a judge")


def check(mac: bool = False):
    del mac
    probe = Check(4)
    auto = choose_judge(CURRENT, TRIAL, CATALOG)
    probe.expect(auto.mode == "third_maker", "auto pick was not a third maker")
    probe.expect(auto.models[0].maker == "google", "auto pick counted provider instead of maker")
    probe.expect(auto.models[0].provider == "nous", "a multi-maker provider could not supply the judge")
    probe.expect(auto.source == "auto", "auto pick was not labeled auto")
    hidden = {model.model for model in judge_options(CURRENT, TRIAL, CATALOG)}
    probe.expect(SAME_MAKER_OTHER_PROVIDER.model not in hidden, "a contestant-maker model stayed in the Change list")
    probe.expect(OTHER_MAKER_SAME_PROVIDER.model in hidden, "a third-maker model was hidden from the Change list")
    _refuse(probe, CURRENT.model, "The judge can't be one of the models being compared.")
    _refuse(probe, "gpt-4.1", "The judge has to come from a maker that made neither model being compared.")
    _refuse(probe, "no-such-model", "No model called no-such-model on your providers.")
    user = choose_judge(CURRENT, TRIAL, CATALOG, THIRD.model)
    probe.expect(user.source == "user" and user.models[0].model == THIRD.model, "a valid named judge was not used")
    fallback = choose_judge(CURRENT, TRIAL, CATALOG, UNREACHABLE.model)
    probe.expect(fallback.source == "auto", "an unreachable pick did not fall back to auto")
    probe.expect("can't be reached" in fallback.notice, "the start card was not told about the fallback")
    backup = choose_judge(CURRENT, TRIAL, [CURRENT, TRIAL])
    probe.expect(backup.mode == "backup_both" and backup.source == "auto", "missing third maker did not use backup judging")

    work = Path(tempfile.mkdtemp(prefix="shadow-judge-"))
    turn = Turn("t1", "c1", "cli", str(work), "Please compare these notes carefully.", "[]")
    selection = Selection([turn], 1, 1, {}, {}, 3)
    store = Store(work / "trial.sqlite")
    engine = TrialEngine(SimpleNamespace(get_config=lambda key, default=None: default), store, ReplayGuard())
    record = engine.prepare(selection, CURRENT, TRIAL, CATALOG, judge=THIRD.model, cap=100)
    record["judge_source"] = "user"
    record["judge"]["source"] = "user"
    start = render_start(record, isolation_verified=True)
    probe.expect(record["judge_source"] == "user", "a named judge was not stored as judge_source=user")
    probe.expect("your pick" in start, "the start card did not say your pick before Start")
    verdict_record = {
        **record,
        "state": "finished",
        "turns": [],
        "totals": {"word": "better", "cant_call_reason": None, "won": 15, "same": 11, "lost": 4, "pairs": 30,
                   "p": 0.02, "U": 0.0, "worst_per_100": 0, "judge_agree": 30, "judge_pairs": 30,
                   "lean": "better", "second_look": False, "reruns": 0, "varied": 0,
                   "receipt": {"counts_readable": "yes", "saved_pct": None, "more_pct": None,
                               "cost_per_turn_new": 0.01, "cost_per_turn_yours": 0.02,
                               "speed_new_s": 1.0, "speed_yours_s": 2.0, "steps_new": 1.0, "steps_yours": 1.0,
                               "broken_new": 0, "broken_yours": 0, "fixes_new": 0, "fixes_yours": 0,
                               "cost_mode": "list_price", "where_won": {"kind": None, "count": 0},
                               "where_lost": {"kind": None, "count": 0}}},
    }
    card = render_verdict(verdict_record)
    probe.expect("your pick" in card, "the verdict card did not say your pick")
    unreachable = engine.prepare(selection, CURRENT, TRIAL, CATALOG, judge=UNREACHABLE.model, cap=100, run_id="unreachable")
    probe.expect(unreachable["judge_source"] == "auto", "unreachable judge_source was not auto")
    probe.expect("can't be reached" in render_start(unreachable, isolation_verified=True), "fallback was not on the start card before Start")
    backup_record = engine.prepare(selection, CURRENT, TRIAL, [CURRENT, TRIAL], cap=100, run_id="backup")
    probe.expect("Backup judging" in render_start(backup_record, isolation_verified=True), "backup judging was not labeled")

    llm = CapturingLLM()
    original = "ORIGINAL_ANSWER_NOT_FOR_THE_JUDGE"
    answers = [
        {"text": f"The {TRIAL.model} draft.", "tool_calls": [], "stopped": None, "did_not_finish": False},
        {"text": f"The {CURRENT.model} draft.", "tool_calls": [], "stopped": None, "did_not_finish": False},
    ]
    Judge(llm, user, (TRIAL, CURRENT), prices=record['prices']).call(turn, answers)
    probe.expect(len(llm.calls) == 2, "the judge was not asked in both orders")
    first = json.loads(llm.calls[0]["input"][0]["text"])
    second = json.loads(llm.calls[1]["input"][0]["text"])
    probe.expect(first["answers"] == list(reversed(second["answers"])), "the two calls did not reverse the answers")
    flat = json.dumps(llm.calls)
    for name in (CURRENT.model, TRIAL.model, CURRENT.name, TRIAL.name, "OpenAI", original):
        probe.expect(name not in flat, f"judge prompt contained {name}")
    return probe.finish(
        "third maker by maker not provider; contestant, same-maker, and unknown names refused; "
        "user pick shown; unreachable falls back; prompts have no model name or original answer"
    )


def test_ac04_judge_is_not_a_contestant():
    result = check(False)
    assert result.status == "PASS", result.evidence
