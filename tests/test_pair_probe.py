import copy
import json
import sys
from types import SimpleNamespace

import pytest
import yaml

from fixtures import FakeContext
from test_probe_dispatch import hermes_dispatch
from test_real_tools import require_hermes_reference
from shadow.switch.pair_probe import PairProbe, clean_route, run_command, save_pair


@pytest.fixture(autouse=True)
def hermes_agent_context(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(require_hermes_reference()))
    monkeypatch.setitem(sys.modules, 'hermes_yaml', yaml)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes-home'))


OLD = 'unknown-maker/river-small'
NEW = 'unknown-maker/river-large'
PROVIDER = 'openrouter'
BASE = 'https://example.invalid/v1'
MODE = 'chat_completions'


def _route(model):
    return {'provider': PROVIDER, 'model': model, 'response_model': model + '-2026-11-10',
            'base_url': BASE, 'api_mode': MODE}


class LiveAgents:
    def __init__(self, mutate=None, hook='post_api_request'):
        self.hooks, self.calls, self.options, self.closed = {}, [], [], []
        self.mutate = mutate or (lambda turn, result, before, after: None)
        self.hook = hook
        self.converter = None

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)
        return SimpleNamespace(dispose=lambda: self.hooks[name].remove(callback))

    def emit(self, name, event):
        for callback in self.hooks.get(name, []):
            callback(**event)

    def agent(self, **options):
        self.options.append(options)
        owner = self

        class Agent:
            tools = []

            def run_conversation(self, user_message, *, conversation_history=None):
                assert self._persist_disabled is True
                assert self.compression_enabled is False
                turn = len(owner.calls)
                history = copy.deepcopy(conversation_history or [])
                owner.calls.append((options['model'], user_message, history))
                messages = history + [{'role': 'user', 'content': user_message}]
                request_messages = [dict(message) for message in messages]
                for message in request_messages:
                    message.pop('codex_reasoning_items', None)
                    if message['role'] == 'assistant':
                        message['type'] = 'message'
                        message['content'] = [{'type': 'output_text', 'text': message['content']}]
                if owner.converter is not None:
                    request_messages = owner.converter(copy.deepcopy(messages), options['model'])
                before = dict(_route(options['model']), session_id=options['session_id'],
                              conversation_history=copy.deepcopy(messages), request_messages=request_messages)
                after = dict(_route(options['model']), session_id=options['session_id'], finish_reason='stop')
                answer = {'role': 'assistant', 'content': 'pebble',
                          'codex_reasoning_items': [{'type': 'reasoning', 'encrypted_content': 'private-live-blob',
                                                     '_issuer_kind': 'codex_backend', '_issuer_model': options['model']}]}
                result = dict(_route(options['model']), completed=True, failed=False, partial=False,
                              interrupted=False, final_response=answer['content'], messages=messages + [answer], api_calls=1)
                owner.mutate(turn, result, before, after)
                owner.emit('pre_api_request', before)
                owner.emit(owner.hook, after)
                return result

            def close(self):
                owner.closed.append(options['model'])

        return Agent()


def _probe(tmp_path, live, written):
    return PairProbe(agent_factory=live.agent, register_hook=live.register_hook,
                     write=lambda entry: written.append(entry) or True, evidence_dir=tmp_path)


