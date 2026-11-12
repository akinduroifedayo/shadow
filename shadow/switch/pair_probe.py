"""Verify a model switch through two live Hermes agent turns."""

from copy import deepcopy
from contextlib import suppress
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from ..common.copy import copy
from ..common.discovery import ModelUnavailable
from .domain import Route
from .reasoning import pair_reasoning

FIRST = (
    'Find the smallest positive integer whose remainders on division by 7, 11, and 13 '
    'are 3, 8, and 5 respectively. Check all three conditions. '
    'If the integer is below 500, reply with pebble; otherwise reply with willow. '
    'Reply with the selected word only.'
)
SECOND = 'What single word did you just say? Reply with that word only.'
EVIDENCE_NAME = 'pair-evidence.json'
logger = logging.getLogger(__name__)


def confirm_line(provider, old_model, new_model):
    return copy('doctor.pair_confirm', provider=provider, old_model=old_model, new_model=new_model)


def clean_route(provider, model, base_url, api_mode):
    try:
        route = Route(provider, model, base_url or '', api_mode)
    except (TypeError, ValueError):
        return None
    if route.base_url:
        parts = urlsplit(route.base_url)
        if parts.username or parts.password or parts.query or parts.fragment:
            return None
    return asdict(route)


def _matches_model(actual, requested):
    return isinstance(actual, str) and (actual == requested or re.fullmatch(re.escape(requested) + r'-(?:\d{4}-\d{2}-\d{2}|\d{8})', actual) is not None)


def _observed_route(observed, provider, model):
    if not isinstance(observed, dict) or observed.get('error') or observed.get('provider') != provider:
        return None
    if not _matches_model(observed.get('response_model'), model) or observed.get('model') != model:
        return None
    if not observed.get('base_url') or not observed.get('api_mode'):
        return None
    return clean_route(provider, model, observed['base_url'], observed['api_mode'])


def _finished(result, provider, model):
    return (isinstance(result, dict) and result.get('completed') is True
            and result.get('failed') is False and result.get('interrupted') is False
            and result.get('partial') is False and not result.get('error')
            and not result.get('cleanup_errors') and not result.get('user_intervened')
            and result.get('provider') == provider and result.get('model') == model
            and result.get('served_model') in (None, '', model)
            and isinstance(result.get('final_response'), str) and bool(result['final_response'].strip())
            and isinstance(result.get('messages'), list) and result.get('api_calls') == 1)


def _encrypted_blobs(value, *, reasoning_details=False):
    if isinstance(value, dict):
        for key, item in value.items():
            sealed = (key in {'encrypted_content', 'thought_signature', 'thoughtSignature'}
                      or key == 'signature' and (reasoning_details or value.get('type') == 'thinking')
                      or key == 'data' and (reasoning_details or value.get('type') == 'redacted_thinking'))
            if sealed and isinstance(item, str) and item.strip():
                yield item
            else:
                yield from _encrypted_blobs(item, reasoning_details=reasoning_details or key == 'reasoning_details')
    elif isinstance(value, list):
        for item in value:
            yield from _encrypted_blobs(item, reasoning_details=reasoning_details)


def _assistant_text(messages):
    parts = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        content = message.get('content')
        if isinstance(content, str):
            parts.append(content.strip())
        elif isinstance(content, list):
            parts.extend(part['text'].strip() for part in content if isinstance(part, dict) and isinstance(part.get('text'), str))
    return parts


