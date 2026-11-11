"""Render records from this repo's trial and switch packages."""
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from fixtures import FakeContext
from shadow import desktop_rpc as d
from shadow.common import models
from shadow.switch import domain
from shadow.trial import engine, picking, verdict

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = Path(__file__).parent / 'fixtures/desktop/copy'
COPY = d.fixture_copy((SNAPSHOT,))


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
@pytest.mark.parametrize('tool,args', [
    ('terminal', {'command': 'touch should-not-exist'}),
    ('read_file', {'path': '../outside.txt'}),
])
def test_guard_veto_persists_a_blocked_step(tmp_path, layer, tool, args):
    from test_engine import setup_engine

    ctx, store, instance, record = setup_engine(tmp_path, 20)
    instance.guard.stop_message = 'Distinct fixture veto message'
    instance.guard.stop_tool_result = '{"error":"Distinct fixture veto message"}'
    original = ctx.subagent_lifecycle.launch

    def launch(request):
        sid = original(request)
        event = dict(session_id=sid, tool_call_id='vetoed', tool_name=tool, args=args)
        if layer == 'veto':
            assert instance.guard.pre_tool_call(**event)['action'] == 'block'
        else:
            assert instance.guard.tool_execution(**event, next_call=lambda _: pytest.fail('real tool ran')) == instance.guard.stop_tool_result
        instance.post_tool_call(**event, status='error', error_message=instance.guard.stop_message)
        instance.subagent_stop(child_session_id=sid, tool_call_history=[{'tool_name': tool, 'args': args, 'status': 'error'}])
        return sid

    ctx.subagent_lifecycle.launch = launch
    instance.attach(record, 'controller')
    instance.step('controller')
    instance.step('controller')
    saved = store.get(record['id'])
    for side in ('new', 'yours'):
        assert saved['turns'][0]['runs'][side][-1]['tool_calls'][0]['status'] == 'blocked'
    source = d.FixtureSource('empty')
    source.world['tests'] = [saved]
    view = d.DesktopRpc(source, COPY).turn_open({'test_id': saved['id'], 'turn_id': 't0'})
    for answer in view['answers']:
        assert answer['steps'][0]['kind'] == 'stop'
        assert answer['steps'][0]['note'] == 'Not run in a test. The replay stopped here.'
    assert 'ran for real' not in str(view['answers'])


def test_answered_read_persists_and_renders_inside_the_test(tmp_path):
    from test_engine import setup_engine
    from shadow.trial.reads import SafeReads

    (tmp_path / 'notes.txt').write_text('Scoped fixture text')
    ctx, store, instance, record = setup_engine(tmp_path, 20)
    instance.guard.reads = SafeReads(instance.guard)
    original = ctx.subagent_lifecycle.launch

    def launch(request):
        sid = original(request)
        event = dict(session_id=sid, tool_call_id='answered', tool_name='read_file', args={'path': 'notes.txt'})
        assert instance.guard.pre_tool_call(**event) is None
        result = instance.guard.tool_execution(**event, next_call=lambda _: pytest.fail('real read ran'))
        assert 'Scoped fixture text' in result
        instance.post_tool_call(**event, status='ok', result=result)
        return sid

    ctx.subagent_lifecycle.launch = launch
    instance.attach(record, 'controller')
    instance.step('controller')
    instance.step('controller')
    saved = store.get(record['id'])
    source = d.FixtureSource('empty')
    source.world['tests'] = [saved]
    view = d.DesktopRpc(source, COPY).turn_open({'test_id': saved['id'], 'turn_id': 't0'})
    for answer in view['answers']:
        assert answer['steps'][0]['kind'] == 'ran'
        parts = answer['steps'][0]['parts']
        assert ''.join(p if isinstance(p, str) else next(iter(p.values())) for p in parts) == 'Read notes.txt · answered inside the test'
    assert 'ran for real' not in str(view['answers'])


def test_record_tests_import_this_repo():
    for module in (engine, models, domain, picking, verdict):
        assert Path(module.__file__).resolve().is_relative_to(ROOT)


@pytest.fixture
def prepared():
    turn = picking.Turn('t1', 'chat', 'desktop', '/tmp', 'List files', '')
    selection = picking.Selection([turn], 1, 1, {}, {}, 7)
    store = SimpleNamespace(save=lambda record: None)
    instance = engine.TrialEngine(FakeContext(), store, None)
    return instance.prepare(selection, models.Model('openai', 'gpt-4.1'),
                            models.Model('openai', 'gpt-4.1-mini'),
                            [models.Model('anthropic', 'claude-sonnet-4-6')])


