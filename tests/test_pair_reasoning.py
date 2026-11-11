from copy import deepcopy

import pytest

from shadow.switch.domain import Proposal, Route
from shadow.switch.pair_probe import PairProbe, run_command
from shadow.switch.plugin import verified_pair_from_config
from test_pair_probe import BASE, MODE, NEW, OLD, PROVIDER, LiveAgents, _service, hermes_agent_context


def config_with_reasoning(effort='high'):
    return {'agent': {'reasoning_effort': effort}, 'model_overrides': {
        PROVIDER: {OLD: {'supports_reasoning': True}, NEW: {'supports_reasoning': True}}}}


def proof_run(tmp_path, config, mutate=None):
    live, written = LiveAgents(mutate=mutate), []
    probe = PairProbe(agent_factory=live.agent, register_hook=live.register_hook,
                      write=lambda entry: written.append(entry) or True, evidence_dir=tmp_path,
                      config=config)
    return probe, live, written


@pytest.mark.parametrize('history', [
    {'codex_reasoning_items': [{'type': 'reasoning', 'encrypted_content': 'sealed-canary'}]},
    {'content': [{'type': 'thinking', 'thinking': 'work', 'signature': 'sealed-canary'},
                 {'type': 'text', 'text': 'pebble'}]},
    {'thinking_blocks': [{'type': 'redacted_thinking', 'data': 'sealed-canary'}]},
    {'extra_content': {'google': {'thought_signature': 'sealed-canary'}}},
    {'reasoning_details': [{'type': 'reasoning.encrypted', 'signature': 'sealed-canary'}]},
    {'reasoning_details': [{'type': 'reasoning.encrypted', 'data': 'sealed-canary'}]},
])
@pytest.mark.parametrize('replayed', [False, True])
def test_reasoning_proof_requires_sealed_history_and_filters_each_provider_shape(
        tmp_path, hermes_agent_context, history, replayed):
    def mutate(turn, result, before, after):
        if turn == 0:
            answer = result['messages'][-1]
            answer.pop('codex_reasoning_items')
            answer.update(deepcopy(history))
        elif replayed:
            before['request_messages'].append({'role': 'assistant', **deepcopy(history)})

    probe, live, written = proof_run(tmp_path, config_with_reasoning(), mutate)
    live.converter = lambda messages, model: [
        {'role': message['role'], 'content': 'pebble' if message['role'] == 'assistant' else message['content']}
        for message in messages]
    assert probe.run(PROVIDER, OLD, NEW) is (not replayed)
    if replayed:
        assert probe.reason_code == 'turn_2_encrypted_reasoning_replayed'
        assert not written
    else:
        assert written[0]['sealed_reasoning_observed'] is True
        assert written[0]['reasoning'] == {
            'old': {'enabled': True, 'effort': 'high'}, 'new': {'enabled': True, 'effort': 'high'}}


def test_capable_model_requests_reasoning_and_refuses_plain_history(tmp_path, hermes_agent_context):
    config = config_with_reasoning()
    config.pop('agent')
    probe, live, written = proof_run(tmp_path, config, lambda n, r, b, a: r['messages'][-1].pop('codex_reasoning_items'))
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert live.options[0]['reasoning_config'] == {'enabled': True}
    assert probe.reason_code == 'turn_1_sealed_reasoning_missing'
    assert not written


