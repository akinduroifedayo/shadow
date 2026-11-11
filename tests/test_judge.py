import json
from types import SimpleNamespace

import pytest

from shadow.common.models import Model, maker_of
from shadow.trial.judge import Judge, choose_judge, judge_options
from shadow.trial.picking import Turn
from shadow.trial.usage import resolve_prices

CURRENT = Model('openai-codex', 'gpt-6.1-sol', plan=True)
TRIAL = Model('openai-codex', 'gpt-6-luna', plan=True)
OUTSIDE = Model('anthropic', 'claude-sonnet-4-5')


class FakeLLM:
    def __init__(self, picks=('1','2'), route=None, malformed=False):
        self.calls = []
        self.picks = iter(picks)
        self.route = route
        self.malformed = malformed
    def complete_structured(self, **kw):
        self.calls.append(kw)
        parsed = {'pick': next(self.picks), 'fixes_1': 'none', 'fixes_2': 'minor', 'reason_kind': 'missed_request', 'reason': 'One answer missed a requested step.'}
        return SimpleNamespace(provider=self.route.provider if self.route else kw['provider'], model=self.route.model if self.route else kw['model'], parsed={} if self.malformed else parsed, text='fixture judgment', usage={'input_tokens':100,'output_tokens':50})


def test_user_pick_and_maker_not_provider():
    assert maker_of('openai/gpt-6.1-sol') == 'openai'
    assert maker_of('anthropic/claude-sonnet-4-5') == 'anthropic'
    proxy_judge = Model('multi-maker', 'anthropic/claude-sonnet-4-5')
    choice = choose_judge(CURRENT, TRIAL, [CURRENT, TRIAL, proxy_judge], proxy_judge.model)
    assert choice.models == (proxy_judge,) and choice.source == 'user'
    assert judge_options(CURRENT, TRIAL, [CURRENT, TRIAL, proxy_judge]) == [proxy_judge]
    with pytest.raises(ValueError, match="can't be one"):
        choose_judge(CURRENT, TRIAL, [OUTSIDE], CURRENT.model)
    with pytest.raises(ValueError, match='maker'):
        choose_judge(CURRENT, TRIAL, [Model('proxy','gpt-4.1')], 'gpt-4.1')


def test_qualified_judge_name_keeps_its_exact_namespace():
    first = Model('proxy', 'one/claude-sonnet-4-5')
    selected = Model('proxy', 'two/claude-sonnet-4-5')
    choice = choose_judge(CURRENT, TRIAL, [first, selected], 'proxy::two/claude-sonnet-4-5')
    assert choice.models == (selected,) and choice.source == 'user'


def test_unknown_aggregator_makers_can_supply_an_independent_named_judge():
    current = Model('openrouter', 'first-lab/new-model-large')
    trial = Model('openrouter', 'second-lab/new-model-small')
    outside = Model('openrouter', 'third-lab/independent-model')
    choice = choose_judge(current, trial, [current, trial, outside], outside.model)
    assert choice.mode == 'third_maker' and choice.source == 'user'
    assert choice.models == (outside,)
    assert judge_options(current, trial, [current, trial, outside]) == [outside]


def test_unknown_aggregator_maker_still_refuses_a_same_maker_judge():
    current = Model('openrouter', 'first-lab/new-model-large')
    trial = Model('openrouter', 'second-lab/new-model-small')
    same_maker = Model('openrouter', 'first-lab/other-model')
    with pytest.raises(ValueError, match='first-lab, like'):
        choose_judge(current, trial, [same_maker], same_maker.model)


@pytest.mark.parametrize('unknown_role', ['current', 'trial', 'judge'])
def test_unqualified_unknown_makers_cannot_authorize_an_independent_judge(unknown_role):
    models = {'current': CURRENT, 'trial': TRIAL, 'judge': OUTSIDE}
    models[unknown_role] = Model('proxy', 'unqualified-unknown')
    with pytest.raises(ValueError, match='maker'):
        choose_judge(models['current'], models['trial'], [models['judge']], models['judge'].model)


def test_blind_prompt_no_names_no_original_answer_opposite_orders():
    llm = FakeLLM()
    judge = Judge(llm, choose_judge(CURRENT, TRIAL, [OUTSIDE]), (TRIAL,CURRENT), prices=resolve_prices((CURRENT, TRIAL, OUTSIDE)))
    turn = Turn('turn', 'chat', 'cli', '/', 'Please fix the code.', '[]')
    answers = [{'text': TRIAL.model + ' first answer', 'tool_calls': []}, {'text': CURRENT.model + ' second answer', 'tool_calls': []}]
    result = judge.call(turn, answers)
    prompts = [json.loads(c['input'][0]['text']) for c in llm.calls]
    assert prompts[0]['answers'] == list(reversed(prompts[1]['answers']))
    assert result['result'] == 'won' and result['agree']
    for c in llm.calls:
        prompt = c['input'][0]['text'] + c['instructions']
        assert CURRENT.model not in prompt and TRIAL.model not in prompt and OUTSIDE.model not in prompt
        assert 'original_answer' not in prompt


def test_fallback_counts_disagreements_as_same():
    choice = choose_judge(CURRENT, TRIAL, [])
    llm = FakeLLM(('1','2','2','1'))
    result = Judge(llm, choice, (TRIAL,CURRENT), prices=resolve_prices((CURRENT, TRIAL, OUTSIDE))).call(Turn('t','c','cli','/','a request','[]'), [{'text':'first'},{'text':'second'}])
    assert choice.mode == 'backup_both' and choice.source == 'auto'
    assert len(llm.calls) == 4
    assert result['result'] == 'same' and result['disagreement']
    assert all(decision['agree'] for decision in result['decisions'])
    assert result['agree']


