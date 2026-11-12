import json
import sys
from dataclasses import replace
from decimal import Decimal
from types import ModuleType

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext, selection
from shadow.common.models import Model, Price, price_for
from shadow.common.storage import Store
from shadow.trial.cards import render_start
from shadow.trial.engine import TrialEngine
from shadow.trial.safety import ReplayGuard
from shadow.trial.usage import Usage, cost_usage


FLAT_MODELS = {'current': Model('openrouter', 'gpt-4.1'),
               'trial': Model('openrouter', 'gpt-4.1-mini'),
               'judge': Model('openrouter', OUTSIDE.model)}


@pytest.fixture(autouse=True)
def hermes_pricing(monkeypatch):
    catalogs = {}
    package = ModuleType('hermes_cli')
    package.__path__ = []
    module = ModuleType('hermes_cli.models_pricing')

    def get_pricing_for_provider(provider, *, base_url='', force_refresh=False, cached_only=False):
        supported = {'nous', 'openrouter', 'ai-gateway', 'novita', 'deepinfra', 'fireworks', 'kilocode'}
        assert cached_only, 'Shadow must not fetch provider pricing'
        return catalogs.get(provider, {}) if provider in supported else {}

    module.get_pricing_for_provider = get_pricing_for_provider
    agent = ModuleType('agent')
    agent.__path__ = []
    metadata = ModuleType('agent.model_metadata')
    def unexpected_fetch(*args, **kwargs):
        pytest.fail('Shadow must not fetch OpenRouter metadata')

    metadata.fetch_model_metadata = unexpected_fetch
    monkeypatch.setitem(sys.modules, 'agent', agent)
    monkeypatch.setitem(sys.modules, 'agent.model_metadata', metadata)
    monkeypatch.setitem(sys.modules, 'hermes_cli', package)
    monkeypatch.setitem(sys.modules, 'hermes_cli.models_pricing', module)
    return catalogs


def prepare(tmp_path, current=CURRENT, trial=TRIAL, judge=OUTSIDE):
    engine = TrialEngine(FakeContext(), Store(tmp_path / 'pricing.sqlite'), ReplayGuard())
    return engine.prepare(selection(tmp_path, 20), current, trial, [judge])


def test_public_token_rates_without_a_public_request_fee_block_start(tmp_path, hermes_pricing):
    trial = FLAT_MODELS['trial']
    hermes_pricing['openrouter'] = {trial.model: {
        'prompt': '0.000002', 'completion': '0.000006',
        'input_cache_read': '0.000001', 'input_cache_write': '0.000003'}}
    record = prepare(tmp_path, trial=trial)
    assert not record['can_start'] and record['estimate']['total'] is None
    assert price_for(trial) is None
    assert '[Start]' not in render_start(record, isolation_verified=True)


@pytest.mark.parametrize('missing', ['current', 'trial', 'judge'])
def test_missing_contestant_or_judge_price_blocks_start(tmp_path, hermes_pricing, missing):
    models = FLAT_MODELS
    hermes_pricing['openrouter'] = {
        model.model: {'prompt': '0.000001', 'completion': '0.000005', 'input_cache_read': '0.000001', 'input_cache_write': '0.000003'}
        for role, model in models.items() if role != missing
    }

    record = prepare(tmp_path, **models)
    text = render_start(record, isolation_verified=True)

    assert not record['can_start']
    assert record['estimate']['total'] is None
    assert record['estimate']['tokens'] > 0
    assert '[Start]' not in text
    assert "Shadow doesn't have a price for " in text
    assert models[missing].name in text
    assert 'tokens' in text


@pytest.mark.parametrize('availability', ['missing_module', 'missing_function', 'lookup_error', 'empty_catalog'])
def test_older_or_unavailable_hermes_preserves_direct_price_fallback(tmp_path, monkeypatch, hermes_pricing, availability):
    module = sys.modules['hermes_cli.models_pricing']
    if availability == 'missing_module':
        monkeypatch.setitem(sys.modules, 'hermes_cli.models_pricing', None)
    elif availability == 'missing_function':
        monkeypatch.delattr(module, 'get_pricing_for_provider')
    elif availability == 'lookup_error':
        def unavailable(*args, **kwargs):
            raise RuntimeError('pricing unavailable')
        monkeypatch.setattr(module, 'get_pricing_for_provider', unavailable)

    record = prepare(tmp_path)

    assert record['can_start']
    assert '[Start]' in render_start(record, isolation_verified=True)
    assert cost_usage(price_for(CURRENT), Usage(1000, 100)) == 0
    assert cost_usage(price_for(OUTSIDE), Usage(1000, 100)) == pytest.approx(0.0045)
    assert cost_usage(price_for(Model('openai', 'gpt-6.1-sol')), Usage(1000, 100)) == pytest.approx(0.003)


