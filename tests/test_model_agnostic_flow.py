import argparse
import sqlite3
from dataclasses import asdict
from decimal import Decimal

import pytest

import fixtures
from fixtures import FakeContext, selection
from shadow.common.models import Model, Price
from shadow.common.storage import Store as TrialStore
from shadow.desktop_source import ProfileSource
from shadow.switch import Engine, Store as SwitchStore
from shadow.switch.plugin import SwitchPlugin
from shadow.trial.plugin import TrialPlugin, register
from test_model_discovery import hermes_profile
from test_pair_probe import BASE, MODE, LiveCompletions, hermes_agent_context
from test_plugin import FixtureLauncher, finish_controller


@pytest.fixture
def profile_plugin(tmp_path, hermes_profile):
    ctx = FakeContext()
    plugin = TrialPlugin(ctx, store=TrialStore(tmp_path / 'trial.sqlite'),
                         launcher=FixtureLauncher(), selector=lambda **kw: selection(tmp_path, kw['count']),
                         config_reader=lambda: {})
    register(ctx, plugin=plugin)
    return ctx, plugin


@pytest.mark.parametrize('command', ['try deepseek-chat', 'try future/ember --judge deepseek-chat'])
def test_trial_refuses_missing_credentials_before_selection_or_calls(profile_plugin, command):
    ctx, plugin = profile_plugin
    plugin.selector = lambda **kw: pytest.fail('credentials must be checked before selection')
    assert 'credentials_unknown' in ctx.commands['shadow'](command)
    assert not plugin.store.list() and not ctx.capture and not plugin.launcher.launched


def test_picker_and_desktop_use_only_authenticated_profile_models(profile_plugin):
    ctx, plugin = profile_plugin
    ctx.commands['shadow']('try')
    choices = ctx.questions[0][1]
    assert [choice['value'] for choice in choices] == ['openrouter::future/ember']
    rows = ProfileSource(plugin).models()
    assert {row['model'] for row in rows} == {'openrouter::future/ember'}
    assert all(row['maker'] == 'future' and row['reachable'] for row in rows)
    assert not ctx.capture


def test_unknown_maker_try_pair_proof_and_gradual_switch_end_to_end(
        profile_plugin, tmp_path, monkeypatch, capsys, hermes_agent_context):
    from shadow.switch import pair_probe
    from shadow.trial import usage

    ctx, plugin = profile_plugin
    current, catalog = plugin.models()
    trial = catalog[1]
    assert current.maker == trial.maker == 'future'
    monkeypatch.setattr(fixtures, 'TRIAL', trial)
    rate = Price(*(Decimal('1') for _ in range(4)))
    monkeypatch.setattr(usage, 'price_for', lambda model: rate)
    ctx.subagent_lifecycle.base_url, ctx.subagent_lifecycle.api_mode = BASE, MODE
    plugin.launcher.launch = lambda record: finish_controller(ctx, plugin, record)
    setup, handler = ctx.cli['shadow']
    parser = argparse.ArgumentParser()
    setup(parser)
    handler(parser.parse_args(['try', trial.model, '--turns', '20', '--yes']))
    record = plugin.store.get()
    assert record['state'] == 'finished' and record['totals']['pairs'] == 20
    assert record['judge_mode'] == 'backup_both'
    assert len(ctx.llm.calls) == 80
    assert {call['provider'] for call in ctx.capture if 'provider' in call} == {'openrouter'}
    assert 'Try Ember against your Aurora' in capsys.readouterr().out

    proof = []
    switch_engine = Engine(SwitchStore(sqlite3.connect(':memory:')),
                           verified_pair=lambda old, new: any(entry['old'] == asdict(old) and
                                                             entry['new'] == asdict(new) for entry in proof))
    service = SwitchPlugin(ctx, switch_engine, config_reader=lambda: {}, proposal_lookup=plugin.proposal)
    ctx.shadow_switch = service
    ctx.shadow_verify_pair = service.verify_pair
    live = LiveCompletions()
    ctx.llm = live
    ctx.register_auxiliary_task = live.register_auxiliary_task
    monkeypatch.setattr(pair_probe, 'profile_home', lambda: tmp_path)
    monkeypatch.setattr(pair_probe, 'save_pair', lambda path, entry: proof.append(entry) or True)
    ctx.register_hook = live.register_hook
    handler(parser.parse_args(['doctor', '--verify-pair', current.model, trial.model, '--provider', current.provider, '--yes']))
    assert len(proof) == 1 and len(live.calls) == 2
    assert proof[0]['old']['model'] == current.model and proof[0]['new']['model'] == trial.model

    service.command('switch ' + record['id'])
    assert switch_engine.record.mode == 'gradual' and switch_engine.record.step == 1
    service.action('advance')
    service.action('advance')
    event = dict(session_id='user', task_id='task', turn_id='turn')
    switch_engine.session_started(session_id='user', platform='cli')
    switch_engine.admit(**event, model=current.model, platform='cli')
    result = switch_engine.rewrite(**event, provider=current.provider, model=current.model,
                                   base_url=BASE, api_mode=MODE, api_request_id='request',
                                   request={'model': current.model, 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert result['request']['model'] == trial.model
    switch_engine.response(**event, provider=trial.provider, response_model=trial.model,
                           base_url=BASE, api_mode=MODE, api_request_id='request')
    switch_engine.finish(**event, completed=True, failed=False, interrupted=False)
    assert switch_engine.record.counts('new').turns == 1
    assert 'Aurora' in service.command('undo')
    assert switch_engine.record.phase == 'undone'


@pytest.mark.parametrize('operation', ['pair', 'switch'])
def test_profile_credential_loss_refuses_pair_and_switch_before_calls(
        profile_plugin, hermes_profile, monkeypatch, operation):
    from shadow.switch import Proposal, Route, pair_probe

    ctx, plugin = profile_plugin
    old = Route('openrouter', 'future/aurora', BASE, MODE)
    new = Route('deepseek', 'deepseek-chat', BASE, MODE)
    if operation == 'pair':
        new = Route('openrouter', 'future/ember', BASE, MODE)
        monkeypatch.delenv('SHADOW_TEST_ROUTER_KEY')
    service = SwitchPlugin(ctx, Engine(SwitchStore(sqlite3.connect(':memory:'))),
                           config_reader=lambda: {}, proposal_lookup=lambda _: Proposal('test', old, new, 'better'))
    ctx.llm.complete = lambda *args, **kwargs: pytest.fail('no provider call may be made')
    text = (service.verify_pair(yes=True, old_model=old.model, new_model=new.model, provider=old.provider)
            if operation == 'pair' else service.command('switch test'))
    assert 'credentials_unknown' in text
    assert service.engine.record is None and not ctx.capture