class PairProbe:
    def __init__(self, *, agent_factory, register_hook, write, evidence_dir, config=None):
        self.agent_factory = agent_factory
        self.register_hook = register_hook
        self.write = write
        self.evidence_dir = Path(evidence_dir)
        self.reason_code = None
        self.config = config or {}

    def _fail(self, reason):
        self.reason_code = reason
        logger.warning('Shadow pair verification failed (%s)', reason)
        return False

    def run(self, provider, old_model, new_model):
        self.reason_code = None

        def isolated_probe():
            from agent.delegation_context import delegated_child_context
            with delegated_child_context():
                return self._run(provider, old_model, new_model)

        try:
            with ThreadPoolExecutor(max_workers=1) as worker:
                return worker.submit(copy_context().run, isolated_probe).result()
        except Exception:
            return self._fail('worker_exception')

    def _run(self, provider, old_model, new_model):
        sessions = [f'shadow-pair-{uuid4().hex}' for _ in range(2)]
        events = {name: {sid: [] for sid in sessions} for name in ('pre_api_request', 'post_api_request', 'api_request_error')}
        active, registrations, agents = True, [], []

        def observer(name):
            def capture(**event):
                sid = event.get('session_id')
                if active and sid in events[name]:
                    fields = ('provider', 'model', 'response_model', 'base_url', 'api_mode', 'error', 'finish_reason',
                              'conversation_history', 'request_messages')
                    events[name][sid].append(deepcopy({key: event.get(key) for key in fields}))
            return capture

        try:
            reasoning, sealed_required = pair_reasoning(self.config, provider, old_model)
            sealed_observed = False
            for name in events:
                registrations.append(self.register_hook(name, observer(name)))
            results, history, first_answer = [], None, None
            for index, model in enumerate((old_model, new_model)):
                agent = self.agent_factory(
                    provider=provider, model=model, session_id=sessions[index],
                    parent_session_id='shadow-pair-probe',
                    enabled_toolsets=[], skip_memory=True, skip_context_files=True,
                    skip_background_review=True, session_db=None, quiet_mode=True,
                    max_iterations=1, max_tokens=512, run_budget_seconds=60,
                    fallback_model=None, save_trajectories=False, side_agent=True,
                    reasoning_config=deepcopy(reasoning['old' if index == 0 else 'new']),
                )
                agents.append(agent)
                agent._persist_disabled = True
                agent.compression_enabled = False
                if agent.tools:
                    return self._fail(f'turn_{index + 1}_tools_enabled')
                result = agent.run_conversation(FIRST if index == 0 else SECOND, conversation_history=deepcopy(history))
                if not _finished(result, provider, model):
                    return self._fail(f'turn_{index + 1}_incomplete')
                sid = sessions[index]
                before, after = events['pre_api_request'][sid], events['post_api_request'][sid]
                if events['api_request_error'][sid]:
                    return self._fail(f'turn_{index + 1}_request_error')
                if len(before) != 1:
                    return self._fail(f'turn_{index + 1}_request_count')
                if len(after) != 1:
                    return self._fail(f'turn_{index + 1}_response_count')
                route = _observed_route(after[0], provider, model)
                if route is None:
                    return self._fail(f'turn_{index + 1}_response_route')
                if _observed_route({**before[0], 'response_model': after[0].get('response_model')}, provider, model) != route:
                    return self._fail(f'turn_{index + 1}_request_route')
                if after[0].get('finish_reason') != 'stop':
                    return self._fail(f'turn_{index + 1}_finish_reason')
                if index == 0:
                    history = deepcopy(result['messages'])
                    first_answer = result['final_response'].strip()
                    if first_answer not in _assistant_text(history):
                        return self._fail('turn_1_history_missing')
                    sealed_observed = any(_encrypted_blobs(history))
                    if sealed_required and not sealed_observed:
                        return self._fail('turn_1_sealed_reasoning_missing')
                else:
                    carried = before[0].get('conversation_history')
                    if not isinstance(carried, list) or carried[:len(history)] != history:
                        return self._fail('turn_2_history_changed')
                    request = before[0].get('request_messages')
                    if first_answer not in _assistant_text(request):
                        return self._fail('turn_2_carried_text_missing')
                    if result['final_response'].strip().strip('.').lower() != first_answer.strip('.').lower():
                        return self._fail('turn_2_answer_changed')
                    if any(json.dumps(blob)[1:-1] in json.dumps(request) for blob in _encrypted_blobs(history)):
                        return self._fail('turn_2_encrypted_reasoning_replayed')
                results.append(route)
            old, new = results
            if old['base_url'] != new['base_url'] or old['api_mode'] != new['api_mode']:
                return self._fail('pair_routes_differ')
            body = {'provider': provider, 'old_model': old_model, 'new_model': new_model,
                    'turns': 2, 'reasoning': reasoning, 'sealed_reasoning_observed': sealed_observed,
                    'reasoning_check': 'sealed_required' if sealed_required else 'preserved_when_present',
                    'live_history_handoff': True,
                    'encrypted_reasoning_replayed': False, 'carried_text': True, 'probe_passed': True,
                    'authorizes_switch': False}
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            evidence = self.evidence_dir / EVIDENCE_NAME
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', dir=self.evidence_dir, prefix='.pair-evidence.', delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(json.dumps(body, sort_keys=True) + '\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                entry = {'old': old, 'new': new, 'probe_passed': True, 'evidence': EVIDENCE_NAME,
                         'reasoning': reasoning, 'sealed_reasoning_observed': sealed_observed}
                temporary.replace(evidence)
                if self.write(entry) is not True:
                    return self._fail('config_save_failed')
                return True
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        except Exception:
            return self._fail('probe_exception')
        finally:
            active = False
            for registration in reversed(registrations):
                if registration is not None:
                    with suppress(Exception):
                        registration.dispose()
            for agent in agents:
                with suppress(Exception):
                    agent.close()


