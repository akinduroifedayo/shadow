from copy import deepcopy

import pytest

from shadow import desktop_rpc as d
from shadow.common.copy import load_copy


COPY = load_copy()


def result_source():
    return d.FixtureSource('result-better')


@pytest.mark.parametrize('reason', ['judge_unsure', 'runs_varied', 'judge_ineligible'])
def test_not_enough_with_thirty_pairs_explains_the_recorded_reason(reason):
    source = result_source()
    record = source.world['tests'][0]
    record['totals'].update(word='not_enough', cant_call_reason=reason, judge_agree=20, varied=6)
    record['totals']['receipt']['counts_readable'] = 'no'
    view = d.DesktopRpc(source, COPY).page_state({})['verdict']
    assert view['sure'] == view['reason']
    assert 'Too few turns' not in view['sure']
    if reason == 'judge_unsure':
        assert '20 of 30' in view['sure'] and '7 in 10' in view['sure']


def test_default_opens_latest_verdict_but_stopped_history_remains_selectable():
    source = result_source()
    result = source.world['tests'][0]
    stopped = deepcopy(result)
    stopped.update(id='stopped', state='cancelled', created_at='2026-11-11T09:00:00', turns=[])
    stopped.pop('totals')
    source.world['tests'].append(stopped)
    rpc = d.DesktopRpc(source, COPY)
    assert rpc.page_state({})['test_id'] == result['id']
    assert rpc.page_state({'test_id': 'stopped'})['mode'] == 'stopped'


def test_active_test_takes_priority_over_a_saved_verdict():
    source = result_source()
    running = deepcopy(source.world['tests'][0])
    running.update(id='active', state='running', created_at='2026-11-11T09:00:00')
    running.pop('totals')
    source.world['tests'].append(running)
    assert d.DesktopRpc(source, COPY).page_state({})['test_id'] == 'active'


def test_default_picks_latest_verdict_from_store_order():
    source = result_source()
    older = source.world['tests'][0]
    newer = deepcopy(older)
    newer.update(id='latest', created_at='2026-11-11T09:00:00')
    source.tests = lambda: [older, newer]
    assert d.DesktopRpc(source, COPY).page_state({})['test_id'] == 'latest'


def test_history_labels_distinguish_tests_with_time_judge_and_verdict():
    source = result_source()
    first = source.world['tests'][0]
    first['created_at'] = '2026-11-11T09:01:00'
    second = deepcopy(first)
    second.update(id='second', created_at='2026-11-11T09:02:00')
    second['totals']['word'] = 'worse'
    source.world['tests'].append(second)
    options = d.DesktopRpc(source, COPY).page_state({})['top']['picker']['options']
    assert len({row['label'] for row in options}) == 2
    labels = {row['id']: row['label'] for row in options}
    assert '9:01 AM' in labels[first['id']] and '9:02 AM' in labels['second']
    assert 'DeepSeek' in labels[first['id']]
    assert 'Better' in labels[first['id']] and 'Worse' in labels['second']


@pytest.mark.parametrize('method,params,key', [
    ('start.card', {'model': 'missing'}, 'trial.model_unknown'),
    ('turn.open', {'test_id': 'missing', 'turn_id': 'missing'}, 'trial.not_found'),
    ('settings.set', {'web_lookups': 'yes'}, 'trial.invalid'),
])
def test_registered_rpc_errors_return_readable_keyed_copy(method, params, key):
    rpc = d.DesktopRpc(result_source(), COPY)
    assert rpc.methods()[method](params, session_id=None)['error']['message'] == COPY[key]


@pytest.mark.parametrize('failure', [OSError, RuntimeError])
def test_rpc_does_not_expose_internal_error_details(failure, caplog):
    source = result_source()
    source.start_facts = lambda model: (_ for _ in ()).throw(failure('/private/profile/path'))
    reply = d.DesktopRpc(source, COPY).methods()['start.card']({'model': 'anything'})
    assert reply == {'error': {'message': COPY['trial.unavailable']}}
    entry, = caplog.records
    assert entry.name == 'shadow.desktop_rpc'
    assert entry.getMessage() == 'shadow desktop rpc start.card failed'
    assert entry.args == ('start.card',)
    assert entry.exc_info[0] is failure


@pytest.mark.parametrize('key,fields', [
    ('judge.unknown', {'name': 'local-fixture', 'slugs': 'other-fixture'}),
    ('judge.refused_same_maker', {'model': 'local-fixture', 'maker': 'OpenAI', 'like': 'your model'}),
    ('judge.maker_unknown', {}),
])
def test_rpc_preserves_trial_value_errors_with_formatted_user_copy(key, fields):
    source = result_source()
    message = COPY[key].format(**fields)

    def fail(action, params):
        raise ValueError(message)

    source.act = fail
    reply = d.DesktopRpc(source, COPY).methods()['act']({'action': 'keep'})
    assert reply == {'error': {'message': message}}


def test_start_card_names_plan_tokens_without_starting():
    source = result_source()
    original = source.start_facts
    source.start_facts = lambda model: {**original(model), 'plan_tokens': 12000, 'plan_cost': 1.25}
    card = d.DesktopRpc(source, COPY).start_card({'model': source.world['start']['try']})
    cost = next(row for row in card['rows'] if row['key'] == 'cost')['value']
    assert ('your plan: 12,000 tokens, worth about $1.25 at list price'
            in ''.join(part for part in cost if isinstance(part, str)))
    assert source.world['tests'][0]['state'] == 'finished'


@pytest.mark.parametrize('web_off', [False, True])
def test_shown_prompts_strip_only_the_complete_replay_suffix(web_off):
    source = result_source()
    record = source.world['tests'][0]
    turn = record['turns'][0]
    original = 'Explain this identifier ' + 'a' * 32
    suffix = '\nNobody is available to answer follow-up questions.'
    if web_off:
        suffix += '\n' + COPY['trial.tools_unavailable']
    turn.update(goal=original + suffix + '\n' + 'b' * 32,
                first_line=original + suffix + '\n' + 'b' * 32)
    rpc = d.DesktopRpc(source, COPY)
    assert rpc.turn_open({'test_id': record['id'], 'turn_id': turn['id']})['ask'] == original
    rows = rpc.page_state({})['turns']['groups']
    row = next(row for group in rows for row in group['rows'] if row['id'] == turn['id'])
    assert row['text'] == original
    assert turn['goal'].endswith('b' * 32)


def test_shown_prompts_keep_user_hex_and_follow_up_prose():
    source = result_source()
    record = source.world['tests'][0]
    turn = record['turns'][0]
    original = 'Nobody is available to answer follow-up questions.\n' + 'c' * 32
    turn['goal'] = original
    assert d.DesktopRpc(source, COPY).turn_open({'test_id': record['id'], 'turn_id': turn['id']})['ask'] == original
