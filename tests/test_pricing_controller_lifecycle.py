from copy import deepcopy

import pytest

from shadow.common.copy import copy, load_copy
from shadow.common.storage import Store
from shadow.desktop_rpc import DesktopRpc
from shadow.desktop_source import ProfileSource
from shadow.trial.plugin import register
from test_controller_runtime import queued_trial
from test_plugin import service


@pytest.mark.parametrize('configuration,reason', [
    ({}, 'controller_exited'),
    ({'agent': {'service_tier': 'priority'}}, 'unpriceable'),
    ({'agent': None}, 'unpriceable'),
    (OSError('configuration unavailable'), 'unpriceable'),
])
def test_dead_controller_reconciles_tiers_without_changing_frozen_prices(tmp_path, monkeypatch, configuration, reason):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    frozen = deepcopy(record['prices'])
    record['spend'] = 0.42
    plugin.store.save(record)
    plugin.launcher.is_alive = lambda _: False

    def read_config():
        if isinstance(configuration, Exception):
            raise configuration
        return configuration

    plugin.config_reader = read_config
    monkeypatch.setattr('shadow.trial.plugin.resolve_prices', lambda *_: pytest.fail('live pricing lookup'))
    plugin.status(plugin.store.get(record['id']))
    saved = Store(tmp_path / 'trial.sqlite').get(record['id'])
    assert saved['state'] == 'interrupted' and saved['stop_reason'] == reason
    assert saved['prices'] == frozen and saved['spend'] == 0.42
    if reason == 'unpriceable':
        assert saved['pricing_blocker'] == 'service_tier' and not saved['can_start']
    plugin.status(saved)
    assert plugin.launcher.launched == [record['id']]
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls


@pytest.mark.parametrize('ending', ['dead_controller', 'session_end'])
@pytest.mark.parametrize('completed_pairs', [0, 20])
def test_unpriceable_controller_stop_preserves_pricing_notice(tmp_path, monkeypatch, ending, completed_pairs):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    frozen = deepcopy(record['prices'])
    record['spend'] = 0.42
    plugin.store.save(record)
    monkeypatch.setattr('shadow.trial.plugin.resolve_prices', lambda *_: pytest.fail('live pricing lookup'))
    monkeypatch.setattr('shadow.trial.engine.resolve_prices', lambda *_: pytest.fail('live pricing lookup'))
    marker = f'shadow-controller:{record["id"]}:{record["controller_token"]}'
    ctx.hook('pre_llm_call', session_id='controller', user_message=marker)
    for _ in range(64):
        if sum(turn['usable'] for turn in plugin.engine.record['turns']) == completed_pairs:
            break
        plugin.step({}, session_id='controller')
    else:
        pytest.fail('fixture did not produce the requested usable pairs')
    plugin.engine.record['spend'] = 0.42
    plugin.engine.save()
    launches = len(ctx.subagent_lifecycle.launches)
    judge_calls = len(ctx.llm.calls)
    ctx.hook('post_api_request', session_id='controller', api_request_id='unpriced-request',
             provider=record['current']['provider'], response_model='unpriced-model',
             usage={'prompt_tokens': 100, 'completion_tokens': 100})
    before = plugin.store.get(record['id'])
    assert before['state'] == 'running' and before['stop_reason'] == 'unpriceable'
    assert before['request_usage'][-1]['cost'] is None and before['spend'] == 0.42
    if ending == 'dead_controller':
        plugin.launcher.is_alive = lambda _: False
    else:
        ctx.hook('on_session_end', session_id='controller', completed=True, turn_exit_reason='completed')

    assert plugin.active_record() is None

    saved = Store(tmp_path / 'trial.sqlite').get(record['id'])
    assert saved['state'] == 'interrupted' and saved['stop_reason'] == 'unpriceable'
    assert copy('trial.unpriceable') in plugin.stopped(saved)
    page = DesktopRpc(ProfileSource(plugin), load_copy()).page_state({'test_id': record['id']})
    assert copy('trial.unpriceable') in page['stopped']['summary']
    if ending == 'session_end':
        assert saved['controller_stop_details'] == {'failed': False, 'interrupted': False,
                                                     'turn_exit_reason': 'completed'}
    assert saved['prices'] == frozen and saved['spend'] == 0.42
    assert saved['request_usage'] == before['request_usage']
    assert plugin.launcher.launched == [record['id']]
    assert len(ctx.subagent_lifecycle.launches) == launches and len(ctx.llm.calls) == judge_calls
    if completed_pairs:
        plugin.command(f'results {record["id"]} --stats', terminal=True)
        result = plugin.store.get(record['id'])
        assert result['totals']['pairs'] == completed_pairs
        assert result['totals']['word'] == 'not_enough'
        assert result['totals']['cant_call_reason'] == 'unpriceable'


