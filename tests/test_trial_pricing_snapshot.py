from collections import Counter
from decimal import Decimal
from types import SimpleNamespace
import sys

import pytest

from fixtures import OUTSIDE, FakeContext, selection
from shadow.common.models import Model
from shadow.common.storage import Store
from shadow.trial.cards import render_start
from shadow.trial.engine import TrialEngine
from shadow.trial.safety import ReplayGuard


CURRENT = Model('openrouter', 'gpt-4.1')
TRIAL = Model('openrouter', 'gpt-4.1-mini')


@pytest.fixture
def pricing(monkeypatch):
    calls = []
    catalog = {model.model: {'prompt': '0.000002', 'completion': '0.000006', 'input_cache_read': '0.000001', 'input_cache_write': '0.000003'}
               for model in (CURRENT, TRIAL, OUTSIDE)}
    module = SimpleNamespace(request_fees={})

    def lookup(provider, **kwargs):
        calls.append(provider)
        return catalog if provider == 'openrouter' else {}

    module.get_pricing_for_provider = lookup
    def entry(model, *, provider):
        assert provider == 'openrouter'
        return SimpleNamespace(request_cost=Decimal(module.request_fees[model])
                               if model in module.request_fees else None) if model in catalog else None

    module.get_pricing_entry = entry
    monkeypatch.setitem(sys.modules, 'hermes_cli.models_pricing', module)
    monkeypatch.setitem(sys.modules, 'agent.usage_pricing', module)
    return module, catalog, calls


def prepared_engine(tmp_path, pricing, *, unknown=False):
    ctx, guard = FakeContext(), ReplayGuard()
    store = Store(tmp_path / 'snapshot.sqlite')
    engine = TrialEngine(ctx, store, guard)
    for name, callback in [('subagent_start', guard.subagent_start), ('pre_llm_call', guard.pre_llm_call),
                           ('post_api_request', engine.post_api_request), ('subagent_stop', engine.subagent_stop)]:
        ctx.register_hook(name, callback)
    current, trial, judge = (Model('openrouter', m.model) for m in (CURRENT, TRIAL, OUTSIDE))
    if unknown:
        pricing[1].pop(trial.model)
    record = engine.prepare(selection(tmp_path, 20), current, trial, [judge])
    return ctx, engine, record


def fail_lookup(module):
    def unavailable(*args, **kwargs):
        raise AssertionError('Hermes pricing must not run after prepare')
    module.get_pricing_for_provider = unavailable
    module.get_pricing_entry = unavailable


def test_prepare_resolves_each_model_once(pricing, tmp_path):
    _, _, record = prepared_engine(tmp_path, pricing)
    assert record['can_start']
    assert Counter(pricing[2]) == {'openrouter': 3}


@pytest.mark.parametrize('dated_response', [False, True])
def test_trial_finishes_when_hermes_pricing_fails_after_start(pricing, tmp_path, dated_response):
    ctx, engine, record = prepared_engine(tmp_path, pricing)
    engine.attach(record, 'controller')
    fail_lookup(pricing[0])
    if dated_response:
        original_launch = ctx.subagent_lifecycle.launch

        def launch(request):
            ctx.subagent_lifecycle.actual_model = request['model'] + '-20261101'
            return original_launch(request)

        ctx.subagent_lifecycle.launch = launch
        original_judge = ctx.llm.complete_structured

        def judge(**kwargs):
            response = original_judge(**kwargs)
            response.model += '-20261101'
            return response

        ctx.llm.complete_structured = judge
    for _ in range(100):
        if not engine.step('controller')['continue']:
            break
    saved = engine.store.get(record['id'])
    assert saved['state'] == 'finished'
    assert saved['stop_reason'] is None
    assert saved['totals']['pairs'] == 20
    assert saved['spend'] > 0
    assert len(ctx.llm.calls) == 40


def test_persisted_preparation_keeps_its_prices_after_reload(pricing, tmp_path):
    _, engine, record = prepared_engine(tmp_path, pricing)
    record = engine.store.get(record['id'])
    fail_lookup(pricing[0])
    resumed = TrialEngine(FakeContext(), engine.store, ReplayGuard())
    resumed.attach(record, 'controller')
    resumed.post_api_request(session_id='controller', api_request_id='controller-request',
                             provider='openrouter', response_model=CURRENT.model,
                             usage={'prompt_tokens': 1000, 'completion_tokens': 100})
    assert record['stop_reason'] is None
    assert record['spend'] == pytest.approx(0.0026)