@pytest.mark.parametrize('malformed,route', [(True,None),(False,CURRENT)])
def test_unusable_or_wrong_identity_rejected(malformed,route):
    with pytest.raises(ValueError):
        Judge(FakeLLM(route=route, malformed=malformed), choose_judge(CURRENT,TRIAL,[OUTSIDE]), (TRIAL,CURRENT), prices=resolve_prices((CURRENT, TRIAL, OUTSIDE))).call(Turn('t','c','cli','/','request','[]'), [{},{}])


def test_unreachable_named_judge_falls_back_before_start():
    unavailable = Model('anthropic', 'claude-opus-4-6', reachable=False)
    choice = choose_judge(CURRENT, TRIAL, [unavailable, OUTSIDE], unavailable.model)
    assert choice.models == (OUTSIDE,) and choice.source == 'auto'
    assert choice.notice == "Claude Opus 4.6 can't be reached. Judged blind by Claude Sonnet 4.5 (auto) instead."


def test_unknown_judge_lists_only_eligible_models():
    with pytest.raises(ValueError) as error:
        choose_judge(CURRENT, TRIAL, [CURRENT, TRIAL, OUTSIDE], 'unknown')
    assert str(error.value) == f'No model called unknown on your providers. Judges you can pick: {OUTSIDE.model}'


def test_invalid_reason_and_missing_fixes_are_refused():
    from shadow.trial.judge import validate_decision
    with pytest.raises(ValueError):
        validate_decision({'pick': '1'})
    with pytest.raises(ValueError):
        validate_decision({'pick': 'same', 'fixes_1': 'none', 'fixes_2': 'none', 'reason_kind': 'other', 'reason': 'Unapproved reason.'})


def test_dated_judge_response_preserves_actual_id_and_price():
    dated = Model(OUTSIDE.provider, OUTSIDE.model + '-20261101')
    llm = FakeLLM(route=dated)
    result = Judge(llm, choose_judge(CURRENT, TRIAL, [OUTSIDE]), (TRIAL, CURRENT), prices=resolve_prices((CURRENT, TRIAL, OUTSIDE))).call(
        Turn('t', 'c', 'cli', '/', 'Review the code.', '[]'), [{}, {}])
    assert result['result'] == 'won'
    assert all(call['cost'] > 0 and call['model'] == dated.model for call in result['calls'])


def test_unknown_snapshot_alias_does_not_gain_a_price_or_verified_identity():
    from shadow.common.models import price_for
    unknown = Model(OUTSIDE.provider, OUTSIDE.model + '-unverified')
    assert price_for(unknown) is None
    with pytest.raises(ValueError, match='route changed'):
        Judge(FakeLLM(route=unknown), choose_judge(CURRENT, TRIAL, [OUTSIDE]), (TRIAL, CURRENT), prices=resolve_prices((CURRENT, TRIAL, OUTSIDE))).call(
            Turn('t', 'c', 'cli', '/', 'Review the code.', '[]'), [{}, {}])


def test_blinding_hides_makers_and_families_in_all_judge_input_fields():
    names = 'Claude GPT Anthropic OpenAI DeepSeek Google Gemini Nous Research Qwen Alibaba Moonshot Kimi Meta Llama Mistral'
    llm = FakeLLM()
    Judge(llm, choose_judge(CURRENT, TRIAL, [OUTSIDE]), (TRIAL, CURRENT), prices=resolve_prices((CURRENT, TRIAL, OUTSIDE))).call(
        Turn('t', 'c', 'cli', '/', names, json.dumps([{'content': names}])),
        [{'text': names, 'tool_calls': [{'args': {'path': names}}]}, {'text': names}])
    for call in llm.calls:
        prompt = call['input'][0]['text'].lower()
        for name in names.split():
            assert name.lower() not in prompt


@pytest.mark.parametrize('picks,result', [(('1', 'same'), 'won'), (('same', '2'), 'won'),
                                         (('2', 'same'), 'lost'), (('same', '1'), 'lost'),
                                         (('1', '1'), 'same'), (('same', 'same'), 'same')])
def test_swap_resolution_distinguishes_votes_from_consistency(picks, result):
    llm = FakeLLM(picks)
    judgment = Judge(llm, choose_judge(CURRENT, TRIAL, [OUTSIDE]), (TRIAL, CURRENT), prices=resolve_prices((CURRENT, TRIAL, OUTSIDE))).call(
        Turn('t', 'c', 'cli', '/', 'Review the code.', '[]'), [{}, {}])
    assert judgment['result'] == result
    assert judgment['decisions'][0]['agree'] is (picks == ('same', 'same'))


@pytest.mark.parametrize('route', [CURRENT, Model('anthropic', 'unpriced-fallback')])
def test_judge_charges_reported_route_before_rejecting_identity(route):
    from shadow.trial.usage import Usage, cost_usage, stored_price

    prices = resolve_prices((CURRENT, TRIAL, OUTSIDE))
    calls = []
    with pytest.raises(ValueError, match='route changed'):
        Judge(FakeLLM(route=route), choose_judge(CURRENT, TRIAL, [OUTSIDE]), (TRIAL, CURRENT), prices=prices).call(
            Turn('t', 'c', 'cli', '/', 'Review the code.', '[]'), [{}, {}], after_call=calls.append)

    assert len(calls) == 1
    assert calls[0]['cost'] == cost_usage(stored_price(prices, route), Usage(100, 50))
