import copy
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from fixtures import FakeContext
from shadow.switch.pair_probe import FIRST, SECOND, PairProbe, clean_route, run_command, save_pair


OLD = 'unknown-maker/river-small'
NEW = 'unknown-maker/river-large'
PROVIDER = 'openrouter'
BASE = 'https://example.invalid/v1'
MODE = 'chat_completions'


@pytest.fixture
def hermes_agent_context(monkeypatch, tmp_path):
    import yaml
    from test_real_tools import require_hermes_reference
    monkeypatch.syspath_prepend(str(require_hermes_reference()))
    monkeypatch.setitem(sys.modules, 'hermes_yaml', yaml)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes-home'))


class TrustDenied(PermissionError):
    pass


@pytest.fixture(autouse=True)
def public_llm_api(monkeypatch):
    module = ModuleType('agent.plugin_llm')
    module.PluginLlmTrustError = TrustDenied
    monkeypatch.setitem(sys.modules, 'agent.plugin_llm', module)
    monkeypatch.setattr('shadow.switch.pair_probe.pair_reasoning', lambda config, provider, model:
                        ({'old': None, 'new': None}, bool(config.get('agent', {}).get('reasoning_effort'))))


class LiveCompletions:
    def __init__(self, mutate=None):
        self.hooks, self.calls, self.tasks = {}, [], set()
        self.mutate = mutate or (lambda turn, result, before, after: None)
        self.llm = self

    def register_auxiliary_task(self, key, **kwargs):
        self.tasks.add(key)
        return SimpleNamespace(dispose=lambda: self.tasks.remove(key))

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)
        return SimpleNamespace(dispose=lambda: self.hooks[name].remove(callback))

    def emit(self, name, event):
        for callback in self.hooks.get(name, []):
            callback(**event)

    def complete(self, messages, *, provider, model, task, **options):
        turn = len(self.calls)
        assert task in self.tasks
        self.calls.append((model, copy.deepcopy(messages), options))
        route = dict(provider=provider, model=model, response_model=model + '-2026-11-10',
                     base_url=BASE, api_mode=MODE, aux_task=task, api_request_id=f'call-{turn}')
        before = dict(route, request_messages=copy.deepcopy(messages))
        after = dict(route, finish_reason='stop', error=None)
        result = dict(text='pebble', provider=provider, model=model + '-2026-11-10')
        self.mutate(turn, result, before, after)
        self.emit('pre_auxiliary_call', before)
        self.emit('post_auxiliary_call', after)
        return SimpleNamespace(**result)


def _probe(tmp_path, live, written, **options):
    return PairProbe(ctx=live, write=lambda entry: written.append(entry) or True,
                     evidence_dir=tmp_path, **options)


def test_public_completions_prove_text_handoff_and_routes(tmp_path):
    live, written = LiveCompletions(), []
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW)
    assert [call[0] for call in live.calls] == [OLD, NEW]
    assert live.calls[0][1] == [{'role': 'user', 'content': FIRST}]
    assert live.calls[1][1] == [{'role': 'user', 'content': FIRST},
                              {'role': 'assistant', 'content': 'pebble'},
                              {'role': 'user', 'content': SECOND}]
    assert all(call[2] == {'max_tokens': 512, 'timeout': 60} for call in live.calls)
    assert written[0]['probe_passed'] is True
    body = json.loads((tmp_path / 'pair-evidence.json').read_text())
    assert body['live_history_handoff'] is False
    assert body['reasoning_check'] == 'text_only'
    assert body['sealed_reasoning_observed'] is False
    assert 'encrypted_reasoning_replayed' not in body
    assert body['reasoning_applied'] is False
    assert written[0]['reasoning_applied'] is False
    assert written[0]['reasoning'] == body['reasoning'] == {'old': None, 'new': None}
    assert body['authorizes_switch'] is False
    assert (tmp_path / 'pair-evidence.json').stat().st_mode & 0o777 == 0o600
    assert not live.tasks and all(not callbacks for callbacks in live.hooks.values())


@pytest.mark.parametrize('turn', [0, 1])
@pytest.mark.parametrize('field,value', [('provider', 'other'), ('model', 'wrong'), ('text', ''), ('text', None)])
def test_pair_refuses_incomplete_or_wrong_completion(tmp_path, turn, field, value):
    live = LiveCompletions(lambda n, result, before, after: result.update({field: value}) if n == turn else None)
    written = []
    probe = _probe(tmp_path, live, written)
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == f'turn_{turn + 1}_incomplete'
    assert written == [] and not (tmp_path / 'pair-evidence.json').exists()
    assert not live.tasks and all(not callbacks for callbacks in live.hooks.values())


