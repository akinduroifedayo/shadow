import json
import math
import secrets
import threading
import time
import uuid
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from urllib.parse import urlsplit

from ..common.copy import copy
from ..common.models import Model, canonical_slug
from ..common.scrub import Scrubber, outbound

try:
    from tools.registry import registry as _tool_registry
except ImportError:
    _tool_registry = None
from .judge import Judge, JudgeChoice, choose_judge
from .picking import FOLLOW_UP, Turn
from .tiers import normal_service_tiers, prices_for_tiers
from .usage import Usage, UsageLedger, cost_usage, parse_usage, resolve_prices, stored_price
from .verdict import finalize


class BudgetStop(Exception):
    pass


@dataclass
class Flight:
    handle: object
    session_id: str
    token: str
    side: str
    model: Model
    attempt: int


def controller_step_limit(turns):
    return 8 * turns + 8


def profile_tool_schemas():
    if _tool_registry is None:
        return []
    schemas = []
    for name in _tool_registry.get_all_tool_names():
        schema = _tool_registry.get_schema(name)
        if schema:
            schemas.append(schema)
    return schemas


def schema_tokens(schemas):
    if not schemas:
        return 0
    payload = json.dumps(list(schemas), ensure_ascii=False, separators=(',', ':')).encode()
    return math.ceil(len(payload) / 3)


def estimate(selection, current, trial, judge, tool_schemas=None, *, prices):
    models = (current, trial, *judge.models)
    model_prices = {}
    for model in models:
        price = stored_price(prices, model)
        model_prices[model] = replace(price, plan=price.plan or model.plan) if price is not None else None
    priced = all(price is not None for price in model_prices.values())
    overhead = schema_tokens(profile_tool_schemas() if tool_schemas is None else tool_schemas)
    contestants = 0.0
    judges = 0.0
    plan_tokens = 0
    plan_cost = 0.0
    total_tokens = 0
    for turn in selection.turns:
        base = math.ceil(len((turn.goal + turn.context).encode()) / 3) + 2048
        usage = Usage(base + overhead, 1024)
        # Reserve two requests per attempt, one retry, and both judge orders.
        for model in (trial, current):
            price = model_prices[model]
            plan = model.plan or bool(price and price.plan)
            total_tokens += 4 * usage.total_tokens
            if priced:
                contestants += 4 * cost_usage(price, usage)
                if plan:
                    plan_cost += 4 * cost_usage(price, usage, list_price=True)
            if plan:
                plan_tokens += 4 * usage.total_tokens
        judge_usage = Usage(base + 4096, 512)
        total_tokens += 2 * judge_usage.total_tokens * len(judge.models)
        for model in judge.models:
            price = model_prices[model]
            plan = model.plan or bool(price and price.plan)
            if priced:
                judges += 2 * cost_usage(price, judge_usage)
                if plan:
                    plan_cost += 2 * cost_usage(price, judge_usage, list_price=True)
            if plan:
                plan_tokens += 2 * judge_usage.total_tokens
    controller_calls = 3 * len(selection.turns) + 2
    controller_usage = Usage(1024 + overhead + 32 * controller_calls, 128)
    total_tokens += controller_calls * controller_usage.total_tokens
    current_price = model_prices[current]
    current_plan = current.plan or bool(current_price and current_price.plan)
    if current_plan:
        plan_tokens += controller_calls * controller_usage.total_tokens
    if not priced:
        return {'total': None, 'contestants': None, 'controller': None, 'judge': None,
                'plan_tokens': plan_tokens, 'plan_cost': None, 'tokens': total_tokens, 'priced': False}
    controller = controller_calls * cost_usage(current_price, controller_usage)
    if current_plan:
        plan_cost += controller_calls * cost_usage(current_price, controller_usage, list_price=True)
    return {'total': contestants + judges + controller, 'contestants': contestants,
            'controller': controller, 'judge': judges, 'plan_tokens': plan_tokens,
            'plan_cost': plan_cost, 'tokens': total_tokens, 'priced': True}


class TrialEngine:
    def __init__(self, ctx, store, guard):
        self.ctx, self.store, self.guard = ctx, store, guard
        self.record = None
        self.flights = {}
        self.ledger = UsageLedger()
        self.charged = {}
        self.tool_history = {}
        self.lock = threading.RLock()
        self.step_lock = threading.RLock()
        self.steps = 0
        self.wake = threading.Event()

    def prepare(self, selection, current, trial, catalog, *, cap=5, days=7, judge=None, origin=None, web_lookups=True, run_id=None, previous=None, service_tiers=()):
        if not math.isfinite(cap) or cap <= 0:
            raise ValueError('invalid budget')
        threshold = self.ctx.get_config('judge_swap_consistency_min', 70)
        if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 100:
            raise ValueError(copy('judge.threshold_invalid'))
        choice = choose_judge(current, trial, catalog, judge)
        prices = prices_for_tiers(resolve_prices((current, trial, *choice.models)), service_tiers)
        def with_billing(model):
            price = stored_price(prices, model)
            return replace(model, plan=model.plan or bool(price and price.plan))
        current, trial = with_billing(current), with_billing(trial)
        choice = replace(choice, models=tuple(with_billing(model) for model in choice.models))
        prediction = estimate(selection, current, trial, choice, prices=prices)
        record = {'id': run_id or uuid.uuid4().hex[:8], 'created_at': time.time(), 'origin': Scrubber().scrub(origin or {}),
                  'current': asdict(current), 'trial': asdict(trial), 'judge': choice.record(), 'judge_mode': choice.mode,
                  'judge_source': choice.source, 'judge_disagreements': 0, 'seed': selection.seed, 'days': days,
                  'judge_policy': deepcopy(previous['judge_policy']) if previous and previous.get('judge_policy') else {
                      'version': 2, 'swap_consistency_min': threshold, 'bootstrap_seed': selection.seed, 'bootstrap_samples': 10000},
                  'total': selection.total, 'qualified': selection.qualified, 'picked': len(selection.turns),
                  'left_out': selection.left_out, 'scrub_counts': selection.scrub_counts, 'cap': cap, 'spend': 0.0,
                  'estimate': prediction, 'prices': prices,
                  'pricing_blocker': None if normal_service_tiers(service_tiers) else 'service_tier',
                  'price_date': '2026-11-10', 'web_lookups': web_lookups,
                  'state': 'prepared', 'stop_reason': None, 'excess_inflight': 0.0, 'reruns': 0, 'varied': 0,
                  'controller_token': secrets.token_hex(12), 'usage_calls': [], 'request_usage': [],
                  'replay_child_session_ids': [],
                  'rate_limit_refusals': 0, 'retry_after': 0, 'routes': {}, 'route_conflicts': [],
                  'turns': [{**t.record(), 'first_line': t.goal.splitlines()[0][:80], 'state': 'waiting', 'runs': {'new': [], 'yours': []}, 'usable': False} for t in selection.turns]}
        if previous:
            if previous.get('second_look') or previous.get('second_look_id') or choice.mode != previous['judge_mode'] or choice.record()['models'] != previous['judge']['models']:
                raise ValueError('second look cannot change the judging method')
            completed = [t for t in previous['turns'] if t['state'] in {'done', 'unusable'}]
            record.update(parent_id=previous['id'], second_look=True, spend=previous['spend'],
                          cap=previous['spend'] + cap, turns=deepcopy(completed) + record['turns'],
                          picked=len(completed) + len(selection.turns), routes=deepcopy(previous.get('routes', {})),
                          route_conflicts=deepcopy(previous.get('route_conflicts', [])),
                          reruns=previous.get('reruns', 0), varied=previous.get('varied', 0),
                          web_cache=deepcopy(previous.get('web_cache', {})),
                          web_stopped_turn_ids=list(previous.get('web_stopped_turn_ids', [])),
                          usage_calls=deepcopy(previous['usage_calls']), request_usage=deepcopy(previous['request_usage']))
            record['judge_source'] = previous['judge_source']
            record['judge']['source'] = previous['judge_source']
            if prediction and prediction.get('total') is not None:
                record['estimate'] = {**prediction, 'total': prediction['total'] + previous['spend']}
        estimate_row = record['estimate']
        affordable = (estimate_row is not None and estimate_row.get('total') is not None
                      and estimate_row['total'] <= record['cap'])
        record['can_start'] = len(selection.turns) >= (30 if previous else 20) and affordable
        self.record = record
        self.save()
        return record

    def attach(self, record, session_id):
        self.record = record
        if 'prices' not in record:
            record.update(state='finished', can_start=False, stop_reason='unpriceable')
            finalize(record)
            self.save()
            raise ValueError('test has no frozen prices')
        if record['state'] not in {'prepared', 'running', 'interrupted'}:
            raise ValueError('test cannot run')
        if not record['can_start']:
            raise ValueError('test cannot start')
        self.guard.add_controller(session_id, exclusive=True)
        self.wake.clear()
        for row in record.get('request_usage', []):
            key = (row['session_id'], row['api_request_id'])
            self.ledger.observe(session_id=row['session_id'], api_request_id=row['api_request_id'], response_model=row['response_model'], provider=row['provider'], usage=row['usage'])
            if row['cost'] is not None:
                self.charged[key] = row['cost']
        self.record['controller_session_id'] = session_id
        self.record['state'] = 'running'
        for turn in self.record['turns']:
            if turn['state'] == 'running':
                turn['state'] = 'waiting'
        self.save()

    def save(self):
        with self.lock:
            if self.record is not None:
                self.record['excess_inflight'] = max(0.0, self.record['spend'] - self.record['cap'])
                self.store.save(self.record)

    @contextmanager
    def lifecycle_callback_window(self):
        self.lock.release()
        try:
            yield
        finally:
            self.lock.acquire()

    def at_cap(self):
        return self.record['spend'] >= self.record['cap']

    def before_judge(self):
        with self.lock:
            if self.cancel_requested():
                raise BudgetStop()
            if self.at_cap() or self.record['stop_reason']:
                self.record['stop_reason'] = self.record['stop_reason'] or 'cap'
                raise BudgetStop()

    def cancel_requested(self):
        saved = self.store.get(self.record['id'])
        return self.record['state'] == 'cancelled' or saved and saved['state'] == 'cancelled'

    def charge_judge(self, call):
        with self.lock:
            self.record['usage_calls'].append({'kind': 'judge', **call})
            if call['cost'] is None:
                self.record['stop_reason'] = 'unpriceable'
                raise BudgetStop()
            self.record['spend'] += call['cost']
            self.save()

    def post_api_request(self, *, session_id='', api_request_id='', response_model='', model='', provider='', usage=None, base_url=None, api_mode=None, **_):
        with self.lock:
            if self.record is None:
                return
            identity = self.guard.children.get(session_id)
            controller = session_id == self.record.get('controller_session_id')
            if identity is None and not controller:
                return
            if not api_request_id:
                return
            flight = self.guard.flights.get(identity.token) if identity else None
            expected = Model(**self.record['current']) if controller else flight.model if flight else None
            if expected is None:
                return
            response_model = response_model or model or ''
            if identity and base_url is not None and api_mode and response_model and canonical_slug(response_model) == canonical_slug(expected.model) and provider == expected.provider:
                endpoint = urlsplit(base_url)
                if not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment and (not base_url or endpoint.scheme in {'https', 'http'} and endpoint.hostname):
                    side = 'trial' if flight.side == 'new' else 'current'
                    route = {'model': expected.model, 'provider': provider, 'base_url': base_url, 'api_mode': api_mode}
                    previous = self.record['routes'].get(side)
                    if previous is not None and previous != route:
                        self.record['route_conflicts'].append(side)
                    self.record['routes'][side] = route
            turn = next((t for t in self.record['turns'] if identity and t['id'] == identity.turn_id), None)
            parsed = parse_usage(usage, input_text=turn['goal'] + turn['context'] if turn else '')
            self.ledger.observe(session_id=session_id, api_request_id=api_request_id, response_model=response_model, provider=provider, usage=parsed.record())
            route = Model(provider, response_model)
            amount = cost_usage(stored_price(self.record['prices'], route), parsed) if route.provider and route.model else None
            key = (session_id, api_request_id)
            if amount is None:
                self.record['stop_reason'] = 'unpriceable'
            else:
                self.record['spend'] += amount - self.charged.get(key, 0)
                self.charged[key] = amount
            item = {'session_id': session_id, 'api_request_id': api_request_id, 'response_model': response_model, 'provider': provider, 'cost': amount, 'usage': parsed.record()}
            self.record['request_usage'] = [r for r in self.record['request_usage'] if (r['session_id'], r['api_request_id']) != key] + [item]
            self.save()
            if self.at_cap() or self.record['stop_reason']:
                self.wake.set()

    def api_request_error(self, *, session_id='', status_code=None, **_):
        with self.lock:
            if self.record is None or self.record['state'] != 'running':
                return
            if session_id not in self.guard.children and session_id not in self.guard.controllers:
                return
            if str(status_code) == '429':
                self.record['rate_limit_refusals'] += 1
                self.record['retry_after'] = time.time() + 40
                if self.record['rate_limit_refusals'] >= 2:
                    self.record['stop_reason'] = 'rate_limited'
                    self.wake.set()
                self.save()

    def post_tool_call(self, *, session_id='', tool_call_id='', tool_name='', function_name='', args=None, function_args=None, status='', error_type='', **_):
        if session_id not in self.guard.children:
            return
        with self.lock:
            if self.guard.consume_veto(session_id, tool_call_id, tool_name or function_name):
                status = 'blocked'
            self.tool_history.setdefault(session_id, []).append(Scrubber().scrub({'name': tool_name or function_name, 'args': args or function_args or {}, 'status': status, 'broken': error_type in {'invalid_tool_arguments', 'invalid_json', 'schema_validation'}}))

    def subagent_stop(self, *, child_session_id='', tool_call_history=None, **_):
        if child_session_id not in self.guard.children:
            return
        if tool_call_history:
            with self.lock:
                observed = list(self.tool_history.get(child_session_id, []))
                calls = []
                for item in tool_call_history:
                    if not isinstance(item, dict):
                        continue
                    name = item.get('tool_name', item.get('tool', item.get('name', '')))
                    prior = next((c for c in observed if c['name'] == name), None)
                    if prior:
                        observed.remove(prior)
                    calls.append(prior or Scrubber().scrub({'name': name, 'args': item.get('args', item.get('tool_input', {})), 'status': item.get('status', ''), 'broken': item.get('error_type') == 'invalid_tool_arguments'}))
                self.tool_history[child_session_id] = calls + observed

    def _launch(self, turn, side):
        model = Model(**self.record['trial' if side == 'new' else 'current'])
        attempt = len(turn['runs'][side]) + 1
        token = secrets.token_hex(16)
        self.guard.expect(token, turn['cwd'], turn_id=turn['id'])
        flight = Flight(None, '', token, side, model, attempt)
        self.guard.flights[token] = flight
        payload = outbound({'goal': turn['goal'], 'context': turn['context']})
        unavailable = '\n' + copy('trial.tools_unavailable') if not self.record['web_lookups'] else ''
        request = {'goal': payload['goal'] + FOLLOW_UP + unavailable + '\n' + token, 'context': payload['context'], 'provider': model.provider, 'model': model.model,
                   'role': 'leaf', 'correlation_id': token}
        with self.lifecycle_callback_window():
            handle = self.ctx.subagent_lifecycle.launch(request)
        sid = next((sid for sid, identity in self.guard.children.items() if identity.token == token), None)
        if sid is None:
            with self.guard.lock:
                self.guard.pending.pop(token, None)
                self.guard.flights.pop(token, None)
            with self.lifecycle_callback_window():
                self.ctx.subagent_lifecycle.cancel(handle, reason='unidentified replay')
            usage = parse_usage(None, input_text=turn['goal'] + turn['context'])
            cost = None
            turn['runs'][side].append({'requested_model': model.model, 'provider': model.provider,
                                      'actual_models': [], 'end_state': 'unidentified', 'ok': False,
                                      'summary': '', 'where_stopped': None, 'usage': usage.record(),
                                      'cost': cost, 'duration_s': None, 'tool_calls': [],
                                      'broken_calls': 0, 'attempt': attempt})
            self.record['usage_calls'].append({'kind': 'replay', 'turn_id': turn['id'], 'side': side,
                                               'usage': usage.record(), 'cost': cost, 'models': []})
            self.record['stop_reason'] = 'unpriceable'
            self.save()
            return
        flight.handle, flight.session_id = handle, sid
        self.flights[side] = flight

    def _collect(self, turn, side, flight):
        stopped = self.guard.children[flight.session_id].stopped
        remaining = 15.0 if stopped else 120.0
        while remaining > 0:
            if self.record['state'] != 'running' or self.cancel_requested() or self.at_cap() or self.record['stop_reason']:
                return False
            duration = min(5.0, remaining)
            with self.lifecycle_callback_window():
                self.ctx.subagent_lifecycle.wait(flight.handle, timeout_seconds=duration)
                result = self.ctx.subagent_lifecycle.result(flight.handle)
            if self.record['state'] != 'running':
                return False
            if self.cancel_requested() or self.at_cap() or self.record['stop_reason']:
                return False
            remaining -= duration
            if result.ready:
                break
            if not stopped and self.guard.children[flight.session_id].stopped:
                stopped = True
                remaining = min(remaining, 15.0)
        if stopped and not result.ready:
            with self.lifecycle_callback_window():
                self.ctx.subagent_lifecycle.cancel(flight.handle, reason='stopped at action')
                self.ctx.subagent_lifecycle.wait(flight.handle, timeout_seconds=1.0)
                result = self.ctx.subagent_lifecycle.result(flight.handle)
            if self.cancel_requested() or self.at_cap() or self.record['stop_reason']:
                return False
        if not result.ready:
            return False
        clean = Scrubber().scrub(json.loads(json.dumps({'summary': result.summary or '',
                                                       'calls': self.tool_history.get(flight.session_id, [])}).replace(flight.token, '')))
        summary = clean['summary']
        usage, cost, actual = self.ledger.summarize(flight.session_id, self.record['prices'], flight.model, input_text=turn['goal'] + turn['context'], output_text=summary)
        state = getattr(result.terminal_state, 'value', result.terminal_state)
        identity = self.guard.children[flight.session_id]
        routes = [v for (sid, _), v in self.ledger.requests.items() if sid == flight.session_id]
        verified_route = bool(routes) and all(v['model'] and canonical_slug(v['model']) == canonical_slug(flight.model.model) and v['provider'] == flight.model.provider for v in routes)
        stopped = identity.stopped
        succeeded = (state == 'SUCCEEDED' or stopped) and verified_route
        calls = clean['calls']
        run = {'requested_model': flight.model.model, 'provider': flight.model.provider, 'actual_models': actual,
               'end_state': 'stopped_at_action' if stopped else state, 'ok': succeeded, 'summary': summary,
               'where_stopped': copy('stop.action') if stopped else None, 'usage': usage.record(), 'cost': cost,
               'duration_s': max(0, result.completed_at - result.started_at) if result.completed_at is not None and result.started_at is not None else None,
               'tool_calls': calls, 'broken_calls': sum(c.get('broken', False) for c in calls), 'attempt': flight.attempt}
        turn['runs'][side].append(run)
        self.record['usage_calls'].append({'kind': 'replay', 'turn_id': turn['id'], 'side': side, 'usage': usage.record(), 'cost': cost, 'models': actual})
        if cost is None:
            self.record['stop_reason'] = 'unpriceable'
        self.flights.pop(side, None)
        self.save()
        return True

    def _finish(self):
        if self.flights:
            self._stop_locked('finished', self.record['stop_reason'])
        else:
            self.guard.end_trial()
        self.record['state'] = 'finished'
        finalize(self.record)
        self.save()
        return {'continue': False, 'id': self.record['id'], 'state': 'finished'}

    def step(self, session_id):
        with self.step_lock, self.lock:
            if self.record is None or session_id not in self.guard.controllers or self.record.get('controller_session_id') != session_id:
                raise ValueError('unidentified controller')
            if self.record['state'] != 'running':
                return {'continue': False, 'state': self.record['state']}
            saved = self.store.get(self.record['id'])
            if saved and saved['state'] == 'cancelled':
                return self._stop_locked('cancelled', 'cancelled')
            if saved and saved['state'] == 'interrupted':
                return self._stop_locked('interrupted', saved['stop_reason'])
            if self.record.get('retry_after', 0) > time.time() and not self.record['stop_reason'] and not self.at_cap():
                remaining = min(40.0, self.record['retry_after'] - time.time())
                while remaining > 0 and not self.cancel_requested() and not self.at_cap() and not self.record['stop_reason']:
                    duration = min(5.0, remaining)
                    with self.lifecycle_callback_window():
                        self.wake.wait(duration)
                    remaining -= duration
                    if self.record['retry_after'] <= time.time():
                        break
                if self.cancel_requested():
                    return self._stop_locked('cancelled', 'cancelled')
                if self.at_cap() or self.record['stop_reason']:
                    self.record['stop_reason'] = self.record['stop_reason'] or 'cap'
                    return self._finish()
                return {'continue': True, 'state': 'waiting', 'retry_after': self.record['retry_after']}
            self.steps += 1
            if self.steps > controller_step_limit(len(self.record['turns'])):
                return self._stop_locked('interrupted', 'step_limit')
            turn = next((t for t in self.record['turns'] if t['state'] not in {'done', 'unusable'}), None)
            if turn is None:
                return self._finish()
            if turn['state'] == 'waiting':
                if self.at_cap() or self.record['stop_reason']:
                    self.record['stop_reason'] = self.record['stop_reason'] or 'cap'
                    return self._finish()
                turn['state'] = 'running'
                self.save()
            for side in ('new', 'yours'):
                if self.at_cap() or self.record['stop_reason']:
                    self.record['stop_reason'] = self.record['stop_reason'] or 'cap'
                    return self._finish()
                if side in self.flights:
                    self._collect(turn, side, self.flights[side])
                if self.cancel_requested():
                    return self._stop_locked('cancelled', 'cancelled')
                runs = turn['runs'][side]
                if side not in self.flights and (not runs or not runs[-1]['ok'] and len(runs) < 2):
                    if self.at_cap() or self.record['stop_reason']:
                        self.record['stop_reason'] = self.record['stop_reason'] or 'cap'
                        return self._finish()
                    else:
                        self._launch(turn, side)
                        if self.cancel_requested():
                            return self._stop_locked('cancelled', 'cancelled')
            if self.at_cap() or self.record['stop_reason']:
                self.record['stop_reason'] = self.record['stop_reason'] or 'cap'
                return self._finish()
            runs = [turn['runs'][side] for side in ('new', 'yours')]
            if self.flights or any(group and not group[-1]['ok'] and len(group) < 2 for group in runs):
                return {'continue': True, 'state': 'running'}
            if any(not group for group in runs):
                return self._finish()
            if all(not group[-1]['ok'] for group in runs):
                turn['state'] = 'unusable'
                turn['unusable_reason'] = 'both_failed'
            else:
                choice = JudgeChoice(self.record['judge_mode'], tuple(Model(**m) for m in self.record['judge']['models']), self.record['judge_source'])
                answers = [{'text': group[-1]['summary'], 'tool_calls': group[-1]['tool_calls'], 'stopped': group[-1]['where_stopped'], 'did_not_finish': not group[-1]['ok']} for group in runs]
                try:
                    with self.lifecycle_callback_window():
                        judgment = Judge(self.ctx.llm, choice, (Model(**self.record['trial']), Model(**self.record['current'])), prices=self.record['prices']).call(Turn(**{k: turn[k] for k in ('id','conversation_id','source','cwd','goal','context')}), answers, before_call=self.before_judge, after_call=self.charge_judge)
                except BudgetStop:
                    if self.cancel_requested():
                        return self._stop_locked('cancelled', 'cancelled')
                    return self._finish()
                except (ValueError, TypeError, KeyError):
                    turn['state'] = 'unusable'
                    turn['unusable_reason'] = 'judge_unusable'
                    judge_calls = [c for c in self.record['usage_calls'] if c['kind'] == 'judge']
                    if len(judge_calls) >= 2 and all(c.get('error') == 'request_failed' for c in judge_calls):
                        # The judge has never answered, after two tries; more replays would only spend on turns nobody can judge.
                        return self._finish()
                else:
                    if self.cancel_requested():
                        return self._stop_locked('cancelled', 'cancelled')
                    if not runs[0][-1]['ok']:
                        judgment['result'] = judgment['failure_result'] = 'lost'
                    elif not runs[1][-1]['ok']:
                        judgment['result'] = judgment['failure_result'] = 'won'
                    turn['judgment'] = judgment
                    turn['usable'] = True
                    turn['state'] = 'done'
            self.save()
            return {'continue': True, 'state': 'running'}

    def cancel(self):
        with self.lock:
            return self._stop_locked('cancelled', 'cancelled')

    def interrupt(self, reason):
        with self.lock:
            return self._stop_locked('interrupted', reason)

    def _stop_locked(self, state, reason):
        self.record['state'] = state
        if self.record.get('stop_reason') != 'unpriceable':
            self.record['stop_reason'] = reason
        self.wake.set()
        self.guard.end_trial()
        self.save()
        flights = list(self.flights.values())
        self.flights.clear()
        with self.lifecycle_callback_window():
            for flight in flights:
                self.ctx.subagent_lifecycle.cancel(flight.handle, reason=reason)
        return {'continue': False, 'state': state}
