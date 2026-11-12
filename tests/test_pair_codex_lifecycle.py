"""Offline pair checks through the public Hermes auxiliary hook builders."""

import json
from types import SimpleNamespace

import pytest

from shadow.switch.pair_probe import PairProbe
from test_pair_probe import BASE, MODE, NEW, OLD, PROVIDER, LiveCompletions, hermes_agent_context


@pytest.mark.parametrize('encrypted_content', [None, '', 'offline-sealed-canary'])
def test_pair_uses_public_auxiliary_payloads_without_claiming_sealed_evidence(
        tmp_path, monkeypatch, hermes_agent_context, encrypted_content):
    from agent.auxiliary_hooks import finish_aux_call_hooks, start_aux_call_hooks
    from agent.plugin_llm import PluginLlmCompleteResult
    from hermes_cli import lifecycle

    ctx, written = LiveCompletions(), []
    monkeypatch.setattr(lifecycle, 'has_hook', lambda name: bool(ctx.hooks.get(name)))
    monkeypatch.setattr(lifecycle, 'invoke_hook', lambda name, **event: ctx.emit(name, event))
    monkeypatch.setattr('shadow.switch.pair_probe.pair_reasoning',
                        lambda *args: ({'old': None, 'new': None}, False))

    def complete(messages, *, provider, model, task, **options):
        ctx.calls.append((model, messages, options))
        hooks = start_aux_call_hooks(
            aux_task=task, metadata={'api_request_id': 'offline-' + model},
            client=SimpleNamespace(base_url=BASE), kwargs={'messages': messages},
            provider=provider, model=model, api_mode=MODE, streaming=False)
        message = SimpleNamespace(role='assistant', content='pebble', tool_calls=[],
                                  encrypted_content=encrypted_content)
        response = SimpleNamespace(model=model, usage=None,
                                   choices=[SimpleNamespace(message=message, finish_reason='stop')])
        finish_aux_call_hooks(hooks, response)
        return PluginLlmCompleteResult(text='pebble', provider=provider, model=model, agent_id='default')

    ctx.llm = SimpleNamespace(complete=complete)
    probe = PairProbe(ctx=ctx, write=lambda entry: written.append(entry) or True, evidence_dir=tmp_path)
    assert probe.run(PROVIDER, OLD, NEW)
    assert [call[0] for call in ctx.calls] == [OLD, NEW]
    evidence = (tmp_path / 'pair-evidence.json').read_text()
    assert json.loads(evidence)['sealed_reasoning_observed'] is False
    assert json.loads(evidence)['reasoning_check'] == 'text_only'
    assert 'offline-sealed-canary' not in evidence
