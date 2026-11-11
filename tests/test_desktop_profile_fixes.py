"""Real-profile Desktop previews use the trial line's local selection and costing."""

import contextvars
import json
import threading
from types import SimpleNamespace

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext, selection
from shadow.common.copy import load_copy
from shadow.common.models import Model
from shadow.common.storage import Store
from shadow.desktop_rpc import Copy, DesktopError, DesktopRpc, FixtureSource, build_start
from shadow.desktop_source import ProfileSource
from shadow.trial.plugin import TrialPlugin


COPY = load_copy()


@pytest.fixture
def profile(tmp_path):
    ctx = FakeContext()
    selected = selection(tmp_path)
    selected.total = 38
    selected.left_out = {'command': 3, 'trivial': 5}
    selected.scrub_counts = {'key': 2, 'email': 1}
    writes = []
    store = SimpleNamespace(list=lambda: [], get=lambda _: None, save=writes.append, prune=lambda: None)
    launcher = SimpleNamespace(isolation_verified=False, profile='default')
    instance = TrialPlugin(ctx, store=store, current=CURRENT,
                           catalog=[CURRENT, TRIAL, OUTSIDE],
                           selector=lambda **_: selected,
                           launcher=launcher,
                           config_reader=lambda: {})
    return ProfileSource(instance), ctx, writes


def test_real_profile_start_card_is_a_local_preview(profile):
    source, ctx, writes = profile
    card = DesktopRpc(source, COPY).start_card({'model': TRIAL.model})

    assert card['model'] == TRIAL.model
    assert card['blocked'] == COPY['trial.isolation_required']
    assert card['estimate'] > 0
    assert card['judge_models'] == [f'{OUTSIDE.provider}::{OUTSIDE.model}']
    assert not any(button['id'] == 'start' for button in card['buttons'])
    assert not any(row['key'] == 'wait' for row in card['rows'])
    assert any(button['id'] == 'cancel' for button in card['buttons'])
    assert ctx.capture == []
    assert ctx.questions == []
    assert writes == []
    assert source.trial.engine is None


def test_real_profile_facts_include_cap_scrubbing_and_plan_usage(profile):
    source, _, _ = profile
    facts = source.start_facts(TRIAL.model)

    assert facts['picked'] == 30
    assert facts['total'] == 38
    assert facts['days'] == 7
    assert facts['cap'] == 5.0
    assert facts['left_out'] == {'command': 3, 'trivial': 5}
    assert facts['scrub'] == {'keys': 2, 'emails': 1}
    assert facts['plan_tokens'] > 0
    assert facts['plan_cost'] > 0
    assert facts['tokens'] > facts['plan_tokens']
    assert facts['replay_estimate'] == 0
    assert facts['judges'][0]['cost'] > 0
    assert facts['judges'][0]['auto'] is True
    assert facts['judges'][0]['maker'] == 'Anthropic'
    assert facts['isolation_verified'] is False


def test_real_profile_model_list_deduplicates_routes_and_excludes_current(profile):
    source, _, _ = profile
    source.trial.catalog = [CURRENT, Model(CURRENT.provider, CURRENT.model), TRIAL, TRIAL,
                            Model('openai', TRIAL.model), OUTSIDE]

    rows = source.models()

    assert [(row['provider'], row['model']) for row in rows] == [
        (TRIAL.provider, f'{TRIAL.provider}::{TRIAL.model}'),
        ('openai', f'openai::{TRIAL.model}'),
        (OUTSIDE.provider, f'{OUTSIDE.provider}::{OUTSIDE.model}')]


@pytest.mark.parametrize('model', ['not-a-profile-model', CURRENT.model])
def test_real_profile_start_rejects_unknown_or_current_model(profile, model):
    source, ctx, writes = profile

    assert source.start_facts(model) is None
    assert ctx.capture == writes == []


