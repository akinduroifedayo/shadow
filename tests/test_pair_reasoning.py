from copy import deepcopy

import pytest

from shadow.switch.domain import Proposal, Route
from shadow.switch.pair_probe import PairProbe, run_command
from shadow.switch.plugin import verified_pair_from_config
from test_pair_probe import BASE, MODE, NEW, OLD, PROVIDER, LiveCompletions, _service, hermes_agent_context


def config_with_reasoning(effort='high'):
    return {'agent': {'reasoning_effort': effort}, 'model_overrides': {
        PROVIDER: {OLD: {'supports_reasoning': True}, NEW: {'supports_reasoning': True}}}}


def proof_run(tmp_path, config, mutate=None):
    live, written = LiveCompletions(mutate=mutate), []
    probe = PairProbe(ctx=live,
                      write=lambda entry: written.append(entry) or True, evidence_dir=tmp_path,
                      config=config)
    return probe, live, written


@pytest.mark.parametrize('effort', ['high', 'low'])
def test_reasoning_pair_fails_closed_without_public_sealed_evidence(tmp_path, hermes_agent_context, effort):
    probe, live, written = proof_run(tmp_path, config_with_reasoning(effort))
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'sealed_reasoning_unobservable'
    assert not live.calls and not written


def test_capable_model_fails_closed_even_without_explicit_effort(tmp_path, hermes_agent_context):
    config = config_with_reasoning()
    config.pop('agent')
    probe, live, written = proof_run(tmp_path, config)
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'sealed_reasoning_unobservable'
    assert not live.calls and not written


def test_pair_reasoning_retains_the_old_models_settings(hermes_agent_context):
    from shadow.switch.reasoning import pair_reasoning
    config = config_with_reasoning()
    config['agent']['reasoning_overrides'] = {NEW: 'low'}
    reasoning, required = pair_reasoning(config, PROVIDER, OLD)
    assert required
    assert reasoning == {'old': {'enabled': True, 'effort': 'high'},
                         'new': {'enabled': True, 'effort': 'high'}}


def test_disabled_reasoning_pair_proof_is_invalid_after_reasoning_is_enabled(tmp_path, hermes_agent_context):
    config = config_with_reasoning('none')
    probe, live, written = proof_run(tmp_path, config)
    assert probe.run(PROVIDER, OLD, NEW)
    assert written[0]['sealed_reasoning_observed'] is False
    config['plugins'] = {'entries': {'shadow': {'settings': {'verified_gradual_pairs': written}}}}
    old, new = Route(PROVIDER, OLD, BASE, MODE), Route(PROVIDER, NEW, BASE, MODE)
    assert verified_pair_from_config(lambda: config, old, new)
    config['agent']['reasoning_effort'] = 'high'
    assert not verified_pair_from_config(lambda: config, old, new)


@pytest.mark.parametrize('phase', ['running', 'done'])
@pytest.mark.parametrize('sealed_verified', [False, True])
@pytest.mark.parametrize('field', ['messages', 'input'])
def test_live_sealed_history_requires_sealed_pair_evidence_before_rewrite(
        tmp_path, hermes_agent_context, phase, sealed_verified, field):
    import sqlite3
    from shadow.switch import Engine, Store

    config = {}
    probe, live, written = proof_run(tmp_path, config)
    assert probe.run(PROVIDER, OLD, NEW)
    written[0]['sealed_reasoning_observed'] = sealed_verified
    config['plugins'] = {'entries': {'shadow': {'settings': {'verified_gradual_pairs': written}}}}
    engine = Engine(Store(sqlite3.connect(':memory:')),
                    verified_pair=lambda old, new: verified_pair_from_config(lambda: config, old, new))
    engine.verified_sealed_pair = lambda old, new: verified_pair_from_config(
        lambda: config, old, new, sealed_required=True)
    engine.start(Proposal('saved', Route(PROVIDER, OLD, BASE, MODE), Route(PROVIDER, NEW, BASE, MODE), 'better'))
    engine.advance()
    engine.advance()
    engine.session_started(session_id='user', platform='cli')
    if phase == 'done':
        with engine.store.transaction() as holder:
            holder[0].phase = 'done'
            session_id = next(iter(holder[0].sessions))
            holder[0].assignments[session_id] = 'new'
            holder[0].draining[session_id] = 'new'
    event = dict(session_id='user', task_id='task', turn_id='turn')
    engine.admit(**event, model=OLD, platform='cli')
    request = {'model': OLD, field: [{'role': 'assistant', 'codex_reasoning_items': [
        {'type': 'reasoning', 'encrypted_content': 'session-override-sealed-canary'}]}]}
    routed = engine.rewrite(**event, provider=PROVIDER, model=OLD, base_url=BASE, api_mode=MODE,
                            api_request_id='request', request=request)
    assert (routed['request']['model'] if routed else request['model']) == (NEW if sealed_verified else OLD)
    followup = engine.rewrite(**event, provider=PROVIDER, model=OLD, base_url=BASE, api_mode=MODE,
                             api_request_id='followup', request={'model': OLD, field: []})
    assert (followup['request']['model'] if followup else OLD) == (NEW if sealed_verified else OLD)
    if phase == 'running':
        engine.response(**event, provider=PROVIDER, base_url=BASE, api_mode=MODE,
                        api_request_id='request', response_model=NEW if sealed_verified else OLD)
        engine.finish(**event, completed=True, failed=False, interrupted=False)
        assert engine.record.counts('new').turns == int(sealed_verified)
        assert engine.record.counts('old').turns == int(not sealed_verified)