def test_live_agent_history_can_authorize_gradual_pair(tmp_path):
    live, written = LiveAgents(), []
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is True
    assert [call[0] for call in live.calls] == [OLD, NEW]
    history = live.calls[1][2]
    assert history[1]['codex_reasoning_items'][0]['encrypted_content'] == 'private-live-blob'
    assert history[0]['role'] == 'user' and history[1]['content'] == 'pebble'
    assert written[0]['probe_passed'] is True
    body = json.loads((tmp_path / 'pair-evidence.json').read_text())
    assert body['live_history_handoff'] is True
    assert body['reasoning_check'] == 'preserved_when_present'
    assert body['encrypted_reasoning_replayed'] is False
    assert body['probe_passed'] is True
    assert 'private-live-blob' not in json.dumps(body)
    assert live.closed == [OLD, NEW]
    assert all(not callbacks for callbacks in live.hooks.values())
    assert all(option['enabled_toolsets'] == [] and option['skip_memory'] and option['skip_context_files']
               and option['skip_background_review'] and option['session_db'] is None
               and option['parent_session_id'] == 'shadow-pair-probe' for option in live.options)
    assert all('api_mode' not in option and 'base_url' not in option and option['reasoning_config'] is None
               for option in live.options)
    assert (tmp_path / 'pair-evidence.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('turn', [0, 1])
@pytest.mark.parametrize('field,value', [('completed', False), ('failed', True), ('partial', True),
                                        ('interrupted', True), ('error', 'failure'), ('cleanup_errors', ['failure']),
                                        ('provider', 'other'), ('model', 'wrong'), ('final_response', '')])
def test_live_pair_refuses_failed_or_wrong_agent_turn(tmp_path, turn, field, value):
    live = LiveAgents(mutate=lambda n, result, before, after: result.update({field: value}) if n == turn else None)
    written = []
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is False
    assert written == [] and not (tmp_path / 'pair-evidence.json').exists()
    assert all(not callbacks for callbacks in live.hooks.values())


@pytest.mark.parametrize('turn', [0, 1])
@pytest.mark.parametrize('field,value', [('provider', 'other'), ('model', 'wrong'), ('response_model', None),
                                        ('response_model', 'wrong'), ('base_url', ''), ('api_mode', ''), ('error', 'failure')])
def test_live_pair_requires_actual_response_identity(tmp_path, turn, field, value):
    live = LiveAgents(mutate=lambda n, result, before, after: after.update({field: value}) if n == turn else None)
    written = []
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is False
    assert written == [] and not (tmp_path / 'pair-evidence.json').exists()


def test_live_pair_refuses_a_different_variant_with_the_requested_prefix(tmp_path):
    live = LiveAgents(mutate=lambda n, result, before, after: after.update(response_model=OLD + '-different') if n == 0 else None)
    written = []
    probe = _probe(tmp_path, live, written)
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'turn_1_response_route'
    assert written == []


def test_live_pair_accepts_models_without_encrypted_reasoning(tmp_path):
    live = LiveAgents(mutate=lambda n, result, before, after: result['messages'][-1].pop('codex_reasoning_items') if n == 0 else None)
    written = []
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is True
    assert len(live.calls) == 2 and len(written) == 1


def test_live_pair_requires_real_reasoning_before_second_call(tmp_path):
    live = LiveAgents(mutate=lambda n, result, before, after: result['messages'][-1].pop('codex_reasoning_items') if n == 0 else None)
    written = []
    probe = PairProbe(agent_factory=live.agent, register_hook=live.register_hook,
                      write=lambda entry: written.append(entry) or True, evidence_dir=tmp_path,
                      config={'agent': {'reasoning_effort': 'high'}})
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'turn_1_sealed_reasoning_missing'
    assert len(live.calls) == 1 and written == []


def test_live_pair_preserves_plain_reasoning_without_requiring_sealed_data(tmp_path):
    def reasoning(n, result, before, after):
        if n == 0:
            answer = result['messages'][-1]
            answer.pop('codex_reasoning_items')
            answer['reasoning_content'] = 'offline reasoning canary'
    live, written = LiveAgents(mutate=reasoning), []
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW)
    assert live.calls[1][2][-1]['reasoning_content'] == 'offline reasoning canary'
    assert 'offline reasoning canary' not in (tmp_path / 'pair-evidence.json').read_text()


