import sys
from collections import namedtuple
from types import ModuleType, SimpleNamespace

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL
from shadow.common.models import Model
from shadow.trial.plugin import register
from test_plugin import service


@pytest.fixture
def switch_api(monkeypatch):
    module = ModuleType('shadow.switch')
    module.is_active = lambda record: record is not None and record.phase in {'running', 'watch', 'paused'}
    module.Route = namedtuple('Route', 'provider model base_url api_mode')
    module.Proposal = lambda test_id, old, new, verdict: SimpleNamespace(test_id=test_id, old=old, new=new, verdict=verdict, allowed=True,
                                                                      gradual=(old.provider, old.base_url, old.api_mode) == (new.provider, new.base_url, new.api_mode))
    monkeypatch.setitem(sys.modules, 'shadow.switch', module)
    return module


@pytest.mark.parametrize('phase', ['running', 'watch', 'paused'])
def test_active_switch_refuses_trial_before_selection(tmp_path, switch_api, phase):
    ctx, plugin = service(tmp_path)
    ctx.shadow_switch = SimpleNamespace(engine=SimpleNamespace(record=SimpleNamespace(phase=phase)))
    plugin.selector = lambda **_: pytest.fail('an active switch must refuse turn selection')
    text = ctx.commands['shadow'](f'try {TRIAL.model}')
    assert text == 'A switch is in progress. Finish it or undo it first.'
    assert not plugin.store.list() and not ctx.capture


def test_trial_uses_exported_switch_activity_contract(tmp_path, switch_api):
    ctx, plugin = service(tmp_path)
    switch_api.is_active = lambda record: True
    ctx.shadow_switch = SimpleNamespace(engine=SimpleNamespace(record=None))
    assert ctx.commands['shadow'](f'try {TRIAL.model}') == 'A switch is in progress. Finish it or undo it first.'


@pytest.mark.parametrize('message', ['shadow-controller:unknown:token', 'shadow-controller:malformed'])
def test_controller_conversations_are_excluded_before_admission(tmp_path, switch_api, message):
    ctx, plugin = service(tmp_path)
    excluded = []
    ctx.shadow_switch = SimpleNamespace(engine=SimpleNamespace(exclude_session=excluded.append))
    replies = ctx.hook('pre_llm_call', session_id='controller', user_message=message)
    assert replies[0]['action'] == 'block'
    assert excluded == ['controller']
    ctx.hook('pre_llm_call', session_id='ordinary', user_message='Please inspect the code.')
    assert excluded == ['controller']


@pytest.mark.parametrize('catalog,expected', [
    ([CURRENT], (False, False)),
    ([CURRENT, TRIAL], (True, True)),
    ([CURRENT, TRIAL, OUTSIDE], (True, True)),
    ([CURRENT, Model('custom', 'unknown-model')], (True, True)),
    ([CURRENT, CURRENT], (False, False)),
])
def test_availability_accepts_a_distinct_comparison_with_independent_or_backup_judging(tmp_path, catalog, expected):
    ctx, plugin = service(tmp_path)
    plugin.catalog = catalog
    assert ctx.shadow_model_availability() == expected
    assert not plugin.store.list() and not ctx.capture


def test_unknown_current_maker_does_not_break_doctor_availability(tmp_path):
    ctx, plugin = service(tmp_path)
    plugin.current = Model('custom', 'unknown-model')
    plugin.catalog = [plugin.current, TRIAL, OUTSIDE]
    assert ctx.shadow_model_availability() == (True, True)


@pytest.mark.parametrize('verb', ['status', 'doctor', 'undo'])
def test_registering_trial_after_switch_preserves_composed_routes(tmp_path, verb):
    ctx, plugin = service(tmp_path)
    calls = []
    def composed(raw_args, **context):
        calls.append((raw_args, context))
        return 'composed ' + verb
    ctx.shadow_command_routes[verb] = composed
    register(ctx, plugin=plugin)
    assert ctx.commands['shadow'](verb, session_id='origin') == 'composed ' + verb
    assert calls == [(verb, {'session_id': 'origin'})]