@pytest.mark.parametrize('ending', ['chat', 'desktop', 'watch', 'engine', 'detached_chat', 'detached_desktop'])
@pytest.mark.parametrize('late_request', [False, True])
@pytest.mark.parametrize('completed_pairs', [0, 20])
def test_cancel_preserves_unpriceable_request_notice(tmp_path, ending, late_request, completed_pairs):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    marker = f'shadow-controller:{record["id"]}:{record["controller_token"]}'
    ctx.hook('pre_llm_call', session_id='controller', user_message=marker)
    for _ in range(64):
        if sum(turn['usable'] for turn in plugin.engine.record['turns']) == completed_pairs:
            break
        plugin.step({}, session_id='controller')
    else:
        pytest.fail('fixture did not produce the requested usable pairs')
    engine = plugin.engine
    frozen = deepcopy(engine.record['prices'])
    spend = engine.record['spend']
    launches, judge_calls = len(ctx.subagent_lifecycle.launches), len(ctx.llm.calls)
    if ending.startswith('detached_'):
        plugin.engine = None

    def unpriced_request():
        engine.post_api_request(session_id='controller', api_request_id='unpriced-request',
                                provider=record['current']['provider'], response_model='unpriced-model',
                                usage={'prompt_tokens': 100, 'completion_tokens': 100})

    if not late_request:
        unpriced_request()
    rpc = DesktopRpc(ProfileSource(plugin), load_copy())
    if ending.endswith('chat'):
        text = ctx.commands['shadow']('cancel')
    elif ending.endswith('desktop'):
        response = rpc.act({'action': 'stop_test', 'test_id': record['id']})
        text = response['page']['stopped']['summary']
    elif ending == 'watch':
        def interrupt(_):
            raise KeyboardInterrupt
        plugin.watch_wait = interrupt
        text = plugin.watch(record['id'], terminal=True)
    else:
        engine.cancel()
        text = plugin.stopped(plugin.store.get(record['id']))
    if late_request:
        unpriced_request()
    else:
        assert copy('trial.unpriceable') in text

    saved = Store(tmp_path / 'trial.sqlite').get(record['id'])
    assert saved['state'] == 'cancelled' and saved['stop_reason'] == 'unpriceable'
    assert copy('trial.unpriceable') in plugin.stopped(saved)
    assert copy('trial.unpriceable') in rpc.page_state({'test_id': record['id']})['stopped']['summary']
    assert saved['prices'] == frozen and saved['spend'] == spend
    assert saved['request_usage'][-1]['cost'] is None
    assert len(ctx.subagent_lifecycle.launches) == launches and len(ctx.llm.calls) == judge_calls
    if completed_pairs:
        plugin.command(f'results {record["id"]} --stats', terminal=True)
        result = plugin.store.get(record['id'])
        assert result['totals']['pairs'] == completed_pairs
        assert result['totals']['word'] == 'not_enough'
        assert result['totals']['cant_call_reason'] == 'unpriceable'


def test_watch_cancel_keeps_unpriced_request_completed_during_wait(tmp_path):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    marker = f'shadow-controller:{record["id"]}:{record["controller_token"]}'
    ctx.hook('pre_llm_call', session_id='controller', user_message=marker)

    def interrupt_after_request(_):
        ctx.hook('post_api_request', session_id='controller', api_request_id='unpriced-during-wait',
                 provider=record['current']['provider'], response_model='unpriced-model',
                 usage={'prompt_tokens': 100, 'completion_tokens': 100})
        raise KeyboardInterrupt

    plugin.watch_wait = interrupt_after_request
    text = plugin.watch(record['id'], terminal=True)
    saved = Store(tmp_path / 'trial.sqlite').get(record['id'])
    assert saved['state'] == 'cancelled' and saved['stop_reason'] == 'unpriceable'
    assert copy('trial.unpriceable') in text
    assert saved['request_usage'][-1]['api_request_id'] == 'unpriced-during-wait'
    assert saved['request_usage'][-1]['cost'] is None


