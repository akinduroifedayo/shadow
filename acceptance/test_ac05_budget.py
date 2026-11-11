"""AC5. Spending stops at the cap."""

from __future__ import annotations

import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

from acceptance.support import Check, Context
from shadow.common.copy import copy
from shadow.common.models import Model, price_for
from shadow.common.storage import Store
from shadow.trial.cards import render_start
from shadow.trial.engine import TrialEngine, estimate
from shadow.trial.usage import resolve_prices
from shadow.trial.judge import JudgeChoice
from shadow.trial.picking import Selection, Turn
from shadow.trial.plugin import TrialPlugin, register

CURRENT = Model("openai-codex", "gpt-6.1-sol", plan=True)
TRIAL = Model("openai-codex", "gpt-6-luna", plan=True)
JUDGE = Model("anthropic", "claude-sonnet-4-5")
UNPRICED_CURRENT = Model("nous", "hermes-4")
UNPRICED_TRIAL = Model("google", "gemini-2.5-pro")
UNPRICED_JUDGE = Model("deepseek", "deepseek-chat")


class LLM:
    def complete_structured(self, **kwargs):
        return SimpleNamespace(
            provider=kwargs["provider"], model=kwargs["model"], text="same",
            parsed={"pick": "1", "fixes_1": "none", "fixes_2": "none",
                    "reason_kind": "missed_request", "reason": "The first answer covered the request."},
            usage={"input_tokens": 30, "output_tokens": 12},
        )


class Lifecycle:
    def __init__(self, ctx):
        self.ctx = ctx
        self.launches = []
        self.results = {}

    def launch(self, request):
        self.launches.append(request)
        sid = f"child-{len(self.launches)}"
        self.ctx.hook(
            "subagent_start", parent_session_id="controller", child_session_id=sid, child_goal=request["goal"]
        )
        huge = len(self.launches) >= 3
        self.ctx.hook(
            "post_api_request",
            session_id=sid,
            api_request_id=f"req-{sid}",
            provider=request["provider"],
            response_model=request["model"],
            base_url="https://api.openai.com/v1",
            api_mode="codex_responses",
            usage={"prompt_tokens": 80, "completion_tokens": 20_000_000 if huge else 40},
        )
        self.results[sid] = SimpleNamespace(
            ready=True, summary="finished answer", terminal_state="SUCCEEDED", started_at=1.0, completed_at=2.0
        )
        self.ctx.hook("subagent_stop", child_session_id=sid, tool_call_history=[])
        return sid

    def wait(self, handle, timeout_seconds=0):
        return None

    def result(self, handle):
        return self.results[handle]

    def cancel(self, handle, reason=""):
        return None


def _selection(work: Path, count: int) -> Selection:
    turns = [
        Turn(f"t{i}", f"c{i}", "cli", str(work), f"Please inspect fixture task {i} carefully.", "[]")
        for i in range(count)
    ]
    return Selection(turns, count, count, {}, {}, 5)