def test_real_profile_preview_uses_both_contestants_without_an_outside_judge(profile):
    source, _, _ = profile
    source.trial.catalog = [CURRENT, TRIAL]
    facts = source.start_facts(TRIAL.model)

    assert facts['judge_mode'] == 'backup_both'
    assert facts['judges'] == []
    assert [row['model'] for row in facts['judge_models']] == [
        f'{TRIAL.provider}::{TRIAL.model}', f'{CURRENT.provider}::{CURRENT.model}']
    assert all(row['cost'] == 0 for row in facts['judge_models'])
    card = build_start(Copy(COPY), source, TRIAL.model)
    assert card['estimate'] == pytest.approx(
        facts['replay_estimate'] + sum(row['cost'] for row in facts['judge_models']))


def test_real_profile_preview_keeps_unknown_and_namespaced_makers(profile):
    from shadow.common.copy import copy
    source, ctx, writes = profile
    namespaced = Model('openrouter', 'x-ai/grok-4')
    unknown = Model('xai', 'grok-unlisted')
    source.trial.catalog = [CURRENT, TRIAL, namespaced, unknown]

    facts = source.start_facts(TRIAL.model)
    assert facts is not None
    assert copy('discovery.maker_unknown') in {row['maker'] for row in facts['judges']}
    card = build_start(Copy(COPY), source, TRIAL.model)
    assert card['blocked'] == COPY['trial.isolation_required']

    backup = source.start_facts(unknown.model)
    assert backup is not None
    assert backup['judge_mode'] == 'backup_both'
    assert copy('discovery.maker_unknown') in backup['hidden_maker']
    assert ctx.capture == writes == []


def test_real_profile_preview_matches_the_cli_cost_disclosure(profile):
    source, _, _ = profile
    card = build_start(Copy(COPY), source, TRIAL.model)
    cli = source.trial.command(f'try {TRIAL.model}')

    cost_row = next(row for row in card['rows'] if row['key'] == 'cost')
    assert cost_row['value'][0] in cli
    plan_sentence = cost_row['value'][-1]
    assert '$0.00' not in plan_sentence
    facts = source.start_facts(TRIAL.model)
    assert plan_sentence == COPY['start.plan'].format(
        tokens=facts['plan_tokens'], cost=facts['plan_cost'])
    assert f"${facts['plan_cost']:.2f}" in plan_sentence
    assert plan_sentence in cli


def test_real_profile_preview_exports_the_profile_and_keeps_the_sample_stable(profile, monkeypatch, tmp_path):
    source, _, writes = profile
    source.trial.selector = None
    source.trial.launcher.profile = 'shadow-demo'
    exported = []
    sessions = [{'id': f'chat{i}', 'cwd': str(tmp_path), 'messages': [
        {'id': f't{i}', 'role': 'user', 'content': f'Inspect and repair this particular sample task {i}.'},
        {'role': 'assistant', 'content': 'The task was completed.'}]} for i in range(40)]

    def export(profile_name, days):
        exported.append((profile_name, days))
        return sessions

    monkeypatch.setattr('shadow.trial.picking.export_sessions', export)
    first = build_start(Copy(COPY), source, TRIAL.model)
    second = build_start(Copy(COPY), source, TRIAL.model)

    assert first['consent'] == second['consent']
    assert exported == [('shadow-demo', 7), ('shadow-demo', 7)]
    assert writes == []


def test_real_profile_unknown_price_shows_a_blocked_preview(profile):
    source, ctx, writes = profile
    source.trial.launcher.isolation_verified = True
    unpriced = Model('openai', 'gpt-unpriced-fixture')
    source.trial.catalog.append(unpriced)

    card = build_start(Copy(COPY), source, unpriced.model)

    assert card['estimate'] is None
    assert 'price' in card['blocked'].lower()
    assert not any(button['id'] == 'start' for button in card['buttons'])
    assert ctx.capture == writes == []


@pytest.mark.parametrize('count', [0, 19])
def test_real_profile_too_few_turns_never_offers_start(profile, tmp_path, count):
    source, ctx, writes = profile
    source.trial.launcher.isolation_verified = True
    source.trial.selector = lambda **_: selection(tmp_path, count=count)

    card = build_start(Copy(COPY), source, TRIAL.model)

    assert '20' in card['blocked']
    assert not any(button['id'] == 'start' for button in card['buttons'])
    assert ctx.capture == writes == []


