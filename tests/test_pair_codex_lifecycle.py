"""Offline Codex response replay through Hermes request and response observers."""

from copy import deepcopy
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from shadow.switch.pair_probe import PairProbe
from test_probe_dispatch import hermes_dispatch


OLD, NEW, PROVIDER = 'gpt-6.1-sol', 'gpt-6.1-terra', 'openai-codex'
BASE, MODE = 'https://chatgpt.com/backend-api/codex', 'codex_responses'


@pytest.mark.parametrize('encrypted_content', [None, '', 'offline-sealed-canary'])
def test_pair_uses_real_codex_transport_and_hermes_lifecycle(tmp_path, monkeypatch, hermes_dispatch, encrypted_content):
    from agent.codex_responses_adapter import _normalize_codex_response
    from agent.transports.codex import ResponsesApiTransport
    from agent.turn_api_request import _fire_pre_api_request_hook
    from agent.turn_response_intake import _fire_post_api_request_hook

    loop = ModuleType('agent.conversation_loop')
    loop._system_prompt_for_hooks = lambda kwargs, messages: kwargs.get('instructions', '')
    loop._moa_reference_metrics_for_hook = lambda response: None
    monkeypatch.setitem(sys.modules, 'agent.conversation_loop', loop)
    calls, sent, written = [], [], []
    manager = hermes_dispatch.manager

    def register(name, callback):
        callbacks = manager._hooks.setdefault(name, [])
        callbacks.append(callback)
        return SimpleNamespace(dispose=lambda: callbacks.remove(callback))

    def factory(**options):
        transport = ResponsesApiTransport()
        model = options['model']
        agent = SimpleNamespace(**options, tools=[], platform='unknown', base_url=BASE + '/', api_mode=MODE)
        agent._api_request_payload_for_hook = lambda kwargs: dict(kwargs)
        agent._api_response_payload_for_hook = lambda response, assistant, **kwargs: {}
        agent._usage_summary_for_api_request_hook = lambda response: {}

        def run(message, *, conversation_history):
            calls.append(model)
            messages = deepcopy(conversation_history or []) + [{'role': 'user', 'content': message}]
            request = transport.build_kwargs(
                model=model, messages=messages, tools=[], provider=PROVIDER,
                base_url=agent.base_url, is_codex_backend=True,
                reasoning_config=options['reasoning_config'], max_tokens=512)
            assert 'api_mode' not in options
            assert request['reasoning'] == {'effort': 'high', 'summary': 'auto'}
            assert request['include'] == ['reasoning.encrypted_content']
            sent.append(deepcopy(request['input']))
            _fire_pre_api_request_hook(
                agent, request, messages, [], messages=messages, original_user_message=message,
                approx_tokens=522, total_chars=100, retry_count=0, api_call_count=1,
                api_request_id='offline-request', api_start_time=1, effective_task_id=None,
                turn_id='offline-turn')
            output = [SimpleNamespace(type='message', role='assistant', status='completed',
                                      content=[SimpleNamespace(type='output_text', text='pebble')])]
            if model == OLD:
                assert 'smallest positive integer' in message
            if encrypted_content is not None:
                output.insert(0, SimpleNamespace(type='reasoning', id='rs_offline',
                                               encrypted_content=encrypted_content, summary=[]))
            response = SimpleNamespace(model=model, status='completed', output=output,
                                       usage=SimpleNamespace(input_tokens=522, output_tokens=7, total_tokens=529))
            assistant, finish = _normalize_codex_response(
                response, issuer_kind=transport._last_issuer_kind, issuer_model=transport._last_issuer_model)
            _fire_post_api_request_hook(
                agent, response, assistant, finish, api_messages=messages, api_call_count=1,
                api_duration=1, api_start_time=1, api_request_id='offline-request',
                effective_task_id=None, turn_id='offline-turn')
            answer = {'role': 'assistant', 'content': assistant.content}
            if assistant.codex_reasoning_items:
                answer['codex_reasoning_items'] = assistant.codex_reasoning_items
            return dict(completed=True, failed=False, interrupted=False, partial=False,
                        provider=PROVIDER, model=model, final_response=assistant.content,
                        messages=messages + [answer], api_calls=1)

        agent.run_conversation = run
        agent.close = lambda: None
        return agent

    probe = PairProbe(agent_factory=factory, register_hook=register,
                      write=lambda entry: written.append(entry) or True, evidence_dir=tmp_path,
                      config={'agent': {'reasoning_effort': 'high'}})
    if not encrypted_content:
        assert probe.run(PROVIDER, OLD, NEW) is False
        assert probe.reason_code == 'turn_1_sealed_reasoning_missing'
        assert calls == [OLD] and not written
        return
    assert probe.run(PROVIDER, OLD, NEW) is True
    assert calls == [OLD, NEW]
    assert 'offline-sealed-canary' not in json.dumps(sent[1])
    assert written[0]['probe_passed'] is True
