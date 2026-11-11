import sys
from dataclasses import replace
from types import ModuleType

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext, selection
from shadow.common.models import Model
from shadow.common.storage import Store
from shadow.trial.cards import render_start
from shadow.trial.engine import TrialEngine
from shadow.trial.safety import ReplayGuard
from shadow.trial.usage import UsageLedger, resolve_prices
from test_engine import setup_engine
from test_plugin import service


@pytest.mark.parametrize('response_model,model,expected_cost', [
    (None, TRIAL.model, 0.00006),
    ('', TRIAL.model, 0.00006),
    (CURRENT.model, TRIAL.model, 0.0012),
    (None, 'gpt-unpriced', None),
    (None, None, None),
    ('', '', None),
])
def test_hook_cost_uses_response_then_hermes_model_and_never_expected(
        tmp_path, response_model, model, expected_cost):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    engine.post_api_request(session_id='controller', api_request_id='request',
                            provider=CURRENT.provider, response_model=response_model, model=model,
                            usage={'prompt_tokens': 100, 'completion_tokens': 100})

    saved = store.get(record['id'])
    row = saved['request_usage'][0]
    assert row['response_model'] == (response_model or model or '')
    assert row['cost'] == (0 if expected_cost is not None else None)
    assert saved['spend'] == 0
    assert saved['stop_reason'] == ('unpriceable' if expected_cost is None else None)
    restored = TrialEngine(ctx, store, engine.guard)
    restored.attach(saved, 'controller')
    _, ledger_cost, actual = restored.ledger.summarize('controller', saved['prices'], CURRENT)
    assert ledger_cost == (pytest.approx(expected_cost) if expected_cost is not None else None)
    assert actual == [response_model or model or '']


@pytest.mark.parametrize('response_model,expected_cost', [(None, 0.00006), (CURRENT.model, 0.0012)])
def test_metered_hook_spend_uses_response_model_before_hermes_model(
        tmp_path, response_model, expected_cost):
    _, store, engine, _ = setup_engine(tmp_path, 20)
    current = Model('openai', CURRENT.model)
    trying = Model('openai', TRIAL.model)
    record = engine.prepare(selection(tmp_path, 20), current, trying, [OUTSIDE])
    engine.attach(record, 'controller')
    engine.post_api_request(session_id='controller', api_request_id='request',
                            provider='openai', response_model=response_model, model=TRIAL.model,
                            usage={'prompt_tokens': 100, 'completion_tokens': 100})

    saved = store.get(record['id'])
    assert saved['request_usage'][0]['cost'] == pytest.approx(expected_cost)
    assert saved['spend'] == pytest.approx(expected_cost)
    assert saved['stop_reason'] is None


@pytest.mark.parametrize('model,expected_cost', [(TRIAL.model, 0.00006), ('', None), (None, None)])
def test_ledger_falls_back_only_to_hook_model(model, expected_cost):
    ledger = UsageLedger()
    ledger.observe(session_id='child', api_request_id='request', model=model,
                   response_model=None, provider=CURRENT.provider,
                   usage={'prompt_tokens': 100, 'completion_tokens': 100})
    _, cost, actual = ledger.summarize('child', resolve_prices((CURRENT, TRIAL)), CURRENT)
    assert cost == (pytest.approx(expected_cost) if expected_cost is not None else None)
    assert actual == [model or '']