def test_unpriced_start_card_uses_prepared_missing_models(pricing, tmp_path):
    _, _, record = prepared_engine(tmp_path, pricing, unknown=True)
    calls = len(pricing[2])
    pricing[1][TRIAL.model] = {'prompt': '0.000002', 'completion': '0.000006', 'input_cache_read': '0.000001', 'input_cache_write': '0.000003'}
    text = render_start(record, isolation_verified=True)
    assert "Shadow doesn't have a price for " + TRIAL.name in text
    assert '[Start]' not in text
    assert len(pricing[2]) == calls


def test_desktop_unpriced_card_does_not_lookup_live_prices(pricing, monkeypatch):
    from shadow import desktop_rpc as desktop
    from shadow.common.copy import load_copy

    source = desktop.FixtureSource('empty')
    facts = source.start_facts(source.world['start']['try'])
    facts.update(priced=False, tokens=12345, missing_price_names=['Prepared missing model'])
    monkeypatch.setattr(source, 'start_facts', lambda _: facts)
    card = desktop.build_start(desktop.Copy(load_copy()), source, facts['trial']['model'])
    assert 'Prepared missing model' in card['blocked']
    assert not any(button['id'] == 'start' for button in card['buttons'])
    assert pricing[2] == []


def test_replay_outside_frozen_table_stops_before_charging(pricing, tmp_path):
    ctx, engine, record = prepared_engine(tmp_path, pricing)
    engine.attach(record, 'controller')
    fail_lookup(pricing[0])
    ctx.subagent_lifecycle.actual_model = 'unpriced-fallback'

    result = engine.step('controller')

    saved = engine.store.get(record['id'])
    assert not result['continue']
    assert saved['stop_reason'] == 'unpriceable'
    assert saved['spend'] == 0
    assert saved['request_usage'][0]['cost'] is None
    assert len(ctx.subagent_lifecycle.launches) == 1


@pytest.mark.parametrize('provider,model,cost', [
    ('openrouter', OUTSIDE.model, 0.0008),
    ('openrouter', OUTSIDE.model + '-20261101', 0.0008),
    ('other-provider', TRIAL.model, None),
])
def test_controller_costs_the_reported_route(pricing, tmp_path, provider, model, cost):
    pricing[1][OUTSIDE.model] = {'prompt': '0.000007', 'completion': '0.000009', 'input_cache_read': '0.000001', 'input_cache_write': '0.000003'}
    _, engine, record = prepared_engine(tmp_path, pricing)
    engine.attach(record, 'controller')
    fail_lookup(pricing[0])

    engine.post_api_request(session_id='controller', api_request_id='r', provider=provider,
                            response_model=model, usage={'prompt_tokens': 50, 'completion_tokens': 50})

    assert record['request_usage'][0]['cost'] == (pytest.approx(cost) if cost is not None else None)
    assert record['spend'] == pytest.approx(cost or 0)
    assert record['stop_reason'] == ('unpriceable' if cost is None else None)


def test_replay_ledger_uses_each_rows_reported_route(pricing, tmp_path):
    pricing[1][OUTSIDE.model] = {'prompt': '0.000007', 'completion': '0.000009', 'input_cache_read': '0.000001', 'input_cache_write': '0.000003'}
    ctx, engine, record = prepared_engine(tmp_path, pricing)
    engine.attach(record, 'controller')
    ctx.subagent_lifecycle.actual_model = OUTSIDE.model
    engine.step('controller')
    engine.step('controller')

    run = record['turns'][0]['runs']['new'][0]
    assert run['cost'] == pytest.approx(0.0016)
    assert not run['ok']


@pytest.mark.parametrize('state', ['prepared', 'running', 'interrupted'])
def test_legacy_record_without_prices_refuses_attach_and_saves(pricing, tmp_path, state):
    ctx, engine, record = prepared_engine(tmp_path, pricing)
    record.pop('prices')
    record.update(state=state, spend=0.42)
    engine.store.save(record)
    legacy = engine.store.get(record['id'])
    fail_lookup(pricing[0])
    resumed = TrialEngine(ctx, engine.store, ReplayGuard())

    with pytest.raises(ValueError, match='price'):
        resumed.attach(legacy, 'controller')

    saved = engine.store.get(record['id'])
    assert saved['stop_reason'] == 'unpriceable'
    assert not saved['can_start']
    assert saved['state'] == 'finished'
    assert saved['spend'] == 0.42
    assert 'prices' not in saved
    assert not resumed.guard.controllers
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls
