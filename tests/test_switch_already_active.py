import sqlite3
from dataclasses import replace

import pytest

from fixtures import FakeContext
from test_switch_override import live_switch
from shadow.common.copy import load_copy
from shadow.common.discovery import ModelCatalog
from shadow.common.models import Model
from shadow.common.storage import Store as TrialStore
from shadow.desktop_rpc import DesktopRpc
from shadow.desktop_source import ProfileSource
from shadow.switch import Engine, Proposal, Route, Store
from shadow.switch.plugin import SwitchPlugin
from shadow.trial.plugin import TrialPlugin


MESSAGE = 'GPT-6.1 Sol is already your active model. Nothing to switch.'


@pytest.fixture
def active_model(tmp_path):
    current = Model('openai', 'gpt-6.1-sol')
    catalog = ModelCatalog.from_models(current, [current])
    catalog.aliases = {'openai-alias': 'openai'}
    ctx = FakeContext()
    trial = TrialPlugin(ctx, store=TrialStore(tmp_path / 'trial.sqlite'), current=current, catalog=[current])
    ctx.shadow_trial = trial
    writes = []
    engine = Engine(Store(sqlite3.connect(':memory:')), verified_pair=lambda *_: False,
                    set_default=writes.append)
    route = Route('openai', current.model, 'https://fixture.invalid/v1', 'chat_completions')
    switch = SwitchPlugin(ctx, engine, config_reader=lambda: {
        'providers': {'openai': {'base_url': route.base_url, 'api_mode': route.api_mode}}},
        catalog_reader=lambda: catalog)
    rpc = DesktopRpc(ProfileSource(trial, switch), load_copy())
    return ctx, trial, switch, rpc, catalog, route, writes


@pytest.mark.parametrize('target', ['gpt-6.1-sol', 'openai::gpt-6.1-sol', 'openai-alias::gpt-6.1-sol'])
@pytest.mark.parametrize('terminal', [False, True])
@pytest.mark.parametrize('confirmed', [False, True])
def test_cli_active_model_is_read_only(active_model, target, terminal, confirmed):
    ctx, trial, switch, _, _, _, writes = active_model
    before = switch.engine.store.row_count()
    result = switch.command('switch ' + target + (' --anyway' if confirmed else ''), terminal=terminal)
    assert result == MESSAGE
    assert switch.engine.record is None
    assert switch.engine.store.row_count() == before
    assert trial.store.list() == []
    assert writes == ctx.capture == []


@pytest.mark.parametrize('target', ['gpt-6.1-sol', 'openai::gpt-6.1-sol', 'openai-alias::gpt-6.1-sol'])
@pytest.mark.parametrize('action', ['switch', 'switch_all', 'anyway'])
@pytest.mark.parametrize('confirmed', [False, True])
def test_desktop_active_model_is_read_only(active_model, target, action, confirmed):
    ctx, trial, switch, rpc, _, _, writes = active_model
    before = switch.engine.store.row_count()
    result = rpc.methods()['act']({'action': action, 'model': target, 'confirm_override': confirmed})
    assert result == {'error': {'message': MESSAGE}}
    assert switch.engine.record is None
    assert switch.engine.store.row_count() == before
    assert trial.store.list() == []
    assert writes == ctx.capture == []


@pytest.mark.parametrize('surface', ['cli', 'desktop', 'engine'])
@pytest.mark.parametrize('endpoint', ['https://fixture.invalid/v1', 'https://other.invalid/v1'])
def test_saved_same_model_needs_no_gradual_verification(active_model, surface, endpoint):
    ctx, _, switch, rpc, _, route, writes = active_model
    proposal = Proposal('saved', route, replace(route, base_url=endpoint), 'holds_up')
    switch.proposal_lookup = lambda _: proposal
    if surface == 'cli':
        assert switch.command('switch saved') == MESSAGE
    elif surface == 'desktop':
        assert rpc.methods()['act']({'action': 'switch', 'model': 'saved'}) == {'error': {'message': MESSAGE}}
    else:
        with pytest.raises(ValueError) as error:
            switch.engine.start(proposal)
        assert str(error.value) == MESSAGE
    assert switch.engine.record is None
    assert writes == ctx.capture == []


def test_short_model_alias_uses_resolved_provider_and_model(active_model):
    _, _, switch, _, catalog, _, _ = active_model
    model = Model('openai', 'openai/gpt-6.1-sol')
    catalog.current = model
    catalog.models = [model]
    assert switch.command('switch openai::gpt-6.1-sol') == MESSAGE


def test_saved_target_already_current_ignores_historical_old_model(active_model):
    _, _, switch, rpc, _, route, writes = active_model
    switch.proposal_lookup = lambda _: Proposal('saved', replace(route, model='gpt-6-luna'), route, 'holds_up')
    assert switch.command('switch saved') == MESSAGE
    assert rpc.methods()['act']({'action': 'switch', 'model': 'saved'}) == {'error': {'message': MESSAGE}}
    assert switch.engine.record is None
    assert writes == []