@pytest.mark.parametrize('state,mode', [
    ('prepared', 'empty'), ('queued', 'progress'), ('running', 'progress'),
    ('interrupted', 'stopped'), ('cancelled', 'stopped'), ('finished', 'result'),
])
def test_real_trial_lifecycle(prepared, state, mode):
    record = deepcopy(prepared)
    record['state'] = state
    if state == 'finished':
        verdict.finalize(record)
    source = d.FixtureSource('empty')
    source.world['tests'] = [record]
    page = d.DesktopRpc(source, COPY).page_state({})
    assert page['mode'] == mode
    assert page['top']['try']['disabled'] == (mode == 'progress')
    if state == 'prepared':
        assert page['top']['picker'] is None
    elif mode == 'progress':
        assert page['stopped'] is None and page['verdict'] is None
        assert page['test_id'] == record['id']
        assert page['turns']['groups'][0]['rows'][0]['kind'] == 'waiting'
        with pytest.raises(ValueError):
            d.DesktopRpc(source, COPY).act({'action': 'delete_test', 'test_id': record['id']})
    else:
        assert page['top']['picker']['options'][0]['id'] == record['id']


def test_stopped_result_uses_trial_finalizer(prepared):
    record = deepcopy(prepared)
    record['state'] = 'cancelled'
    template = record['turns'][0]
    record['turns'] = []
    for i in range(22):
        turn = deepcopy(template)
        turn.update(id=f't{i}', state='done', usable=True,
                    judgment={'result': 'won', 'agree': True, 'disagreement': i == 0,
                              'reason_kind': 'missed_request', 'reason': 'Complete',
                              'fixes_new': 'none', 'fixes_yours': 'minor'})
        turn['runs'] = {side: [
            {'cost': cost, 'duration_s': duration, 'usage': {'total_tokens': 100, 'estimated': False},
             'tool_calls': [], 'broken_calls': 0, 'summary': 'Done'}
            for cost, duration in ((.01, 2), (.02, 3))]
            for side in ('new', 'yours')}
        record['turns'].append(turn)
    source = d.FixtureSource('empty')
    source.world['tests'] = [record]
    page = d.DesktopRpc(source, COPY).act({'action': 'get_result', 'test_id': record['id']})['page']
    assert page['mode'] == 'result'
    assert record['totals']['receipt']['speed_new_s'] == 5
    assert record['totals']['receipt']['cost_per_turn_new'] == pytest.approx(.03)
    assert record['totals']['receipt']['tokens_new'] == 4400
    assert record['judge_disagreements'] == 1


@pytest.fixture
def switch_modules():
    return domain


@pytest.mark.parametrize('mode,phase,kind', [
    ('all_at_once', 'watch', 'watch_start'), ('all_at_once', 'paused', 'watch_trip'),
    ('gradual', 'running', 'step1'), ('gradual', 'paused', 'rollback'),
])
def test_real_switch_records_render_counts_trips_and_history(switch_modules, mode, phase, kind):
    domain = switch_modules
    old = domain.Route('openai', 'gpt-4.1', '', 'chat')
    new = domain.Route('anthropic' if mode == 'all_at_once' else 'openai', 'gpt-4.1-mini', '', 'chat')
    record = domain.SwitchRecord('switch-real', 'b71c', old, new, mode, phase=phase)
    record.turns['one'] = domain.Turn('s', 't', 'one', 'new', 0,
                                    response_model=new.model, finished=True, signals=['failed_requests'])
    record.turns['old-epoch'] = domain.Turn('s', 't', 'old', 'new', -1,
                                          response_model=new.model, finished=True)
    record.last_response_model = new.model
    fields = {}
    if phase == 'paused':
        trip = {'reason': 'requests', 'new': {'turns': 1, 'trouble': 1},
                'old': {'turns': 0, 'trouble': 0}, 'failures': 3}
        record.failure_streak = 3
        record.trips = [trip]
        fields = trip
    record.history = [{'at': '2026-11-10T10:00:00+00:00', 'id': 'notice', 'kind': kind, **fields},
                      {'at': '2026-11-10T10:01:00+00:00', 'kind': 'future_unknown_kind'}]
    source = d.FixtureSource('result')
    source.world['switch'] = record.to_dict()
    page = d.DesktopRpc(source, COPY).page_state({})
    assert page['mode'] == ('watch' if mode == 'all_at_once' else 'switch')
    panel = page['switch']
    assert panel['paused'] == (phase == 'paused')
    assert panel['trouble']['rows'][0][1:] == ['1', '1 of 1']
    assert len(panel['log']['rows']) == 1
    assert panel['log']['rows'][0]['text']
    assert 'failed' in panel['sub'] if phase == 'paused' else panel['sub']
    assert {b['id'] for b in panel['buttons']} == (
        {'switch_again', 'stay'} if mode == 'all_at_once' and phase == 'paused' else
        {'resume', 'stop_switch'} if phase == 'paused' else
        {'undo'} if mode == 'all_at_once' else {'move_on', 'undo'})
    record.phase = 'done'
    source.world['switch'] = record.to_dict()
    assert d.DesktopRpc(source, COPY).page_state({})['mode'] == 'result'