@pytest.mark.parametrize('phase', ['running', 'done'])
def test_sealed_followup_preserves_a_turn_already_sent_to_the_new_model(phase, hermes_agent_context):
    import sqlite3
    from shadow.switch import Engine, Store

    engine = Engine(Store(sqlite3.connect(':memory:')), verified_pair=lambda old, new: True)
    engine.start(Proposal('saved', Route(PROVIDER, OLD, BASE, MODE), Route(PROVIDER, NEW, BASE, MODE), 'better'))
    engine.advance()
    engine.advance()
    engine.session_started(session_id='user', platform='cli')
    if phase == 'done':
        with engine.store.transaction() as holder:
            holder[0].phase = 'done'
            session_id = next(iter(holder[0].sessions))
            holder[0].assignments[session_id] = 'new'
            holder[0].draining[session_id] = 'new'
    event = dict(session_id='user', task_id='task', turn_id='turn', provider=PROVIDER,
                 model=OLD, base_url=BASE, api_mode=MODE)
    engine.admit(**event, platform='cli')
    first = engine.rewrite(**event, api_request_id='first', request={'model': OLD, 'messages': []})
    assert first['request']['model'] == NEW
    followup = engine.rewrite(**event, api_request_id='followup', request={'model': OLD, 'messages': [
        {'role': 'assistant', 'codex_reasoning_items': [
            {'type': 'reasoning', 'encrypted_content': 'new-model-sealed-canary', '_issuer_model': NEW}]}]})
    assert (followup['request']['model'] if followup else OLD) == NEW


def test_done_sealed_request_already_on_new_model_is_not_rewritten():
    import sqlite3
    from shadow.switch import Engine, Store

    engine = Engine(Store(sqlite3.connect(':memory:')), verified_pair=lambda old, new: True)
    engine.start(Proposal('saved', Route(PROVIDER, OLD, BASE, MODE), Route(PROVIDER, NEW, BASE, MODE), 'better'))
    engine.session_started(session_id='user', platform='cli')
    with engine.store.transaction() as holder:
        holder[0].phase = 'done'
        session_id = next(iter(holder[0].sessions))
        holder[0].assignments[session_id] = 'new'
        holder[0].draining[session_id] = 'new'
    event = dict(session_id='user', task_id='task', turn_id='turn', provider=PROVIDER,
                 model=NEW, base_url=BASE, api_mode=MODE)
    engine.admit(**event, platform='cli')
    request = {'model': NEW, 'messages': [{'role': 'assistant', 'codex_reasoning_items': [
        {'type': 'reasoning', 'encrypted_content': 'new-model-sealed-canary'}]}]}
    assert engine.rewrite(**event, api_request_id='request', request=request) is None
    assert engine.rewrite(**event, api_request_id='followup', request=request) is None


def test_pair_alias_is_canonical_in_probe_and_saved_proof(tmp_path, monkeypatch, hermes_agent_context):
    from shadow.switch import pair_probe

    live, written = LiveCompletions(), []
    service = _service(require=lambda provider, model: type('Found', (), {'provider': PROVIDER, 'model': model})())
    service.ctx.register_hook = live.register_hook
    service.ctx.register_auxiliary_task = live.register_auxiliary_task
    service.ctx.llm = live
    monkeypatch.setattr(pair_probe, 'profile_home', lambda: tmp_path)
    monkeypatch.setattr(pair_probe, 'save_pair', lambda path, entry: written.append(entry) or True)
    output = run_command(service, terminal=False, yes=True, provider='custom:lab', old_model=OLD, new_model=NEW)
    assert pair_probe.copy('doctor.pair_pass') in output
    assert all(route['provider'] == PROVIDER for route in (written[0]['old'], written[0]['new']))
    assert written[0]['old']['provider'] == written[0]['new']['provider'] == PROVIDER


def test_cross_provider_saved_pair_has_a_specific_refusal(monkeypatch):
    from shadow.switch import pair_probe

    proposal = Proposal('saved', Route(PROVIDER, OLD, BASE, MODE), Route('other', NEW, BASE, MODE), 'better')
    output = run_command(_service(proposal=proposal), terminal=False, yes=True)
    assert 'cross_provider_pair' in output
    assert 'same provider' in output and 'save an eligible test first' not in output
