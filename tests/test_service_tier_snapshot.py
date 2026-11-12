from copy import deepcopy

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, selection, set_judge_pick
from shadow.common.copy import copy, load_copy
from shadow.desktop_rpc import DesktopRpc
from shadow.desktop_source import ProfileSource
from shadow.trial.engine import TrialEngine
from shadow.trial.verdict import finalize
from test_controller_runtime import queued_trial
from test_plugin import completed_service, finish_controller, service


def test_controller_launch_freezes_current_profile_tiers(tmp_path):
    ctx, plugin = service(tmp_path)
    record = TrialEngine(ctx, plugin.store, plugin.guard).prepare(
        selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE], service_tiers=('normal', 'default'))
    record['state'] = 'queued'
    plugin.store.save(record)
    plugin.config_reader = lambda: {'agent': {'service_tier': 'standard'}}
    launches = []
    plugin.launcher.launch = lambda snapshot: launches.append(deepcopy(snapshot)) or 123

    assert plugin.start_controller(record)

    saved = plugin.store.get(record['id'])
    assert launches[0]['service_tiers'] == ['standard', None]
    assert saved['service_tiers'] == ['standard', None]
    assert saved['prices'] == record['prices']


def test_controller_launch_blocks_tier_changed_since_prepare(tmp_path):
    ctx, plugin = service(tmp_path)
    record = TrialEngine(ctx, plugin.store, plugin.guard).prepare(
        selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    record['state'] = 'queued'
    plugin.store.save(record)
    plugin.config_reader = lambda: {'agent': {'service_tier': 'priority'}}

    assert plugin.start_controller(record) is False

    assert copy('start.service_tier') in plugin.stopped(plugin.store.get(record['id']))
    assert not plugin.launcher.launched
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls


@pytest.mark.parametrize('surface', ['status', 'desktop'])
def test_recovery_reads_and_freezes_changed_profile_tiers(tmp_path, surface):
    _, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    plugin.config_reader = lambda: {'agent': {'service_tier': 'priority'}}
    plugin.launcher.is_alive = lambda _: False

    if surface == 'status':
        text = plugin.status(plugin.store.get(record['id']))
    else:
        page = DesktopRpc(ProfileSource(plugin), load_copy()).page_state({'test_id': record['id']})
        text = page['stopped']['summary']

    saved = plugin.store.get(record['id'])
    assert saved['service_tiers'] == ['priority', None]
    assert saved['state'] == 'interrupted' and saved['stop_reason'] == 'unpriceable'
    assert saved['prices'] == record['prices'] and saved['spend'] == 0
    assert copy('start.service_tier') in text
    assert plugin.launcher.launched == [record['id']]


@pytest.mark.parametrize('recovery', [False, True])
def test_controller_attach_rechecks_profile_tier_before_inference(tmp_path, recovery):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    marker = f'shadow-controller:{record["id"]}:{record["controller_token"]}'
    if recovery:
        ctx.hook('pre_llm_call', session_id='controller', user_message=marker)
        plugin.engine = None
    plugin.config_reader = lambda: {'agent': {'service_tier': 'priority'}}

    decision = plugin.pre_llm_call(session_id='controller', user_message=marker)

    saved = plugin.store.get(record['id'])
    assert decision['action'] == 'block'
    assert copy('start.service_tier') in decision['message']
    assert copy('start.service_tier') in plugin.status(plugin.store.get(record['id']))
    assert saved['service_tiers'] == ['priority', None]
    assert saved['state'] == 'interrupted' and saved['stop_reason'] == 'unpriceable'
    assert not saved['can_start'] and saved['spend'] == 0
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls


def test_attach_tier_block_releases_controller_claim(tmp_path):
    _, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    plugin.config_reader = lambda: {'agent': {'service_tier': 'priority'}}

    plugin.pre_llm_call(
        session_id='controller',
        user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}')

    saved = plugin.store.get(record['id'])
    saved['state'] = 'queued'
    plugin.store.save(saved)
    plugin.config_reader = lambda: {'delegation': {'inherit_mcp_toolsets': False}}

    assert plugin.start_controller(saved)
    assert plugin.launcher.launched == [record['id'], record['id']]


@pytest.mark.parametrize(('spend', 'new_spend'), [(0, 0), (0.42, 0), (0.42, 0.01)])
def test_attach_tier_block_preserves_unused_second_look(tmp_path, spend, new_spend):
    ctx, plugin, parent = completed_service(tmp_path)
    for index, turn in enumerate(parent['turns']):
        set_judge_pick(turn, '1' if index < 4 else '2' if index < 10 else 'same')
    parent['spend'] = spend
    finalize(parent)
    plugin.store.save(parent)
    plugin.selector = lambda **kw: selection(tmp_path, 90)
    record = TrialEngine(ctx, plugin.store, plugin.guard).prepare(
        selection(tmp_path, 30), CURRENT, TRIAL, [OUTSIDE], previous=parent)
    assert plugin.store.reserve_second_look(parent['id'], record['id'])
    record['state'] = 'queued'
    plugin.store.save(record)
    plugin.launcher.launch = lambda _: 123
    assert plugin.start_controller(record)
    if new_spend:
        plugin.pre_llm_call(
            session_id='followup-controller',
            user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}')
        plugin.engine.record['spend'] += new_spend
        plugin.engine.save()
        plugin.engine = None
    plugin.config_reader = lambda: {'agent': {'service_tier': 'priority'}}
    calls = len(ctx.llm.calls)
    launches = len(ctx.subagent_lifecycle.launches)

    decision = plugin.pre_llm_call(
        session_id='followup-controller',
        user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}')

    assert decision['action'] == 'block'
    assert copy('start.service_tier') in plugin.status(plugin.store.get(record['id']))
    assert len(ctx.llm.calls) == calls and len(ctx.subagent_lifecycle.launches) == launches
    saved_parent = plugin.store.get(parent['id'])
    if new_spend:
        assert saved_parent['second_look_id'] == record['id']
        plugin.command(f'results {parent["id"]}')
        assert 'more' not in {choice['value'] for choice in ctx.questions[-1][1]}
        return
    plugin.config_reader = lambda: {'delegation': {'inherit_mcp_toolsets': False}}
    plugin.launcher.launch = lambda snapshot: finish_controller(ctx, plugin, snapshot)
    ctx.questions.clear()
    ctx.answers = ['more', 'start']
    plugin.command(f'results {parent["id"]}')

    assert 'more' in {choice['value'] for choice in ctx.questions[0][1]}
    retried = plugin.store.get()
    assert retried['id'] != record['id'] and retried['parent_id'] == parent['id']
    assert retried['state'] == 'finished'
    assert plugin.store.get(parent['id'])['second_look_id'] == retried['id']


def test_running_controller_keeps_startup_tier_until_recovery(tmp_path):
    ctx, plugin = service(tmp_path)
    record = queued_trial(tmp_path, plugin)
    marker = f'shadow-controller:{record["id"]}:{record["controller_token"]}'
    ctx.hook('pre_llm_call', session_id='controller', user_message=marker)
    plugin.config_reader = lambda: {'agent': {'service_tier': 'priority'}}

    assert plugin.pre_llm_call(session_id='controller', user_message=marker) is None
    plugin.step({}, session_id='controller')

    saved = plugin.store.get(record['id'])
    assert saved['service_tiers'] == [None, None]
    assert saved['state'] == 'running' and saved['stop_reason'] is None
    assert ctx.subagent_lifecycle.launches