def test_probe_preserves_current_agent_reasoning_across_model_only_switch(tmp_path, hermes_agent_context):
    config = config_with_reasoning()
    config['agent']['reasoning_overrides'] = {NEW: 'low'}
    probe, live, written = proof_run(tmp_path, config)
    assert probe.run(PROVIDER, OLD, NEW)
    assert [options['reasoning_config'] for options in live.options] == [
        {'enabled': True, 'effort': 'high'}, {'enabled': True, 'effort': 'high'}]
    assert written[0]['reasoning']['new'] == {'enabled': True, 'effort': 'high'}

    from shadow.switch import Engine, Store
    import sqlite3
    engine = Engine(Store(sqlite3.connect(':memory:')), verified_pair=lambda old, new: True)
    engine.start(Proposal('saved', Route(PROVIDER, OLD, BASE, MODE), Route(PROVIDER, NEW, BASE, MODE), 'better'))
    engine.advance()
    engine.advance()
    engine.session_started(session_id='user', platform='cli')
    event = dict(session_id='user', task_id='task', turn_id='turn')
    engine.admit(**event, model=OLD, platform='cli')
    result = engine.rewrite(**event, provider=PROVIDER, model=OLD, base_url=BASE, api_mode=MODE,
                            api_request_id='request', request={'model': OLD, 'messages': [],
                            'extra_body': {'reasoning': {'enabled': True, 'effort': 'high'}}})
    assert result['request']['model'] == NEW
    assert result['request']['extra_body']['reasoning'] == written[0]['reasoning']['new']


def test_switch_only_accepts_matching_reasoning_and_live_sealed_evidence(tmp_path, hermes_agent_context):
    config = config_with_reasoning()
    probe, live, written = proof_run(tmp_path, config)
    assert probe.run(PROVIDER, OLD, NEW)
    config['plugins'] = {'entries': {'shadow': {'settings': {'verified_gradual_pairs': written}}}}
    old, new = Route(PROVIDER, OLD, BASE, MODE), Route(PROVIDER, NEW, BASE, MODE)
    assert verified_pair_from_config(lambda: config, old, new)
    written[0]['sealed_reasoning_observed'] = False
    assert not verified_pair_from_config(lambda: config, old, new)
    written[0]['sealed_reasoning_observed'] = True
    config['agent']['reasoning_effort'] = 'low'
    assert not verified_pair_from_config(lambda: config, old, new)
    config['agent']['reasoning_effort'] = 'high'
    del written[0]['reasoning']
    assert not verified_pair_from_config(lambda: config, old, new)


def test_disabled_reasoning_can_verify_a_capable_model_without_sealed_history(tmp_path, hermes_agent_context):
    config = config_with_reasoning('none')
    probe, live, written = proof_run(
        tmp_path, config, lambda n, r, b, a: r['messages'][-1].pop('codex_reasoning_items'))
    assert probe.run(PROVIDER, OLD, NEW)
    assert all(options['reasoning_config'] == {'enabled': False} for options in live.options)
    assert written[0]['sealed_reasoning_observed'] is False
    config['plugins'] = {'entries': {'shadow': {'settings': {'verified_gradual_pairs': written}}}}
    assert verified_pair_from_config(lambda: config, Route(PROVIDER, OLD, BASE, MODE), Route(PROVIDER, NEW, BASE, MODE))


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

    live, written = LiveAgents(), []
    service = _service(require=lambda provider, model: type('Found', (), {'provider': PROVIDER, 'model': model})())
    service.ctx.register_hook = live.register_hook
    monkeypatch.setattr(pair_probe, '_agent_factory', live.agent)
    monkeypatch.setattr(pair_probe, 'profile_home', lambda: tmp_path)
    monkeypatch.setattr(pair_probe, 'save_pair', lambda path, entry: written.append(entry) or True)
    output = run_command(service, terminal=False, yes=True, provider='custom:lab', old_model=OLD, new_model=NEW)
    assert pair_probe.copy('doctor.pair_pass') in output
    assert all(options['provider'] == PROVIDER for options in live.options)
    assert written[0]['old']['provider'] == written[0]['new']['provider'] == PROVIDER


def test_cross_provider_saved_pair_has_a_specific_refusal(monkeypatch):
    from shadow.switch import pair_probe

    proposal = Proposal('saved', Route(PROVIDER, OLD, BASE, MODE), Route('other', NEW, BASE, MODE), 'better')
    monkeypatch.setattr(pair_probe, '_agent_factory', lambda **kwargs: pytest.fail('agent created'))
    output = run_command(_service(proposal=proposal), terminal=False, yes=True)
    assert 'cross_provider_pair' in output
    assert 'same provider' in output and 'save an eligible test first' not in output