def test_completed_real_switch_does_not_block_a_consented_start(switch_modules):
    domain = switch_modules
    old = domain.Route('openai', 'gpt-4.1', '', 'chat')
    new = domain.Route('openai', 'gpt-4.1-mini', '', 'chat')
    record = domain.SwitchRecord('complete', 'b71c', old, new, 'gradual', phase='done')
    source = d.FixtureSource('result')
    source.world['switch'] = record.to_dict()
    rpc = d.DesktopRpc(source, COPY)
    assert not rpc.page_state({})['top']['try']['disabled']
    card = rpc.start_card({'model': source.world['start']['try']})
    reply = rpc.act({'action': 'start', 'model': card['model'], 'judge': card['judge_slug'],
                     **{key: card[key] for key in ('judge_mode', 'judge_models', 'estimate', 'consent')}})
    assert reply['page']['mode'] == 'progress'


@pytest.fixture
def real_switch(switch_modules):
    domain = switch_modules
    return domain.SwitchRecord(
        'switch-real', 'b71c', domain.Route('nous', 'nousresearch/hermes-4-405b', '', 'chat'),
        domain.Route('nous', 'nousresearch/hermes-4-70b-fp8-2026-08-14', '', 'chat'), 'gradual')


def test_real_gradual_history_uses_the_design_log_rows(real_switch):
    real_switch.history = [
        {'at': f'2026-11-10T{hour}:00:00', 'kind': kind}
        for hour, kind in [('10', 'step1'), ('11', 'step2'), ('12', 'step3'), ('13', 'resume')]]
    real_switch.history.append({'at': '2026-11-10T14:00:00', 'kind': 'future_unknown_kind'})
    source = d.FixtureSource('result')
    source.world['switch'] = real_switch.to_dict()
    rows = d.DesktopRpc(source, COPY).page_state({})['switch']['log']['rows']
    assert [row['text'] for row in rows] == [
        'Resumed at 1 time in 10',
        'Started at 1 time in 10 from test b71c (Holds up, won 6, same 19, lost 5)',
    ]


@pytest.mark.parametrize('mode,kind', [('gradual', 'rollback'), ('all_at_once', 'watch_trip')])
def test_real_trip_copy_has_desktop_log_wrapper_and_no_chat_footer(real_switch, mode, kind):
    real_switch.mode, real_switch.phase = mode, 'paused'
    trip = {'reason': 'requests', 'new': {'turns': 9, 'trouble': 3},
            'old': {'turns': 120, 'trouble': 4}, 'failures': 3}
    real_switch.trips = [trip]
    real_switch.history = [{'at': '2026-11-10T10:00:00', 'kind': kind, **trip}]
    source = d.FixtureSource('result')
    source.world['switch'] = real_switch.to_dict()
    panel = d.DesktopRpc(source, COPY).page_state({})['switch']
    expected = (
        "You're back on Hermes 4 405B. Shadow paused the switch. The last 3 requests to Hermes 4 70B "
        'failed in a row, after 9 turns on it. This can be a provider hiccup. Nothing else changed. '
        "Your chats, settings and test result are as they were. Shadow won't resume by itself."
        if mode == 'gradual' else
        "You're back on Hermes 4 405B, and it's your default again. In 9 turns on Hermes 4 70B, "
        '3 requests in a row failed. This can be a provider hiccup. Nothing else changed. '
        "Shadow won't switch again by itself.")
    assert panel['sub'] == expected
    assert panel['log']['rows'][0]['text'] == f'Back on Hermes 4 405B: {expected}'
    assert '/shadow' not in str(panel)
    assert panel['trouble']['note'] not in panel['sub']


