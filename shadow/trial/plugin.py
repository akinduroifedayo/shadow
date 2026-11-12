import argparse
import contextvars
import json
import logging
import os
import secrets
import shlex
import sys
import tempfile
import threading
import time
from dataclasses import replace

from ..common.discovery import ModelCatalog, ModelUnavailable, ProviderPresence, discover_models
from ..common.copy import copy
from ..common.controller_stop import controller_stop_text
from ..common.models import MAKER_NAMES, Model
from ..common.storage import Store
from .cards import render_start, render_verdict, result_actions, render_turns, run_message
from .controller import ControllerLauncher, IsolationRequired
from .engine import TrialEngine, estimate
from .probe import SETUP_PREFIX, active_home, claim_setup_job, confirm_line, failure_text, finish_setup, is_setup_request, launch_setup, new_job, setup_request_text, success_text
from .judge import JudgeChoice, choose_judge, judge_options
from .picking import Turn, export_sessions, pick_turns
from .reads import SafeReads
from .safety import ReplayGuard
from .tiers import profile_service_tiers, normal_service_tiers, prices_for_tiers
from .usage import resolve_prices
from .verdict import finalize, refresh_statistics


logger = logging.getLogger(__name__)
_TURN_EXIT_CODES = frozenset({
    'unknown', 'completed', 'text_response', 'interpreter_shutdown', 'local_processing_error',
    'repeated_outer_errors', 'error_near_max_iterations', 'max_iterations_reached',
    'all_retries_exhausted_no_response', 'empty_response_exhausted', 'pending_tool_result',
    'context_compression_timeout', 'context_compression_exhausted', 'session_persistence_failed',
    'interrupted', 'interrupted_during_api_call', 'interrupted_during_tool_execution',
    'interrupted_by_user', 'interrupted_by_system', 'blocked_by_plugin_pre_llm_call',
    'review_input_budget_exhausted', 'budget_exhausted', 'redirect_restart_limit_exceeded',
    'rebuilt_restart_limit_exceeded', 'compaction_handoff_not_actionable',
    'ollama_runtime_context_too_small', 'guardrail_halt',
    'partial_stream_recovery', 'fallback_prior_turn_content',
    'auth', 'authentication_error', 'rate_limit', 'rate_limited', 'server_error', 'timeout',
})


def _turn_exit_code(reason):
    code = reason.split('(', 1)[0] if isinstance(reason, str) else ''
    return code if code in _TURN_EXIT_CODES else 'unknown'


class CommandError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CommandError(copy('trial.invalid'))