@pytest.mark.parametrize('provider', ['openrouter', 'fireworks', 'novita'])
@pytest.mark.parametrize('row', [
    {'prompt': '0.0', 'completion': '6e-06'},
    {'prompt': '2e-06', 'completion': '0'},
    {'prompt': '0', 'completion': '0.0'},
])
def test_zero_hermes_rate_is_unknown_and_blocks_start(tmp_path, hermes_pricing, provider, row):
    trial = Model(provider, FLAT_MODELS['trial'].model)
    hermes_pricing[provider] = {trial.model: row}

    record = prepare(tmp_path, trial=trial)

    assert price_for(trial) is None
    assert not record['can_start']
    assert record['estimate']['total'] is None
    assert '[Start]' not in render_start(record, isolation_verified=True)


@pytest.mark.parametrize('provider', ['openrouter', 'fireworks'])
@pytest.mark.parametrize('row', [
    {'prompt': 'NaN', 'completion': '0'},
    {'prompt': '0', 'completion': 'Infinity'},
    {'prompt': '-0.000001', 'completion': '0'},
    {'prompt': '0', 'completion': '-0.000001'},
    {'prompt': 'invalid', 'completion': '0'},
    {'prompt': '1e999999999', 'completion': '0'},
    {'prompt': None, 'completion': '0'},
    {'prompt': '0'},
    {'completion': '0'},
    {'prompt': '0.000002', 'completion': '0.000006', 'input_cache_read': '-1'},
    {'prompt': '0.000002', 'completion': '0.000006', 'input_cache_write': 'NaN'},
    [],
    None,
])
def test_invalid_hermes_rows_are_unknown_and_block_start(tmp_path, hermes_pricing, provider, row):
    trial = Model(provider, FLAT_MODELS['trial'].model)
    hermes_pricing[provider] = {trial.model: row}

    record = prepare(tmp_path, trial=trial)

    assert price_for(trial) is None
    assert cost_usage(price_for(trial), Usage(1000, 100)) is None
    assert not record['can_start']
    assert '[Start]' not in render_start(record, isolation_verified=True)


@pytest.mark.parametrize('priced_id,other_id', [
    ('vendor/gpt-4.1', 'gpt-4.1'),
    ('gpt-4.1-2026-11-10', 'gpt-4.1'),
    ('gpt-4.1:free', 'gpt-4.1'),
    ('gpt-4.1', 'vendor/gpt-4.1'),
])
def test_proxy_prices_keep_namespace_date_and_free_suffix_identity(hermes_pricing, priced_id, other_id):
    hermes_pricing['openrouter'] = {priced_id: {'prompt': '0.000002', 'completion': '0.000006', 'input_cache_read': '0.000001', 'input_cache_write': '0.000003'}}

    assert price_for(Model('openrouter', priced_id)) is None
    assert price_for(Model('openrouter', other_id)) is None


@pytest.mark.parametrize('provider', ['openrouter', 'nous', 'unknown-proxy'])
def test_unknown_proxy_price_never_borrows_a_direct_provider_price(hermes_pricing, provider):
    assert price_for(Model(provider, 'gpt-6.1-sol')) is None


@pytest.mark.parametrize('billing_mode', ['subscription', 'credits', None])
def test_nous_billing_mode_does_not_prove_a_zero_request_fee(tmp_path, hermes_pricing, billing_mode):
    trial = Model('nous', 'hermes-3')
    hermes_pricing['nous'] = {
        trial.model: {'prompt': '0.000002', 'completion': '0.000006',
                      'input_cache_read': '0.000001', 'input_cache_write': '0.000003',
                      'billing_mode': billing_mode}
    }
    record = prepare(tmp_path, trial=trial)
    assert price_for(trial) is None
    assert not record['can_start']
    assert record['estimate']['total'] is None


@pytest.mark.parametrize('plan', [False, True])
def test_price_snapshot_round_trip_keeps_usage_cost_and_billing_mode(plan):
    price = replace(price_for(Model('openai', 'gpt-6.1-sol')), plan=plan)
    snapshot = json.loads(json.dumps(price.record()))
    restored = Price.from_record(snapshot)

    assert restored == price
    assert restored.cost(1000, 100) == price.cost(1000, 100)
    assert restored.cost(300000, 1000, 200, 100) == price.cost(300000, 1000, 200, 100)


