"""Pair verification reports bounded failure codes without publishing private evidence."""

import pytest

from test_pair_probe import LiveAgents, NEW, OLD, PROVIDER, _probe, _service, hermes_agent_context


def _different_route(turn, result, before, after):
    if turn == 1:
        before['base_url'] = after['base_url'] = 'https://example.invalid/codex'


@pytest.mark.parametrize('mutate,reason', [
    (lambda n, result, before, after: before.update(session_id='other'), 'turn_1_request_count'),
    (lambda n, result, before, after: after.update(session_id='other'), 'turn_1_response_count'),
    (lambda n, result, before, after: before.update(conversation_history=[]) if n == 1 else None,
     'turn_2_history_changed'),
    (lambda n, result, before, after: before.update(request_messages=[]) if n == 1 else None,
     'turn_2_carried_text_missing'),
    (lambda n, result, before, after: result.update(final_response='different') if n == 1 else None,
     'turn_2_answer_changed'),
    (lambda n, result, before, after: before['request_messages'].append(before['conversation_history'][1]) if n == 1 else None,
     'turn_2_encrypted_reasoning_replayed'),
    (_different_route, 'pair_routes_differ'),
    (lambda n, result, before, after: before.update(base_url='https://example.invalid/v1?key=fixture') or after.update(base_url='https://example.invalid/v1?key=fixture'),
     'turn_1_response_route'),
])
def test_pair_rejects_each_missing_or_unsafe_evidence_with_a_reason(tmp_path, caplog, mutate, reason):
    live, written = LiveAgents(mutate=mutate), []
    probe = _probe(tmp_path, live, written)
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == reason
    assert reason in caplog.text
    assert 'private-live-blob' not in caplog.text
    assert written == []
    assert not (tmp_path / 'pair-evidence.json').exists()


def test_pair_provider_error_has_its_own_reason(tmp_path, caplog):
    live, written = LiveAgents(hook='api_request_error'), []
    probe = _probe(tmp_path, live, written)
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'turn_1_request_error'
    assert probe.reason_code in caplog.text
    assert written == []


def test_pair_construction_error_never_logs_exception_contents(tmp_path, caplog):
    live, written = LiveAgents(), []
    probe = _probe(tmp_path, live, written)
    def broken_factory(**options):
        raise RuntimeError('private-constructor-canary')
    probe.agent_factory = broken_factory
    assert probe.run(PROVIDER, OLD, NEW) is False
    assert probe.reason_code == 'probe_exception'
    assert probe.reason_code in caplog.text
    assert 'private-constructor-canary' not in caplog.text
    assert written == []


@pytest.mark.parametrize('configuration,yes,reason', [
    ({'model': {'provider': PROVIDER, 'default': 'other'}}, True, 'pair_required'),
    ({'model': {'provider': PROVIDER, 'default': OLD}}, False, 'confirmation_declined'),
    ({'model': {'provider': PROVIDER, 'default': OLD}, 'context': {'engine': 'custom'}}, True,
     'context_engine_unsupported'),
])
def test_pair_command_preflight_exits_also_print_and_log_a_reason(configuration, yes, reason, caplog):
    from fixtures import FakeContext
    from shadow.switch.pair_probe import run_command
    service = _service(ctx=FakeContext(), config_reader=lambda: configuration)
    pair = {} if reason == 'pair_required' else {'old_model': OLD, 'new_model': NEW}
    output = run_command(service, terminal=False, yes=yes, **pair)
    assert reason in output
    assert reason in caplog.text
