import pytest

from shadow.switch import Proposal
from test_switch_plugin import plugin
from test_switch_safety import OLD, NEW
from test_switch_states import CROSS


@pytest.mark.parametrize('word,reason', [('worse', 'worse'), ('too_close', 'too_close'),
                                        ('not_enough', 'too_close'), ('untested', 'no_passing_test')])
def test_warning_is_read_only_then_confirmation_switches_and_logs(plugin, word, reason):
    _, service = plugin
    service.proposal_lookup = lambda _: Proposal('saved', OLD, CROSS, word)
    before = service.engine.store.read()
    warning = service.command('switch saved', terminal=True)
    assert 'Switch anyway?' in warning
    assert 'hermes shadow switch saved --anyway' in warning
    assert service.engine.store.read() == before
    result = service.command('switch saved --anyway', terminal=True)
    record = service.engine.record
    assert record.mode == 'all_at_once' and record.phase == 'watch', result
    assert record.override['by'] == 'owner' and record.override['reason'] == reason
    assert record.override['at'] and record.override['headline']
    assert 'Owner override' in service.command('status')


@pytest.mark.parametrize('word', ['better', 'holds_up'])
def test_backed_switch_has_no_warning(plugin, word):
    _, service = plugin
    service.proposal_lookup = lambda _: Proposal('saved', OLD, NEW, word)
    assert 'Switch anyway?' not in service.command('switch saved')
    assert service.engine.record.override is None


@pytest.mark.parametrize('word', ['better', 'holds_up', 'worse', 'too_close', 'not_enough', 'untested'])
@pytest.mark.parametrize('confirmed', [False, True])
def test_every_switch_requires_the_same_pair_proof_before_warning(plugin, word, confirmed):
    _, service = plugin
    service.engine.verified_pair = lambda *_: False
    service.proposal_lookup = lambda _: Proposal('saved', OLD, NEW, word)
    result = service.command('switch saved' + (' --anyway' if confirmed else ''))
    assert 'verify-pair' in result
    assert 'Switch anyway?' not in result
    assert service.engine.record is None
    with pytest.raises(ValueError, match='compatibility'):
        service.engine.start(Proposal('saved', OLD, NEW, word), confirm_override=True)
    assert service.engine.record is None


@pytest.mark.parametrize('confirmation', [None, False, 'true', 1])
def test_engine_requires_a_real_boolean_confirmation(plugin, confirmation):
    _, service = plugin
    with pytest.raises(ValueError):
        service.engine.start(Proposal('saved', OLD, CROSS, 'worse'), confirm_override=confirmation)
    assert service.engine.record is None


def test_untested_model_name_warns_and_confirmed_switch_is_watched(plugin):
    _, service = plugin
    service.proposal_lookup = lambda _: None
    service.config_reader = lambda: {'providers': {
        route.provider: {'base_url': route.base_url, 'api_mode': route.api_mode}
        for route in (OLD, CROSS)}}
    before = service.engine.store.row_count()
    warning = service.command(f'switch {CROSS.model}')
    assert "Shadow hasn't tested" in warning and 'Switch anyway?' in warning
    assert service.engine.store.row_count() == before
    service.command(f'switch {CROSS.model} --anyway')
    assert service.engine.record.phase == 'watch'
    assert service.engine.record.override['reason'] == 'no_passing_test'


@pytest.mark.parametrize('word', ['worse', 'too_close', 'not_enough'])
def test_desktop_rpc_warns_then_confirmation_uses_normal_switch_engine(word):
    from shadow.common.copy import load_copy
    from shadow.desktop_rpc import DesktopRpc, FixtureSource

    source = FixtureSource('result-worse')
    record = source.tests()[0]
    record['totals']['word'] = word
    if word == 'not_enough':
        record['totals']['cant_call_reason'] = 'judge_ineligible'
    rpc = DesktopRpc(source, load_copy())
    params = {'action': 'anyway', 'test_id': record['id']}
    response = rpc.act(params)
    assert response['confirm_override']['message'].startswith('Won ')
    assert 'Switch anyway?' in response['confirm_override']['message']
    assert source.world['switch'] is None
    response = rpc.act({**params, 'confirm_override': True})
    assert source.world['switch']['override']['by'] == 'owner'
    assert response['page']['mode'] == 'switch'
    assert any('Owner override' in row['text'] for row in response['page']['switch']['log']['rows'])