def test_live_pair_rejects_sealed_data_in_any_provider_history_shape(tmp_path):
    sealed = 'sealed-"line\ncanary-β'
    def reasoning(n, result, before, after):
        if n == 0:
            answer = result['messages'][-1]
            answer.pop('codex_reasoning_items')
            answer['provider_history'] = {'reasoning': [{'encrypted_content': sealed}]}
    live, written = LiveAgents(mutate=reasoning), []
    probe = _probe(tmp_path, live, written)
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'turn_2_encrypted_reasoning_replayed'
    assert written == [] and not (tmp_path / 'pair-evidence.json').exists()


def test_live_pair_accepts_any_shared_hermes_endpoint_and_transport(tmp_path):
    def routes(n, result, before, after):
        before.update(base_url='https://unfamiliar.example.invalid/runtime', api_mode='native_messages')
        after.update(base_url='https://unfamiliar.example.invalid/runtime', api_mode='native_messages')
    written = []
    assert _probe(tmp_path, LiveAgents(mutate=routes), written).run(PROVIDER, OLD, NEW)
    assert written[0]['old']['api_mode'] == 'native_messages'
    assert written[0]['new']['base_url'] == 'https://unfamiliar.example.invalid/runtime'


def test_live_pair_requires_state_in_second_live_request_context(tmp_path):
    def strip(n, result, before, after):
        if n == 1:
            before['conversation_history'][1].pop('codex_reasoning_items')
    live, written = LiveAgents(mutate=strip), []
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is False
    assert written == []


def test_auxiliary_only_evidence_cannot_authorize_gradual_pair(tmp_path):
    live, written = LiveAgents(hook='post_auxiliary_call'), []
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is False
    assert written == [] and not (tmp_path / 'pair-evidence.json').exists()


def test_live_pair_failure_evidence_does_not_authorize_when_config_save_fails(tmp_path):
    live = LiveAgents()
    probe = PairProbe(agent_factory=live.agent, register_hook=live.register_hook,
                      write=lambda entry: False, evidence_dir=tmp_path)
    assert probe.run(PROVIDER, OLD, NEW) is False
    evidence = json.loads((tmp_path / 'pair-evidence.json').read_text())
    assert evidence['probe_passed'] is True and evidence['authorizes_switch'] is False


def test_live_pair_observers_reject_other_sessions(tmp_path):
    def other(n, result, before, after):
        before['session_id'] = after['session_id'] = 'unrelated-user-session'
    live, written = LiveAgents(mutate=other), []
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is False
    assert written == []


def test_command_uses_live_agents_without_auxiliary_calls(tmp_path, monkeypatch):
    from shadow.switch import pair_probe
    live, written = LiveAgents(), []
    ctx = FakeContext()
    ctx.register_hook = live.register_hook
    ctx.llm = SimpleNamespace(complete=lambda *a, **k: pytest.fail('auxiliary call'))
    monkeypatch.setattr(pair_probe, '_agent_factory', live.agent)
    monkeypatch.setattr(pair_probe, 'profile_home', lambda: tmp_path)
    monkeypatch.setattr(pair_probe, 'save_pair', lambda path, entry: written.append(entry) or True)
    service = _service(ctx=ctx, config_reader=lambda: {'model': {'provider': PROVIDER, 'default': OLD}})
    assert pair_probe.copy('doctor.pair_pass') in run_command(service, terminal=False, yes=True,
                                                              old_model=OLD, new_model=NEW)
    assert written[0]['probe_passed'] is True


def test_route_with_a_query_is_not_stored():
    assert clean_route(PROVIDER, OLD, BASE + '?key=secret', MODE) is None
    assert clean_route(PROVIDER, OLD, BASE, MODE)['base_url'] == BASE




def _service(*, ctx=None, config_reader=None, proposal=None, require=None):
    catalog = SimpleNamespace(require=require or (lambda provider, model: SimpleNamespace(provider=provider, model=model)))
    return SimpleNamespace(ctx=ctx or FakeContext(),
                           config_reader=config_reader or (lambda: {'model': {'provider': PROVIDER, 'default': OLD}}),
                           proposal_lookup=lambda _: proposal, model_catalog=lambda: catalog)