class TrialPlugin:
    def __init__(self, ctx, *, store=None, current=None, catalog=None, launcher=None, selector=None, config_reader=None, watch_wait=None):
        self.ctx = ctx
        self.store = store or Store()
        self.current = current
        self.catalog = catalog
        self.provider_presence = ProviderPresence()
        self.launcher = launcher or ControllerLauncher(getattr(ctx, 'profile_name', 'default'))
        self.selector = selector
        self.config_reader = config_reader
        self.watch_wait = watch_wait or time.sleep
        self.guard = ReplayGuard()
        self.guard_registrations = {}
        self.isolation_message = copy('trial.isolation_required')
        self.message_watcher = None
        self.setup_active = None
        self.setup_running = False
        self.setup_used = set()
        self.setup_lock = threading.Lock()
        self.setup_requests = {}
        self.route_capture = None
        self.setup_launch = launch_setup
        self.reads = SafeReads(self.guard, web_fetch=self.lookup)
        self.guard.reads = self.reads
        self.engine = None

    def lookup(self, args):
        if 'url' in args:
            return self.ctx.dispatch_tool('web_extract', {'urls': [args['url']]})
        return self.ctx.dispatch_tool('web_search', {'query': args['query']})

    def configuration(self):
        if self.config_reader:
            return self.config_reader()
        from hermes_cli.config import load_config_readonly
        return load_config_readonly() or {}

    def model_catalog(self):
        if self.current and self.catalog is not None:
            return ModelCatalog.from_models(self.current, self.catalog)
        return discover_models(live_providers=self.provider_presence.providers())

    def models(self):
        catalog = self.model_catalog()
        if catalog.failure:
            raise ModelUnavailable(catalog.failure)
        return catalog.current, catalog.candidates

    def active_record(self):
        for record in reversed(self.store.list()):
            record = self.reconcile_controller(record)
            if record['state'] in {'queued', 'running'}:
                return record
        return None

    def availability(self):
        try:
            inventory = self.model_catalog()
            current = inventory.require(inventory.current.provider, inventory.current.model)
            catalog = inventory.candidates
            others = [m for m in catalog if m.reachable and (m.provider, m.model) != (current.provider, current.model)]
            judge = any(all(model.reachable for model in choose_judge(current, other, catalog).models) for other in others)
            return bool(others), judge
        except ValueError:
            return False, False

    def parser(self, *, terminal=False):
        parser = Parser(prog='hermes shadow' if terminal else '/shadow', add_help=True)
        verbs = parser.add_subparsers(dest='verb')
        trial = verbs.add_parser('try', aliases=['compare'])
        trial.add_argument('model', nargs='?')
        trial.add_argument('--turns', type=int, default=30)
        trial.add_argument('--days', type=int, default=7)
        trial.add_argument('--budget', type=float, default=5.0)
        trial.add_argument('--judge', help=copy('cli.judge_help'))
        if terminal:
            trial.add_argument('--yes', action='store_true')
        results = verbs.add_parser('results')
        results.add_argument('id', nargs='?')
        results.add_argument('--stats', action='store_true')
        for name in ('status', 'cancel', 'undo'):
            verbs.add_parser(name)
        doctor = verbs.add_parser('doctor')
        doctor.add_argument('--verify', action='store_true')
        doctor.add_argument('--verify-pair', action='store_true')
        doctor.add_argument('--yes', action='store_true')
        doctor.add_argument('old_model', nargs='?')
        doctor.add_argument('new_model', nargs='?')
        doctor.add_argument('--provider')
        forget = verbs.add_parser('forget')
        forget.add_argument('id')
        return parser

    def command(self, raw_args, *, terminal=False, session_id=None, session_key=None, platform=None, **_):
        try:
            args = self.parser(terminal=terminal).parse_args(shlex.split(raw_args))
            if not args.verb:
                return self.doctor()
            if args.verb == 'doctor':
                if args.verify and args.verify_pair:
                    return copy('doctor.verify_one')
                if args.verify:
                    return self.verify_isolation(terminal=terminal, yes=args.yes)
                if args.verify_pair:
                    return self.verify_pair(terminal=terminal, yes=args.yes, **({'old_model': args.old_model, 'new_model': args.new_model, 'provider': args.provider} if args.old_model or args.new_model or args.provider else {}))
                return self.doctor()
            self.store.prune()
            if args.verb in {'try', 'compare'}:
                return self.prepare(args, terminal=terminal, origin={'session_id': session_id, 'session_key': session_key, 'platform': platform})
            if args.verb == 'status':
                record = self.active_record() or self.store.get()
                return self.status(record) if record and record['state'] in {'queued', 'running', 'interrupted'} else copy('status.idle')
            if args.verb == 'undo':
                return copy('trial.switch_unavailable')
            if args.verb == 'forget':
                active = self.active_record()
                if active and args.id in {'all', active['id']}:
                    return copy('trial.still_running', id=active['id'])
                self.store.forget(args.id)
                return copy('trial.forgotten')
            record = self.active_record() if args.verb == 'cancel' else self.store.get(args.id)
            if not record:
                return copy('trial.not_found')
            record = self.reconcile_controller(record)
            if args.verb == 'cancel':
                record['state'] = 'cancelled'
                if record.get('stop_reason') != 'unpriceable':
                    record['stop_reason'] = 'cancelled'
                self.store.save(record)
                if self.engine and self.engine.record['id'] == record['id']:
                    self.engine.cancel()
                return self.stopped(record)
            if record['state'] in {'cancelled', 'interrupted'} and 'totals' not in record:
                count = sum(t['usable'] for t in record['turns'])
                if count < 20:
                    return self.stopped(record)
                choice = 'result' if terminal else self.ctx.ask_choice(copy('button.result', count=count), [{'value': 'result', 'label': copy('button.result', count=count)}, {'value': 'cancel', 'label': copy('button.cancel')}])
                if choice != 'result':
                    return self.stopped(record)
                finalize(record)
                self.store.save(record)
            return self.result(record, terminal=terminal, stats=args.stats) if 'totals' in record else self.status(record)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            if isinstance(exc, (CommandError, ModelUnavailable)):
                return str(exc)
            if isinstance(exc, ValueError) and str(exc).startswith(('The judge', 'No model called', 'Shadow needs')):
                return str(exc)
            return copy('trial.unavailable')

    def result(self, record, *, terminal=False, stats=False):
        record = self.verdict_view(record)
        interactive = terminal and sys.stdin.isatty() and sys.stdout.isatty()
        text = render_verdict(record, cli=terminal, stats=stats, interactive=interactive)
        if stats:
            return text
        choices = result_actions(record)
        if terminal:
            if not interactive:
                return text
            print(text)
            for index, choice in enumerate(choices, 1):
                print(f'{index}  {choice["label"]}')
            default = next(i for i, c in enumerate(choices, 1) if c['value'] == 'keep')
            selected = input(copy('cli.choose', default=default)).strip() or str(default)
            action = choices[int(selected) - 1]['value'] if selected.isdigit() and 1 <= int(selected) <= len(choices) else 'keep'
        else:
            action = self.ctx.ask_choice(text, choices)
        if action == 'keep':
            return copy('trial.kept', current=Model(**record['current']).name)
        if action == 'turns':
            return render_turns(record, cli=terminal)
        if action == 'switch' and any(c['value'] == 'switch' for c in choices):
            handler = self.ctx.shadow_command_routes.get('switch')
            return handler('switch ' + record['id'], terminal=terminal) if handler else copy('trial.switch_unavailable')
        if action == 'more' and any(c['value'] == 'more' for c in choices):
            args = self.parser(terminal=terminal).parse_args(['compare' if record['intent'] == 'compare' else 'try', f'{record["trial"]["provider"]}::{record["trial"]["model"]}', '--days', str(record['days']), '--budget', str(record['cap'])])
            return self.prepare(args, terminal=terminal, origin=record['origin'], previous=record)
        return text

    def verdict_view(self, record):
        record = refresh_statistics(record)
        switch = getattr(self.ctx, 'shadow_switch', None)
        if switch:
            proposal = self.proposal(record['id'])
            if proposal and proposal.gradual and not switch.engine.verified_pair(proposal.old, proposal.new):
                return {**record, 'switch_limit': 'trial.switch_compatibility'}
        return record

    def prepare(self, args, *, terminal, origin, previous=None):
        if args.turns < 20 or args.turns > 60 or args.days < 1 or args.days > 365:
            return copy('trial.minimum')
        active = self.active_record()
        if active:
            return copy('trial.still_running', id=active['id'])
        switch = getattr(self.ctx, 'shadow_switch', None)
        if switch:
            try:
                from ..switch import is_active
            except ImportError:
                pass
            else:
                if is_active(switch.engine.record):
                    return copy('trial.switch_running')
        inventory = self.model_catalog()
        current, catalog = inventory.current, inventory.candidates
        if previous:
            current = Model(**previous['current'])
            if previous['judge_mode'] == 'third_maker':
                judge_model = previous['judge']['models'][0]
                args.judge = f'{judge_model["provider"]}::{judge_model["model"]}'
        inventory.require(current.provider, current.model)
        if not args.model:
            name = self.ctx.ask_choice(copy('trial.pick_model'), [{'value': f'{m.provider}::{m.model}', 'label': f'{m.name} · {m.provider}'} for m in sorted(catalog, key=lambda m: m.model) if m != current])
            if not name:
                return copy('button.cancel')
            args.model = name
        matches = inventory.matches(args.model)
        if len(matches) > 1:
            provider = self.ctx.ask_choice(copy('trial.pick_provider'), [{'value': m.provider, 'label': m.provider} for m in matches])
            matches = [m for m in matches if m.provider == provider]
        if len(matches) != 1:
            inventory.resolve(args.model)
        trial = inventory.require(matches[0].provider, matches[0].model)
        if trial == current:
            return copy('trial.same_model')
        if args.judge:
            chosen = inventory.resolve(args.judge)
            args.judge = f'{chosen.provider}::{chosen.model}'
        choose_judge(current, trial, catalog, args.judge)
        if self.selector:
            selection = self.selector(count=60 if previous else args.turns, days=args.days)
        else:
            earlier = [Turn(**{k: t[k] for k in Turn.__dataclass_fields__}) for t in previous['turns']] if previous else []
            selection = pick_turns(export_sessions(self.launcher.profile, args.days), seed=secrets.randbits(32), count=30 if previous else args.turns, previous=earlier)
        if previous and self.selector:
            from collections import Counter
            ids = {t['id'] for t in previous['turns']}
            goals = {t['goal'] for t in previous['turns']}
            conversations = Counter(t['conversation_id'] for t in previous['turns'])
            extra = []
            for turn in selection.turns:
                if turn.id not in ids and turn.goal not in goals and conversations[turn.conversation_id] < 3 and len(extra) < 30:
                    extra.append(turn)
                    conversations[turn.conversation_id] += 1
            selection = replace(selection, turns=extra, left_out={**selection.left_out, 'not_picked': selection.left_out.get('not_picked', 0) + len(selection.turns) - len(extra)})
        engine = TrialEngine(self.ctx, self.store, self.guard)
        config = self.configuration()
        service_tiers = profile_service_tiers(config)
        record = engine.prepare(selection, current, trial, catalog, cap=args.budget, days=args.days, judge=args.judge, origin=origin, web_lookups=self.ctx.get_config('web_lookups', True), previous=previous, service_tiers=service_tiers, intent='compare' if args.verb == 'compare' else 'switch')
        while True:
            text = render_start(record, cli=terminal, isolation_verified=self.launcher.isolation_verified)
            if not self.launcher.isolation_verified:
                return text
            if terminal:
                break
            actions = [{'value': 'start', 'label': copy('button.start')}] if record['can_start'] else []
            if not previous and len(selection.turns) > 20:
                actions.append({'value': 'fewer', 'label': copy('button.fewer')})
            actions.append({'value': 'preview', 'label': copy('button.preview', picked=len(selection.turns))})
            if not previous:
                actions.append({'value': 'judge', 'label': copy('start.judge_change')})
            actions.append({'value': 'cancel', 'label': copy('button.cancel')})
            action = self.ctx.ask_choice(text, actions)
            if action == 'start' and record['can_start']:
                start = True
                break
            if action == 'fewer' and not previous and len(selection.turns) > 20:
                selection = replace(selection, turns=selection.turns[:20], left_out={**selection.left_out, 'not_picked': selection.left_out.get('not_picked', 0) + len(selection.turns) - 20})
            elif action == 'preview':
                dropped = self.ctx.ask_choice(copy('button.preview', picked=record['picked']), [{'value': t.id, 'label': t.goal.splitlines()[0][:80]} for t in selection.turns])
                remaining = [t for t in selection.turns if t.id != dropped]
                selection = replace(selection, turns=remaining, left_out={**selection.left_out, 'not_picked': selection.left_out.get('not_picked', 0) + len(selection.turns) - len(remaining)})
            elif action == 'judge' and not previous:
                options = judge_options(current, trial, catalog)[:5]
                makers = ', '.join(dict.fromkeys(MAKER_NAMES.get(m.maker, copy('discovery.maker_unknown')) for m in (current, trial)))
                title = copy('judge.list.header') + '\n' + copy('judge.list.hidden_note', maker=makers)
                service_tiers = profile_service_tiers(self.configuration())
                prices = prices_for_tiers(resolve_prices((current, trial, *options)), service_tiers)
                choices = []
                unpriced = copy('trial.unpriceable' if normal_service_tiers(service_tiers) else 'start.service_tier')
                for index, model in enumerate(options):
                    prediction = estimate(selection, current, trial, JudgeChoice('third_maker', (model,)), prices=prices)
                    cost = copy('judge.list.option_cost', cost=prediction['judge']) if prediction['judge'] is not None else unpriced
                    maker = MAKER_NAMES.get(model.maker, copy('discovery.maker_unknown'))
                    label = f'{model.name} · {maker} · {model.provider} · {cost}'
                    choices.append({'value': str(index), 'label': label + (' ' + copy('judge.list.auto_label') if index == 0 else '')})
                selected = self.ctx.ask_choice(title, choices) if choices else None
                if selected in {c['value'] for c in choices}:
                    chosen = options[int(selected)]
                    args.judge = f'{chosen.provider}::{chosen.model}' if selected != '0' else None
            else:
                return text
            record = engine.prepare(selection, current, trial, catalog, cap=args.budget, days=args.days, judge=args.judge, origin=origin, web_lookups=record['web_lookups'], run_id=record['id'], previous=previous, service_tiers=service_tiers, intent='compare' if args.verb == 'compare' else 'switch')
        if not record['can_start']:
            return text
        if terminal:
            print(text)
            start = getattr(args, 'yes', False)
            if not start:
                import sys
                start = sys.stdin.isatty() and input(copy('start.confirm')).lower() == 'y'
        if start:
            if previous:
                if not self.store.reserve_second_look(previous['id'], record['id']):
                    return copy('trial.unavailable')
            record['state'] = 'queued'
            self.store.save(record)
            try:
                if not self.start_controller(record):
                    if previous and record.get('pricing_blocker') == 'service_tier':
                        self.store.release_second_look(previous['id'], record['id'])
                    return self.stopped(record)
            except (OSError, IsolationRequired):
                record['state'] = 'interrupted'
                self.store.save(record)
                return self.isolation_message
            if terminal:
                return self.watch(record['id'], terminal=True)
            else:
                context = contextvars.copy_context()
                threading.Thread(target=lambda: context.run(self.watch, record['id']), daemon=True).start()
        return '' if terminal else text

    def freeze_profile_service_tiers(self, record):
        try:
            tiers = profile_service_tiers(self.configuration())
        except (ImportError, OSError, ValueError, TypeError):
            tiers = None
        record['service_tiers'] = list(tiers) if tiers is not None else None
        return tiers is not None and normal_service_tiers(tiers)

    def start_controller(self, record, *, expected_claim=None):
        claim = self.store.claim_controller(record['id'], record.get('controller_pid'), expected_claim=expected_claim)
        if claim is None:
            return
        record.update(self.store.get(record['id']))
        if not self.freeze_profile_service_tiers(record):
            record.update(state='interrupted', stop_reason='unpriceable', can_start=False,
                          pricing_blocker='service_tier')
            self.store.save(record)
            self.store.controller_failed(record['id'], claim)
            return False
        self.store.save(record)
        try:
            pid = self.launcher.launch(self.store.get(record['id']))
        except (OSError, IsolationRequired):
            self.store.controller_failed(record['id'], claim)
            raise
        self.store.controller_started(record['id'], claim, pid)
        return True

    def reconcile_controller(self, record):
        if record['state'] not in {'queued', 'running'}:
            return record
        check = getattr(self.launcher, 'is_alive', None)
        pid = record.get('controller_pid')
        owner = pid if pid is not None else record.get('controller_owner_pid')
        if check and owner is not None and not check(owner):
            reason = 'controller_exited' if self.freeze_profile_service_tiers(record) else 'unpriceable'
            tiers = record['service_tiers']
            if self.store.controller_stopped(record['id'], record['controller_token'], reason):
                record = self.store.get(record['id'])
                record['service_tiers'] = tiers
                if reason == 'unpriceable':
                    record.update(can_start=False, pricing_blocker='service_tier')
                self.store.save(record)
            return self.store.get(record['id'])
        return record

    def on_session_end(self, *, session_id='', failed=False, interrupted=False, turn_exit_reason='', **_):
        if self.engine is None or self.engine.record.get('controller_session_id') != session_id:
            return
        record = self.engine.record
        reason = 'controller_failed' if failed else 'controller_interrupted' if interrupted else 'controller_exited'
        details = {'failed': bool(failed), 'interrupted': bool(interrupted), 'turn_exit_reason': _turn_exit_code(turn_exit_reason)}
        if self.store.controller_stopped(record['id'], record['controller_token'], reason, details=details):
            self.engine.interrupt('unpriceable' if record.get('stop_reason') == 'unpriceable' else reason)

    def watch(self, run_id, *, terminal=False):
        import sys
        handle, previous = None, None
        for _ in range(7200):
            record = self.store.get(run_id)
            if not record:
                return
            record = self.reconcile_controller(record)
            done = sum(t['usable'] for t in record['turns'])
            marker = (done, record['state'])
            if marker != previous:
                text = (self.stopped(record) if record['state'] in {'cancelled', 'interrupted'} else
                        run_message(record, 'progress'))
                if terminal:
                    if sys.stdout.isatty():
                        print('\r' + text, end='', flush=True)
                    elif done % 5 == 0 or record['state'] in {'finished', 'cancelled', 'interrupted'}:
                        print(text, flush=True)
                elif handle:
                    handle.edit(text)
                else:
                    handle = self.ctx.post_message(text)
                previous = marker
            if record['state'] in {'finished', 'cancelled', 'interrupted'}:
                if terminal and sys.stdout.isatty():
                    print()
                if record['state'] == 'finished':
                    if terminal:
                        return self.result(record, terminal=True)
                    self.ctx.post_message(render_verdict(self.verdict_view(record)))
                return self.stopped(record) if terminal else None
            try:
                self.watch_wait(1)
            except KeyboardInterrupt:
                record = self.store.get(run_id)
                record['state'] = 'cancelled'
                if record.get('stop_reason') != 'unpriceable':
                    record['stop_reason'] = 'cancelled'
                self.store.save(record)
                return self.stopped(record)
        return self.status(record) if terminal else None

    def status(self, record):
        record = self.reconcile_controller(record)
        if record['state'] in {'cancelled', 'interrupted'}:
            return self.stopped(record)
        return run_message(record, 'status')

    def stopped(self, record):
        count = sum(t['usable'] for t in record['turns'])
        text = run_message(record, 'stopped')
        if note := controller_stop_text(record, copy):
            text += '\n' + note
        return text + ('\n[' + copy('button.result', count=count) + ']' if count >= 20 and 'totals' not in record else '')

    def remember_replay_child(self, session_id, parent_session_id):
        if not session_id:
            return
        engine = self.engine
        with self.guard.lock:
            identified = session_id in self.guard.children
            quarantined = session_id in self.guard.unidentified
            controller_parent = parent_session_id in self.guard.controllers
        if identified and engine is not None:
            with engine.lock:
                run_id = engine.record['id']
            self.store.remember_replay_child(run_id, session_id)
            return
        if not quarantined:
            return
        for record in self.store.list():
            controller_child = controller_parent and parent_session_id == record.get('controller_session_id')
            if controller_child or parent_session_id in record.get('replay_child_session_ids', []):
                self.store.remember_replay_child(record['id'], session_id)

    def subagent_start(self, *, child_session_id='', parent_session_id='', **event):
        self.guard.subagent_start(child_session_id=child_session_id, parent_session_id=parent_session_id, **event)
        self.remember_replay_child(child_session_id, parent_session_id)

    def pre_llm_call(self, *, session_id='', user_message='', **event):
        if isinstance(user_message, str) and user_message.startswith(SETUP_PREFIX):
            return finish_setup(self, session_id, user_message, data_dir=self.launcher.directory)
        prefix = 'shadow-controller:'
        if isinstance(user_message, str) and user_message.startswith(prefix):
            switch = getattr(self.ctx, 'shadow_switch', None)
            if switch:
                switch.engine.exclude_session(session_id)
            marker = user_message.splitlines()[0].split(':')
            if len(marker) == 3:
                record = self.store.get(marker[1])
                if record and record['controller_token'] == marker[2] and 'prices' not in record and record['state'] in {'queued', 'running', 'interrupted'}:
                    with self.store.lock:
                        record = self.store.get(marker[1])
                        if not record or record['controller_token'] != marker[2] or 'prices' in record or record['state'] not in {'queued', 'running', 'interrupted'}:
                            return {'action': 'block', 'message': self.guard.controller_stop_message}
                        record.update(state='finished', can_start=False, stop_reason='unpriceable')
                        finalize(record)
                        self.store.save(record)
                        self.store.controller_failed(record['id'], marker[2])
                    with self.guard.lock:
                        self.guard.controllers.discard(record.get('controller_session_id'))
                        if not self.guard.controllers:
                            self.guard.exclusive = False
                        for child in record.get('replay_child_session_ids', []):
                            self.guard.unidentified.add(child)
                            if child in self.guard.children:
                                self.guard.children[child].stopped = True
                    return {'action': 'block', 'message': self.guard.controller_stop_message}
                if record and record['controller_token'] == marker[2] and record['state'] in {'queued', 'running'}:
                    self.store.controller_started(record['id'], marker[2], os.getpid())
                    if self.engine is None or self.engine.record['state'] in {'finished', 'cancelled', 'interrupted'} and self.engine.record['id'] != record['id']:
                        if not self.freeze_profile_service_tiers(record):
                            record.update(state='interrupted', stop_reason='unpriceable', can_start=False,
                                          pricing_blocker='service_tier')
                            self.store.save(record)
                            self.store.controller_failed(record['id'], marker[2])
                            parent = self.store.get(record['parent_id']) if record.get('parent_id') else None
                            if parent and record['spend'] <= parent['spend']:
                                self.store.release_second_look(parent['id'], record['id'])
                            with self.guard.lock:
                                self.guard.controllers.discard(record.get('controller_session_id'))
                                if not self.guard.controllers:
                                    self.guard.exclusive = False
                            return {'action': 'block', 'message': copy('start.service_tier')}
                        self.engine = TrialEngine(self.ctx, self.store, self.guard)
                        record['state'] = 'interrupted'
                        try:
                            self.engine.attach(record, session_id)
                        except ValueError:
                            return {'action': 'block', 'message': self.guard.controller_stop_message}
                        self.reads.bind(self.engine.record, on_change=self.engine.save)
                    return self.guard.pre_llm_call(session_id=session_id, user_message=user_message, **event)
            return {'action': 'block', 'message': self.guard.controller_stop_message}
        result = self.guard.pre_llm_call(session_id=session_id, user_message=user_message, **event)
        self.remember_replay_child(session_id, event.get('parent_session_id', ''))
        return result

    def llm_execution(self, *, request, next_call, session_id='', original_request=None, **_):
        if session_id in self.setup_requests:
            self.setup_requests[session_id] = True
            return None
        for payload in (request, original_request or {}):
            if is_setup_request(payload):
                job = claim_setup_job(self, session_id, setup_request_text(payload), data_dir=self.launcher.directory)
                if job is not None:
                    self.setup_requests[session_id] = True
                return None
        return next_call(request)

    def step(self, args, *, session_id='', **_):
        if self.engine is None or session_id not in self.guard.controllers:
            return json.dumps({'error': self.guard.controller_stop_message, 'continue': False})
        try:
            result = self.engine.step(session_id)
        except Exception as exc:
            error_type = type(exc).__name__
            logger.warning('Shadow controller step failed error_type=%s', error_type)
            record = self.engine.record
            if self.store.controller_stopped(record['id'], record['controller_token'], 'step_error', details={'error_type': error_type}):
                return json.dumps(self.engine.interrupt('step_error'))
            saved = self.store.get(record['id'])
            return json.dumps({'continue': False, 'state': saved['state']})
        self.engine.record['web_turns'] = len(self.reads.web_turns)
        self.engine.record['web_stopped'] = len(self.reads.web_stopped)
        self.engine.save()
        return json.dumps(result)

    def doctor(self):
        config = self.configuration() if self.config_reader or self.current is None else {}
        lines = [copy('trial.commands')]
        from ..switch.doctor import hermes_command, setup_doctor
        lines.append(setup_doctor(config, catalog=self.model_catalog(), profile_name=getattr(self.ctx, 'profile_name', 'default')))
        verified = self.launcher.verification() if hasattr(self.launcher, 'verification') else None
        if verified:
            key = 'doctor.verified_no_memory' if verified.get('no_saved_memory') else 'doctor.verified'
            lines.append(copy(key, date=verified['checked_at'][:10]))
        elif not self.launcher.isolation_verified:
            lines.append(copy('switch.doctor.isolation_required',
                              command=hermes_command(getattr(self.ctx, 'profile_name', 'default'))))
        return '\n'.join(lines)

    def _current_names(self):
        try:
            current, _catalog = self.models()
        except (TypeError, ValueError, AttributeError):
            return '', ''
        return current.provider, current.model

    def _confirmed(self, line, *, terminal, yes):
        if yes:
            if terminal:
                print(line)
            return True
        if terminal:
            print(line)
            if not sys.stdin.isatty():
                return False
            return input(copy('doctor.verify_answer')).strip().lower() == 'y'
        choice = self.ctx.ask_choice(line, [
            {'value': 'y', 'label': copy('doctor.verify_continue')},
            {'value': 'n', 'label': copy('button.cancel')},
        ])
        return choice == 'y'

    def verify_isolation(self, *, terminal, yes):
        if active := self.active_record():
            return copy('trial.still_running', id=active['id'])
        provider, model = self._current_names()
        if not provider or not model:
            return copy('doctor.verify_fail')
        line = confirm_line(provider, model)
        if not self._confirmed(line, terminal=terminal, yes=yes):
            return copy('doctor.verify_declined') if terminal else line + '\n' + copy('doctor.verify_declined')
        home = active_home()
        if not home:
            return copy('doctor.verify_fail')
        try:
            with tempfile.TemporaryDirectory(prefix='shadow-setup-') as workspace:
                job = new_job(home=home, workspace=workspace, provider=provider, model=model, profile=self.launcher.profile)
                result = self.setup_launch(self.launcher, job)
        except OSError:
            text = copy('doctor.verify_fail')
        else:
            if isinstance(result, dict) and result.get('passed') and self.launcher.isolation_verified:
                verified = self.launcher.verification()
                text = success_text(verified, date=verified['checked_at'][:10])
            else:
                text = failure_text(result if isinstance(result, dict) else {})
        return text if terminal else line + '\n' + text

    def verify_pair(self, *, terminal, yes, **pair):
        verifier = getattr(self.ctx, 'shadow_verify_pair', None)
        if verifier is None:
            return copy('doctor.pair_unavailable')
        return verifier(terminal=terminal, yes=yes, **pair)

    def setup_tool(self, _args, **_):
        verified = self.launcher.verification() if hasattr(self.launcher, 'verification') else None
        if verified:
            return success_text(verified, date=verified['checked_at'][:10])
        return copy('doctor.verify_fail')

    def proposal(self, run_id=None):
        if self.active_record():
            return None
        try:
            from ..switch import Proposal, Route
        except ImportError:
            return None
        records = [self.store.get(run_id)] if run_id else reversed(self.store.list())
        for record in records:
            if not record or 'totals' not in record or set(record.get('routes', {})) != {'current', 'trial'} or record.get('route_conflicts'):
                continue
            record = refresh_statistics(record)
            try:
                proposal = Proposal(record['id'], Route(**record['routes']['current']), Route(**record['routes']['trial']), record['totals']['word'])
            except (TypeError, ValueError, KeyError):
                continue
            return proposal
        return None