def check(mac: bool = False):
    del mac
    probe = Check(5)
    work = Path(tempfile.mkdtemp(prefix="shadow-budget-"))
    selection = _selection(work, 20)
    choice = JudgeChoice("third_maker", (JUDGE,), "auto")
    prediction = estimate(selection, CURRENT, TRIAL, choice, prices=resolve_prices((CURRENT, TRIAL, JUDGE)))
    probe.expect(prediction["plan_tokens"] > 0, "plan usage was not counted in the estimate")
    probe.expect(prediction["total"] == prediction["judge"], "included plan usage increased the dollar estimate")
    probe.expect(prediction["plan_cost"] > 0, "plan list-price worth was not retained")

    ctx = Context()
    ctx.llm = LLM()
    plugin = TrialPlugin(
        ctx, store=Store(work / "over.sqlite"), current=CURRENT, catalog=[CURRENT, TRIAL, JUDGE],
        config_reader=lambda: {},
        selector=lambda **kwargs: selection,
        launcher=SimpleNamespace(isolation_verified=True, profile="shadow-demo", launch=lambda record: 0),
    )
    register(ctx, plugin)
    card = plugin.command(f"try {TRIAL.model} --budget 0.50 --yes", terminal=True)
    saved = plugin.store.get()
    probe.expect(saved is not None and saved["can_start"] is False, "an over-cap estimate was allowed to start")
    probe.expect(saved["state"] == "prepared", "an over-cap estimate left the prepared state")
    probe.expect(f"[{copy('button.start')}]" not in card, "the over-cap card offered Start")
    probe.expect("your plan:" in card, "the card did not count plan usage at list price")
    probe.expect(saved.get("state") != "queued", "over-cap test was queued")

    with redirect_stdout(StringIO()) as output:
        plugin.command(f"try {TRIAL.model} --budget {prediction['judge'] + 0.01:.6f}", terminal=True)
    within_cap = output.getvalue()
    affordable = plugin.store.get()
    probe.expect(affordable["can_start"] is True, "plan contestants blocked a judge-affordable test")
    probe.expect(affordable["estimate"]["total"] == prediction["judge"], "Start counted plan list prices")
    probe.expect("your plan:" in within_cap, "affordable card lost plan list-price worth")

    plan_engine = TrialEngine(ctx, plugin.store, plugin.guard)
    plugin.engine = plan_engine
    plan_record = plan_engine.prepare(selection, CURRENT, TRIAL, [JUDGE], cap=prediction["total"])
    plan_engine.attach(plan_record, "controller")
    plan_engine.post_api_request(session_id="controller", api_request_id="huge-plan",
                                 provider=CURRENT.provider, response_model=CURRENT.model,
                                 usage={"prompt_tokens": 3_000_000, "completion_tokens": 20_000_000})
    probe.expect(plan_record["spend"] == 0, "included plan usage increased spend")
    probe.expect(not plan_engine.at_cap(), "included plan usage tripped the cap")

    run_ctx = Context()
    run_ctx.llm = LLM()
    lifecycle = Lifecycle(run_ctx)
    run_ctx.subagent_lifecycle = lifecycle
    run_plugin = TrialPlugin(
        run_ctx, store=Store(work / "run.sqlite"), current=CURRENT, catalog=[CURRENT, TRIAL, JUDGE],
        config_reader=lambda: {},
        launcher=SimpleNamespace(isolation_verified=True, profile="shadow-demo"),
    )
    register(run_ctx, run_plugin)
    engine = TrialEngine(run_ctx, run_plugin.store, run_plugin.guard)
    run_plugin.engine = engine
    metered_current, metered_trial = (replace(model, provider="openai", plan=False) for model in (CURRENT, TRIAL))
    metered_prices = resolve_prices((metered_current, metered_trial, JUDGE))
    metered_prediction = estimate(selection, metered_current, metered_trial, choice, prices=metered_prices)
    record = engine.prepare(selection, metered_current, metered_trial, [JUDGE], cap=metered_prediction["total"])
    probe.expect(record["can_start"] is True, "a within-cap estimate could not start")
    engine.attach(record, "controller")
    outcome = {"continue": True}
    for _ in range(12):
        outcome = engine.step("controller")
        if not outcome.get("continue", False):
            break
    finished = engine.record
    usable = [turn["id"] for turn in finished["turns"] if turn.get("usable")]
    probe.expect(finished["stop_reason"] == "cap", f"mid-test crossing did not stop for the cap ({finished['stop_reason']})")
    probe.expect(usable == ["t0"], f"verdict did not keep only whole turns in saved order: {usable}")
    probe.expect(finished["turns"][1]["state"] != "done", "a turn started after the cap was completed")
    probe.expect(len(lifecycle.launches) == 3, f"new work continued after the cap ({len(lifecycle.launches)} launches)")
    probe.expect(finished["excess_inflight"] > 0, "in-flight excess was not listed")
    probe.expect(
        abs(finished["excess_inflight"] - (finished["spend"] - finished["cap"])) < 1e-6,
        "listed excess does not equal spend above the cap",
    )
    inflight = max(row["cost"] or 0 for row in finished["request_usage"])
    probe.expect(finished["spend"] <= finished["cap"] + inflight + 1e-6, "spend exceeded the cap plus in-flight requests")

    unpriced = (UNPRICED_CURRENT, UNPRICED_TRIAL, UNPRICED_JUDGE)
    probe.expect(all(price_for(model) is None for model in unpriced), "the unknown-price fixture has a list price")
    unknown_estimate = estimate(selection, *unpriced[:2], JudgeChoice("third_maker", (UNPRICED_JUDGE,), "auto"), prices=resolve_prices(unpriced))
    unknown_ctx = Context()
    unknown_ctx.llm = LLM()
    unknown_life = Lifecycle(unknown_ctx)
    unknown_ctx.subagent_lifecycle = unknown_life
    unknown_plugin = TrialPlugin(
        unknown_ctx, store=Store(work / "unknown.sqlite"), current=UNPRICED_CURRENT,
        config_reader=lambda: {},
        catalog=list(unpriced), launcher=SimpleNamespace(isolation_verified=True, profile="shadow-demo"),
    )
    register(unknown_ctx, unknown_plugin)
    unknown_engine = TrialEngine(unknown_ctx, unknown_plugin.store, unknown_plugin.guard)
    unknown_plugin.engine = unknown_engine
    unknown = unknown_engine.prepare(selection, UNPRICED_CURRENT, UNPRICED_TRIAL, list(unpriced), cap=5)
    unknown_card = render_start(unknown, isolation_verified=True)
    token_line = f"{unknown_estimate['tokens']:,} tokens"
    missing = ", ".join(dict.fromkeys(model.name for model in (UNPRICED_TRIAL, UNPRICED_CURRENT, UNPRICED_JUDGE)))
    no_price = copy("start.no_price", model=missing)
    probe.expect(unknown["can_start"] is False, "an unknown price offered Start")
    probe.expect(f"[{copy('button.start')}]" not in unknown_card, "the unknown-price card offered Start")
    probe.expect(token_line in unknown_card, f"the unknown-price card did not show {token_line}")
    probe.expect(no_price in unknown_card, "the unknown-price card did not name the models with no price")
    probe.expect("About $" not in unknown_card, "the unknown-price card showed a dollar estimate")
    unknown["can_start"] = True
    unknown_engine.attach(unknown, "controller")
    for _ in range(40):
        step = unknown_engine.step("controller")
        if not step.get("continue", False):
            break
    unknown_done = unknown_engine.record
    used_tokens = sum((row.get("usage") or {}).get("total_tokens", 0) for row in unknown_done["request_usage"])
    probe.expect(unknown_done["stop_reason"] == "unpriceable",
                 f"the unpriceable backstop did not stop the trial ({unknown_done['stop_reason']})")
    probe.expect(unknown_done["spend"] < unknown_done["cap"], "unknown-price spend reached the dollar cap")
    probe.expect(used_tokens < unknown_estimate["tokens"], "unknown-price usage reached the displayed token estimate")
    return probe.finish(
        f"judge over-cap card has no Start; judge-affordable plan trial can start with zero plan spend; plan tokens {prediction['plan_tokens']}; "
        f"priced run stopped at the cap with excess {finished['excess_inflight']:.4f} and only t0 usable; "
        f"unknown price has no Start, shows {token_line}, and names the models with no price; "
        f"the unpriceable backstop stops the running trial "
        f"(spend {unknown_done['spend']:.4f} of cap {unknown_done['cap']}, "
        f"{used_tokens} tokens used of {unknown_estimate['tokens']} shown, "
        f"{len(unknown_life.launches)} launch)"
    )


def test_ac05_spending_stops_at_the_cap():
    result = check(False)
    assert result.status == "PASS", result.evidence