def test_real_profile_start_facts_resolves_provider_qualified_routes(profile):
    source, ctx, writes = profile
    source.trial.catalog = [CURRENT, TRIAL, Model('openai', TRIAL.model), OUTSIDE]

    openai_facts = source.start_facts(f'openai::{TRIAL.model}')
    codex_facts = source.start_facts(f'{TRIAL.provider}::{TRIAL.model}')

    assert openai_facts['trial']['provider'] == 'openai'
    assert openai_facts['trial']['model'] == TRIAL.model
    assert codex_facts['trial']['provider'] == TRIAL.provider
    assert ctx.capture == writes == []


def test_real_profile_start_refuses_an_unauthenticated_current_model(profile):
    source, ctx, writes = profile
    source.trial.current = Model(CURRENT.provider, CURRENT.model, reachable=False)
    source.trial.catalog = [source.trial.current, TRIAL, OUTSIDE]

    with pytest.raises(DesktopError) as error:
        build_start(Copy(COPY), source, TRIAL.model)

    assert error.value.key == 'trial.model_unknown'
    assert ctx.capture == writes == []


def test_web_lookup_setting_survives_a_new_context(tmp_path, profile):
    source, _, _ = profile
    path = tmp_path / 'plugin-settings.json'

    class PersistentContext:
        def get_config(self, key, default=None):
            saved = json.loads(path.read_text()) if path.exists() else {}
            return saved.get(key, default)

        def set_config(self, key, value):
            saved = json.loads(path.read_text()) if path.exists() else {}
            path.write_text(json.dumps({**saved, key: value}))

    source.trial.ctx = PersistentContext()
    source.set_web_lookups(False)
    reopened = ProfileSource(SimpleNamespace(ctx=PersistentContext()))

    assert reopened.settings() == {'web_lookups': False}
    source.set_web_lookups(True)
    assert reopened.settings() == {'web_lookups': True}


@pytest.fixture
def startable_profile(tmp_path):
    class Launcher:
        profile = 'shadow-demo'
        isolation_verified = True

        def __init__(self):
            self.launches = []
            self.error = None

        def launch(self, record):
            if self.error:
                raise self.error
            self.launches.append(record)
            return 12345

    ctx, launcher = FakeContext(), Launcher()
    store = Store(tmp_path / 'profile.sqlite')
    chosen = selection(tmp_path)
    trial = TrialPlugin(ctx, store=store, current=CURRENT, catalog=[CURRENT, TRIAL, OUTSIDE],
                        selector=lambda **_: chosen, launcher=launcher,
                        config_reader=lambda: {})
    trial.watch = lambda run_id: None
    source = ProfileSource(trial)
    yield source, DesktopRpc(source, COPY), launcher, ctx, chosen
    store.close()


def confirm(card):
    return {'action': 'start', 'model': card['model'], 'judge': card['judge_slug'],
            **{key: card[key] for key in ('judge_mode', 'judge_models', 'estimate', 'consent')}}


def test_live_preview_does_not_save_or_launch(startable_profile):
    source, rpc, launcher, ctx, _ = startable_profile
    card = rpc.start_card({'model': TRIAL.model})

    assert card['estimate'] > 0
    assert card['blocked'] is None
    assert any(button['id'] == 'start' for button in card['buttons'])
    assert any(row['key'] == 'wait' for row in card['rows'])
    assert source.tests() == []
    assert launcher.launches == ctx.capture == []


@pytest.mark.parametrize('stale', [False, True])
def test_missing_or_stale_desktop_consent_never_launches(startable_profile, stale):
    source, rpc, launcher, ctx, _ = startable_profile
    card = rpc.start_card({'model': TRIAL.model})
    params = confirm(card) if stale else {'action': 'start', 'model': TRIAL.model}
    if stale:
        source.set_web_lookups(False)

    reply = rpc.act(params)

    assert 'start_card' in reply
    assert source.tests() == []
    assert launcher.launches == ctx.capture == []


