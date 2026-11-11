import json
from dataclasses import replace

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext, selection
from shadow.common.copy import copy
from shadow.common.models import Model
from shadow.common.storage import Store
from shadow.trial.cards import render_start
from shadow.trial.engine import TrialEngine
from shadow.trial.reads import SafeReads
from shadow.trial.safety import ReplayGuard


def setup_engine(tmp_path, count=30):
    ctx, store, guard = FakeContext(), Store(tmp_path / 'test.sqlite'), ReplayGuard()
    engine = TrialEngine(ctx, store, guard)
    for name, callback in [('subagent_start', guard.subagent_start), ('pre_llm_call', guard.pre_llm_call), ('pre_tool_call', guard.pre_tool_call), ('post_api_request', engine.post_api_request), ('subagent_stop', engine.subagent_stop)]:
        ctx.register_hook(name, callback)
    ctx.tools['shadow_read'] = SafeReads(guard).read
    record = engine.prepare(selection(tmp_path, count), CURRENT, TRIAL, [OUTSIDE])
    return ctx, store, engine, record


def finish(engine, record):
    engine.attach(record, 'controller')
    for _ in range(200):
        if not engine.step('controller')['continue']:
            return
    pytest.fail('controller failed to finish within its bound')


@pytest.mark.parametrize('ending', ['finish', 'cancel', 'interrupt'])
def test_trial_end_releases_normal_chats_and_keeps_controller_and_children_blocked(tmp_path, ending):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    if ending == 'finish':
        finish(engine, record)
    else:
        engine.attach(record, 'controller')
        assert engine.guard.pre_llm_call(session_id='telegram-1')['action'] == 'block'
        engine.step('controller')
        if ending == 'cancel':
            engine.cancel()
        else:
            engine.interrupt('controller_failed')
    guard = engine.guard
    assert guard.pre_llm_call(session_id='telegram-1') is None
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='telegram-1') is None
    assert guard.tool_execution(tool_name='terminal', args={}, session_id='telegram-1',
                                next_call=lambda _: 'normal chat tool') == 'normal chat tool'
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='controller')['action'] == 'block'
    assert guard.controller_stop_message in guard.tool_execution(
        tool_name='terminal', args={}, session_id='controller',
        next_call=lambda _: pytest.fail('controller tool ran'))
    assert guard.pre_llm_call(session_id='ordinary-child', parent_session_id='controller')['action'] == 'block'
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='ordinary-child')['action'] == 'block'
    assert guard.stop_message in guard.tool_execution(
        tool_name='terminal', args={}, session_id='ordinary-child',
        next_call=lambda _: pytest.fail('controller child tool ran'))
    child = next(iter(guard.children))
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id=child)['action'] == 'block'
    assert guard.stop_message in guard.tool_execution(tool_name='terminal', args={}, session_id=child,
                                                     next_call=lambda _: pytest.fail('real tool ran'))