def test_pair_command_names_the_exact_requested_models():
    service = _service()
    declined = run_command(service, terminal=False, yes=False, old_model=OLD, new_model=NEW)
    assert OLD in declined and NEW in declined and PROVIDER in declined
    assert 'Nothing was called.' in declined


def test_pair_command_names_the_models_and_refuses_a_different_current_model():
    service = _service(config_reader=lambda: {'model': {'provider': PROVIDER, 'default': 'another-model'}})
    declined = run_command(service, terminal=False, yes=False, old_model=OLD, new_model=NEW)
    assert OLD in declined and NEW in declined and PROVIDER in declined
    assert 'Nothing was called.' in declined
    refused = run_command(service, terminal=False, yes=True)
    assert 'pair_required' in refused and 'Nothing was called.' in refused


def test_pair_command_requires_an_exact_pair_without_guessing():
    refused = run_command(_service(), terminal=False, yes=True)
    assert 'pair_required' in refused and 'Nothing was called.' in refused


def test_pair_command_refuses_a_model_without_credentials_before_agent_creation(monkeypatch):
    from shadow.common.discovery import ModelUnavailable
    from shadow.switch import pair_probe
    seen = []
    def require(provider, model):
        seen.append((provider, model))
        if model == NEW:
            raise ModelUnavailable('credentials_unavailable', provider, model)
        return SimpleNamespace(provider=provider, model=model)
    monkeypatch.setattr(pair_probe, '_agent_factory', lambda **options: pytest.fail('agent was constructed'))
    refused = run_command(_service(require=require), terminal=False, yes=True, old_model=OLD, new_model=NEW)
    assert 'credentials_unavailable' in refused and 'Nothing was called.' in refused
    assert seen == [(PROVIDER, OLD), (PROVIDER, NEW)]


def test_pair_command_can_derive_an_exact_saved_proposal_pair():
    from shadow.switch.domain import Proposal, Route
    proposal = Proposal('test-exact', Route(PROVIDER, OLD, BASE, MODE), Route(PROVIDER, NEW, BASE, MODE), 'better')
    declined = run_command(_service(proposal=proposal), terminal=False, yes=False)
    assert OLD in declined and NEW in declined and 'confirmation_declined' in declined



def test_trial_command_delegates_the_pair_check(tmp_path):
    from shadow.common.storage import Store
    from shadow.trial.controller import ControllerLauncher
    from shadow.trial.isolation import Identity
    from shadow.trial.plugin import TrialPlugin, register
    from test_setup_check import IDENTITY
    ctx = FakeContext()
    plugin = TrialPlugin(ctx, store=Store(tmp_path / 'trial.sqlite'), launcher=ControllerLauncher('demo', tmp_path, identity=lambda: IDENTITY))
    register(ctx, plugin=plugin)
    seen = []
    ctx.shadow_verify_pair = lambda *, terminal, yes: seen.append((terminal, yes)) or 'pair result'
    assert ctx.commands['shadow']('doctor --verify-pair --yes') == 'pair result'
    assert seen == [(False, True)]






@pytest.mark.parametrize('field', ['conversation_history', 'request_messages'])
def test_live_pair_rejects_history_lost_before_second_provider_request(tmp_path, field):
    def strip(n, result, before, after):
        if n == 1:
            before[field] = []
    live, written = LiveAgents(mutate=strip), []
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is False
    assert written == []


def test_live_pair_rejects_provider_errors_even_when_turn_later_completes(tmp_path):
    live, written = LiveAgents(), []
    original_emit = live.emit
    def emit(name, event):
        if name == 'post_api_request':
            original_emit('api_request_error', dict(event, error='earlier failure'))
        original_emit(name, event)
    live.emit = emit
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is False
    assert written == []


def test_live_pair_requires_one_observed_response_per_turn(tmp_path):
    live, written = LiveAgents(), []
    original_emit = live.emit
    def emit(name, event):
        original_emit(name, event)
        if name == 'post_api_request':
            original_emit(name, event)
    live.emit = emit
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is False
    assert written == []