def test_confirmed_desktop_start_queues_and_launches_the_preview_once(startable_profile):
    source, rpc, launcher, ctx, chosen = startable_profile
    card = rpc.start_card({'model': TRIAL.model})
    reply = rpc.act(confirm(card), session_id='desktop-chat')

    assert reply['page']['mode'] == 'progress'
    assert len(launcher.launches) == 1
    record = source.test(reply['page']['test_id'])
    assert record['state'] == 'queued'
    assert record['controller_pid'] == 12345
    assert record['seed'] == chosen.seed
    assert [turn['id'] for turn in record['turns']] == [turn.id for turn in chosen.turns]
    assert record['estimate']['total'] == card['estimate']
    assert record['origin'] == {'session_id': 'desktop-chat', 'platform': 'desktop'}
    assert ctx.capture == []
    with pytest.raises(ValueError):
        rpc.act(confirm(card))
    assert len(source.tests()) == len(launcher.launches) == 1


def test_confirmed_desktop_start_uses_the_rpc_copy_for_both_consent_checks(startable_profile):
    source, _, launcher, _, _ = startable_profile
    table = {**COPY, 'start.wait': 'About {minutes} minutes. Custom profile copy.'}
    rpc = DesktopRpc(source, table)
    card = rpc.start_card({'model': TRIAL.model})

    reply = rpc.act(confirm(card))

    assert 'start_card' not in reply
    assert reply['page']['mode'] == 'progress'
    assert len(launcher.launches) == 1


@pytest.mark.parametrize('blocked', ['cap', 'isolation', 'switch'])
def test_blocked_desktop_confirmation_never_launches(startable_profile, tmp_path, blocked):
    source, rpc, launcher, ctx, _ = startable_profile
    if blocked == 'cap':
        source.trial.selector = lambda **_: selection(tmp_path, count=120)
    elif blocked == 'isolation':
        launcher.isolation_verified = False
    else:
        source.active_switch = lambda: {'id': 'active-switch'}
    card = rpc.start_card({'model': TRIAL.model})

    if blocked == 'isolation':
        assert card['blocked'] == COPY['trial.isolation_required']
        assert not any(button['id'] == 'start' for button in card['buttons'])
        assert not any(row['key'] == 'wait' for row in card['rows'])

    with pytest.raises(ValueError):
        rpc.act(confirm(card))

    assert launcher.launches == ctx.capture == []


def test_failed_desktop_controller_launch_is_saved_as_interrupted(startable_profile):
    source, rpc, launcher, ctx, _ = startable_profile
    launcher.error = OSError('fixture launch failed')
    card = rpc.start_card({'model': TRIAL.model})

    with pytest.raises(ValueError):
        rpc.act(confirm(card))

    assert len(source.tests()) == 1
    assert source.tests()[0]['state'] == 'interrupted'
    assert launcher.launches == ctx.capture == []


def test_desktop_judge_pick_keeps_the_selected_provider(startable_profile):
    source, rpc, launcher, ctx, _ = startable_profile
    routed = Model('openrouter', 'anthropic/claude-sonnet-4-5')
    source.trial.catalog = [CURRENT, TRIAL, routed, OUTSIDE]

    card = rpc.start_card({'model': f'{TRIAL.provider}::{TRIAL.model}',
                           'judge': f'{OUTSIDE.provider}::{OUTSIDE.model}'})
    slugs = [option['slug'] for option in card['judge']['options']]
    assert card['judge_slug'] == f'{OUTSIDE.provider}::{OUTSIDE.model}'
    assert f'{routed.provider}::{routed.model}' in slugs
    assert f'{OUTSIDE.provider}::{OUTSIDE.model}' in slugs

    reply = rpc.act(confirm(card))
    record = source.test(reply['page']['test_id'])
    assert record['judge']['models'][0]['provider'] == OUTSIDE.provider
    assert record['judge']['models'][0]['model'] == OUTSIDE.model
    assert len(launcher.launches) == 1
    assert ctx.capture == []