def test_real_watch_history_uses_desktop_start_copy(real_switch):
    real_switch.mode, real_switch.phase = 'all_at_once', 'watch'
    real_switch.history = [{'at': '2026-11-10T10:00:00', 'kind': 'watch_start'}]
    source = d.FixtureSource('result')
    source.world['switch'] = real_switch.to_dict()
    row = d.DesktopRpc(source, COPY).page_state({})['switch']['log']['rows'][0]
    assert row['text'] == (
        'Default set to Hermes 4 70B on nous, from test b71c (Holds up, won 6, same 19, lost 5). '
        'Hermes 4 405B on nous saved for undo.')


def test_real_trial_without_turn_timestamp_does_not_show_the_test_date(prepared):
    source = d.FixtureSource('empty')
    source.world['tests'] = [prepared]
    prepared['state'] = 'running'
    rpc = d.DesktopRpc(source, COPY)
    assert rpc.page_state({})['turns']['groups'][0]['rows'][0]['day'] == ''
    turn = prepared['turns'][0]
    turn.update(state='done', usable=True,
                judgment={'result': 'won', 'reason': 'Complete', 'reason_kind': 'missed_request'})
    turn['runs'] = {side: [{'summary': 'Done', 'tool_calls': []}] for side in ('new', 'yours')}
    prepared.update(state='finished', totals=deepcopy(d.FixtureSource('result').tests()[0]['totals']))
    assert rpc.page_state({})['turns']['groups'][0]['rows'][0]['day'] == ''
    assert rpc.turn_open({'test_id': prepared['id'], 'turn_id': turn['id']})['meta'] == 'from Hermes Desktop'


def test_saved_turn_timestamps_keep_their_own_dates():
    source = d.FixtureSource('result')
    record = source.tests()[0]
    record['turns'][0]['at'] = '2026-11-03T12:00:00'
    record['turns'][1]['at'] = '2026-11-04T12:00:00'
    rpc = d.DesktopRpc(source, COPY)
    rows = {row['id']: row for group in rpc.page_state({})['turns']['groups'] for row in group['rows']}
    assert rows[record['turns'][0]['id']]['day'] == 'Tue'
    assert rows[record['turns'][1]['id']]['day'] == 'Wed'
    assert rpc.turn_open({'test_id': record['id'], 'turn_id': record['turns'][1]['id']})['meta'].startswith('Wed, Nov 4 · ')


def test_real_switch_times_display_in_the_local_timezone(real_switch, monkeypatch):
    real_switch.history = [{'at': '2026-11-10T20:14:00+00:00', 'kind': 'step1'}]
    source = d.FixtureSource('result')
    source.world['switch'] = real_switch.to_dict()
    try:
        with monkeypatch.context() as local:
            local.setenv('TZ', 'America/Chicago')
            time.tzset()
            page = d.DesktopRpc(source, COPY).page_state({})
            assert page['switch']['log']['rows'][0]['when'] == 'Nov 10, 2:14 PM'
            assert 'started Nov 10, 2:14 PM' in ''.join(page['top']['sub'])
    finally:
        time.tzset()


@pytest.mark.parametrize('boundary', ['rpc', 'source'])
def test_resume_from_an_earlier_pause_cannot_resume_a_later_epoch(real_switch, boundary):
    real_switch.phase = 'paused'
    source = d.FixtureSource('result')
    source.world['switch'] = real_switch.to_dict()
    rpc = d.DesktopRpc(source, COPY)
    stale = rpc.page_state({})['switch']['identity']
    real_switch.epoch += 1
    real_switch.resumes += 1
    source.world['switch'] = real_switch.to_dict()
    before = deepcopy(source.world)
    if boundary == 'rpc':
        assert rpc.methods()['act']({'action': 'resume', **stale}) == {
            'page': rpc.page_state({}), 'notice': None}
    else:
        with pytest.raises(ValueError):
            source.act('resume', stale)
    assert source.world == before
    fresh = rpc.page_state({})['switch']['identity']
    assert fresh['epoch'] == 1
    reply = rpc.act({'action': 'resume', **fresh})
    assert reply['page']['switch']['identity']['epoch'] == 2
