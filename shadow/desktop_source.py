"""Live Desktop source: saved tests from the profile store the trial line already uses."""

import contextvars
from collections import Counter
import secrets
import threading

from .common.models import canonical_slug


class ProfileSource:
    def __init__(self, trial, switch=None):
        self.trial = trial
        self.switch = switch
        self._preview_seed = secrets.randbits(32)
        self._previews = {}
        self._start_lock = threading.RLock()

    def tests(self):
        return [self.trial.verdict_view(self.trial.reconcile_controller(record)) for record in self.trial.store.list()]

    def test(self, test_id):
        record = self.trial.store.get(test_id) if test_id else None
        return self.trial.verdict_view(self.trial.reconcile_controller(record)) if record else None

    def active_switch(self):
        engine = getattr(self.switch, 'engine', None)
        record = engine.record if engine is not None else None
        # Lazy so package registration still works when tests replace the switch package.
        from .switch.domain import is_active
        if not is_active(record):
            return None
        return record.to_dict()

    def settings(self):
        ctx = self.trial.ctx
        reader = getattr(ctx, 'get_config', None)
        on = reader('web_lookups', True) if reader else True
        return {'web_lookups': bool(on)}

    def set_web_lookups(self, on):
        ctx = self.trial.ctx
        writer = getattr(ctx, 'set_config', None)
        if writer:
            writer('web_lookups', bool(on))
            return
        config = getattr(ctx, 'config', None)
        if isinstance(config, dict):
            config['web_lookups'] = bool(on)

    def save_pick(self, test_id, turn_id, pick):
        record = self.trial.store.get(test_id)
        if record is None:
            raise ValueError('no such test')
        blind = record.setdefault('blind', {})
        blind.setdefault('picks', {}).setdefault(turn_id, pick)
        self.trial.store.save(record)

    def models(self):
        current, catalog = self.trial.models()
        rows = []
        seen = {(current.provider, canonical_slug(current.model))}
        for model in catalog:
            route = (model.provider, canonical_slug(model.model))
            if route not in seen:
                seen.add(route)
                rows.append({**_model_row(model), 'model': f'{model.provider}::{model.model}'})
        labels = Counter(row['name'] for row in rows)
        for row in rows:
            if labels[row['name']] > 1:
                row['name'] = row['model'].split('::', 1)[1]
        return rows

    def start_facts(self, model):
        with self._start_lock:
            return self._start_facts(model)

    def _start_facts(self, model):
        from .common.copy import copy
        from .common.discovery import ModelUnavailable
        from .common.models import MAKER_NAMES
        from .trial.engine import estimate
        from .trial.judge import JudgeChoice, choose_judge, judge_options
        from .trial.picking import export_sessions, pick_turns
        from .trial.tiers import normal_service_tiers, prices_for_tiers, profile_service_tiers
        from .trial.usage import resolve_prices, stored_price

        try:
            inventory = self.trial.model_catalog()
            if inventory.failure:
                return None
            current = inventory.require(inventory.current.provider, inventory.current.model)
            trying = inventory.resolve(model)
        except (ModelUnavailable, ValueError, TypeError):
            return None
        if (trying.provider, trying.model) == (current.provider, current.model):
            return None
        catalog = list({(m.provider, m.model): m for m in inventory.candidates}.values())
        args = self.trial.parser().parse_args(['try', f'{trying.provider}::{trying.model}'])
        choice = choose_judge(current, trying, catalog)
        if self.trial.selector:
            selection = self.trial.selector(count=args.turns, days=args.days)
        else:
            selection = pick_turns(export_sessions(self.trial.launcher.profile, args.days),
                                   seed=self._preview_seed, count=args.turns)
        options = judge_options(current, trying, catalog)
        service_tiers = profile_service_tiers(self.trial.configuration())
        prices = prices_for_tiers(resolve_prices((current, trying, *options)), service_tiers)
        prediction = estimate(selection, current, trying, choice, prices=prices)
        base_prediction = estimate(selection, current, trying, JudgeChoice('third_maker', ()), prices=prices)

        def maker_label(item):
            return MAKER_NAMES.get(item.maker, copy('discovery.maker_unknown'))

        def judge_row(judge, *, auto=False):
            cost = estimate(selection, current, trying, JudgeChoice('third_maker', (judge,)), prices=prices)['judge']
            return {**_model_row(judge), 'model': f'{judge.provider}::{judge.model}',
                    'maker': maker_label(judge), 'cost': cost, 'auto': auto,
                    'priced': stored_price(prices, judge) is not None}

        judges = [judge_row(judge, auto=index == 0) for index, judge in enumerate(options)]
        judging = [judge_row(judge) for judge in choice.models]
        picked = len(selection.turns)
        web_lookups = self.settings()['web_lookups']
        preview = {'selection': selection, 'current': current, 'trial': trying,
                   'catalog': catalog, 'args': args, 'web_lookups': web_lookups}
        route = f'{trying.provider}::{trying.model}'
        self._previews[route] = preview
        self._previews[model] = preview
        return {'trial': {**_model_row(trying), 'priced': stored_price(prices, trying) is not None},
                'current': {**_model_row(current), 'priced': stored_price(prices, current) is not None},
                'judges': judges, 'judge_mode': choice.mode, 'judge_models': judging,
                'picked': picked, 'total': selection.total, 'days': args.days,
                'left_out': selection.left_out,
                'scrub': {'keys': selection.scrub_counts.get('key', 0),
                          'emails': selection.scrub_counts.get('email', 0)},
                'replay_estimate': base_prediction['total'], 'cap': args.budget,
                'plan_tokens': prediction['plan_tokens'], 'plan_cost': prediction['plan_cost'],
                'tokens': prediction['tokens'],
                'priced': base_prediction['priced'], 'minutes': max(1, picked),
                'hidden_maker': ', '.join(dict.fromkeys(maker_label(m) for m in (current, trying))),
                'isolation_verified': self.trial.launcher.isolation_verified,
                'pricing_blocker': None if normal_service_tiers(service_tiers) else 'service_tier',
                'web_lookups': web_lookups}

    def act(self, action, params):
        if action == 'start':
            from .common.copy import load_copy
            from .desktop_rpc import Copy, DesktopError, build_start, consent_matches
            from .trial.controller import IsolationRequired
            from .trial.engine import TrialEngine
            from .trial.tiers import profile_service_tiers

            with self._start_lock:
                table = params['_copy_table'] if '_copy_table' in params else load_copy()
                card = build_start(Copy(table), self, params.get('model'), params.get('judge'),
                                   refresh=bool(params.get('consent')))
                if not consent_matches(params, card):
                    return {'notice': None, 'test_id': params.get('test_id'), 'start_card': card}
                active = self.trial.active_record()
                if active:
                    raise DesktopError('trial.still_running', id=active['id'])
                if self.active_switch():
                    raise DesktopError('trial.switch_running')
                if not self.trial.launcher.isolation_verified:
                    raise DesktopError('trial.isolation_required')
                preview = self._previews.get(params.get('model'))
                if preview is None:
                    raise DesktopError('trial.unavailable')
                args = preview['args']
                self.trial.store.prune()
                engine = TrialEngine(self.trial.ctx, self.trial.store, self.trial.guard)
                record = engine.prepare(preview['selection'], preview['current'], preview['trial'],
                                        preview['catalog'], cap=args.budget, days=args.days,
                                        judge=params.get('judge'), origin=params.get('origin'),
                                        web_lookups=preview['web_lookups'], intent=params.get('intent', 'switch'),
                                        service_tiers=profile_service_tiers(self.trial.configuration()))
                if not record['can_start']:
                    raise DesktopError('trial.unavailable')
                record['state'] = 'queued'
                self.trial.store.save(record)
                self.trial.engine = engine
                try:
                    self.trial.start_controller(record)
                except (OSError, IsolationRequired) as error:
                    record['state'] = 'interrupted'
                    self.trial.store.save(record)
                    key = 'trial.isolation_required' if isinstance(error, IsolationRequired) else 'trial.unavailable'
                    raise DesktopError(key) from None
                self._preview_seed = secrets.randbits(32)
                context = contextvars.copy_context()
                threading.Thread(target=lambda: context.run(self.trial.watch, record['id']), daemon=True).start()
                return {'notice': None, 'test_id': record['id']}
        if action == 'stop_test':
            record = self.trial.store.get(params.get('test_id'))
            record['state'] = 'cancelled'
            if record.get('stop_reason') != 'unpriceable':
                record['stop_reason'] = 'cancelled'
            self.trial.store.save(record)
            engine = self.trial.engine
            if engine and engine.record['id'] == record['id']:
                engine.record = record
                engine.cancel()
            return {'notice': None, 'test_id': record['id']}
        switch = self.switch
        if switch is not None and action in {'switch', 'switch_all', 'anyway'}:
            from .common.copy import copy
            from .switch.decision import switch_warning
            from .desktop_rpc import DesktopError

            active = self.trial.active_record()
            if active:
                raise DesktopError('trial.still_running', id=active['id'])
            switch.engine.reconcile_config(force=True)
            proposal = switch.lookup_proposal(params.get('test_id') or params.get('model'))
            if proposal is None:
                raise DesktopError('switch.error.no_result')
            switch.require_target_change(proposal.new)
            switch.validate_routes(proposal.old, proposal.new)
            switch.engine.validate_routes(proposal)
            confirmed = params.get('confirm_override') is True
            if proposal.override_reason and not confirmed:
                return {'test_id': params.get('test_id'), 'confirm_override': {
                    'message': switch_warning(proposal), 'confirm': copy('switch.override.confirm'),
                    'cancel': copy('switch.override.cancel')}}
            switch.engine.start(proposal, confirm_override=confirmed)
            return {'notice': switch.notice_text() or None}
        if switch is not None and action in {'undo', 'move_on', 'resume', 'stop_switch'}:
            name = {'move_on': 'advance', 'stop_switch': 'stop'}.get(action, action)
            notice = switch.action(name, switch_id=params.get('switch_id'))
            return {'notice': notice or None, 'test_id': params.get('test_id')}
        return {'notice': None, 'test_id': params.get('test_id')}



def _model_row(model):
    return {'model': model.model, 'name': model.name, 'provider': model.provider,
            'provider_name': model.provider, 'maker': model.maker, 'reachable': model.reachable,
            'plan': model.plan}