def test_live_pair_disposes_observers_and_closes_agent_on_failure(tmp_path):
    live, written = LiveAgents(), []
    def fail(*args):
        raise RuntimeError('turn failed')
    live.mutate = fail
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is False
    assert live.closed == [OLD]
    assert all(not callbacks for callbacks in live.hooks.values())


def test_live_pair_does_not_authorize_when_evidence_publication_fails(tmp_path, monkeypatch):
    from pathlib import Path
    live, written = LiveAgents(), []
    replace = Path.replace
    def fail_evidence(path, target):
        if Path(target).name == 'pair-evidence.json':
            raise OSError('evidence publication interrupted')
        return replace(path, target)
    monkeypatch.setattr(Path, 'replace', fail_evidence)
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is False
    assert written == [] and list(tmp_path.iterdir()) == []


def test_pair_command_refuses_custom_context_engine_before_any_model_call(tmp_path, monkeypatch):
    from shadow.switch import pair_probe
    monkeypatch.setattr(pair_probe, '_agent_factory', lambda **kwargs: pytest.fail('agent was constructed'))
    ctx = FakeContext()
    service = _service(ctx=ctx, config_reader=lambda: {
        'model': {'provider': PROVIDER, 'default': OLD}, 'context': {'engine': 'profile-context'}})
    result = run_command(service, terminal=False, yes=True, old_model=OLD, new_model=NEW)
    assert 'built-in context engine' in result and 'Nothing was called.' in result


def test_pair_proof_accepts_installed_hermes_responses_conversion(tmp_path, hermes_dispatch):
    from agent.codex_responses_adapter import _chat_messages_to_responses_input
    live, written = LiveAgents(), []
    requests = []
    def convert(messages, model):
        sent = _chat_messages_to_responses_input(
            messages, current_issuer_kind='codex_backend', current_issuer_model=model,
            replay_encrypted_reasoning=True)
        requests.append(sent)
        return sent
    live.converter = convert
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is True
    assert 'private-live-blob' not in json.dumps(requests[1])
    assistant = next(item for item in requests[1] if item.get('role') == 'assistant')
    assert assistant['content'] == [{'type': 'output_text', 'text': 'pebble'}]
    assert written[0]['probe_passed'] is True


@pytest.mark.parametrize('passes', [False, True])
def test_pair_agents_preserve_the_callers_session_identity(tmp_path, monkeypatch, hermes_dispatch, passes):
    import os
    import threading
    from contextvars import ContextVar
    from gateway.session_context import get_session_env, scoped_current_session_id, set_current_session_id
    live, written = LiveAgents(), []
    if not passes:
        live.mutate = lambda turn, result, before, after: result.update(completed=False)
    factory = live.agent
    route = ContextVar('pair-test-route', default='caller-route')
    logger = threading.local()
    logger.session_id = 'caller-log-session'

    def publishing_agent(**options):
        route.set('child-route')
        logger.session_id = options['session_id']
        set_current_session_id(options['session_id'])
        agent = factory(**options)
        close = agent.close
        def publishing_close():
            route.set('closing-route')
            logger.session_id = 'closing-session'
            set_current_session_id('closing-session')
            close()
        agent.close = publishing_close
        return agent

    monkeypatch.setenv('HERMES_SESSION_ID', 'caller-process-session')
    with scoped_current_session_id('caller-task-session'):
        probe = PairProbe(agent_factory=publishing_agent, register_hook=live.register_hook,
                          write=lambda entry: written.append(entry) or True, evidence_dir=tmp_path)
        assert probe.run(PROVIDER, OLD, NEW) is passes
        assert get_session_env('HERMES_SESSION_ID') == 'caller-task-session'
        assert os.environ['HERMES_SESSION_ID'] == 'caller-process-session'
        assert route.get() == 'caller-route'
        assert logger.session_id == 'caller-log-session'