def test_priced_judge_can_replace_an_unpriced_auto_judge(startable_profile):
    source, rpc, launcher, ctx, _ = startable_profile
    unknown = Model('anthropic', 'claude-unpriced-fixture')
    source.trial.catalog = [CURRENT, TRIAL, unknown, OUTSIDE]

    auto = rpc.start_card({'model': TRIAL.model})
    picked = rpc.start_card({'model': TRIAL.model, 'judge': OUTSIDE.model})

    assert auto['blocked'] is not None
    assert picked['blocked'] is None
    assert picked['estimate'] > 0
    reply = rpc.act(confirm(picked))
    record = source.test(reply['page']['test_id'])
    assert record['judge']['models'][0]['model'] == OUTSIDE.model
    assert record['judge_source'] == 'user'
    assert len(launcher.launches) == 1
    assert ctx.capture == []


def test_picking_an_unpriced_judge_blocks_an_otherwise_priced_preview(profile):
    source, ctx, writes = profile
    unknown = Model('anthropic', 'claude-unpriced-fixture')
    source.trial.catalog.append(unknown)

    card = DesktopRpc(source, COPY).start_card({'model': TRIAL.model, 'judge': unknown.model})

    assert card['blocked'] is not None
    assert card['estimate'] is None
    assert not any(button['id'] == 'start' for button in card['buttons'])
    assert ctx.capture == writes == []


def test_unpriced_selected_judge_is_named_on_the_start_card(profile):
    source, ctx, writes = profile
    source.trial.launcher.isolation_verified = True
    unknown = Model('anthropic', 'claude-unpriced-fixture')
    source.trial.catalog = [CURRENT, TRIAL, OUTSIDE, unknown]

    card = DesktopRpc(source, COPY).start_card({'model': TRIAL.model, 'judge': unknown.model})

    assert card['blocked'] == Copy(COPY)('start.no_price', model=unknown.name)
    assert card['estimate'] is None
    assert not any(button['id'] == 'start' for button in card['buttons'])
    assert ctx.capture == writes == []


def test_backup_both_unpriced_trial_names_the_trial_once(profile):
    source, ctx, writes = profile
    source.trial.launcher.isolation_verified = True
    trying = Model('anthropic', 'claude-unpriced-fixture')
    source.trial.catalog = [CURRENT, trying]

    card = DesktopRpc(source, COPY).start_card({'model': trying.model})

    assert card['judge_mode'] == 'backup_both'
    assert card['blocked'] == Copy(COPY)('start.no_price', model=trying.name)
    assert not any(button['id'] == 'start' for button in card['buttons'])
    assert ctx.capture == writes == []


def test_priority_service_tier_blocks_the_start_card_and_judge_costs(startable_profile):
    source, rpc, launcher, ctx, _ = startable_profile
    source.trial.config_reader = lambda: {'agent': {'service_tier': 'priority'}}
    card = rpc.start_card({'model': TRIAL.model})
    notice = Copy(COPY)('start.service_tier')

    assert card['blocked'] == notice
    assert not any(button['id'] == 'start' for button in card['buttons'])
    assert all(option['cost'] == notice for option in card['judge']['options'])
    assert launcher.launches == ctx.capture == []


def test_confirmed_desktop_start_stores_frozen_prices(startable_profile):
    source, rpc, _, _, _ = startable_profile
    card = rpc.start_card({'model': TRIAL.model})
    record = source.test(rpc.act(confirm(card))['page']['test_id'])

    assert record['prices']
    assert record['pricing_blocker'] is None


def test_desktop_started_test_can_be_stopped(startable_profile):
    source, rpc, launcher, ctx, _ = startable_profile
    card = rpc.start_card({'model': TRIAL.model})
    started = rpc.act(confirm(card))
    test_id = started['page']['test_id']
    updated = source.test(test_id)
    updated['spend'] = .5
    source.trial.store.save(updated)

    stopped = rpc.act({'action': 'stop_test', 'test_id': test_id})

    assert stopped['page']['mode'] == 'stopped'
    record = source.test(test_id)
    assert record['state'] == record['stop_reason'] == 'cancelled'
    assert record['spend'] == .5
    assert source.trial.active_record() is None
    assert len(launcher.launches) == 1
    assert ctx.capture == []