def test_new_trial_keeps_quarantined_children_after_it_ends(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.guard.unidentified.add('orphan-from-prior-trial')
    engine.attach(record, 'controller')
    engine.cancel()
    assert engine.guard.pre_llm_call(session_id='telegram-1') is None
    assert engine.guard.pre_tool_call(tool_name='terminal', args={}, session_id='orphan-from-prior-trial')['action'] == 'block'
    assert engine.guard.stop_message in engine.guard.tool_execution(
        tool_name='terminal', args={}, session_id='orphan-from-prior-trial',
        next_call=lambda _: pytest.fail('real tool ran'))


@pytest.mark.parametrize('with_ids', [True, False])
def test_guard_veto_status_stays_with_its_tool_call(tmp_path, with_ids):
    _, store, engine, record = setup_engine(tmp_path)
    engine.attach(record, 'controller')
    engine.step('controller')
    blocked = dict(session_id='child1', tool_name='read_file', args={'path': '../outside.txt'},
                   tool_call_id='blocked' if with_ids else '')
    assert engine.guard.pre_tool_call(**blocked)['action'] == 'block'
    engine.post_tool_call(session_id='child2', tool_name='read_file', tool_call_id=blocked['tool_call_id'], status='ok')
    engine.post_tool_call(session_id='child1', tool_name='read_file' if with_ids else 'search_files',
                          tool_call_id='answered' if with_ids else '', status='ok')
    engine.post_tool_call(**blocked, status='error')
    engine.step('controller')
    runs = store.get(record['id'])['turns'][0]['runs']
    assert [call['status'] for call in runs['yours'][-1]['tool_calls']] == ['ok']
    assert [call['status'] for call in runs['new'][-1]['tool_calls']] == ['ok', 'blocked']


@pytest.mark.parametrize('session', ['controller', 'unidentified', 'other'])
@pytest.mark.parametrize('layer', ['veto', 'middleware'])
def test_non_child_vetoes_do_not_retain_markers_in_exclusive_mode(tmp_path, session, layer):
    _, _, engine, record = setup_engine(tmp_path)
    engine.attach(record, 'controller')
    guard = engine.guard
    guard.expect('fixture-token', tmp_path)
    guard.subagent_start(parent_session_id='controller', child_session_id='child', child_goal='fixture-token')
    guard.subagent_start(parent_session_id='child', child_session_id='unidentified', child_goal='fixture-token')
    for index in range(3):
        call = dict(session_id=session, tool_call_id=f'call-{index}', tool_name='terminal', args={})
        if layer == 'veto':
            directive = guard.pre_tool_call(**call)
            message = guard.controller_stop_message if session == 'controller' else copy('stop.action')
            assert directive == {'action': 'block', 'message': message}
        else:
            result = guard.tool_execution(**call, next_call=lambda _: pytest.fail('tool ran'))
            message = guard.controller_stop_message if session == 'controller' else copy('stop.action')
            assert json.loads(result) == {'error': message}
        engine.post_tool_call(**call, status='error')
    for index in range(3):
        assert guard.consume_veto(session, f'call-{index}', 'terminal') is False


def test_complete_trial_persists_every_pair_in_saved_order(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path)
    finish(engine, record)
    saved = store.get(record['id'])
    assert saved['state'] == 'finished'
    assert saved['totals']['pairs'] == 30 and saved['totals']['won'] == 30
    assert [t['id'] for t in saved['turns']] == [t.id for t in selection(tmp_path).turns]
    assert len(ctx.subagent_lifecycle.launches) == 60 and len(ctx.llm.calls) == 60
    assert max(ctx.subagent_lifecycle.waits) <= 120
    assert saved['spend'] > 0 and saved['totals']['receipt']['cost_mode'] == 'plan'
    assert all(t['runs']['new'][0]['actual_models'] == [TRIAL.model] for t in saved['turns'])
    assert all(r.get('allowed_toolsets') is None and 'blocked_tools' not in r for r in ctx.subagent_lifecycle.launches)


def test_plan_usage_does_not_trip_cap_but_metered_judge_still_does(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path)
    ctx.subagent_lifecycle.tokens = 3_000_000
    finish(engine, record)
    saved = store.get(record['id'])
    assert saved['stop_reason'] is None
    assert saved['totals']['pairs'] == 30
    assert saved['spend'] < 5 and saved['excess_inflight'] == 0
    assert all(row['cost'] == 0 for row in saved['request_usage'])
    assert saved['spend'] == pytest.approx(sum(row['cost'] for row in saved['usage_calls'] if row['kind'] == 'judge'))

    ctx, store, engine, record = setup_engine(tmp_path)
    ctx.llm.spend_tokens = 3_000_000
    finish(engine, record)
    saved = store.get(record['id'])
    assert saved['stop_reason'] == 'cap'
    assert len(ctx.subagent_lifecycle.launches) == 2
    assert saved['totals']['pairs'] == 0
    assert saved['spend'] > 5 and saved['excess_inflight'] == saved['spend'] - 5
    assert len(ctx.llm.calls) == 1


def test_cannot_attach_over_cap_or_unpriceable_preparation(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path)
    over = engine.prepare(selection(tmp_path), CURRENT, TRIAL, [OUTSIDE], cap=.01)
    assert not over['can_start']
    with pytest.raises(ValueError):
        engine.attach(over, 'controller')
    unlisted = Model('openai-codex', 'gpt-unlisted', plan=True)
    unknown = engine.prepare(selection(tmp_path), CURRENT, unlisted, [OUTSIDE])
    assert unknown['estimate']['total'] is None and unknown['estimate']['tokens'] > 0
    assert not unknown['can_start']
    text = render_start(unknown, isolation_verified=True)
    assert copy('start.tokens', tokens=unknown['estimate']['tokens']) in text
    assert copy('start.no_price', model=unlisted.name) in text
    assert '[Start]' not in text
    with pytest.raises(ValueError):
        engine.attach(unknown, 'controller')


def test_missing_usage_estimated_and_actual_route_required(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path)
    ctx.subagent_lifecycle.omit_usage = True
    finish(engine, record)
    saved = store.get(record['id'])
    assert saved['spend'] > 0
    assert saved['turns'][0]['runs']['new'][0]['usage']['estimated']
    assert saved['totals']['receipt']['cost_mode'] == 'not_reported'


def test_failures_retry_once_and_both_failed_are_left_out(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    ctx.subagent_lifecycle.fail_sides = {'new', 'yours'}
    finish(engine, record)
    assert record['totals']['pairs'] == 0
    assert len(ctx.subagent_lifecycle.launches) == 80
    assert all(t['unusable_reason'] == 'both_failed' for t in record['turns'])
    assert not ctx.llm.calls


def test_history_and_allowed_file_secret_absent_from_every_outbound(tmp_path):
    fake = 'sk-' + 'planted_value_0123456789012345'
    (tmp_path / 'notes.txt').write_text(fake)
    ctx, store, engine, _ = setup_engine(tmp_path, 20)
    record = engine.prepare(selection(tmp_path, 20, fake + ' Please inspect notes.txt.'), CURRENT, TRIAL, [OUTSIDE])
    ctx.subagent_lifecycle.read_path = 'notes.txt'
    finish(engine, record)
    assert ctx.capture
    assert fake not in json.dumps(ctx.capture)
    assert fake not in json.dumps(store.get(record['id']))


def test_cancel_keeps_completed_turns_and_result_is_explicit(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path)
    engine.attach(record, 'controller')
    while sum(t['usable'] for t in record['turns']) < 20:
        engine.step('controller')
    engine.cancel()
    saved = store.get(record['id'])
    assert saved['state'] == 'cancelled' and 'totals' not in saved
    assert sum(t['usable'] for t in saved['turns']) == 20


def test_duplicate_request_usage_charged_once_and_journaled(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    current = Model('openai', CURRENT.model)
    record = engine.prepare(selection(tmp_path, 20), current, TRIAL, [OUTSIDE])
    engine.attach(record, 'controller')
    event = dict(session_id='controller', api_request_id='controller-request', provider=current.provider,
                 response_model=CURRENT.model, usage={'prompt_tokens': 100, 'completion_tokens': 50})
    engine.post_api_request(**event)
    cost = record['spend']
    engine.post_api_request(**event)
    assert record['spend'] == cost > 0
    assert store.get(record['id'])['request_usage'][0]['api_request_id'] == 'controller-request'


def test_wrong_provider_is_not_accepted_as_replay_identity(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    ctx.subagent_lifecycle.actual_provider = 'other-provider'
    finish(engine, record)
    assert record['totals']['pairs'] == 0


def test_repeated_rate_limit_stops_new_turns(tmp_path, monkeypatch):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    engine.api_request_error(session_id='controller', status_code=429)
    assert record['rate_limit_refusals'] == 1
    monkeypatch.setattr(engine.wake, 'wait', lambda _: engine.api_request_error(session_id='controller', status_code=429))
    engine.step('controller')
    assert not ctx.subagent_lifecycle.launches
    assert record['rate_limit_refusals'] == 2
    assert record['state'] == 'finished'
    assert record['stop_reason'] == 'rate_limited'


def test_disallowed_action_preserves_finished_stop_plan(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    ctx.subagent_lifecycle.tool = 'terminal'
    finish(engine, record)
    assert not ctx.subagent_lifecycle.cancelled
    assert record['totals']['pairs'] == 20
    assert all(t['runs']['new'][-1]['end_state'] == 'stopped_at_action' for t in record['turns'])


def test_restart_retains_spend_and_finishes_only_unfinished_work(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    while not record['turns'][0]['usable']:
        engine.step('controller')
    saved = store.get(record['id'])
    spent = saved['spend']
    restored = TrialEngine(ctx, store, engine.guard)
    restored.attach(saved, 'controller')
    assert restored.record['spend'] == spent
    restored.post_api_request(session_id='controller', api_request_id='repeat', response_model=CURRENT.model,
                              provider=CURRENT.provider, usage={'prompt_tokens': 100, 'completion_tokens': 50})
    once = restored.record['spend']
    again = TrialEngine(ctx, store, engine.guard)
    again.attach(store.get(record['id']), 'controller')
    again.post_api_request(session_id='controller', api_request_id='repeat', response_model=CURRENT.model,
                          provider=CURRENT.provider, usage={'prompt_tokens': 100, 'completion_tokens': 50})
    assert again.record['spend'] == once


def test_missing_usage_reserves_the_actual_input_size(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    record['turns'][0]['context'] = 'x ' * 14000
    ctx.subagent_lifecycle.omit_usage = True
    engine.attach(record, 'controller')
    engine.step('controller')
    while record['turns'][0]['state'] == 'running':
        engine.step('controller')
    new = record['turns'][0]['runs']['new'][0]
    charged = sum(r['cost'] for r in record['request_usage'] if r['session_id'] == 'child1')
    assert charged == 0
    assert new['cost'] > 0
    assert new['usage']['input_tokens'] > 9000


def test_lifecycle_wait_allows_child_usage_callbacks(tmp_path):
    import threading
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    engine.step('controller')
    callback_completed = []
    def wait(handle, **kwargs):
        def callback():
            engine.post_api_request(session_id=handle, api_request_id='threaded', response_model=TRIAL.model,
                                    provider=TRIAL.provider, usage={'prompt_tokens': 100, 'completion_tokens': 50})
            callback_completed.append(handle)
        thread = threading.Thread(target=callback)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive(), 'child callback blocked by controller wait'
    ctx.subagent_lifecycle.wait = wait
    engine.step('controller')
    assert callback_completed


def test_terminal_controller_usage_is_still_charged(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    current = Model('openai', CURRENT.model)
    record = engine.prepare(selection(tmp_path, 20), current, TRIAL, [OUTSIDE])
    engine.attach(record, 'controller')
    record['state'] = 'finished'
    before = record['spend']
    engine.post_api_request(session_id='controller', api_request_id='final', response_model=current.model,
                            provider=current.provider, usage={'prompt_tokens': 100, 'completion_tokens': 50})
    assert store.get(record['id'])['spend'] > before


@pytest.mark.parametrize('current,cost', [(CURRENT, 0.0), (Model('openai', CURRENT.model), 0.0007)],
                         ids=['plan', 'metered'])
def test_controller_usage_after_cancel_is_still_charged(tmp_path, current, cost):
    ctx, store, engine, _ = setup_engine(tmp_path, 20)
    record = engine.prepare(selection(tmp_path, 20), current, TRIAL, [OUTSIDE])
    engine.attach(record, 'controller')
    engine.cancel()
    before = record['spend']
    engine.post_api_request(session_id='controller', api_request_id='final', response_model=current.model,
                            provider=current.provider, usage={'prompt_tokens': 100, 'completion_tokens': 50})
    saved = store.get(record['id'])
    assert saved['state'] == 'cancelled' and saved['stop_reason'] == 'cancelled'
    assert saved['spend'] == pytest.approx(before + cost)
    assert saved['request_usage'][-1]['cost'] == pytest.approx(cost)


def test_judge_timeout_marks_each_turn_unusable_with_an_estimated_charge(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    def timeout(**kwargs):
        raise TimeoutError('fixture judge timeout')
    ctx.llm.complete_structured = timeout
    finish(engine, record)
    assert record['state'] == 'finished' and record['totals']['pairs'] == 0
    assert record.get('stop_reason') != 'unpriceable'
    assert record['totals']['cant_call_reason'] == 'judge_unreachable'
    calls = [c for c in record['usage_calls'] if c['kind'] == 'judge']
    assert len(calls) == 2
    assert all(c['cost'] > 0 and c['usage']['estimated'] and not c['provider'] and not c['model'] for c in calls)
    assert record['spend'] == pytest.approx(sum(row['cost'] for row in record['request_usage']) + sum(c['cost'] for c in calls))
    assert [t.get('unusable_reason') for t in record['turns'][:2]] == ['judge_unusable'] * 2
    assert len(ctx.subagent_lifecycle.launches) == 4


def test_cancel_during_judge_prevents_reverse_order_request(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    original = ctx.llm.complete_structured
    def cancel(**kwargs):
        saved = store.get(record['id'])
        saved['state'] = 'cancelled'
        store.save(saved)
        return original(**kwargs)
    ctx.llm.complete_structured = cancel
    engine.attach(record, 'controller')
    engine.step('controller')
    result = engine.step('controller')
    assert not result['continue'] and result['state'] == 'cancelled'
    assert len(ctx.llm.calls) == 1
    assert not record['turns'][0]['usable'] and 'totals' not in record


def test_cancel_during_unpriced_judge_keeps_incomplete_cost_reason(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    original = ctx.llm.complete_structured

    def cancel_with_unpriced_response(**kwargs):
        saved = store.get(record['id'])
        saved['state'] = 'cancelled'
        store.save(saved)
        response = original(**kwargs)
        response.model = 'unpriced-model'
        return response

    ctx.llm.complete_structured = cancel_with_unpriced_response
    engine.attach(record, 'controller')
    engine.step('controller')
    assert engine.step('controller') == {'continue': False, 'state': 'cancelled'}
    saved = store.get(record['id'])
    assert saved['state'] == 'cancelled' and saved['stop_reason'] == 'unpriceable'
    assert len(ctx.llm.calls) == 1 and saved['usage_calls'][-1]['cost'] is None
    assert not saved['turns'][0]['usable'] and 'totals' not in saved


def test_poll_limit_cancels_children_before_interrupting(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    engine.step('controller')
    for result in ctx.subagent_lifecycle.results.values():
        result.ready = False
    from shadow.trial.engine import controller_step_limit
    engine.steps = controller_step_limit(len(record['turns']))
    assert engine.step('controller') == {'continue': False, 'state': 'interrupted'}
    assert len(ctx.subagent_lifecycle.cancelled) == 2
    assert all(identity.stopped for identity in engine.guard.children.values())


def test_real_lifecycle_tool_history_names_and_summaries_survive(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.subagent_stop(child_session_id='child1', tool_call_history=[
        {'tool_name': 'shadow_read', 'tool_input': {'argument_keys': ['path'], 'targets': {'path': 'notes.txt'}}, 'status': 'ok'}])
    engine.step('controller')
    call = record['turns'][0]['runs']['new'][0]['tool_calls'][0]
    assert call['name'] == 'shadow_read' and 'notes.txt' in json.dumps(call)


def test_credential_aliases_never_reach_any_captured_request(tmp_path):
    values = {'apiKey': 'fixture_short_api_value', 'refresh_token': 'fixture_refresh_value',
              'client_secret': 'fixture_client_value'}
    encoded = json.dumps(values)
    (tmp_path / 'notes.txt').write_text(encoded)
    ctx, store, engine, _ = setup_engine(tmp_path, 20)
    record = engine.prepare(selection(tmp_path, 20, 'Please inspect notes.txt. ' + encoded), CURRENT, TRIAL, [OUTSIDE])
    ctx.subagent_lifecycle.read_path = 'notes.txt'
    finish(engine, record)
    captured = json.dumps(ctx.capture)
    saved = json.dumps(store.get(record['id']))
    for value in values.values():
        assert value not in captured and value not in saved


def test_second_look_keeps_only_completed_old_turns_and_freezes_judging(tmp_path):
    from copy import deepcopy
    from dataclasses import replace
    from shadow.common.models import Model
    from shadow.trial.verdict import finalize
    ctx, store, engine, record = setup_engine(tmp_path)
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    engine.cancel()
    finalize(record)
    previous = deepcopy(record)
    extra = selection(tmp_path, 30)
    extra.turns = [replace(t, id='extra-' + t.id, conversation_id='extra-' + t.conversation_id, goal='Extra ' + t.goal) for t in extra.turns]
    followup = engine.prepare(extra, CURRENT, TRIAL, [OUTSIDE], previous=previous)
    assert followup['picked'] == 31 and len(followup['turns']) == 31
    assert followup['turns'][0] == previous['turns'][0]
    assert all(t['id'].startswith('extra-') for t in followup['turns'][1:])
    assert followup['judge_source'] == previous['judge_source']
    other = Model('anthropic', 'claude-opus-4-6')
    with pytest.raises(ValueError):
        engine.prepare(extra, CURRENT, TRIAL, [OUTSIDE, other], judge=other.model, previous=previous)


def test_new_controller_rejects_previous_run_identities(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    engine.step('controller')
    old_session = next(iter(engine.guard.children))
    engine.cancel()
    fresh = TrialEngine(ctx, store, engine.guard)
    prepared = fresh.prepare(selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    fresh.attach(prepared, 'new-controller')
    assert engine.guard.pre_tool_call(tool_name='shadow_trial_step', args={}, session_id='controller')['action'] == 'block'
    assert old_session not in engine.guard.children
    fresh.post_api_request(session_id=old_session, api_request_id='late', response_model=TRIAL.model,
                           provider=TRIAL.provider, usage={'prompt_tokens': 100})
    assert prepared['spend'] == 0 and not prepared['request_usage']


def test_slow_replays_complete_without_controller_poll_amplification(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    original_launch = ctx.subagent_lifecycle.launch
    remaining = {}
    def launch(request):
        handle = original_launch(request)
        remaining[handle] = 90
        ctx.subagent_lifecycle.results[handle].ready = False
        return handle
    def wait(handle, *, timeout_seconds):
        remaining[handle] -= timeout_seconds
        ctx.subagent_lifecycle.results[handle].ready = remaining[handle] <= 0
    ctx.subagent_lifecycle.launch = launch
    ctx.subagent_lifecycle.wait = wait
    engine.attach(record, 'controller')
    calls = 0
    while engine.step('controller')['continue']:
        calls += 1
        if calls > 1000:
            pytest.fail('slow replay never finished')
    assert record['state'] == 'finished' and record['totals']['pairs'] == 20
    assert calls <= 4 * 20


def test_estimate_adds_loaded_tool_schema_tokens():
    from shadow.trial.engine import estimate
    from shadow.trial.judge import JudgeChoice
    picked = selection('/tmp/shadow-estimate', 20)
    choice = JudgeChoice('third_maker', (OUTSIDE,))
    from shadow.trial.usage import resolve_prices
    current, trial = Model('openai', CURRENT.model), Model('openai', TRIAL.model)
    prices = resolve_prices((current, trial, OUTSIDE))
    bare = estimate(picked, current, trial, choice, tool_schemas=[], prices=prices)
    schemas = [{'name': 'terminal', 'description': 'x' * 4000, 'parameters': {'type': 'object'}}]
    loaded = estimate(picked, current, trial, choice, tool_schemas=schemas, prices=prices)
    assert loaded['contestants'] > bare['contestants']
    assert loaded['controller'] > bare['controller']
    assert loaded['judge'] == bare['judge']
    assert loaded['total'] > bare['total']


def test_prepare_estimate_includes_profile_tool_schemas(tmp_path, monkeypatch):
    schemas = [{'name': 'read_file', 'description': 'y' * 5000}]
    monkeypatch.setattr('shadow.trial.engine.profile_tool_schemas', lambda: schemas)
    ctx, store, engine, _ = setup_engine(tmp_path, 20)
    current, trial = Model('openai', CURRENT.model), Model('openai', TRIAL.model)
    loaded = engine.prepare(selection(tmp_path, 20), current, trial, [OUTSIDE])
    monkeypatch.setattr('shadow.trial.engine.profile_tool_schemas', lambda: [])
    bare = engine.prepare(selection(tmp_path, 20), current, trial, [OUTSIDE], run_id='bare-estimate')
    assert loaded['estimate']['contestants'] > bare['estimate']['contestants']
    assert loaded['estimate']['controller'] > bare['estimate']['controller']
    assert loaded['estimate']['judge'] == bare['estimate']['judge']


def test_start_estimate_reserves_controller_and_multirequest_work(tmp_path):
    ctx, store, engine, _ = setup_engine(tmp_path, 20)
    current, trial = Model('openai', CURRENT.model), Model('openai', TRIAL.model)
    record = engine.prepare(selection(tmp_path, 20), current, trial, [OUTSIDE], cap=.5)
    assert not record['can_start']
    assert record['estimate']['controller'] > 0
    assert record['estimate']['contestants'] > record['estimate']['judge']


@pytest.mark.parametrize('structured', [False, True])
def test_prefixed_credentials_in_history_and_allowed_files_never_leave_scope(tmp_path, structured):
    values = {'DATABASE_PASSWORD': 'fixture_db_value', 'OPENAI_API_KEY': 'fixture_openai_value',
              'STRIPE_SECRET': 'fixture_stripe_value', 'service-auth-token': 'fixture_auth_value'}
    content = json.dumps(values) if structured else '\n'.join(f'{key}={value}' for key, value in values.items())
    (tmp_path / 'config.yaml').write_text(content)
    ctx, store, engine, _ = setup_engine(tmp_path, 20)
    record = engine.prepare(selection(tmp_path, 20, 'Inspect configuration. ' + content), CURRENT, TRIAL, [OUTSIDE])
    ctx.subagent_lifecycle.read_path = 'config.yaml'
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    for value in values.values():
        assert value not in json.dumps(ctx.capture)
        assert value not in json.dumps(store.get(record['id']))


def test_dated_provider_responses_are_verified_priced_and_proposed(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    original = ctx.subagent_lifecycle.launch
    def dated(request):
        ctx.subagent_lifecycle.actual_model = request['model'] + '-2026-11-01'
        return original(request)
    ctx.subagent_lifecycle.launch = dated
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    turn = record['turns'][0]
    assert turn['usable'] and not record['stop_reason']
    assert all(turn['runs'][side][-1]['cost'] > 0 for side in ('new', 'yours'))
    assert set(record['routes']) == {'current', 'trial'}
    assert turn['runs']['new'][0]['actual_models'] == [TRIAL.model + '-2026-11-01']


def test_available_profile_tools_have_no_removal_disclosure(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    engine.step('controller')
    for request in ctx.subagent_lifecycle.launches:
        assert 'Some tools are unavailable' not in request['goal']


def test_stopped_child_can_finish_its_plan_before_collection(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    ctx.subagent_lifecycle.tool = 'terminal'
    original = ctx.subagent_lifecycle.launch
    def launch(request):
        handle = original(request)
        ctx.subagent_lifecycle.results[handle].ready = False
        return handle
    def wait(handle, *, timeout_seconds):
        assert handle not in ctx.subagent_lifecycle.cancelled
        assert timeout_seconds <= 15
        assert engine.guard.pre_tool_call(session_id=handle, tool_name='shadow_read', args={'path': 'notes.txt'})['action'] == 'block'
        ctx.subagent_lifecycle.results[handle].summary = 'I would run the checks to verify the change.'
        ctx.subagent_lifecycle.results[handle].ready = True
    ctx.subagent_lifecycle.launch, ctx.subagent_lifecycle.wait = launch, wait
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    turn = record['turns'][0]
    assert turn['usable']
    assert all('I would run the checks' in turn['runs'][side][-1]['summary'] for side in ('new', 'yours'))
    assert all('I would run the checks' in call['input'][0]['text'] for call in ctx.llm.calls)


def test_stopped_child_that_ignores_stop_is_cancelled_after_plan_grace(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    ctx.subagent_lifecycle.tool = 'terminal'
    engine.attach(record, 'controller')
    engine.step('controller')
    for result in ctx.subagent_lifecycle.results.values():
        result.ready = False
    ctx.subagent_lifecycle.waits.clear()
    engine.step('controller')
    assert len(ctx.subagent_lifecycle.cancelled) == 2
    assert max(ctx.subagent_lifecycle.waits) <= 5
    assert ctx.subagent_lifecycle.waits.count(5) == 6


def test_replay_identifiers_are_opaque_and_cannot_reach_judge_or_saved_answers(tmp_path):
    import re
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    original = ctx.subagent_lifecycle.launch
    def echo(request):
        handle = original(request)
        token = request['correlation_id']
        ctx.subagent_lifecycle.results[handle].summary += ' ' + token
        engine.tool_history[handle] = [{'name': 'shadow_read', 'args': {'path': token}}]
        return handle
    ctx.subagent_lifecycle.launch = echo
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    requests = ctx.subagent_lifecycle.launches
    assert len({request['correlation_id'] for request in requests}) == 2
    normalized = []
    for request in requests:
        token = request['correlation_id']
        assert re.fullmatch('[a-f0-9]{32}', token)
        normalized.append(request['goal'].replace(token, ''))
        assert token not in json.dumps(record['turns'][0]['runs'])
        assert all(token not in call['input'][0]['text'] for call in ctx.llm.calls)
    assert normalized[0] == normalized[1]


@pytest.mark.parametrize('structured', [False, True])
def test_ac3_suffixed_credentials_and_service_urls_never_leave_scope(tmp_path, structured):
    values = {
        'SECRET_KEY': 'fixture_django_!@#$^&*(',
        'SECRET_KEY_BASE': 'fixture_base_!@#$^&*(',
        'DATABASE_PASSWORD_PRIMARY': 'fixture_suffixed_password;fixture_password_tail',
        'SECRET_KEY_ROTATED': 'fixture_key_head}fixture_key_tail',
        'CACHE_PWD_V2': 'fixture_space_head fixture_space_tail',
        'SERVICE_API_KEY_V2': 'fixture_suffixed_api',
        'DB_PASS': 'fixture_pass_value',
        'DB_PASSWD': 'fixture_passwd_value',
        'MYSQL_PWD': 'fixture_pwd_value',
        'service_password_primary': 'fixture_dict_password',
        'service_token_primary': 'fixture_dict_token',
        'stripe_secret_key_version': 'fixture_dict_secret_key',
        'sample_a': 'postgres://app:fixture_postgres_pw@db:5432/x',
        'sample_b': 'redis://:fixture_redis_pw@cache:6379',
        'sample_c': 'custom+db.v1-x://app:fixture_custom_pw@host/x',
    }
    for prefix in ('sk', 'rk', 'pk'):
        for mode in ('live', 'test'):
            values[f'sample_{prefix}_{mode}'] = prefix + '_' + mode + '_' + 'fixture0123456789ABCDEF'
    content = json.dumps(values) if structured else '\n'.join(f'{key} = \'{value}\'' for key, value in values.items())
    (tmp_path / 'settings.txt').write_text(content)
    ctx, store, engine, _ = setup_engine(tmp_path, 20)
    picked = selection(tmp_path, 20, 'Inspect configuration. ' + content)
    picked.turns[:] = [replace(turn, context=content) for turn in picked.turns]
    record = engine.prepare(picked, CURRENT, TRIAL, [OUTSIDE])
    ctx.subagent_lifecycle.read_path = 'settings.txt'
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    assert ctx.subagent_lifecycle.launches and ctx.llm.calls
    for value in values.values():
        assert value not in json.dumps(ctx.capture)
        assert value not in json.dumps(store.get(record['id']))
    for password in ('fixture_postgres_pw', 'fixture_redis_pw', 'fixture_custom_pw',
                     'fixture_password_tail', 'fixture_key_tail', 'fixture_space_tail'):
        assert password not in json.dumps(ctx.capture)
        assert password not in json.dumps(store.get(record['id']))


@pytest.mark.parametrize('delay', [40, 1000])
def test_rate_limit_step_waits_before_returning_or_launching(tmp_path, monkeypatch, delay):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    now = [1000.0]
    monkeypatch.setattr('shadow.trial.engine.time.time', lambda: now[0])
    monkeypatch.setattr(engine.wake, 'wait', lambda seconds: now.__setitem__(0, now[0] + seconds))
    engine.attach(record, 'controller')
    engine.api_request_error(session_id='controller', status_code=429)
    record['retry_after'] = now[0] + delay
    launched_at = []
    launch = ctx.subagent_lifecycle.launch
    def timed_launch(request):
        launched_at.append(now[0])
        return launch(request)
    ctx.subagent_lifecycle.launch = timed_launch
    result = engine.step('controller')
    assert result['continue']
    assert now[0] >= 1040
    assert now[0] < 1060
    assert result['state'] == 'waiting' and not launched_at
    assert engine.steps == 0
    if delay == 40:
        engine.step('controller')
        assert len(launched_at) == 2 and min(launched_at) >= record['retry_after']


def test_cancel_during_rate_limit_wait_stops_without_launching(tmp_path, monkeypatch):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    engine.api_request_error(session_id='controller', status_code=429)
    def cancel(seconds):
        engine.cancel()
    monkeypatch.setattr(engine.wake, 'wait', cancel)
    result = engine.step('controller')
    assert result == {'continue': False, 'state': 'cancelled'}
    assert store.get(record['id'])['state'] == 'cancelled'
    assert not ctx.subagent_lifecycle.launches


@pytest.mark.parametrize('crossing', ['first_launch', 'second_launch', 'between_steps', 'during_wait'])
def test_cap_crossing_cancels_incomplete_turn_without_collecting_more(tmp_path, crossing):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    current, trial = Model('openai', CURRENT.model), Model('openai', TRIAL.model)
    record = engine.prepare(selection(tmp_path, 20), current, trial, [OUTSIDE])
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    assert record['turns'][0]['usable']
    judge_calls = len(ctx.llm.calls)
    launch = ctx.subagent_lifecycle.launch
    outstanding = []
    def pending_launch(request):
        number = len(outstanding) + 1
        if crossing == 'first_launch' and number == 1 or crossing == 'second_launch' and number == 2:
            ctx.subagent_lifecycle.tokens = 30_000_000
        handle = launch(request)
        outstanding.append(handle)
        ctx.subagent_lifecycle.results[handle].ready = False
        return handle
    ctx.subagent_lifecycle.launch = pending_launch
    def cross_cap():
        engine.post_api_request(session_id='controller', api_request_id='cap-crossing',
                                provider=current.provider, response_model=current.model,
                                usage={'prompt_tokens': 3_000_000, 'completion_tokens': 100})
    result = engine.step('controller')
    ctx.subagent_lifecycle.waits.clear()
    if crossing == 'between_steps':
        cross_cap()
        result = engine.step('controller')
    elif crossing == 'during_wait':
        def wait(handle, *, timeout_seconds):
            ctx.subagent_lifecycle.waits.append(timeout_seconds)
            cross_cap()
        ctx.subagent_lifecycle.wait = wait
        result = engine.step('controller')
    assert result == {'continue': False, 'id': record['id'], 'state': 'finished'}
    assert set(ctx.subagent_lifecycle.cancelled) == set(outstanding)
    assert len(outstanding) == (1 if crossing == 'first_launch' else 2)
    assert len(ctx.subagent_lifecycle.waits) == (1 if crossing == 'during_wait' else 0)
    saved = store.get(record['id'])
    assert saved['stop_reason'] == 'cap' and saved['totals']['pairs'] == 1
    assert saved['turns'][0]['usable'] and not saved['turns'][1]['usable']
    assert saved['spend'] > saved['cap'] and saved['excess_inflight'] == saved['spend'] - saved['cap']
    assert len(ctx.llm.calls) == judge_calls


@pytest.mark.parametrize('response_timing', ['during_launch', 'after_launch'])
def test_same_model_on_two_providers_keeps_distinct_saved_routes(tmp_path, response_timing):
    from shadow.common.models import Model

    ctx, store, engine, _ = setup_engine(tmp_path, 20)
    trial = Model('openai', CURRENT.model)
    record = engine.prepare(selection(tmp_path, 20), CURRENT, trial, [OUTSIDE])
    engine.attach(record, 'controller')
    engine.step('controller')
    if response_timing == 'after_launch':
        for flight in engine.flights.values():
            engine.post_api_request(session_id=flight.session_id, api_request_id='later-' + flight.session_id,
                                    provider=flight.model.provider, response_model=flight.model.model,
                                    base_url='https://api.openai.com/v1', api_mode='codex_responses',
                                    usage={'prompt_tokens': 100, 'completion_tokens': 50})
    engine.step('controller')
    saved = store.get(record['id'])
    assert saved['turns'][0]['usable']
    assert set(saved['routes']) == {'current', 'trial'}
    assert saved['routes']['current']['provider'] == CURRENT.provider
    assert saved['routes']['trial']['provider'] == trial.provider
    assert saved['routes']['current']['model'] == saved['routes']['trial']['model'] == CURRENT.model
    assert not saved['route_conflicts']


def test_ac3_auth_schemes_and_cli_credentials_never_leave_scope(tmp_path):
    secrets = ['Zml4dHVyZTpzYW1wbGU=', 'fixture_token_value', 'fixture_digest_value',
               'fixture_user:fixture_pass', 'fixture_long_user:fixture_long_pass',
               'fixture_password', 'fixture_passwd', 'fixture_pass_value',
               'fixture_flag_token', 'fixture_flag_secret', 'dTpw', 'fixture_short',
               'fixture_digest_response', 'fixture_log_credential']
    content = '\n'.join([
        f'Authorization: Basic {secrets[0]}',
        f'Authorization: Token {secrets[1]}',
        f'Authorization: Digest {secrets[2]}',
        f'Authorization: Basic {secrets[10]}',
        f'Authorization: Token {secrets[11]}',
        f'''curl -H 'Authorization: Digest username="sample", response="{secrets[12]}"' https://example.test''',
        f'INFO: API_KEY={secrets[13]}',
        f'curl -H "Authorization: Basic {secrets[0]}" https://example.test',
        f'curl -u {secrets[3]} https://example.test',
        f'curl --user "{secrets[4]}" https://example.test',
        *[f'client --{flag} {secret}' for flag, secret in zip(
            ('password', 'passwd', 'pass', 'token', 'secret'), secrets[5:])],
    ])
    (tmp_path / 'settings.txt').write_text(content)
    ctx, store, engine, _ = setup_engine(tmp_path, 20)
    picked = selection(tmp_path, 20, 'Inspect configuration. ' + content)
    picked.turns[:] = [replace(turn, context=content) for turn in picked.turns]
    record = engine.prepare(picked, CURRENT, TRIAL, [OUTSIDE])
    ctx.subagent_lifecycle.read_path = 'settings.txt'
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    assert ctx.subagent_lifecycle.launches and ctx.llm.calls
    for secret in secrets:
        assert secret not in json.dumps(ctx.capture)
        assert secret not in json.dumps(store.get(record['id']))


@pytest.mark.parametrize('trigger', ['saved_cancel', 'cap'])
def test_collection_checks_stop_between_short_wait_slices(tmp_path, trigger):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    current = Model('openai', CURRENT.model)
    record = engine.prepare(selection(tmp_path, 20), current, TRIAL, [OUTSIDE])
    engine.attach(record, 'controller')
    engine.step('controller')
    handles = set(ctx.subagent_lifecycle.results)
    for result in ctx.subagent_lifecycle.results.values():
        result.ready = False
    waits = []
    def wait(handle, *, timeout_seconds):
        waits.append(timeout_seconds)
        if trigger == 'saved_cancel':
            saved = store.get(record['id'])
            saved.update(state='cancelled', stop_reason='cancelled')
            store.save(saved)
        else:
            engine.post_api_request(session_id='controller', api_request_id='cap-in-wait',
                                    provider=current.provider, response_model=current.model,
                                    usage={'prompt_tokens': 3_000_000, 'completion_tokens': 100})
    ctx.subagent_lifecycle.wait = wait
    outcome = engine.step('controller')
    assert not outcome['continue']
    assert len(waits) == 1 and waits[0] <= 5
    assert set(ctx.subagent_lifecycle.cancelled) == handles
    assert not ctx.llm.calls
    assert record['turns'][0]['runs'] == {'new': [], 'yours': []}


@pytest.mark.parametrize('trigger', ['saved_cancel', 'cap'])
def test_rate_pause_checks_stop_between_short_slices(tmp_path, monkeypatch, trigger):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    current = Model('openai', CURRENT.model)
    record = engine.prepare(selection(tmp_path, 20), current, TRIAL, [OUTSIDE])
    engine.attach(record, 'controller')
    engine.api_request_error(session_id='controller', status_code=429)
    pauses = []
    def pause(seconds):
        pauses.append(seconds)
        if trigger == 'saved_cancel':
            saved = store.get(record['id'])
            saved.update(state='cancelled', stop_reason='cancelled')
            store.save(saved)
        else:
            engine.post_api_request(session_id='controller', api_request_id='cap-in-pause',
                                    provider=current.provider, response_model=current.model,
                                    usage={'prompt_tokens': 3_000_000, 'completion_tokens': 100})
    monkeypatch.setattr(engine.wake, 'wait', pause)
    outcome = engine.step('controller')
    assert not outcome['continue']
    assert len(pauses) == 1 and pauses[0] <= 5
    assert not ctx.subagent_lifecycle.launches


def test_inprocess_cancel_during_collection_keeps_cancelled_state(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    engine.step('controller')
    ctx.subagent_lifecycle.wait = lambda *a, **kw: engine.cancel()
    outcome = engine.step('controller')
    assert outcome == {'continue': False, 'state': 'cancelled'}
    assert store.get(record['id'])['state'] == 'cancelled'
    assert record['turns'][0]['runs'] == {'new': [], 'yours': []}
    assert len(ctx.subagent_lifecycle.cancelled) == 2


def test_request_spend_uses_the_estimate_marginal_cost_rule(tmp_path):
    from shadow.trial.usage import Usage, cost_usage, stored_price

    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    engine.step('controller')
    assert record['request_usage']
    assert record['spend'] == 0
    assert all(row['cost'] == 0 for row in record['request_usage'])

    usage = Usage(1000, 100)
    engine.post_api_request(session_id='controller', api_request_id='metered',
                            provider=OUTSIDE.provider, response_model=OUTSIDE.model,
                            usage=usage.record())
    expected = cost_usage(stored_price(record['prices'], OUTSIDE), usage)
    assert expected > 0
    assert record['spend'] == pytest.approx(expected)
    assert store.get(record['id'])['request_usage'][-1]['cost'] == pytest.approx(expected)