@pytest.mark.parametrize('word,counts', [
    ('worse', (3, 9, 18)), ('too_close', (4, 20, 6)),
    ('not_enough', (7, 8, 4)), ('better', (15, 11, 4)),
])
def test_live_desktop_adapter_enforces_confirmation_and_persists_override(tmp_path, word, counts):
    import sqlite3
    from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext
    from shadow.common.copy import load_copy
    from shadow.common.storage import Store as TrialStore
    from shadow.desktop_rpc import DesktopRpc
    from shadow.desktop_source import ProfileSource
    from shadow.switch import Engine, Store
    from shadow.switch.plugin import SwitchPlugin
    from shadow.trial.plugin import TrialPlugin
    from test_cards import card_record

    ctx = FakeContext()
    trial = TrialPlugin(ctx, store=TrialStore(tmp_path / 'trial.sqlite'), current=CURRENT,
                        catalog=[CURRENT, TRIAL, OUTSIDE])
    ctx.shadow_trial = trial
    saved = card_record(tmp_path, counts)
    assert saved['totals']['word'] == word
    trial.store.save(saved)
    engine = Engine(Store(sqlite3.connect(':memory:')), verified_pair=lambda *_: True)
    switch = SwitchPlugin(ctx, engine, config_reader=lambda: {}, proposal_lookup=trial.proposal)
    rpc = DesktopRpc(ProfileSource(trial, switch), load_copy())
    params = {'action': 'switch', 'test_id': saved['id']}
    response = rpc.act(params)
    if word == 'better':
        assert 'confirm_override' not in response and engine.record.override is None
    else:
        assert response['confirm_override']['message'].startswith('Won ')
        assert engine.record is None
        response = rpc.act({**params, 'confirm_override': True})
        assert engine.store.read().override['headline'].startswith('Won ')
        assert any('Owner override' in row['text'] for row in response['page']['switch']['log']['rows'])
    assert response['page']['mode'] == 'switch'
    assert not ctx.capture


@pytest.mark.parametrize('terminal', [False, True])
def test_model_name_uses_saved_losing_numbers_instead_of_claiming_untested(tmp_path, terminal):
    import sqlite3
    from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext
    from shadow.common.storage import Store as TrialStore
    from shadow.switch import Engine, Store
    from shadow.switch.plugin import SwitchPlugin
    from shadow.trial.plugin import TrialPlugin
    from test_cards import card_record

    ctx = FakeContext()
    trial = TrialPlugin(ctx, store=TrialStore(tmp_path / 'trial.sqlite'), current=CURRENT,
                        catalog=[CURRENT, TRIAL, OUTSIDE])
    ctx.shadow_trial = trial
    saved = card_record(tmp_path, (1, 21, 8))
    trial.store.save(saved)
    engine = Engine(Store(sqlite3.connect(':memory:')), verified_pair=lambda *_: True)
    switch = SwitchPlugin(ctx, engine, config_reader=lambda: {}, proposal_lookup=trial.proposal)
    warning = switch.command(f'switch {TRIAL.model}', terminal=terminal)
    assert 'Won 1, lost 8, tied 21 of 30 turns' in warning
    prefix = 'hermes shadow' if terminal else '/shadow'
    assert f'{prefix} switch {saved["id"]} --anyway' in warning
    newer = card_record(tmp_path, (3, 9, 18))
    newer['created_at'] = saved['created_at'] + 1
    trial.store.save(newer)
    command = warning.split(f'To confirm, run: {prefix} ')[1]
    switch.command(command)
    assert engine.record.test_id == saved['id']
    assert 'Won 1, lost 8, tied 21 of 30 turns' in engine.record.override['headline']
    assert "hasn't tested" not in warning