def test_setting_change_between_rpc_consent_and_launch_refreshes_the_card(startable_profile):
    source, rpc, launcher, ctx, _ = startable_profile
    source.set_web_lookups(False)
    card = rpc.start_card({'model': TRIAL.model})
    act = source.act

    def change_setting(action, params):
        source.set_web_lookups(True)
        source.start_facts(TRIAL.model)
        return act(action, params)

    source.act = change_setting

    reply = rpc.act(confirm(card))

    assert reply['start_card']['consent'] != card['consent']
    assert source.tests() == []
    assert launcher.launches == ctx.capture == []


def test_next_desktop_test_draws_a_fresh_sample_after_successful_launch(startable_profile, monkeypatch, tmp_path):
    source, rpc, launcher, _, _ = startable_profile
    sessions = [{'id': f'chat{i}', 'cwd': str(tmp_path), 'messages': [
        {'id': f't{i}', 'role': 'user', 'content': f'Inspect and repair this particular sample task {i}.'},
        {'role': 'assistant', 'content': 'The task was completed.'}]} for i in range(40)]
    source.trial.selector = None
    source._preview_seed = 42
    monkeypatch.setattr('shadow.trial.picking.export_sessions', lambda *args: sessions)
    monkeypatch.setattr('shadow.desktop_source.secrets.randbits', lambda bits: 43)

    first_card = rpc.start_card({'model': TRIAL.model})
    assert first_card == rpc.start_card({'model': TRIAL.model})
    first = rpc.act(confirm(first_card))['page']['test_id']
    rpc.act({'action': 'stop_test', 'test_id': first})
    second_card = rpc.start_card({'model': TRIAL.model})
    second = rpc.act(confirm(second_card))['page']['test_id']

    first_record, second_record = source.test(first), source.test(second)
    assert first_record['seed'] == 42
    assert second_record['seed'] == 43
    assert [t['id'] for t in first_record['turns']] != [t['id'] for t in second_record['turns']]
    assert len(launcher.launches) == 2


def test_desktop_start_prunes_expired_saved_tests(startable_profile):
    source, rpc, _, _, _ = startable_profile
    expired = FixtureSource('result').tests()[0]
    expired.update(id='expired', created_at=0)
    source.trial.store.save(expired)

    reply = rpc.act(confirm(rpc.start_card({'model': TRIAL.model})))

    assert source.test('expired') is None
    assert [record['id'] for record in source.tests()] == [reply['page']['test_id']]


def test_desktop_watch_reconciles_a_dead_controller_in_a_daemon_with_caller_context(startable_profile):
    source, rpc, launcher, _, _ = startable_profile
    done = threading.Event()
    caller = contextvars.ContextVar('desktop_caller', default=None)
    observed = []
    launcher.is_alive = lambda pid: False

    def finish(_):
        record = source.trial.store.get()
        record.update(state='cancelled', stop_reason='cancelled')
        source.trial.store.save(record)

    def watch(run_id):
        try:
            TrialPlugin.watch(source.trial, run_id)
            observed.append((threading.current_thread().daemon, caller.get()))
        finally:
            done.set()

    source.trial.watch_wait = finish
    source.trial.watch = watch
    token = caller.set('desktop-request')
    try:
        rpc.act(confirm(rpc.start_card({'model': TRIAL.model})))
        assert done.wait(2), 'Desktop did not start its controller watcher'
    finally:
        caller.reset(token)
        done.wait(2)

    assert observed == [(True, 'desktop-request')]
    assert len(launcher.launches) == 1
    record = source.trial.store.get()
    assert record['restarts'] == 0
    assert record['state'] == 'interrupted' and record['stop_reason'] == 'controller_exited'
