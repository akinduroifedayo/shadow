from types import SimpleNamespace

import pytest

from shadow.trial.usage import parse_usage, resolve_prices


def test_normalized_hermes_usage_counts_all_input_buckets():
    usage = parse_usage({'input_tokens': 1000, 'prompt_tokens': 1500, 'output_tokens': 100,
                         'cache_read_tokens': 400, 'cache_write_tokens': 100})
    assert usage.input_tokens == 1500 and usage.total_tokens == 1600


def test_raw_anthropic_usage_and_absent_plugin_usage_are_conservative():
    usage = parse_usage({'input_tokens': 1000, 'output_tokens': 100, 'cache_read_input_tokens': 400})
    assert usage.input_tokens == 1400
    assert parse_usage(SimpleNamespace(input_tokens=0, output_tokens=0)).estimated


def test_sol_to_terra_plan_gets_a_dollar_estimate(tmp_path):
    from decimal import Decimal

    from fixtures import FakeContext, selection
    from shadow.common.models import Model, price_for
    from shadow.common.storage import Store
    from shadow.trial.cards import render_start
    from shadow.trial.engine import TrialEngine, estimate
    from shadow.trial.judge import JudgeChoice
    from shadow.trial.safety import ReplayGuard

    price = price_for(Model('openai-codex', 'gpt-6.1-terra', plan=True))
    assert price.dated == '2026-11-10'
    assert price.source == 'https://developers.openai.com/api/docs/pricing'
    assert (price.input, price.output, price.cache_read, price.cache_write) == (
        Decimal('1'), Decimal('5'), Decimal('0.05'), Decimal('1.25'))
    assert price.tier_threshold_tokens == 272000
    # Above 272K input tokens, input and both cache rates are 2x and output is 1.5x.
    assert price.above == (Decimal('2'), Decimal('7.5'), Decimal('0.10'), Decimal('2.50'))
    assert price.cost(272000, 0) == Decimal(272000) / Decimal(1_000_000)
    assert price.cost(272001, 0) == Decimal(272001) * 2 / Decimal(1_000_000)
    assert price.cost(272001, 1000, 100, 50) == (
        Decimal(272001 - 150) * 2 + Decimal(1000) * Decimal('7.5')
        + Decimal(100) * Decimal('0.10') + Decimal(50) * Decimal('2.50')) / Decimal(1_000_000)

    sol = Model('openai-codex', 'gpt-6.1-sol', plan=True)
    terra = Model('openai-codex', 'gpt-6.1-terra', plan=True)
    judge = Model('anthropic', 'claude-sonnet-4-5')
    picked = selection(tmp_path, 20)
    prediction = estimate(picked, sol, terra, JudgeChoice('third_maker', (judge,), 'auto'), prices=resolve_prices((sol, terra, judge)))
    assert prediction['priced'] is True
    assert prediction['total'] == pytest.approx(prediction['judge'])
    assert prediction['plan_tokens'] > 0 and prediction['plan_cost'] > 0
    engine = TrialEngine(FakeContext(), Store(tmp_path / 'trial.sqlite'), ReplayGuard())
    record = engine.prepare(picked, sol, terra, [judge])
    assert record['can_start'] and record['price_date'] == '2026-11-10'
    text = render_start(record, isolation_verified=True)
    assert f"About ${prediction['total']:.2f}" in text
    assert 'your plan:' in text


def test_whole_request_long_context_prices_match_the_dated_table():
    from decimal import Decimal
    from shadow.common.models import Model, price_for
    for name, ordinary, above in [('gpt-6.1-sol', '2', '4'), ('gpt-6-sol', '2', '4'), ('gpt-6-luna', '.1', '.2')]:
        price = price_for(Model('openai-codex', name))
        assert price.cost(272000, 0) == Decimal(272000) * Decimal(ordinary) / 1000000
        assert price.cost(272001, 0) == Decimal(272001) * Decimal(above) / 1000000
    price = price_for(Model('openai-codex', 'gpt-6.1-sol'))
    assert price.cost(300000, 100, 20000, 10000) == Decimal('1.1355')


def test_estimate_requires_and_uses_a_frozen_price_table(tmp_path, monkeypatch):
    import pytest
    from fixtures import CURRENT, OUTSIDE, TRIAL, selection
    from shadow.trial import usage
    from shadow.trial.engine import estimate
    from shadow.trial.judge import JudgeChoice

    choice = JudgeChoice('third_maker', (OUTSIDE,), 'auto')
    prices = usage.resolve_prices((CURRENT, TRIAL, OUTSIDE))

    def unavailable(*args, **kwargs):
        raise AssertionError('estimate must not resolve prices')
    monkeypatch.setattr(usage, 'price_for', unavailable)

    assert estimate(selection(tmp_path, 20), CURRENT, TRIAL, choice, prices=prices)['priced']
    with pytest.raises(TypeError, match='prices'):
        estimate(selection(tmp_path, 20), CURRENT, TRIAL, choice)


@pytest.mark.parametrize('judge_name', ['claude-opus-4-6', 'claude-sonnet-4-6'])
@pytest.mark.parametrize('above_judge', [True, False])
def test_codex_start_cap_counts_only_metered_judge(tmp_path, judge_name, above_judge):
    from fixtures import FakeContext, selection
    from shadow.common.models import Model
    from shadow.common.storage import Store
    from shadow.trial.cards import render_start
    from shadow.trial.engine import TrialEngine, estimate
    from shadow.trial.judge import JudgeChoice

    current = Model('openai-codex', 'gpt-6.1-sol')
    trial = Model('openai-codex', 'gpt-6.1-terra')
    judge = Model('anthropic', judge_name)
    picked = selection(tmp_path, 30)
    prediction = estimate(picked, current, trial, JudgeChoice('third_maker', (judge,)),
                          prices=resolve_prices((current, trial, judge)))
    cap = prediction['judge'] + 0.01 if above_judge else prediction['judge'] / 2
    engine = TrialEngine(FakeContext(), Store(tmp_path / 'cap.sqlite'), None)
    record = engine.prepare(picked, current, trial, [judge], cap=cap)

    assert record['can_start'] is above_judge
    assert prediction['total'] == pytest.approx(prediction['judge'])
    assert record['estimate']['total'] == pytest.approx(prediction['judge'])
    assert record['current']['plan'] and record['trial']['plan']
    assert record['estimate']['plan_tokens'] > 0
    assert record['estimate']['plan_cost'] > 0
    card = render_start(record, isolation_verified=True)
    assert 'your plan:' in card
    assert ('[Start]' in card) is above_judge