@pytest.mark.parametrize('turn', [0, 1])
@pytest.mark.parametrize('field,value', [('provider', 'other'), ('model', 'wrong'), ('response_model', None),
                                        ('response_model', 'wrong'), ('base_url', ''), ('api_mode', ''),
                                        ('error', 'failure'), ('finish_reason', 'length')])
def test_pair_requires_actual_response_route_and_stop(tmp_path, turn, field, value):
    live = LiveCompletions(lambda n, result, before, after: after.update({field: value}) if n == turn else None)
    written = []
    assert _probe(tmp_path, live, written).run(PROVIDER, OLD, NEW) is False
    assert written == [] and not (tmp_path / 'pair-evidence.json').exists()


@pytest.mark.parametrize('field,value,reason', [
    ('model', 'wrong', 'turn_1_request_route'),
    ('provider', 'wrong', 'turn_1_request_route'),
    ('base_url', BASE + '/elsewhere', 'turn_1_request_route'),
    ('api_mode', 'different', 'turn_1_request_route'),
    ('api_request_id', 'other', 'turn_1_request_identity'),
])
def test_pair_requires_matching_request_observations(tmp_path, field, value, reason):
    live = LiveCompletions(lambda n, result, before, after: before.update({field: value}) if n == 0 else None)
    probe = _probe(tmp_path, live, [])
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == reason


@pytest.mark.parametrize('event,reason', [('pre_auxiliary_call', 'request_count'),
                                         ('post_auxiliary_call', 'response_count')])
@pytest.mark.parametrize('copies', [0, 2])
def test_pair_requires_one_physical_attempt_per_turn(tmp_path, event, reason, copies):
    live = LiveCompletions()
    emit = live.emit
    live.emit = lambda name, payload: [emit(name, payload) for _ in range(copies if name == event else 1)]
    probe = _probe(tmp_path, live, [])
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'turn_1_' + reason


@pytest.mark.parametrize('field', ['base_url', 'api_mode'])
def test_pair_requires_same_endpoint_and_transport(tmp_path, field):
    def change(n, result, before, after):
        if n == 1:
            before[field] = after[field] = 'https://different.invalid' if field == 'base_url' else 'native_messages'
    probe = _probe(tmp_path, LiveCompletions(change), [])
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'pair_routes_differ'


@pytest.mark.parametrize('change,reason', [
    (lambda result, before: result.update(text='willow'), 'turn_2_answer_changed'),
    (lambda result, before: result.update(text='Pebble.'), 'turn_2_answer_changed'),
    (lambda result, before: before.update(request_messages=[]), 'turn_2_carried_text_missing'),
])
def test_pair_requires_the_exact_first_answer(tmp_path, change, reason):
    live = LiveCompletions(lambda n, result, before, after: change(result, before) if n == 1 else None)
    probe = _probe(tmp_path, live, [])
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == reason


def test_pair_ignores_unrelated_auxiliary_traffic(tmp_path):
    live = LiveCompletions()
    emit = live.emit
    def with_noise(name, payload):
        emit(name, dict(payload, aux_task='unrelated'))
        emit(name, payload)
    live.emit = with_noise
    assert _probe(tmp_path, live, []).run(PROVIDER, OLD, NEW)


def test_pair_fails_closed_when_sealed_reasoning_is_required(tmp_path, caplog):
    live, written = LiveCompletions(), []
    probe = _probe(tmp_path, live, written, config={'agent': {'reasoning_effort': 'high'}})
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'sealed_reasoning_unobservable'
    assert probe.reason_code in caplog.text
    assert not live.calls and not written and not list(tmp_path.iterdir())


@pytest.mark.parametrize('error,reason', [(TrustDenied, 'llm_trust_denied'), (RuntimeError, 'probe_exception')])
def test_pair_reports_safe_completion_failures(tmp_path, caplog, error, reason):
    def fail(*args):
        raise error('secret-exception-canary')
    live = LiveCompletions(fail)
    probe = _probe(tmp_path, live, [])
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == reason and reason in caplog.text
    assert 'secret-exception-canary' not in caplog.text
    assert not live.tasks and all(not callbacks for callbacks in live.hooks.values())