def register(ctx, plugin=None):
    service = plugin or TrialPlugin(ctx)
    records = service.store.list() if service.store.path is None or service.store.path.exists() else ()
    for record in records:
        active = record['state'] in {'queued', 'running'}
        service.guard.unidentified.update(record.get('replay_child_session_ids', []))
        if not record.get('controller_session_id') or not active:
            continue
        service.guard.add_controller(record['controller_session_id'])
        attached = service.engine is not None and service.engine.record['id'] == record['id']
        if active and record.get('controller_pid') == os.getpid() and not attached:
            service.store.controller_stopped(record['id'], record['controller_token'], 'plugin_reloaded')
    ctx.shadow_trial = service
    ctx.shadow_proposal_lookup = service.proposal
    ctx.shadow_model_availability = service.availability
    ctx.register_hook('subagent_start', service.subagent_start)
    ctx.register_hook('subagent_start', service.provider_presence.note_child)
    ctx.register_hook('post_api_request', service.provider_presence.observe)
    ctx.register_hook('post_compaction', service.provider_presence.compacted)

    def record_child(**event):
        if service.setup_active is not None:
            service.setup_active.note_child(**event)

    ctx.register_hook('subagent_start', record_child)
    ctx.register_hook('pre_llm_call', service.pre_llm_call)
    ctx.register_hook('on_session_end', service.on_session_end)
    service.guard_registrations['veto'] = ctx.register_hook('pre_tool_call', service.guard.pre_tool_call)

    def observe_request(**event):
        if service.message_watcher is not None:
            service.message_watcher(**event)

    ctx.register_hook('pre_api_request', observe_request)
    for hook in ('post_api_request', 'api_request_error', 'post_tool_call', 'subagent_stop'):
        def observe(_name=hook, **event):
            if _name == 'post_api_request' and service.setup_active is not None:
                service.setup_active.note_call(**event)
            if _name == 'post_api_request' and service.route_capture is not None:
                service.route_capture.append({key: event.get(key) for key in ('provider', 'response_model', 'base_url', 'api_mode')})
            if service.engine is not None:
                return getattr(service.engine, _name)(**event)
        ctx.register_hook(hook, observe)
    service.guard_registrations['middleware'] = ctx.register_middleware('tool_execution', service.guard.tool_execution)
    ctx.register_middleware('llm_execution', service.llm_execution)
    tools = [
        ('shadow_read', service.reads.read, {'path': {'type': 'string'}}, ['path']),
        ('shadow_search', service.reads.search, {'path': {'type': 'string'}, 'query': {'type': 'string'}}, ['query']),
        ('shadow_web', service.reads.web, {'query': {'type': 'string'}, 'url': {'type': 'string'}}, []),
        ('shadow_clarify', service.reads.clarify, {}, []),
        ('shadow_trial_step', service.step, {}, []),
        ('shadow_setup_check', service.setup_tool, {}, []),
    ]
    for name, handler, properties, required in tools:
        ctx.register_tool(name=name, toolset='shadow_controller' if name in {'shadow_trial_step', 'shadow_setup_check'} else 'shadow_replay', schema={'name': name, 'description': copy('tool.' + name), 'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}, handler=handler)
    routes = getattr(ctx, 'shadow_command_routes', None)
    if routes is None:
        routes = ctx.shadow_command_routes = {}
    routes.update({name: service.command for name in ('', 'try', 'compare', 'results', 'cancel', 'forget')})
    for name in ('status', 'doctor', 'undo'):
        routes.setdefault(name, service.command)
    if not getattr(ctx, 'shadow_router_registered', False):
        def dispatch(raw_args, **context):
            verb = raw_args.strip().split(maxsplit=1)[0] if raw_args.strip() else ''
            return ctx.shadow_command_routes.get(verb, service.command)(raw_args, **context)
        def cli_setup(parser):
            parser.add_argument('shadow_args', nargs=argparse.REMAINDER)
        def cli_handler(args):
            print(dispatch(shlex.join(args.shadow_args), terminal=True))
        ctx.register_command('shadow', dispatch, description=copy('trial.commands'), args_hint='<command>')
        ctx.register_cli_command('shadow', copy('trial.commands'), cli_setup, cli_handler)
        ctx.shadow_router_registered = True
    return service
