"""Verify a model switch through two public Hermes completions."""

from copy import deepcopy
from contextlib import suppress
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
    def __init__(self, *, ctx, write, evidence_dir, config=None):
        self.ctx = ctx
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
        task = 'shadow_pair_' + uuid4().hex
        events = {name: [[], []] for name in ('pre_auxiliary_call', 'post_auxiliary_call')}
        active_turn, registrations = None, []

        def observer(name):
            def capture(**event):
                if active_turn is not None and event.get('aux_task') == task:
                    fields = ('provider', 'model', 'response_model', 'base_url', 'api_mode', 'error',
                              'finish_reason', 'request_messages', 'api_request_id')
                    events[name][active_turn].append(deepcopy({key: event.get(key) for key in fields}))
            return capture

        try:
            from agent.plugin_llm import PluginLlmTrustError

            reasoning, sealed_required = pair_reasoning(self.config, provider, old_model)
            if sealed_required:
                return self._fail('sealed_reasoning_unobservable')
            # A unique task keeps concurrent auxiliary traffic out of this proof.
            registrations.append(self.ctx.register_auxiliary_task(
                task, display_name='Shadow pair check', description='Verify an exact model pair.'))
            for name in events:
                registrations.append(self.ctx.register_hook(name, observer(name)))
            results, first_answer = [], None
            messages = [{'role': 'user', 'content': FIRST}]
            for index, model in enumerate((old_model, new_model)):
                active_turn = index
                try:
                    result = self.ctx.llm.complete(
                        deepcopy(messages), provider=provider, model=model,
                        max_tokens=512, timeout=60, task=task)
                except PluginLlmTrustError:
                    return self._fail('llm_trust_denied')
                finally:
                    active_turn = None
                if (getattr(result, 'provider', None) != provider
                        or not _matches_model(getattr(result, 'model', None), model)
                        or not isinstance(getattr(result, 'text', None), str) or not result.text.strip()):
                    return self._fail(f'turn_{index + 1}_incomplete')
                before, after = events['pre_auxiliary_call'][index], events['post_auxiliary_call'][index]
                if len(before) != 1:
                    return self._fail(f'turn_{index + 1}_request_count')
                if len(after) != 1:
                    return self._fail(f'turn_{index + 1}_response_count')
                if after[0].get('error'):
                    return self._fail(f'turn_{index + 1}_request_error')
                if not before[0].get('api_request_id') or before[0]['api_request_id'] != after[0].get('api_request_id'):
                    return self._fail(f'turn_{index + 1}_request_identity')
                route = _observed_route(after[0], provider, model)
                if route is None:
                    return self._fail(f'turn_{index + 1}_response_route')
                if _observed_route({**before[0], 'response_model': after[0].get('response_model')}, provider, model) != route:
                    return self._fail(f'turn_{index + 1}_request_route')
                if after[0].get('finish_reason') != 'stop':
                    return self._fail(f'turn_{index + 1}_finish_reason')
                if index == 0:
                    first_answer = result.text.strip()
                    messages += [{'role': 'assistant', 'content': result.text}, {'role': 'user', 'content': SECOND}]
                else:
                    if first_answer not in _assistant_text(before[0].get('request_messages')):
                        return self._fail('turn_2_carried_text_missing')
                    if result.text.strip() != first_answer:
                        return self._fail('turn_2_answer_changed')
                results.append(route)
            old, new = results
            if old['base_url'] != new['base_url'] or old['api_mode'] != new['api_mode']:
                return self._fail('pair_routes_differ')
            body = {'provider': provider, 'old_model': old_model, 'new_model': new_model,
                    'turns': 2, 'reasoning': reasoning, 'sealed_reasoning_observed': False,
                    'reasoning_check': 'text_only', 'live_history_handoff': False,
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
                         'reasoning': reasoning, 'sealed_reasoning_observed': False}
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
            active_turn = None
            for registration in reversed(registrations):
                if registration is not None:
                    with suppress(Exception):
                        registration.dispose()


def profile_home():
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return None


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
    home = profile_home()
    if home is None:
        return _command_failure('doctor.pair_fail', 'profile_home_unavailable')
    probe = PairProbe(ctx=service.ctx,
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