def test_package_registration_preserves_switch_composition(tmp_path, switch_api, monkeypatch):
    import shadow
    from shadow import trial
    ctx, plugin = service(tmp_path)
    plugin.launcher.isolation_verified = False
    monkeypatch.setattr(trial, 'register', lambda ctx: register(ctx, plugin=plugin))
    monkeypatch.setattr(shadow, 'switch', switch_api, raising=False)
    def register_switch(ctx):
        trial_doctor = ctx.shadow_command_routes['doctor']
        ctx.shadow_command_routes['doctor'] = lambda raw_args, **kw: trial_doctor(raw_args, **kw) + '\nswitch diagnostics'
        ctx.shadow_command_routes['status'] = switch_api.composed_status
    switch_api.composed_status = lambda raw_args, **kw: 'composed status'
    switch_api.register = register_switch
    shadow.register(ctx)
    composed = ctx.shadow_command_routes['status']
    assert ctx.commands['shadow']('status') == 'composed status'
    assert composed is switch_api.composed_status
    doctor = ctx.commands['shadow']('doctor')
    assert 'isolation' in doctor.lower() and 'switch diagnostics' in doctor
    assert doctor.count('isolation') == 1


def saved_proposal(plugin, tmp_path):
    from test_cards import card_record
    record = card_record(tmp_path)
    record['routes'] = {side: {'provider': model.provider, 'model': model.model,
                              'base_url': 'https://api.openai.com/v1', 'api_mode': 'codex_responses'}
                        for side, model in [('current', CURRENT), ('trial', TRIAL)]}
    plugin.store.save(record)
    return record


def test_proposal_uses_only_public_switch_exports(tmp_path, switch_api):
    ctx, plugin = service(tmp_path)
    record = saved_proposal(plugin, tmp_path)
    proposal = ctx.shadow_proposal_lookup(record['id'])
    assert proposal.test_id == record['id']
    assert proposal.old == switch_api.Route(**record['routes']['current'])
    assert proposal.new == switch_api.Route(**record['routes']['trial'])


def test_proposal_is_unavailable_without_optional_switch(tmp_path, monkeypatch):
    ctx, plugin = service(tmp_path)
    record = saved_proposal(plugin, tmp_path)
    monkeypatch.setitem(sys.modules, 'shadow.switch', None)
    assert ctx.shadow_proposal_lookup(record['id']) is None


def test_proposal_refuses_switch_while_any_trial_runs(tmp_path, switch_api):
    from fixtures import selection
    from shadow.trial.engine import TrialEngine
    ctx, plugin = service(tmp_path)
    record = saved_proposal(plugin, tmp_path)
    running = TrialEngine(ctx, plugin.store, plugin.guard).prepare(selection(tmp_path), CURRENT, TRIAL, [OUTSIDE])
    running['state'] = 'queued'
    plugin.store.save(running)
    assert ctx.shadow_proposal_lookup(record['id']) is None


def test_no_id_proposal_skips_newer_ineligible_records(tmp_path, switch_api):
    from copy import deepcopy
    ctx, plugin = service(tmp_path)
    record = saved_proposal(plugin, tmp_path)
    newer = deepcopy(record)
    newer.update(id='newer', created_at=record['created_at'] + 1, routes={})
    plugin.store.save(newer)
    assert ctx.shadow_proposal_lookup().test_id == record['id']


def test_malformed_observed_routes_do_not_escape_proposal_lookup(tmp_path, switch_api):
    ctx, plugin = service(tmp_path)
    record = saved_proposal(plugin, tmp_path)
    record['routes']['trial'].pop('api_mode')
    plugin.store.save(record)
    assert ctx.shadow_proposal_lookup(record['id']) is None


def test_doctor_names_missing_comparison_and_judge_without_switch(tmp_path):
    ctx, plugin = service(tmp_path)
    plugin.catalog = [CURRENT]
    text = ctx.commands['shadow']('doctor')
    assert 'at least one other model to try' in text
    assert 'hermes model' in text
    assert 'a model that can act as judge' in text
    assert 'Third-maker judge: unavailable' in text
    assert 'Fix: hermes model' in text


@pytest.mark.parametrize('surface', ['result', 'watch'])
def test_card_discloses_an_unverified_exact_pair(tmp_path, switch_api, surface):
    ctx, plugin = service(tmp_path)
    record = saved_proposal(plugin, tmp_path)
    ctx.shadow_switch = SimpleNamespace(engine=SimpleNamespace(verified_pair=lambda *_: False))
    if surface == 'result':
        plugin.result(record)
        text, choices = ctx.questions[-1]
        assert 'switch' not in {choice['value'] for choice in choices}
    else:
        plugin.watch(record['id'])
        text = ctx.messages[-1]
    assert 'waiting for the exact model pair to pass its compatibility check' in text
    assert 'Switch gradually' not in text
