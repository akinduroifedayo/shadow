import json

import pytest

from fixtures import OUTSIDE, FakeContext
from shadow.common.copy import load_copy
from shadow.desktop_rpc import Copy, _receipt
from shadow.trial.cards import render_start, render_verdict
from shadow.trial.engine import TrialEngine
from shadow.trial.safety import ReplayGuard
from test_plugin import service
from test_trial_pricing_snapshot import CURRENT, TRIAL, fail_lookup, prepared_engine, pricing


@pytest.mark.parametrize('model,requests', [(CURRENT, 142), (TRIAL, 80), (OUTSIDE, 40)])
def test_start_estimate_includes_known_request_fees(tmp_path, pricing, model, requests):
    _, _, baseline = prepared_engine(tmp_path, pricing)
    pricing[0].request_fees[model.model] = '0.01'

    _, engine, record = prepared_engine(tmp_path, pricing)

    expected = baseline['estimate']['total'] + requests * .01
    assert record['can_start']
    assert record['estimate']['total'] == pytest.approx(expected)
    assert f'About ${expected:.2f}' in render_start(record, isolation_verified=True)
    snapshot = engine.store.get(record['id'])['prices']
    assert snapshot['openrouter'][model.model]['request_cost'] == '0.01'


@pytest.mark.parametrize('model', [CURRENT, TRIAL, OUTSIDE])
def test_request_fees_can_put_start_over_the_cap(tmp_path, pricing, model):
    pricing[0].request_fees[model.model] = '0.2'

    _, _, record = prepared_engine(tmp_path, pricing)

    text = render_start(record, isolation_verified=True)
    assert not record['can_start']
    assert record['estimate']['total'] > record['cap']
    assert '[Start]' not in text
    assert 'raise the cap:' in text


def test_frozen_fees_reach_cli_and_desktop_verdict_costs(tmp_path, pricing):
    fees = pricing[0].request_fees
    for model, fee in ((CURRENT, '0.001'), (TRIAL, '0.002'), (OUTSIDE, '0.003')):
        fees[model.model] = fee
    ctx, engine, record = prepared_engine(tmp_path, pricing)
    assert record['can_start']
    frozen = engine.store.get(record['id'])
    fees.clear()
    fail_lookup(pricing[0])
    engine.attach(frozen, 'controller')
    for _ in range(100):
        if not engine.step('controller')['continue']:
            break

    saved = engine.store.get(record['id'])
    assert saved['totals']['pairs'] == 20
    assert saved['spend'] == pytest.approx(.232)
    receipt = saved['totals']['receipt']
    assert receipt['cost_per_turn_new'] == pytest.approx(.0028)
    assert receipt['cost_per_turn_yours'] == pytest.approx(.0018)
    assert '$0.003 vs $0.002' in render_verdict(saved, cli=True)
    desktop_cost = next(cell for cell in _receipt(Copy(load_copy()), receipt, saved['totals'])
                        if cell['key'] == 'cost')
    assert '$0.003' in json.dumps(desktop_cost['value'])
    assert '$0.002' in json.dumps(desktop_cost['value'])


def test_request_fee_is_charged_once_per_request_across_hook_updates_and_reload(tmp_path, pricing):
    pricing[0].request_fees[CURRENT.model] = '0.01'
    _, engine, record = prepared_engine(tmp_path, pricing)
    assert record['can_start']
    engine.attach(record, 'controller')
    request = dict(session_id='controller', api_request_id='one', provider='openrouter',
                   response_model=CURRENT.model, usage={'prompt_tokens': 1000, 'completion_tokens': 100})
    engine.post_api_request(**request)
    engine.post_api_request(**request)
    restored = TrialEngine(FakeContext(), engine.store, ReplayGuard())
    restored.attach(engine.store.get(record['id']), 'controller')
    restored.post_api_request(**request)
    restored.post_api_request(**{**request, 'api_request_id': 'two'})

    saved = engine.store.get(record['id'])
    assert saved['spend'] == pytest.approx(.0252)
    assert [row['cost'] for row in saved['request_usage']] == pytest.approx([.0126, .0126])


def test_request_fee_trips_runtime_cap_before_another_replay(tmp_path, pricing):
    pricing[0].request_fees[CURRENT.model] = '0.01'
    ctx, engine, record = prepared_engine(tmp_path, pricing)
    assert record['can_start']
    engine.attach(record, 'controller')
    engine.record['spend'] = record['cap'] - .005
    engine.post_api_request(session_id='controller', api_request_id='last', provider='openrouter',
                            response_model=CURRENT.model, usage={'prompt_tokens': 10})

    assert not engine.step('controller')['continue']
    saved = engine.store.get(record['id'])
    assert saved['stop_reason'] == 'cap'
    assert saved['spend'] == pytest.approx(5.00502)
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls


def test_desktop_start_cost_includes_request_fees(tmp_path, pricing):
    from shadow.common.models import Model
    from shadow.desktop_rpc import build_start
    from shadow.desktop_source import ProfileSource

    _, plugin = service(tmp_path)
    plugin.current = CURRENT
    plugin.catalog = [CURRENT, TRIAL, Model('openrouter', OUTSIDE.model)]
    source = ProfileSource(plugin)
    baseline = source.start_facts(TRIAL.model)
    pricing[0].request_fees[CURRENT.model] = '0.001'

    facts = source.start_facts(TRIAL.model)
    card = build_start(Copy(load_copy()), source, TRIAL.model)

    assert facts['replay_estimate'] == pytest.approx(baseline['replay_estimate'] + .212)
    cost = next(row for row in card['rows'] if row['key'] == 'cost')
    total = facts['replay_estimate'] + facts['judge_models'][0]['cost']
    assert f'About ${total:.2f}' in json.dumps(cost['value'])


def test_old_snapshot_without_request_fee_keeps_its_token_cost(tmp_path, pricing):
    _, engine, record = prepared_engine(tmp_path, pricing)
    for catalog in record['prices'].values():
        for price in catalog.values():
            price.pop('request_cost', None)
    engine.store.save(record)
    fail_lookup(pricing[0])
    engine.attach(engine.store.get(record['id']), 'controller')
    engine.post_api_request(session_id='controller', api_request_id='legacy', provider='openrouter',
                            response_model=CURRENT.model, usage={'prompt_tokens': 1000, 'completion_tokens': 100})

    assert engine.store.get(record['id'])['spend'] == pytest.approx(.0026)