def test_replay_without_usage_hook_stops_before_counting_money(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    ctx.hooks['post_api_request'] = []
    engine.attach(record, 'controller')
    engine.step('controller')
    result = engine.step('controller')

    saved = store.get(record['id'])
    assert not result['continue']
    assert saved['stop_reason'] == 'unpriceable'
    assert saved['spend'] == 0
    run = saved['turns'][0]['runs']['new'][0]
    assert run['cost'] is None and run['usage']['estimated']
    assert len(ctx.subagent_lifecycle.launches) == 2
    assert not ctx.llm.calls


def test_unidentified_replay_stops_before_counting_money(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    ctx.subagent_lifecycle.start_hook = False
    ctx.hooks['pre_llm_call'] = []
    engine.attach(record, 'controller')
    result = engine.step('controller')

    saved = store.get(record['id'])
    assert not result['continue']
    assert saved['stop_reason'] == 'unpriceable'
    assert saved['spend'] == 0
    assert saved['turns'][0]['runs']['new'][0]['cost'] is None
    assert len(ctx.subagent_lifecycle.launches) == 1
    assert ctx.subagent_lifecycle.cancelled == ['child1']
    assert not ctx.llm.calls


@pytest.mark.parametrize('provider', ['nous', 'kilocode', 'ai-gateway', 'novita', 'deepinfra', 'fireworks'])
@pytest.mark.parametrize('role', ['current', 'trial', 'judge'])
def test_unknown_catalog_fee_blocks_start_even_with_all_token_rates(tmp_path, monkeypatch, provider, role):
    models = {'current': CURRENT, 'trial': TRIAL, 'judge': OUTSIDE}
    models[role] = replace(models[role], provider=provider)
    module = ModuleType('hermes_cli.models_pricing')
    module.get_pricing_for_provider = lambda name, **_: {
        models[role].model: {'prompt': '0.000002', 'completion': '0.000006',
                             'input_cache_read': '0.000001', 'input_cache_write': '0.000003',
                             'billing_mode': 'subscription'}
    } if name == provider else {}
    package = ModuleType('hermes_cli')
    package.__path__ = []
    monkeypatch.setitem(sys.modules, 'hermes_cli', package)
    monkeypatch.setitem(sys.modules, 'hermes_cli.models_pricing', module)
    ctx = FakeContext()
    engine = TrialEngine(ctx, Store(tmp_path / 'fees.sqlite'), ReplayGuard())

    record = engine.prepare(selection(tmp_path, 20), models['current'], models['trial'], [models['judge']])

    assert not record['can_start']
    assert record['estimate']['total'] is None
    assert '[Start]' not in render_start(record, isolation_verified=True)
    with pytest.raises(ValueError, match='cannot start'):
        engine.attach(record, 'controller')
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls


def test_pricing_lookup_reads_only_hermes_local_catalog(tmp_path, monkeypatch):
    calls = []
    module = ModuleType('hermes_cli.models_pricing')

    def lookup(provider, *, cached_only=False):
        calls.append(cached_only)
        return {}

    module.get_pricing_for_provider = lookup
    package = ModuleType('hermes_cli')
    package.__path__ = []
    monkeypatch.setitem(sys.modules, 'hermes_cli', package)
    monkeypatch.setitem(sys.modules, 'hermes_cli.models_pricing', module)
    engine = TrialEngine(FakeContext(), Store(tmp_path / 'local.sqlite'), ReplayGuard())
    record = engine.prepare(selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    assert record['can_start']
    assert calls and all(calls)


@pytest.mark.parametrize('section', ['agent', 'delegation'])
@pytest.mark.parametrize('tier', ['fast', 'priority', 'ultrafast', 'auto', 'cold', 'unknown'])
def test_profile_non_normal_service_tier_blocks_start(tmp_path, section, tier):
    ctx, plugin = service(tmp_path)
    plugin.config_reader = lambda: {section: {'service_tier': tier},
                                     'delegation': {'inherit_mcp_toolsets': False, **(
                                         {'service_tier': tier} if section == 'delegation' else {})}}
    ctx.answers = ['cancel']
    ctx.commands['shadow']('try gpt-6-luna --turns 20', session_id='origin')
    record = plugin.store.get()
    assert not record['can_start']
    assert record['estimate']['total'] is None
    assert '[Start]' not in render_start(record, isolation_verified=True)
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls


@pytest.mark.parametrize('tier', [None, '', 'normal', ' NORMAL ', 'default', 'standard', 'off', 'none'])
def test_profile_normal_service_tier_remains_priced(tmp_path, tier):
    ctx, plugin = service(tmp_path)
    plugin.config_reader = lambda: {'agent': {'service_tier': tier},
                                     'delegation': {'service_tier': tier, 'inherit_mcp_toolsets': False}}
    ctx.answers = ['cancel']
    ctx.commands['shadow']('try gpt-6-luna --turns 20', session_id='origin')
    record = plugin.store.get()
    assert record['can_start']
    assert record['estimate']['total'] > 0