@pytest.mark.parametrize('mutate,reason', [
    (lambda n, result, before, after: result.update(completed=False), 'turn_1_incomplete'),
    (lambda n, result, before, after: after.update(response_model='wrong'), 'turn_1_response_route'),
    (lambda n, result, before, after: before.update(model='wrong'), 'turn_1_request_route'),
    (lambda n, result, before, after: after.update(finish_reason='length'), 'turn_1_finish_reason'),
    (lambda n, result, before, after: result.update(messages=[]), 'turn_1_history_missing'),
])
def test_pair_failures_report_a_bounded_reason_without_payloads(tmp_path, caplog, mutate, reason):
    live, written = LiveAgents(mutate=mutate), []
    probe = _probe(tmp_path, live, written)
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == reason
    assert reason in caplog.text
    assert 'private-live-blob' not in caplog.text
    assert written == []


@pytest.mark.parametrize('sealed_required', [False, True])
def test_pair_doctor_prints_the_same_safe_reason_it_logs(tmp_path, monkeypatch, caplog, sealed_required):
    from shadow.switch import pair_probe
    def missing(n, result, before, after):
        if sealed_required:
            result['messages'][-1].pop('codex_reasoning_items')
        else:
            result.update(messages=[])
    live = LiveAgents(mutate=missing)
    ctx = FakeContext()
    ctx.register_hook = live.register_hook
    monkeypatch.setattr(pair_probe, '_agent_factory', live.agent)
    monkeypatch.setattr(pair_probe, 'profile_home', lambda: tmp_path)
    config = {'model': {'provider': PROVIDER, 'default': OLD}}
    if sealed_required:
        config['agent'] = {'reasoning_effort': 'high'}
    service = _service(ctx=ctx, config_reader=lambda: config)
    output = run_command(service, terminal=True, yes=True, old_model=OLD, new_model=NEW)
    reason = 'turn_1_sealed_reasoning_missing' if sealed_required else 'turn_1_history_missing'
    assert reason in output
    assert reason in caplog.text
    assert 'private-live-blob' not in output + caplog.text


def test_pair_worker_exception_has_a_safe_diagnostic(tmp_path, caplog):
    live, written = LiveAgents(), []
    probe = _probe(tmp_path, live, written)
    probe._run = lambda *args: (_ for _ in ()).throw(RuntimeError('secret-exception-canary'))
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'worker_exception'
    assert 'worker_exception' in caplog.text
    assert 'secret-exception-canary' not in caplog.text


def test_pair_config_save_failure_has_a_safe_diagnostic(tmp_path, caplog):
    live = LiveAgents()
    probe = PairProbe(agent_factory=live.agent, register_hook=live.register_hook,
                      write=lambda entry: False, evidence_dir=tmp_path)
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'config_save_failed'
    assert 'config_save_failed' in caplog.text


@pytest.mark.parametrize('saved_slash', [False, True])
def test_sdk_route_proof_unlocks_sessions_without_trailing_slash(tmp_path, saved_slash):
    from shadow.switch.domain import Route
    from shadow.switch.doctor import pair_doctor
    from shadow.switch.plugin import verified_pair_from_config

    def sdk_routes(n, result, before, after):
        before['base_url'] = after['base_url'] = BASE + '/'

    written = []
    assert _probe(tmp_path, LiveAgents(mutate=sdk_routes), written).run(PROVIDER, OLD, NEW)
    assert written[0]['old']['base_url'] == BASE
    if saved_slash:
        for side in ('old', 'new'):
            written[0][side]['base_url'] += '/'
    config = {'plugins': {'entries': {'shadow': {'settings': {'verified_gradual_pairs': written}}}}}
    old = Route(PROVIDER, OLD, BASE, MODE)
    new = Route(PROVIDER, NEW, BASE, MODE)
    assert verified_pair_from_config(lambda: config, old, new)
    assert 'pair_missing' not in pair_doctor(config, old=old, new=new)
    from shadow.switch.copy import copy as switch_copy
    assert pair_doctor(config, old=old, new=new) == switch_copy('switch.doctor.pair_ready')