@pytest.mark.parametrize('state', ['queued', 'running', 'interrupted'])
def test_legacy_refusal_preserves_quarantine_and_terminal_state_after_reload(tmp_path, monkeypatch, state):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    record.pop('prices')
    record.update(state='running', controller_session_id='old-controller', spend=0.42)
    plugin.store.save(record)
    plugin.store.remember_replay_child(record['id'], 'replay-child')
    if state == 'interrupted':
        assert plugin.store.controller_stopped(record['id'], record['controller_token'], 'plugin_reloaded')
    else:
        record['state'] = state
        plugin.store.save(record)
    plugin.guard.add_controller('old-controller', exclusive=True)
    plugin.guard.unidentified.add('replay-child')
    monkeypatch.setattr('shadow.trial.plugin.TrialEngine', lambda *_: pytest.fail('legacy refusal created an engine'))
    monkeypatch.setattr('shadow.trial.plugin.resolve_prices', lambda *_: pytest.fail('live pricing lookup'))
    marker = f'shadow-controller:{record["id"]}:{record["controller_token"]}'

    assert plugin.pre_llm_call(session_id='old-controller', user_message=marker)['action'] == 'block'

    saved = Store(tmp_path / 'trial.sqlite').get(record['id'])
    assert saved['state'] == 'finished' and saved['stop_reason'] == 'unpriceable'
    assert saved['totals']['pairs'] == 0 and saved['spend'] == 0.42
    assert not saved['can_start'] and 'prices' not in saved
    assert plugin.engine is None and not plugin.guard.controllers
    assert plugin.pre_llm_call(session_id='ordinary', user_message='Hello') is None
    assert plugin.guard.pre_tool_call(tool_name='terminal', args={}, session_id='replay-child')['action'] == 'block'
    monkeypatch.setattr('shadow.trial.plugin.Store', lambda: Store(tmp_path / 'trial.sqlite'))
    ctx.hooks.clear()
    ctx.middleware.clear()
    register(ctx)
    recovered = ctx.shadow_trial
    assert recovered.store.get(record['id'])['state'] == 'finished'
    assert recovered.pre_llm_call(session_id='old-controller', user_message=marker)['action'] == 'block'
    assert recovered.pre_llm_call(session_id='ordinary', user_message='Hello') is None
    assert recovered.guard.pre_tool_call(tool_name='terminal', args={}, session_id='replay-child')['action'] == 'block'
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls


@pytest.mark.parametrize('legacy,state,token', [
    (True, 'interrupted', 'stale-token'),
    (True, 'cancelled', None),
    (True, 'finished', None),
    (False, 'interrupted', None),
])
def test_controller_refusal_does_not_mutate_ineligible_records(tmp_path, legacy, state, token):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    if legacy:
        record.pop('prices')
    record['state'] = state
    plugin.store.save(record)
    before = plugin.store.get(record['id'])
    marker = f'shadow-controller:{record["id"]}:{token or record["controller_token"]}'

    assert plugin.pre_llm_call(session_id='controller', user_message=marker)['action'] == 'block'

    assert plugin.store.get(record['id']) == before
    assert plugin.engine is None and not plugin.guard.controllers
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls


def test_legacy_refusal_preserves_another_controller_guard(tmp_path):
    _, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    record.pop('prices')
    record.update(state='interrupted', controller_session_id='old-controller')
    plugin.store.save(record)
    plugin.guard.add_controller('other-controller', exclusive=True)
    plugin.guard.add_controller('old-controller')
    marker = f'shadow-controller:{record["id"]}:{record["controller_token"]}'

    decision = plugin.pre_llm_call(session_id='old-controller', user_message=marker)

    assert decision == {'action': 'block', 'message': plugin.guard.controller_stop_message}
    assert plugin.guard.controllers == {'other-controller'}
    assert plugin.guard.exclusive
    assert plugin.guard.pre_tool_call(tool_name='terminal', args={}, session_id='other-controller')['action'] == 'block'


def test_superseded_legacy_token_cannot_finalize_a_new_controller_claim(tmp_path, monkeypatch):
    _, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    record.pop('prices')
    plugin.store.save(record)
    read_record = plugin.store.get
    superseding = []

    def replace_claim_after_read(run_id=None):
        snapshot = read_record(run_id)
        if not superseding:
            superseding.append(plugin.store.claim_controller(record['id'], record['controller_pid']))
        return snapshot

    monkeypatch.setattr(plugin.store, 'get', replace_claim_after_read)
    marker = f'shadow-controller:{record["id"]}:{record["controller_token"]}'

    assert plugin.pre_llm_call(session_id='controller', user_message=marker)['action'] == 'block'

    saved = read_record(record['id'])
    assert superseding[0] and saved['controller_token'] == superseding[0]
    assert saved['state'] == 'queued' and 'totals' not in saved
    assert plugin.engine is None and not plugin.guard.controllers