def test_untested_override_preserves_profile_endpoint_with_verified_gradual_pair(plugin):
    _, service = plugin
    service.proposal_lookup = lambda _: None
    service.config_reader = lambda: {
        'model': {'provider': OLD.provider, 'base_url': 'https://profile-proxy.invalid/v1', 'api_mode': OLD.api_mode},
        'providers': {OLD.provider: {'base_url': OLD.base_url, 'api_mode': OLD.api_mode}}}
    service.command(f'switch {NEW.model} --anyway')
    record = service.engine.record
    assert record.old.base_url == record.new.base_url == 'https://profile-proxy.invalid/v1'
    assert record.mode == 'gradual'
    assert record.override['reason'] == 'no_passing_test'
    from shadow.switch.domain import choose_conversation
    session = next(str(i) for i in range(1000) if choose_conversation(str(i), record.id, 10))
    event = {'session_id': session, 'task_id': 'task', 'turn_id': 'turn', 'platform': 'cli'}
    service.engine.session_started(session_id=session, platform='cli')
    service.engine.admit(**event, model=OLD.model)
    service.engine.rewrite(**event, provider=OLD.provider, model=OLD.model, base_url=record.old.base_url,
                           api_mode=record.old.api_mode, api_request_id='request', request={'model': OLD.model})
    service.engine.response(**event, provider=NEW.provider, response_model=NEW.model, base_url=record.new.base_url,
                            api_mode=record.new.api_mode, api_request_id='request')
    service.engine.finish(**event, completed=True, failed=False, interrupted=False)
    assert service.engine.record.counts('new').turns == 1


@pytest.mark.parametrize('wire,expected', [('native', 'anthropic_messages'), ('chat', 'chat_completions')])
def test_untested_nous_route_uses_model_and_profile_wire_choice(wire, expected):
    from shadow.common.discovery import ModelCatalog
    from shadow.common.models import Model
    from shadow.switch.decision import untested_proposal

    old, new = Model('nous', 'nousresearch/hermes-4'), Model('nous', 'anthropic/claude-sonnet-4-5')
    catalog = ModelCatalog.from_models(old, [old, new])
    config = {'model': {'provider': 'nous'}, 'nous': {'anthropic_wire': wire},
              'providers': {'nous': {'base_url': 'https://fixture.invalid/v1', 'api_mode': 'chat_completions'}}}
    proposal = untested_proposal(catalog, config, new.model)
    assert proposal.old.api_mode == 'chat_completions'
    assert proposal.new.api_mode == expected


def test_untested_codex_route_keeps_app_server_choice():
    from shadow.common.discovery import ModelCatalog
    from shadow.common.models import Model
    from shadow.switch.decision import untested_proposal

    old, new = Model('openai-codex', 'gpt-6.1-sol'), Model('openai-codex', 'gpt-6-luna')
    config = {'model': {'provider': old.provider, 'base_url': 'https://fixture.invalid',
                        'api_mode': 'codex_responses', 'openai_runtime': 'codex_app_server'}}
    proposal = untested_proposal(ModelCatalog.from_models(old, [old, new]), config, new.model)
    assert proposal.old.api_mode == proposal.new.api_mode == 'codex_app_server'


def test_untested_provider_metadata_is_cached_only(monkeypatch):
    from types import ModuleType, SimpleNamespace
    import sys
    from shadow.common.discovery import ModelCatalog
    from shadow.common.models import Model
    from shadow.switch.decision import untested_proposal

    providers = ModuleType('hermes_cli.providers')
    lookups = []
    providers.get_provider = lambda provider, *, allow_network: (
        lookups.append((provider, allow_network)) or SimpleNamespace(base_url='https://fixture.invalid', transport='openai_chat'))
    providers.resolve_user_provider = lambda *_: None
    providers.resolve_custom_provider = lambda *_: None
    providers.TRANSPORT_TO_API_MODE = {'openai_chat': 'chat_completions'}
    providers.host_mandated_api_mode = lambda *_: None
    monkeypatch.setitem(sys.modules, 'hermes_cli.providers', providers)
    old, new = Model('fixture', 'old'), Model('fixture', 'new')
    proposal = untested_proposal(ModelCatalog.from_models(old, [old, new]), {}, 'new')
    assert proposal.old.api_mode == proposal.new.api_mode == 'chat_completions'
    assert lookups == [('fixture', False), ('fixture', False)]


def test_confirmed_nous_native_override_keeps_same_provider_protocol_guard():
    import sqlite3
    from shadow.common.discovery import ModelCatalog
    from shadow.common.models import Model
    from shadow.switch import Engine, Store
    from shadow.switch.decision import untested_proposal

    old, new = Model('nous', 'nousresearch/hermes-4'), Model('nous', 'anthropic/claude-sonnet-4-5')
    config = {'model': {'provider': 'nous'}, 'nous': {'anthropic_wire': 'native'},
              'providers': {'nous': {'base_url': 'https://fixture.invalid/v1', 'api_mode': 'chat_completions'}}}
    proposal = untested_proposal(ModelCatalog.from_models(old, [old, new]), config, new.model)
    writes = []
    engine = Engine(Store(sqlite3.connect(':memory:')), set_default=writes.append)
    with pytest.raises(ValueError):
        engine.start(proposal, confirm_override=True)
    assert engine.record is None
    assert writes == []