def test_pair_keeps_diagnostic_evidence_if_config_write_fails(tmp_path):
    live = LiveCompletions()
    probe = PairProbe(ctx=live, write=lambda entry: False, evidence_dir=tmp_path)
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'config_save_failed'
    body = json.loads((tmp_path / 'pair-evidence.json').read_text())
    assert body['probe_passed'] is True and body['authorizes_switch'] is False


def test_pair_does_not_save_approval_if_evidence_publication_fails(tmp_path, monkeypatch):
    from pathlib import Path
    def fail(*args):
        raise OSError('publication failed')
    monkeypatch.setattr(Path, 'replace', fail)
    written = []
    assert _probe(tmp_path, LiveCompletions(), written).run(PROVIDER, OLD, NEW) is False
    assert written == [] and list(tmp_path.iterdir()) == []


def test_pair_command_uses_ctx_llm_without_a_parent_session(tmp_path, monkeypatch):
    from shadow.switch import pair_probe
    live, written = LiveCompletions(), []
    monkeypatch.setattr(pair_probe, 'profile_home', lambda: tmp_path)
    monkeypatch.setattr(pair_probe, 'save_pair', lambda ctx, entry: written.append(entry) or True)
    assert pair_probe.copy('doctor.pair_pass') in run_command(
        _service(ctx=live), terminal=True, yes=True, old_model=OLD, new_model=NEW)
    assert len(live.calls) == 2 and written[0]['probe_passed'] is True


@pytest.mark.parametrize('profile,command', [('default', 'hermes'), ('work', 'hermes -p work')])
@pytest.mark.parametrize('terminal', [False, True])
def test_pair_trust_refusal_explains_both_grants(tmp_path, monkeypatch, profile, command, terminal):
    from shadow.switch import pair_probe

    def denied(*args):
        raise TrustDenied('secret-exception-canary')

    live = LiveCompletions(denied)
    live.profile_name = profile
    monkeypatch.setattr(pair_probe, 'profile_home', lambda: tmp_path)
    output = run_command(_service(ctx=live), terminal=terminal, yes=True, old_model=OLD, new_model=NEW)
    assert 'llm_trust_denied' in output
    assert 'plugins.entries.shadow-trial.llm.allow_provider_override' in output
    assert 'plugins.entries.shadow-trial.llm.allow_model_override' in output
    assert command + ' plugins enable shadow-trial' in output
    assert 'secret-exception-canary' not in output


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






@pytest.fixture
def pair_config_backend():
    state = SimpleNamespace(document={'model': {'default': 'user-model'}, 'plugins': {'entries': {
        'shadow-trial': {'settings': {'verified_gradual_pairs': []}}}}}, writes=[], refuse=False)

    def get_config(key, default=None):
        return state.document['plugins']['entries']['shadow-trial']['settings'].get(key, default)

    def set_config(key, value):
        if state.refuse:
            raise PermissionError('locked')
        state.writes.append((key, value))
        state.document['plugins']['entries']['shadow-trial']['settings'][key] = value

    state.ctx = SimpleNamespace(get_config=get_config, set_config=set_config)
    return state


def _proof():
    return {'old': clean_route(PROVIDER, OLD, BASE, MODE),
            'new': clean_route(PROVIDER, NEW, BASE, MODE),
            'probe_passed': True, 'evidence': 'pair-evidence.json'}


def test_save_pair_uses_selected_backend_and_changes_only_proofs(tmp_path, pair_config_backend):
    state = pair_config_backend
    pairs = state.document['plugins']['entries']['shadow-trial']['settings']['verified_gradual_pairs']
    pairs.append({'old': {'model': 'kept'}, 'new': {'model': 'kept'}, 'probe_passed': True, 'evidence': 'older.json'})
    path = tmp_path / 'config.yaml'
    assert save_pair(state.ctx, _proof())
    assert save_pair(state.ctx, _proof())
    pairs = state.document['plugins']['entries']['shadow-trial']['settings']['verified_gradual_pairs']
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
    state.document['plugins']['entries']['shadow-trial']['settings']['verified_gradual_pairs'] = existing
    original = copy.deepcopy(state.document)
    assert save_pair(state.ctx, _proof()) is False
    assert state.document == original and state.writes == []