@pytest.mark.parametrize('surface', ['cli', 'desktop'])
def test_same_model_slug_on_other_provider_still_switches(active_model, surface):
    _, _, switch, rpc, catalog, route, writes = active_model
    other = Model('other', route.model)
    catalog.models.append(other)
    switch.config_reader = lambda: {'providers': {
        provider: {'base_url': route.base_url, 'api_mode': route.api_mode}
        for provider in ('openai', 'other')}}
    if surface == 'cli':
        result = switch.command('switch other::gpt-6.1-sol --anyway')
        assert 'Your default model is now' in result
    else:
        result = rpc.methods()['act']({'action': 'switch', 'model': 'other::gpt-6.1-sol',
                                       'confirm_override': True})
        assert result['page']['mode'] == 'watch'
    assert switch.engine.record.new.provider == 'other'
    assert len(writes) == 1


@pytest.mark.parametrize('surface', ['cli', 'desktop'])
def test_active_model_comparison_uses_reconciled_default(active_model, surface):
    from shadow.switch.domain import SwitchRecord

    _, trial, switch, rpc, catalog, route, writes = active_model
    other = Model('other', 'different')
    catalog.models.append(other)
    target = replace(route, provider=other.provider, model=other.model)
    switch.config_reader = lambda: {'providers': {
        provider: {'base_url': route.base_url, 'api_mode': route.api_mode}
        for provider in ('openai', 'other')}}

    def set_default(value):
        writes.append(value)
        catalog.current = next(model for model in catalog.models
                               if (model.provider, model.model) == (value.provider, value.model))
        trial.current = catalog.current

    switch.engine.set_default = set_default
    with switch.engine.store.transaction(create=True) as holder:
        holder[0] = SwitchRecord('pending', 'saved', route, target,
                                 'all_at_once', phase='watch', config_pending='new')
    expected = 'Different is already your active model. Nothing to switch.'
    if surface == 'cli':
        assert switch.command('switch other::different') == expected
    else:
        assert rpc.methods()['act']({'action': 'switch', 'model': 'other::different'}) == {'error': {'message': expected}}
    assert writes == [target]
    assert switch.engine.record.id == 'pending'
    assert switch.engine.record.config_pending is None


@pytest.mark.parametrize('surface', ['cli', 'desktop'])
def test_paused_switch_resumes_with_pending_default_restore(active_model, surface):
    from shadow.switch.domain import SwitchRecord

    _, trial, switch, rpc, catalog, new, writes = active_model
    previous = Model('other', 'different')
    catalog.models.append(previous)
    old = replace(new, provider=previous.provider, model=previous.model)
    fail_restore = True

    def set_default(value):
        if fail_restore:
            raise OSError('fixture restore failed')
        writes.append(value)
        catalog.current = next(model for model in catalog.models
                               if (model.provider, model.model) == (value.provider, value.model))
        trial.current = catalog.current

    switch.engine.set_default = set_default
    with switch.engine.store.transaction(create=True) as holder:
        holder[0] = SwitchRecord('paused', 'saved', old, new, 'all_at_once',
                                 phase='paused', config_pending='old', default_changed=True)
    with pytest.raises(OSError):
        switch.engine.reconcile_config()
    fail_restore = False
    expected = 'Your default model is now GPT-6.1 Sol (was Different).'
    if surface == 'cli':
        assert expected in switch.command('switch')
    else:
        identity = rpc.page_state({})['switch']['identity']
        result = rpc.methods()['act']({'action': 'resume', **identity})
        assert expected in result['notice']
        assert result['page']['mode'] == 'watch'
    assert writes == [old, new]
    assert catalog.current.model == new.model
    assert switch.engine.record.resumes == 1
    assert switch.engine.record.config_pending is None


def test_desktop_saved_losing_test_already_active_has_no_confirmation(live_switch):
    from fixtures import TRIAL

    trial, switch, rpc, saved = live_switch
    trial.current = TRIAL
    result = rpc.methods()['act']({'action': 'switch', 'test_id': saved['id']})
    assert result == {'error': {'message': 'GPT-6 Luna is already your active model. Nothing to switch.'}}
    assert switch.engine.record is None


@pytest.mark.parametrize('issue', ['conflict', 'missing', 'api_mode', 'base_url'])
@pytest.mark.parametrize('surface', ['cli', 'desktop'])
def test_saved_active_target_precedes_historical_route_blockers(live_switch, issue, surface):
    from fixtures import TRIAL

    trial, switch, rpc, saved = live_switch
    trial.current = TRIAL
    if issue == 'conflict':
        saved['route_conflicts'] = ['trial']
    elif issue == 'missing':
        saved['routes'].pop('trial')
    elif issue == 'api_mode':
        saved['routes']['trial']['api_mode'] = 'anthropic_messages'
    else:
        saved['routes']['trial']['base_url'] = 'https://other.invalid'
    trial.store.save(saved)
    expected = 'GPT-6 Luna is already your active model. Nothing to switch.'
    if surface == 'cli':
        assert switch.command('switch ' + saved['id']) == expected
    else:
        assert rpc.methods()['act']({'action': 'switch', 'test_id': saved['id']}) == {'error': {'message': expected}}
    assert switch.engine.record is None