def test_live_desktop_untested_target_does_not_borrow_unrelated_saved_numbers(tmp_path):
    import sqlite3
    from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext
    from shadow.common.copy import load_copy
    from shadow.common.storage import Store as TrialStore
    from shadow.desktop_rpc import DesktopRpc
    from shadow.desktop_source import ProfileSource
    from shadow.switch import Engine, Store
    from shadow.switch.plugin import SwitchPlugin
    from shadow.trial.plugin import TrialPlugin
    from test_cards import card_record

    ctx = FakeContext()
    trial = TrialPlugin(ctx, store=TrialStore(tmp_path / 'trial.sqlite'), current=CURRENT,
                        catalog=[CURRENT, TRIAL, OUTSIDE])
    ctx.shadow_trial = trial
    trial.store.save(card_record(tmp_path, (1, 21, 8)))
    engine = Engine(Store(sqlite3.connect(':memory:')), set_default=lambda _: None)
    configuration = {'providers': {
        CURRENT.provider: {'base_url': 'https://fixture.invalid/old', 'api_mode': 'codex_responses'},
        OUTSIDE.provider: {'base_url': 'https://fixture.invalid/new', 'api_mode': 'anthropic_messages'}}}
    switch = SwitchPlugin(ctx, engine, config_reader=lambda: configuration, proposal_lookup=trial.proposal)
    rpc = DesktopRpc(ProfileSource(trial, switch), load_copy())
    params = {'action': 'switch', 'model': OUTSIDE.model}
    response = rpc.act(params)
    assert "hasn't tested Claude Sonnet 4.5" in response['confirm_override']['message']
    assert 'Won 1' not in response['confirm_override']['message']
    assert engine.record is None
    response = rpc.act({**params, 'confirm_override': True})
    assert response['page']['mode'] == 'watch'
    assert engine.record.override['reason'] == 'no_passing_test'
    assert 'Owner switch to' in ''.join(response['page']['top']['sub'])


def test_override_does_not_bypass_same_provider_endpoint_guard(plugin):
    from dataclasses import replace

    _, service = plugin
    proposal = Proposal('saved', OLD, replace(NEW, base_url='https://other.invalid/v1'), 'worse')
    with pytest.raises(ValueError):
        service.engine.start(proposal, confirm_override=True)
    assert service.engine.record is None


@pytest.fixture
def live_switch(tmp_path):
    import sqlite3
    from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext
    from shadow.common.copy import load_copy
    from shadow.common.storage import Store as TrialStore
    from shadow.desktop_rpc import DesktopRpc
    from shadow.desktop_source import ProfileSource
    from shadow.switch import Engine, Store
    from shadow.switch.plugin import SwitchPlugin
    from shadow.trial.plugin import TrialPlugin
    from test_cards import card_record

    ctx = FakeContext()
    trial = TrialPlugin(ctx, store=TrialStore(tmp_path / 'trial.sqlite'), current=CURRENT,
                        catalog=[CURRENT, TRIAL, OUTSIDE])
    ctx.shadow_trial = trial
    saved = card_record(tmp_path, (1, 21, 8))
    trial.store.save(saved)
    engine = Engine(Store(sqlite3.connect(':memory:')), verified_pair=lambda *_: True)
    switch = SwitchPlugin(ctx, engine, config_reader=lambda: {
        'providers': {CURRENT.provider: {'base_url': saved['routes']['current']['base_url'],
                                         'api_mode': saved['routes']['current']['api_mode']}}},
                          proposal_lookup=trial.proposal)
    ctx.shadow_switch = switch
    return trial, switch, DesktopRpc(ProfileSource(trial, switch), load_copy()), saved


@pytest.mark.parametrize('issue,key', [('conflict', 'trial.switch_routes_conflict'),
                                       ('missing', 'trial.switch_routes_missing'),
                                       ('api_mode', 'trial.switch_incompatible'),
                                       ('base_url', 'trial.switch_incompatible'),
                                       ('pair', 'trial.switch_compatibility')])