@pytest.mark.parametrize('role,tokens,cost', [
    ('trial', 245840, 0.81936),
    ('current', 440272, 1.239968),
    ('judge', 266280, 0.61448),
])
def test_plan_label_counts_only_plan_side_usage(tmp_path, hermes_pricing, monkeypatch, role, tokens, cost):
    from shadow.trial import engine as trial_engine

    monkeypatch.setattr(trial_engine, 'profile_tool_schemas', lambda: [])
    models = {'current': Model('openai', CURRENT.model), 'trial': Model('openai', TRIAL.model),
              'judge': OUTSIDE}
    models[role] = replace(FLAT_MODELS[role], plan=True)
    hermes_pricing['openrouter'] = {
        models[role].model: {'prompt': '0.000002', 'completion': '0.000006',
                             'input_cache_read': '0.000001', 'input_cache_write': '0.000003'}
    }
    def known_price(model):
        return (Price(*(Decimal(value) for value in ('2', '6', '1', '3')), plan=model.plan)
                if model.provider == 'openrouter' else price_for(model))
    monkeypatch.setattr('shadow.trial.usage.price_for', known_price)
    picked = selection(tmp_path, 20)
    picked = replace(picked, turns=[replace(turn, goal='abc', context='') for turn in picked.turns])
    engine = TrialEngine(FakeContext(), Store(tmp_path / 'mixed.sqlite'), None)

    record = engine.prepare(picked, models['current'], models['trial'], [models['judge']])

    metered = {key: replace(model, plan=False) for key, model in models.items()}
    baseline = engine.prepare(picked, metered['current'], metered['trial'], [metered['judge']])

    assert record['can_start']
    assert record['estimate']['total'] == pytest.approx(baseline['estimate']['total'] - cost)
    assert record['estimate']['plan_tokens'] == tokens
    assert record['estimate']['plan_cost'] == pytest.approx(cost)
    if role == 'trial':
        assert record['estimate']['contestants'] > 0
    if role == 'current':
        assert record['estimate']['controller'] == 0
    if role == 'judge':
        assert record['estimate']['judge'] == 0
    assert cost_usage(Price.from_record(record['prices'][models[role].provider][models[role].model]), Usage(1000, 100)) == 0
    assert f'your plan: {tokens:,} tokens, worth about ${cost:.2f} at list price' in render_start(record, isolation_verified=True)


@pytest.mark.parametrize('bucket', ['input_cache_read', 'input_cache_write'])
@pytest.mark.parametrize('rate', [None, '0', '0.0'])
def test_unknown_cache_rate_blocks_start(tmp_path, hermes_pricing, bucket, rate):
    trial = FLAT_MODELS['trial']
    row = {'prompt': '0.000002', 'completion': '0.000006',
           'input_cache_read': '0.000001', 'input_cache_write': '0.000003'}
    if rate is None:
        del row[bucket]
    else:
        row[bucket] = rate
    hermes_pricing['openrouter'] = {trial.model: row}

    record = prepare(tmp_path, trial=trial)

    assert not record['can_start']
    assert price_for(trial) is None
    assert '[Start]' not in render_start(record, isolation_verified=True)


@pytest.mark.parametrize('role', ['current', 'trial', 'judge'])
def test_unknown_request_fee_blocks_each_role_without_fetching_metadata(tmp_path, hermes_pricing, role):
    models = {'current': CURRENT, 'trial': TRIAL, 'judge': OUTSIDE}
    models[role] = FLAT_MODELS[role]
    model = models[role]
    hermes_pricing['openrouter'] = {model.model: {
        'prompt': '0.000002', 'completion': '0.000006',
        'input_cache_read': '0.000001', 'input_cache_write': '0.000003'}}
    record = prepare(tmp_path, **models)
    assert not record['can_start']
    assert record['estimate']['total'] is None
    assert price_for(model) is None
    assert '[Start]' not in render_start(record, isolation_verified=True)


@pytest.mark.parametrize('provider', ['openai-codex', 'openai'])
def test_direct_price_records_subscription_inclusion(provider):
    model = Model(provider, 'gpt-6.1-sol')
    price = price_for(model)
    assert price.plan is (provider == 'openai-codex')
    assert price.cost(1000, 100) == Decimal('0.003')
    assert cost_usage(price, Usage(1000, 100)) == (0 if price.plan else 0.003)


def test_hermes_subscription_route_is_frozen_for_estimate_and_spend(monkeypatch):
    from types import SimpleNamespace
    from shadow.trial.usage import resolve_prices, stored_price

    module = ModuleType('agent.usage_pricing')
    calls = []
    def resolve_billing_route(model_name, provider=None, base_url=None):
        calls.append((model_name, provider))
        return SimpleNamespace(billing_mode='subscription_included')
    module.resolve_billing_route = resolve_billing_route
    monkeypatch.setitem(sys.modules, 'agent.usage_pricing', module)
    model = Model('openai', 'gpt-6.1-sol')
    prices = resolve_prices((model,))
    assert calls == [(model.model, model.provider)]
    module.resolve_billing_route = lambda *args, **kwargs: pytest.fail('frozen billing must not be resolved again')
    price = stored_price(prices, model)
    assert price.plan
    assert cost_usage(price, Usage(3_000_000, 100)) == 0
    assert price.cost(1000, 100) == Decimal('0.003')