@pytest.mark.parametrize('first,second', [('Pebble.', 'pebble'), ('pebble', 'Pebble.'), ('pebble\n', 'pebble')])
def test_pair_accepts_capitalization_and_sentence_punctuation(tmp_path, first, second):
    def answers(n, result, before, after):
        answer = first if n == 0 else second
        result['final_response'] = result['messages'][-1]['content'] = answer
    assert _probe(tmp_path, LiveAgents(mutate=answers), []).run(PROVIDER, OLD, NEW)


@pytest.fixture
def pair_config_backend():
    state = SimpleNamespace(document={'model': {'default': 'user-model'}, 'plugins': {'entries': {
        'shadow': {'settings': {'verified_gradual_pairs': []}}}}}, writes=[], refuse=False)

    def get_config(key, default=None):
        return state.document['plugins']['entries']['shadow']['settings'].get(key, default)

    def set_config(key, value):
        if state.refuse:
            raise PermissionError('locked')
        state.writes.append((key, value))
        state.document['plugins']['entries']['shadow']['settings'][key] = value

    state.ctx = SimpleNamespace(get_config=get_config, set_config=set_config)
    return state


def _proof():
    return {'old': clean_route(PROVIDER, OLD, BASE, MODE),
            'new': clean_route(PROVIDER, NEW, BASE, MODE),
            'probe_passed': True, 'evidence': 'pair-evidence.json'}


def test_save_pair_uses_selected_backend_and_changes_only_proofs(tmp_path, pair_config_backend):
    state = pair_config_backend
    pairs = state.document['plugins']['entries']['shadow']['settings']['verified_gradual_pairs']
    pairs.append({'old': {'model': 'kept'}, 'new': {'model': 'kept'}, 'probe_passed': True, 'evidence': 'older.json'})
    path = tmp_path / 'config.yaml'
    assert save_pair(state.ctx, _proof())
    assert save_pair(state.ctx, _proof())
    pairs = state.document['plugins']['entries']['shadow']['settings']['verified_gradual_pairs']
    assert len(pairs) == 2 and pairs[0]['old']['model'] == 'kept' and pairs[1] == _proof()
    assert state.document['model']['default'] == 'user-model'
    assert not path.exists()
    assert all(key == 'verified_gradual_pairs' for key, value in state.writes)


def test_save_pair_honors_backend_write_refusal(tmp_path, pair_config_backend):
    state = pair_config_backend
    state.refuse = True
    original = copy.deepcopy(state.document)
    assert save_pair(state.ctx, _proof()) is False
    assert state.document == original and state.writes == []
    assert not (tmp_path / 'config.yaml').exists()


@pytest.mark.parametrize('existing', [None, {}, 'keep this setting'])
def test_save_pair_refuses_non_list_pairs_without_changing_config(tmp_path, pair_config_backend, existing):
    state = pair_config_backend
    state.document['plugins']['entries']['shadow']['settings']['verified_gradual_pairs'] = existing
    original = copy.deepcopy(state.document)
    assert save_pair(state.ctx, _proof()) is False
    assert state.document == original and state.writes == []


@pytest.mark.parametrize('turn', [0, 1])
@pytest.mark.parametrize('reported', [None, '', 'matching', 'another-model', 'dated-alias'])
def test_pair_rejects_a_conflicting_reported_model(tmp_path, turn, reported):
    def report(n, result, before, after):
        if n == turn:
            result['served_model'] = (result['model'] if reported == 'matching' else
                                     result['model'] + '-2026-11-10' if reported == 'dated-alias' else reported)

    live, written = LiveAgents(mutate=report), []
    probe = _probe(tmp_path, live, written)
    expected = reported in (None, '', 'matching')
    assert probe.run(PROVIDER, OLD, NEW) is expected
    if not expected:
        assert len(live.calls) == turn + 1
        assert written == [] and not (tmp_path / 'pair-evidence.json').exists()