@pytest.mark.parametrize('word,counts', [
    ('better', (15, 11, 4)), ('holds_up', (6, 19, 5)), ('worse', (3, 9, 18)),
    ('too_close', (4, 20, 6)), ('not_enough', (7, 8, 4)),
])
def test_blocked_saved_routes_offer_no_override_on_any_surface(tmp_path, live_switch, issue, key, word, counts):
    from shadow.common.copy import copy
    from shadow.trial.cards import render_verdict, result_actions

    from test_cards import card_record

    trial, switch, rpc, _ = live_switch
    saved = card_record(tmp_path, counts)
    assert saved['totals']['word'] == word
    if issue == 'conflict':
        saved['route_conflicts'] = ['trial']
    elif issue == 'missing':
        saved['routes'].pop('trial')
    elif issue == 'api_mode':
        saved['routes']['trial']['api_mode'] = 'anthropic_messages'
    elif issue == 'base_url':
        saved['routes']['trial']['base_url'] = 'https://other.invalid'
    else:
        switch.engine.verified_pair = lambda *_: False
    trial.store.save(saved)
    card = trial.verdict_view(saved)
    assert 'switch' not in {choice['value'] for choice in result_actions(card)}
    expected_note = copy(key) if word in {'better', 'holds_up'} else None
    for cli in (False, True):
        rendered = render_verdict(card, cli=cli)
        assert (copy(key) in ' '.join(rendered.split())) == (expected_note is not None)
    for target in (saved['id'], saved['trial']['model']):
        for flag in ('', ' --anyway'):
            reply = switch.command('switch ' + target + flag)
            assert 'Switch anyway?' not in reply
            expected = copy('switch.error.compatibility') if issue == 'pair' else copy(key)
            assert reply == expected
            assert switch.engine.record is None
    verdict = rpc.page_state({'test_id': saved['id']})['verdict']
    assert not any(button['id'] in {'switch', 'switch_all', 'anyway'} for button in verdict['buttons'])
    assert verdict['quiet'] is None
    assert verdict['note'] == expected_note
    for confirmed in (False, True):
        reply = rpc.methods()['act']({'action': 'anyway', 'test_id': saved['id'],
                                     'confirm_override': confirmed})
        assert reply.get('error', {}).get('message') == copy(key)
        assert 'confirm_override' not in reply
        assert switch.engine.record is None


@pytest.mark.parametrize('when,label', [('2026-11-11T19:02:37.447483+00:00', 'Nov 11, 1:02 PM'),
                                       ('2026-06-11T19:02:37.447483+00:00', 'Jun 11, 2:02 PM')])
def test_override_time_is_local_and_readable_without_changing_saved_iso(live_switch, monkeypatch, when, label):
    import time

    trial, switch, rpc, saved = live_switch
    monkeypatch.setenv('TZ', 'America/Chicago')
    time.tzset()
    try:
        switch.command('switch ' + saved['id'] + ' --anyway')
        with switch.engine.store.transaction() as holder:
            holder[0].override['at'] = when
            next(event for event in holder[0].history if event['kind'] == 'override')['at'] = when
        assert f'at {label}:' in switch.command('status')
        assert when not in switch.command('status')
        page = rpc.page_state({})
        row = next(row for row in page['switch']['log']['rows'] if 'Owner override' in row['text'])
        assert row['when'] == label
        assert when not in row['text']
        assert switch.engine.record.override['at'] == when
        assert next(event for event in switch.engine.record.history if event['kind'] == 'override')['at'] == when
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize('reason,label', [
    ('worse', 'the test favored your current model'),
    ('too_close', 'the test did not establish a clear difference'),
    ('no_passing_test', 'no passing test was available'),
])
def test_override_status_and_desktop_log_explain_the_reason(live_switch, reason, label):
    _, switch, rpc, saved = live_switch
    switch.command('switch ' + saved['id'] + ' --anyway')
    with switch.engine.store.transaction() as holder:
        holder[0].override['reason'] = reason
        next(event for event in holder[0].history if event['kind'] == 'override')['reason'] = reason
    status = switch.command('status', terminal=True)
    assert f'Owner override ({label})' in status
    assert f'({reason})' not in status
    assert f'Owner override ({label})' in switch.panel()['status']
    row = next(row for row in rpc.page_state({})['switch']['log']['rows'] if 'Owner override' in row['text'])
    assert f'Owner override ({label})' in row['text']
    assert f'({reason})' not in row['text']
    assert switch.engine.store.read().override['reason'] == reason