def profile_home():
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return None


def _agent_factory(**options):
    from run_agent import AIAgent
    return AIAgent(**options)


def _command_failure(key, reason, **values):
    logger.warning('Shadow pair verification failed (%s)', reason)
    return copy(key, **values) + '\n' + copy('switch.doctor.pair_failure_reason', reason=reason)


def run_command(service, *, terminal, yes, old_model=None, new_model=None, provider=None):
    config = service.config_reader() or {}
    model = config.get('model', {}) if isinstance(config, dict) else {}
    configured_provider = model.get('provider', '') if isinstance(model, dict) else ''
    if not old_model and not new_model:
        lookup = getattr(service, 'proposal_lookup', None)
        proposal = lookup(None) if callable(lookup) else None
        if proposal and proposal.old.provider != proposal.new.provider:
            return _command_failure('doctor.pair_cross_provider', 'cross_provider_pair')
        if proposal:
            provider = provider or proposal.old.provider
            old_model, new_model = proposal.old.model, proposal.new.model
    provider = provider or configured_provider
    if not provider or not old_model or not new_model or old_model == new_model:
        return _command_failure('doctor.pair_required', 'pair_required')
    catalog = getattr(service, 'model_catalog', None)
    if not callable(catalog):
        return _command_failure('doctor.pair_unavailable', 'discovery_unavailable')
    try:
        available = catalog()
        old = available.require(provider, old_model)
        new = available.require(provider, new_model)
        if old.provider != new.provider:
            return _command_failure('doctor.pair_cross_provider', 'cross_provider_pair')
        provider = old.provider
    except ModelUnavailable as exc:
        return _command_failure('doctor.pair_model_unavailable', exc.code, detail=str(exc))
    pair = old_model, new_model
    line = confirm_line(provider, pair[0], pair[1])
    if terminal:
        print(line)
    if not yes:
        if terminal:
            if not sys.stdin.isatty() or input(copy('doctor.verify_answer')).strip().lower() != 'y':
                return _command_failure('doctor.pair_declined', 'confirmation_declined')
        else:
            choice = service.ctx.ask_choice(line, [
                {'value': 'y', 'label': copy('doctor.verify_continue')},
                {'value': 'n', 'label': copy('button.cancel')},
            ])
            if choice != 'y':
                return line + '\n' + _command_failure('doctor.pair_declined', 'confirmation_declined')
    context = config.get('context', {})
    if not isinstance(context, dict) or context.get('engine', 'compressor') not in (None, '', 'compressor'):
        return _command_failure('doctor.pair_context_engine', 'context_engine_unsupported')
    home = profile_home()
    if home is None:
        return _command_failure('doctor.pair_fail', 'profile_home_unavailable')
    probe = PairProbe(agent_factory=_agent_factory, register_hook=service.ctx.register_hook,
                      write=lambda entry: save_pair(service.ctx, entry),
                      evidence_dir=home / 'plugin-data' / 'shadow', config=config)
    passed = probe.run(provider, pair[0], pair[1])
    text = copy('doctor.pair_pass') if passed else copy('doctor.pair_fail') + '\n' + copy('switch.doctor.pair_failure_reason', reason=probe.reason_code)
    return text if terminal else line + '\n' + text


def save_pair(ctx, entry):
    try:
        pairs = ctx.get_config('verified_gradual_pairs', [])
        if not isinstance(pairs, list):
            return False
        pairs = [item for item in pairs if not (isinstance(item, dict)
                 and item.get('old') == entry['old'] and item.get('new') == entry['new'])]
        ctx.set_config('verified_gradual_pairs', [*pairs, entry])
        return True
    except Exception:
        return False
