import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, selection
from shadow.trial.engine import BudgetStop
from shadow.trial.judge import Judge, choose_judge
from shadow.trial.usage import UsageLedger, resolve_prices
from test_engine import setup_engine


@pytest.mark.parametrize('provider', ['', None])
def test_hook_without_answering_provider_stops_before_spend_changes(tmp_path, provider):
    _, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')

    engine.post_api_request(session_id='controller', api_request_id='missing-provider',
                            provider=provider, response_model=CURRENT.model,
                            usage={'prompt_tokens': 100, 'completion_tokens': 100})

    saved = store.get(record['id'])
    assert saved['stop_reason'] == 'unpriceable'
    assert saved['spend'] == 0
    assert saved['request_usage'][0]['cost'] is None
    assert saved['request_usage'][0]['provider'] == provider


@pytest.mark.parametrize('provider', ['', None, CURRENT.provider])
def test_replay_success_requires_observed_provider(tmp_path, provider):
    ctx, store, engine, record = setup_engine(tmp_path, 20)

    def observe(**event):
        engine.ledger.observe(**{**event, 'provider': provider})

    ctx.hooks['post_api_request'] = [observe]
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')

    saved = store.get(record['id'])
    run = saved['turns'][0]['runs']['new'][0]
    assert run['end_state'] == 'SUCCEEDED'
    assert run['ok'] == (provider == TRIAL.provider)
    if not provider:
        assert run['cost'] is None
        assert saved['stop_reason'] == 'unpriceable'
        assert not saved['turns'][0]['usable']
        assert not ctx.llm.calls
    else:
        assert run['cost'] > 0
        assert saved['turns'][0]['usable']


@pytest.mark.parametrize('provider,expected_cost', [('', None), (None, None), (CURRENT.provider, 0.0012)])
def test_ledger_requires_answering_provider(provider, expected_cost):
    ledger = UsageLedger()
    ledger.observe(session_id='child', api_request_id='response', provider=provider,
                   response_model=CURRENT.model, usage={'prompt_tokens': 100, 'completion_tokens': 100})

    usage, cost, actual = ledger.summarize('child', resolve_prices((CURRENT,)), CURRENT)

    assert usage.total_tokens == 200
    assert actual == [CURRENT.model]
    assert cost == (pytest.approx(expected_cost) if expected_cost is not None else None)


@pytest.mark.parametrize('field,value', [('provider', ''), ('provider', None), ('model', ''), ('model', None)])
def test_judge_without_answering_identity_stops_before_spend_changes(tmp_path, field, value):
    ctx, _, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    complete = ctx.llm.complete_structured

    def incomplete_response(**kwargs):
        response = complete(**kwargs)
        setattr(response, field, value)
        return response

    ctx.llm.complete_structured = incomplete_response
    judge = Judge(ctx.llm, choose_judge(CURRENT, TRIAL, [OUTSIDE]), (TRIAL, CURRENT), prices=record['prices'])

    with pytest.raises(BudgetStop):
        judge.call(selection(tmp_path, 20).turns[0], [{}, {}], after_call=engine.charge_judge)

    assert record['stop_reason'] == 'unpriceable'
    assert record['spend'] == 0
    assert len(record['usage_calls']) == 1
    assert record['usage_calls'][0]['cost'] is None
    assert record['usage_calls'][0][field] == value
    assert len(ctx.llm.calls) == 1


def test_judge_exception_holds_an_estimate_and_does_not_stop_as_unpriceable(tmp_path):
    ctx, _, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')

    def request_failed(**_):
        raise TimeoutError('request timed out')

    ctx.llm.complete_structured = request_failed
    judge = Judge(ctx.llm, choose_judge(CURRENT, TRIAL, [OUTSIDE]), (TRIAL, CURRENT), prices=record['prices'])

    with pytest.raises(ValueError, match='judge request failed'):
        judge.call(selection(tmp_path, 20).turns[0], [{}, {}], after_call=engine.charge_judge)

    call = record['usage_calls'][0]
    assert record.get('stop_reason') != 'unpriceable'
    assert call['error'] == 'request_failed'
    assert not call['provider'] and not call['model']
    assert (call['requested_provider'], call['requested_model']) == (OUTSIDE.provider, OUTSIDE.model)
    assert call['usage']['estimated'] is True
    assert call['cost'] > 0
    assert record['spend'] == pytest.approx(call['cost'])


def test_judge_exception_with_unknown_judge_price_keeps_the_unknown_price_backstop(tmp_path):
    ctx, _, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')

    def request_failed(**_):
        raise TimeoutError('request timed out')

    ctx.llm.complete_structured = request_failed
    judge = Judge(ctx.llm, choose_judge(CURRENT, TRIAL, [OUTSIDE]), (TRIAL, CURRENT), prices={})

    with pytest.raises(BudgetStop):
        judge.call(selection(tmp_path, 20).turns[0], [{}, {}], after_call=engine.charge_judge)

    assert record['stop_reason'] == 'unpriceable'
    assert record['spend'] == 0
    assert record['usage_calls'][0]['cost'] is None
