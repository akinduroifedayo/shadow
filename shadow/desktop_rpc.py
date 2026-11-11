"""Shadow's Desktop page over plugin RPC: method names, response shapes, and the view builders.

The page (``desktop/plugin.js``) is layout only. Every string it shows, numbers included, is rendered
here from the saved records and the keyed copy table, so the page and the chat card read the same.

Methods (wire name ``plugin.shadow.<name>``, handler ``(params, *, session_id, **kwargs)``):

``page.state``    ``{test_id?}`` -> PageView. ``test_id`` picks an older test; default is the active
                  test, else the active switch, else the newest verdict or stopped test. Empty picker id selects this default.
``turn.open``     ``{test_id, turn_id}`` -> TurnView. Blind: no model names, judge call, judge reason or
                  original answer until the turn is revealed.
``turn.reveal``   ``{test_id, turn_id, pick: 1 | 2 | "same"}`` -> TurnView, revealed. The pick is
                  saved with the test; ``tally`` counts every revealed turn the user picked on.
``start.models``  ``{}`` -> ModelPicker: the user's own models, alphabetical, no marks.
``start.card``    ``{model, judge?}`` -> StartCard. ``judge`` is a slug from the card's judge list.
``settings.set``  ``{web_lookups: bool, test_id?}`` -> PageView.
``act``           Action parameters -> ``{page: PageView, notice: str | None, start_card?: StartCard}``.
                  Start submits its card consent. Switch actions submit the panel identity.
                  See desktop/RECORD_VIEWS.md. The page shows ``notice`` as a toast.

Session event (``ctx.emit_to_session``): ``plugin.shadow.page.changed`` with ``{mode, test_id, switch_id}``.
Registered methods return ``{error: {message, code?}}`` for rejected requests, using keyed Shadow copy.
Emit it through ``page_changed`` on every finished turn and every switch step, trip, undo or watch turn.
The page refetches ``page.state`` on the event, and polls every few seconds while a test or switch runs
in case it is not viewing the session that emitted.

Rich text is a list of segments: a plain string, ``{"b": text}`` bold, ``{"code": text}`` monospace,
``{"i": text}`` italic, ``{"k": text}`` muted.

PageView::

    {v: 1, mode: "empty" | "progress" | "stopped" | "result" | "switch" | "watch",
     test_id: str | None,                     # the test shown (progress, stopped or result)
     status: str | None,                      # status bar item; None when idle
     top: {title, sub: Rich, web: {on, label, word}, picker: {current, options: [{id, label}]} | None,
           try: {label, disabled, tip}},
     empty: {title, intro, steps: [str], buttons: [Button], web: {on, title, body}, idle} | None,
     progress: {heading, sub, frac, stop: Button, notes: [str], hidden: {title, paras: [str]}} | None,
     stopped: {summary, buttons: [Button]} | None, # cancelled without totals, no automatic verdict
     verdict: Verdict | None,
     turns: TurnList | None,
     switch: SwitchPanel | None,
     labels: {key: str}}                      # static UI words (LABELS), some with {n} for UI position

    Button = {id, label, primary}
    Verdict = {word, reason, second_look: str | None, caption, faint,
               receipt: [{key, label, value: Rich, sub: [str], accent_sub}],
               bar: {won, same, lost, label}, sure, facts: [str], buttons: [Button],
               quiet: Button | None, note: str | None,
               maths: {summary, rows: [{label, value: Rich, warn: str | None}], method}}
    TurnList = {mode: "result" | "progress", title, count, judge3_label | None,
                groups: [{key, label, folded, rows: [Row]}],
                judge3: {title, intro, note, rows: [Row]} | None}
    Row = {id, tag, kind, text, day, open}    # kind: lost | won | same | pick | waiting | running | judging | done | more
    SwitchPanel = {kind: "gradual" | "watch", paused, heading, sub, buttons: [Button],
                   identity: {switch_id, phase, step, epoch},
                   rail: {nodes: [{state: origin | done | now | todo, title, sub}], fill: float | None, segments},
                   trouble: {title, cols: [str], rows: [[str]], note},
                   meters: {title, rows: [{label, value, of, frac}], rule},
                   log: {title, rows: [{when, text}]}}

TurnView::

    {id, test_id, ask, meta, can_pick, revealed, pick,
     answers: [{n, title, hidden, name: Rich | None, steps: [Step], said_label, said, picked}],
     judge: str | None, tally: str | None, at_the_time: {label, text} | None, judge3: {done, of, tally} | None}
    Step = {kind: "ran", parts: Rich} | {kind: "stop", label, code: str | None, note}

StartCard::

    {title, id_line, rows: [{key, label, value: Rich}], judge: {change, close, header, options:
     [{slug, name, auto, who, cost, selected}], more: Rich | None, footer}, notice: str | None,
     blocked: str | None, buttons: [Button], model, judge_slug: str | None,
     judge_mode: "third_maker" | "backup_both", judge_models: [slug], estimate: float, consent: str}

    In backup mode ``judge`` is null and the Judge row names both contestants judging blind.
    Source start facts carry ``judge_mode`` and ``judge_models`` with per-model judging cost estimates.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import random
import re
import string
from dataclasses import asdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .common.controller_stop import controller_stop_text

PLUGIN_ID = 'shadow'
EVENT_CHANGED = 'page.changed'
ACTIONS = ('start', 'see', 'stop_test', 'get_result', 'delete_test', 'keep', 'switch', 'switch_all', 'run_more',
           'anyway', 'undo', 'move_on', 'resume', 'stop_switch', 'switch_again', 'stay', 'doctor')
MINUS = '−'
REASON_KINDS = ('missed_request', 'tool_details', 'wrong_facts', 'weaker_action', 'stopped_early', 'harder_to_use')
SHARES = {1: 'desktop.share.step1', 2: 'desktop.share.step2', 3: 'desktop.share.step3'}
GRADUAL_TRIP = {'trouble': 4, 'gap': 20, 'streak': 3}
WATCH_TRIP = {'trouble': 5, 'streak': 3, 'turns': 20}
TRIAL_MODES = {'prepared': None, 'queued': 'progress', 'running': 'progress',
               'interrupted': 'stopped', 'cancelled': 'stopped', 'finished': 'result'}
SWITCH_ACTIONS = frozenset({'undo', 'move_on', 'resume', 'stop_switch', 'switch_again', 'stay'})
ACTIVE_TRIAL_STATES = frozenset(k for k, mode in TRIAL_MODES.items() if mode == 'progress')
LABELS = ('desktop.sbs.answer', 'desktop.sbs.hidden', 'desktop.sbs.said', 'desktop.sbs.pick', 'desktop.sbs.picked',
          'desktop.sbs.reveal', 'desktop.sbs.same', 'desktop.sbs.hint', 'desktop.sbs.prev', 'desktop.sbs.next',
          'desktop.sbs.fold', 'desktop.sbs.folded', 'desktop.sbs.judge3_pos', 'desktop.judge3.title',
          'desktop.judge3.pos', 'desktop.judge3.back', 'desktop.group.show', 'desktop.group.hide',
          'desktop.models.title', 'desktop.models.empty', 'button.cancel', 'start.judge_change', 'start.judge_close',
          'desktop.sbs.open_hint', 'desktop.progress.open_hint', 'desktop.start.loading', 'trial.unavailable')


class DesktopError(ValueError):
    def __init__(self, key: str, *, code: str | None = None, **fields: Any):
        self.key = key
        self.code = code
        self.fields = fields
        super().__init__(key)


class Source(Protocol):
    """What the page reads and writes. ``FixtureSource`` serves fixtures; the merge adapter serves records.

    Tests are saved trial records. Switches are ``SwitchRecord.to_dict()`` records or
    fixture SwitchViews. View builders add presentation fields. See desktop/RECORD_VIEWS.md."""

    def tests(self) -> list[dict]: ...
    def test(self, test_id: str) -> dict | None: ...
    def active_switch(self) -> dict | None: ...
    def settings(self) -> dict: ...
    def set_web_lookups(self, on: bool) -> None: ...
    def save_pick(self, test_id: str, turn_id: str, pick: Any) -> None: ...
    def models(self) -> list[dict]: ...
    def start_facts(self, model: str) -> dict | None: ...
    def act(self, action: str, params: dict) -> dict: ...


def _loaded_as(*parts: str) -> set[str]:
    """Module names a relative import reports, under whatever Hermes called this plugin."""
    package = __package__ or ""
    return {f"{package}.{part}" if package else part for part in parts}


def display_name(model: str) -> str:
    """Switch arm labels use the switch line's short name, which follows the shared table."""
    try:
        from .switch.copy import short_name
    except ModuleNotFoundError as error:
        if error.name not in _loaded_as("switch", "switch.copy"):
            raise
        try:
            from .common.models import short_name
        except ModuleNotFoundError as nested:
            if nested.name not in _loaded_as("common", "common.models"):
                raise
            return model
    return short_name(model)


def model_view(model: dict) -> dict:
    if 'name' in model and 'provider_name' in model:
        return dict(model)
    return {**model, 'name': model.get('name') or display_name(model['model']),
            'provider_name': model.get('provider_name', model['provider'])}


def trial_view(record: dict) -> dict:
    """TrialView preserves saved measurements and adds display names."""
    return {**record, 'current': model_view(record['current']), 'trial': model_view(record['trial']),
            'judge': {**record['judge'], 'models': [model_view(m) for m in record['judge']['models']]},
            'turns': [dict(turn) for turn in record['turns']]}


def switch_view(raw: dict | None, test: dict | None) -> dict | None:
    """SwitchView derives measurements through the switch line's exported domain."""
    if raw is None:
        return None
    if 'counts' in raw:
        return raw if raw['phase'] in {'running', 'watch', 'paused'} else None
    from .switch.domain import SwitchRecord, is_active
    from .switch.copy import copy, fields
    from .switch.plugin import HISTORY_KINDS, panel_history
    record = SwitchRecord.from_dict(raw)
    if not is_active(record):
        return None
    new, old = model_view(raw['new']), model_view(raw['old'])
    totals = test.get('totals') if test else None
    history = []
    projected = panel_history(record)
    raw_public = [event for event in record.history if event.get('kind') in HISTORY_KINDS]
    for public, event in zip(projected, raw_public):
        kind = public['kind']
        row = {'at': public['at']}
        if kind in {'step2', 'step3'}:
            turns = public.get('turns')
            if type(turns) is not int or turns < 0:
                continue
            row.update(kind='step', share=int(kind[-1]), turns=turns)
            if public.get('manual') is True:
                row['manual'] = True
            history.append(row)
            continue
        if kind in {'step1', 'watch_start'}:
            if not totals:
                continue
            row.update(kind='start' if kind == 'step1' else 'watch_start', test=record.test_id,
                       **{key: totals[key] for key in ('word', 'won', 'same', 'lost')})
            if kind == 'watch_start':
                row.update(new=new['name'], provider=new['provider_name'],
                           old=old['name'], old_provider=old['provider_name'])
        elif kind == 'override':
            row.update(kind='override', headline=public['headline'])
        elif kind == 'resume':
            row.update(kind='resume')
        elif kind in {'rollback', 'watch_trip'}:
            row.update(kind='rollback', old=old['name'],
                       what=copy(f'switch.{kind}.{public["reason"]}', **fields(record, event)))
        else:
            continue
        history.append(row)
    trip_text = None
    if record.trips:
        kind = 'watch_trip' if record.mode == 'all_at_once' else 'rollback'
        trip = record.trips[-1]
        trip_text = copy(f'switch.{kind}.{trip["reason"]}', **fields(record, trip))
    last = record.last_response_model
    last_answered = next((m for m in (new, old) if m['model'] == last), None)
    if last and last_answered is None:
        last_answered = model_view({'model': last, 'provider': raw['new']['provider']})
    counts = {side: asdict(record.counts(side)) for side in ('new', 'old')}
    return {**raw, 'new': new, 'old': old, 'counts': counts,
            'step_progress': max(0, counts['new']['turns'] - record.step_start),
            'last_answered': last_answered,
            'started_at': record.history[0]['at'] if record.history else None,
            'test': {'id': record.test_id, 'word': test['totals']['word']} if test and test.get('totals') else None,
            'history': history, 'trip_text': trip_text, 'signals_note': copy('switch.signals_limit')}


def check_action_identity(source: Source, action: str, params: dict) -> None:
    if action in SWITCH_ACTIONS:
        switch = source.active_switch()
        expected = {'switch_id': switch['id'], 'phase': switch['phase'], 'step': switch['step'],
                    'epoch': switch.get('epoch', 0)} if switch else {}
        if not expected or switch['phase'] not in {'running', 'watch', 'paused'} or any(params.get(k) != v for k, v in expected.items()):
            raise ValueError('the switch has changed; refresh before acting')
    elif action == 'stop_test':
        test = source.test(params.get('test_id'))
        if test is None or test['state'] not in ACTIVE_TRIAL_STATES:
            raise ValueError('the test has changed; refresh before stopping')


# ---------- copy ----------

class Copy:
    def __init__(self, table: Mapping[str, str]):
        self.table = table

    def __call__(self, key: str, **fields: Any) -> str:
        return self.table[key].format(**fields)

    def rich(self, key: str, **fields: Any) -> list:
        """The template as segments: a field whose value is a segment dict stays a segment."""
        out: list = []
        for literal, name, spec, conv in string.Formatter().parse(self.table[key]):
            if literal:
                out.append(literal)
            if name is None:
                continue
            value = fields[name]
            out.append(value if isinstance(value, dict) else format(value, spec or ''))
        return _merge(out)


def _merge(parts: list) -> list:
    merged: list = []
    for part in parts:
        if isinstance(part, str) and merged and isinstance(merged[-1], str):
            merged[-1] += part
        elif part != '':
            merged.append(part)
    return merged


def b(text: Any) -> dict:
    return {'b': str(text)}


def _vs(c: Copy, new: str, yours: str) -> list:
    return [b(new), ' ', {'k': c('desktop.receipt.vs')}, ' ', b(yours)]


# ---------- numbers (design 2.4) ----------

def half_up(value: float, places: int = 1) -> Decimal:
    return Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)


def money_total(c: Copy, value: float) -> str:
    return c('desktop.under_cent') if 0 < value < 0.005 else f'${half_up(value, 2)}'


def money_turn(c: Copy, value: float) -> str:
    if 0 < value < 0.0005:
        return c('receipt.under_cent')
    return f'${half_up(value, 3 if value < 1 else 2)}'


def speed(value: float) -> str:
    if value < 59.95:
        return f'{half_up(value, 1)}s'
    whole = int(half_up(value, 0))
    return f'{whole // 60}m {whole % 60}s'


def signed_pct(value: int) -> str:
    return ('+' if value > 0 else MINUS if value < 0 else '') + f'{abs(value)}%'


def thousands(value: int) -> str:
    return f'{value:,}'


def sign_p_text(c: Copy, p: float, alpha: float) -> str:
    if p < 0.001:
        return c('desktop.maths.p_tiny')
    exponent = math.floor(math.log10(p)) - 1
    shown = math.ceil(round(p / 10 ** exponent, 9)) * 10 ** exponent
    if p < alpha <= shown:
        return c('desktop.maths.p_just_under', alpha=f'{alpha:g}')
    return f'{shown:.{max(0, -exponent)}f}'


def luck_text(c: Copy, p: float) -> str:
    if p >= 0.01:
        phrase = c('sure.luck_hundred', count=math.ceil(round(100 * p, 9)))
    elif p >= 0.001:
        phrase = c('sure.luck_thousand', count=math.ceil(round(1000 * p, 9)))
    else:
        phrase = c('sure.luck_small')
    return c('sure.luck', luck=phrase)


# ---------- dates ----------

def _when(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    when = datetime.fromisoformat(value)
    return when.astimezone() if when.tzinfo is not None else when


def day_short(value: Any) -> str:
    return _when(value).strftime('%a') if value is not None else ''


def date_short(value: Any) -> str:
    when = _when(value)
    return f'{when:%b} {when.day}'


def date_day(value: Any) -> str:
    when = _when(value)
    return f'{when:%a}, {when:%b} {when.day}'


def stamp(value: Any) -> str:
    when = _when(value)
    hour = when.hour % 12 or 12
    return f'{when:%b} {when.day}, {hour}:{when:%M} {"AM" if when.hour < 12 else "PM"}'


# ---------- page ----------

def build_page(source: Source, c: Copy, test_id: str | None = None) -> dict:
    tests = [trial_view(t) for t in source.tests() if TRIAL_MODES[t['state']] is not None]
    tests.sort(key=lambda t: _when(t['created_at']).timestamp(), reverse=True)
    raw_switch = source.active_switch()
    switch = switch_view(raw_switch, source.test(raw_switch['test_id']) if raw_switch else None)
    running = next((t for t in tests if t['state'] in ACTIVE_TRIAL_STATES), None)
    settings = source.settings()
    latest = next((t for t in tests if t.get('totals')), tests[0] if tests else None)
    test = next((t for t in tests if t['id'] == test_id), None) if test_id else running or (None if switch else latest)
    if test_id and test is None:
        raise DesktopError('trial.not_found', code='test_not_found')
    if test is not None:
        mode = TRIAL_MODES[test['state']]
        if mode == 'stopped' and test.get('totals'):
            mode = 'result'
    elif switch is not None:
        mode = 'watch' if switch['mode'] == 'all_at_once' else 'switch'
    else:
        mode = 'empty'
    page = {'v': 1, 'mode': mode, 'test_id': test['id'] if test else None, 'status': _status(c, running, switch),
            'top': _top(c, mode, test, switch, tests, settings, running),
            'empty': None, 'progress': None, 'stopped': None, 'verdict': None, 'turns': None, 'switch': None,
            'labels': {key: c.table[key] for key in LABELS}}
    if mode == 'empty':
        page['empty'] = _empty(c, settings)
    elif mode == 'progress':
        page['progress'] = _progress(c, test)
        page['turns'] = _progress_turns(c, test)
    elif mode == 'stopped':
        page['stopped'] = _stopped(c, test)
        finished = {**test, 'turns': [t for t in test['turns'] if t['state'] in {'done', 'unusable'}]}
        page['turns'] = _progress_turns(c, finished, show_done=len(finished['turns']))
    elif mode == 'result':
        page['verdict'] = _verdict(c, test)
        page['turns'] = _result_turns(c, test)
    else:
        page['switch'] = _watch(c, switch, source) if mode == 'watch' else _gradual(c, switch, source)
        page['switch']['identity'] = {key: switch[field] for key, field in
                                     (('switch_id', 'id'), ('phase', 'phase'), ('step', 'step'))}
        page['switch']['identity']['epoch'] = switch.get('epoch', 0)
    return page


def _status(c: Copy, running: dict | None, switch: dict | None) -> str | None:
    if running is not None:
        return c('desktop.status.test', done=_done(running), picked=running['picked'])
    if switch is None:
        return None
    new, old = switch['new']['name'], switch['old']['name']
    if switch['phase'] == 'paused':
        return c('desktop.status.paused', old=old)
    if switch['mode'] == 'all_at_once':
        return c('desktop.status.watch', new=new, done=switch['counts']['new']['turns'], need=WATCH_TRIP['turns'])
    return c('desktop.status.step', step=switch['step'], share=c(f'desktop.share_short.step{switch["step"]}'))


def _done(test: dict) -> int:
    return sum(t['state'] in {'done', 'unusable'} for t in test['turns'])


def _judge_phrase(c: Copy, test: dict) -> str:
    if test['judge_mode'] == 'backup_both':
        return c('desktop.top.judge_backup')
    name = test['judge']['models'][0]['name']
    return c('desktop.top.judge_user' if test['judge_source'] == 'user' else 'desktop.top.judge', judge=name)


def _top(c: Copy, mode: str, test: dict | None, switch: dict | None, tests: list[dict], settings: dict,
         running: dict | None) -> dict:
    if mode == 'empty':
        sub = [c('empty.tagline')]
    elif test is not None:
        sub = _merge(c.rich('desktop.top.models', trying=b(test['trial']['name']), current=b(test['current']['name']))
                     + [' · ' + _judge_phrase(c, test) + ' · ' + c('desktop.top.turns', picked=test['picked'], days=test['days'])])
    else:
        origin = switch.get('test')
        fields = {'test': switch['test_id'], 'word': c(f'verdict.{origin["word"]}') if origin else '',
                  'when': stamp(switch['started_at']) if switch.get('started_at') else ''}
        if not switch['test_id']:
            sub = [c('desktop.top.owner_switch', new=switch['new']['name'], provider=switch['new']['provider_name'], **fields)]
        elif switch['mode'] == 'all_at_once':
            sub = [c('desktop.top.watch_unverified' if switch['new']['provider'] == switch['old']['provider'] else 'desktop.top.watch', new=switch['new']['name'], provider=switch['new']['provider_name'], **fields)]
        else:
            sub = [c('desktop.top.switch', provider=switch['new']['provider_name'], **fields)]
    blocked = running or switch
    tip = (c('trial.still_running', id=running['id']) if running else c('trial.switch_running')) if blocked else None
    picker = None
    if tests:
        current = '' if mode in {'progress', 'switch', 'watch'} else test['id']
        activity = [{'id': '', 'label': _status(c, running, switch)}] if blocked else []
        options = []
        for row in tests:
            label = c('desktop.picker.option', date=stamp(row['created_at']), trying=row['trial']['name'])
            label += ' · ' + _judge_phrase(c, row)
            if row.get('totals'):
                label += ' · ' + c('verdict.' + row['totals']['word'])
            options.append({'id': row['id'], 'label': label + ' · ' + row['id']})
        picker = {'current': current, 'options': activity + options}
    on = bool(settings['web_lookups'])
    return {'title': c('desktop.title'), 'sub': sub,
            'web': {'on': on, 'label': c('desktop.web.label'), 'word': c('desktop.web.on' if on else 'desktop.web.off')},
            'picker': picker, 'try': {'label': c('desktop.try'), 'disabled': bool(blocked), 'tip': tip}}


def _empty(c: Copy, settings: dict) -> dict:
    on = bool(settings['web_lookups'])
    return {'title': c('trial.not_found'), 'intro': c('desktop.empty.intro'),
            'steps': [c('desktop.empty.step1'), c('desktop.empty.step2'), c('desktop.empty.step3')],
            'buttons': [_button(c, 'try', 'desktop.try', primary=True), _button(c, 'doctor', 'desktop.empty.doctor')],
            'web': {'on': on, 'title': c('desktop.empty.web_title', state=c('desktop.web.on' if on else 'desktop.web.off')),
                    'body': c('desktop.empty.web_body')},
            'idle': c('desktop.empty.idle')}


def _button(c: Copy, action: str, key: str, primary: bool = False, **fields: Any) -> dict:
    return {'id': action, 'label': c(key, **fields), 'primary': primary}


# ---------- progress ----------

def _progress(c: Copy, test: dict) -> dict:
    done, picked = _done(test), test['picked']
    sub = [_pricing_notice(c, test) if test.get('stop_reason') == 'unpriceable' else
           c('desktop.progress.spent', spend=money_total(c, test['spend']), cap=money_total(c, test['cap']))]
    live = test.get('live', {})
    if live.get('minutes_left') is not None:
        sub.append(c('desktop.progress.left', minutes=live['minutes_left']))
    sub.append(c('desktop.progress.keep_working' if live.get('own_session', True) else 'desktop.progress.busy'))
    notes = []
    if live.get('running'):
        r = live['running']
        whose = c('desktop.progress.for_yours') if r['side'] == 'yours' else test['trial']['name']
        notes.append(c('desktop.progress.running', count=r['count'], provider=r['provider'], whose=whose, max=r['max']))
    notes.append(c('desktop.progress.keeps', done=done))
    if live.get('slow'):
        notes.insert(0, c('desktop.progress.slow', provider=live['slow']['provider'], seconds=live['slow']['seconds']))
    return {'heading': c('desktop.progress.heading', done=done, picked=picked), 'sub': ' · '.join(sub),
            'frac': done / picked if picked else 0.0, 'stop': _button(c, 'stop_test', 'desktop.progress.stop'),
            'notes': notes,
            'hidden': {'title': c('desktop.progress.hidden_title', picked=picked),
                       'paras': [c('desktop.progress.hidden_1'), c('desktop.progress.hidden_2')]}}


def _stopped(c: Copy, test: dict) -> dict:
    usable = sum(t['state'] == 'done' and t.get('usable', False) for t in test['turns'])
    enough = usable >= 20
    key = 'trial.stopped_usable' if enough else 'trial.stopped'
    button = (_button(c, 'get_result', 'button.result', count=usable) if enough else
              _button(c, 'turns', 'button.turns', count=usable))
    summary = (c('trial.stopped_unpriceable', id=test['id'], done=usable, pricing=_pricing_notice(c, test))
               if test.get('stop_reason') == 'unpriceable' else
               c(key, id=test['id'], done=usable, spend=test['spend'], cap=test['cap']))
    if note := controller_stop_text(test, c):
        summary += '\n' + note
    return {'summary': summary,
            'buttons': [button, _button(c, 'delete_test', 'button.delete')]}


def _progress_turns(c: Copy, test: dict, show_done: int = 5, show_waiting: int = 1) -> dict:
    states = {'done': [], 'active': [], 'waiting': []}
    for turn in test['turns']:
        state = turn['state']
        bucket = 'done' if state in {'done', 'unusable'} else 'waiting' if state == 'waiting' else 'active'
        kind = 'done' if bucket == 'done' else state
        states[bucket].append({'id': turn['id'], 'tag': c(f'desktop.state.{kind}'), 'kind': kind,
                               'text': _shown_prompt(c, turn['first_line']), 'day': day_short(turn.get('at')), 'open': state == 'done'})
    rows = states['done'][:show_done]
    if len(states['done']) > show_done:
        rows.append(_more(c, 'desktop.turns.more_done', len(states['done']) - show_done))
    rows += states['active'] + states['waiting'][:show_waiting]
    if len(states['waiting']) > show_waiting:
        rows.append(_more(c, 'desktop.turns.more_waiting', len(states['waiting']) - show_waiting))
    return {'mode': 'progress', 'title': c('desktop.turns.title'),
            'count': c('desktop.turns.done_count', done=_done(test), picked=test['picked']), 'judge3_label': None,
            'groups': [{'key': 'all', 'label': None, 'folded': False, 'rows': rows}], 'judge3': None}


def _more(c: Copy, key: str, count: int) -> dict:
    return {'id': None, 'tag': '', 'kind': 'more', 'text': c(key, count=count), 'day': '', 'open': False}


# ---------- verdict ----------

def _verdict(c: Copy, test: dict) -> dict:
    from .trial.presentation import verdict_presentation
    from .trial.cards import switch_limit

    presentation = verdict_presentation(test, c)
    totals = test['totals']
    receipt = totals['receipt']
    word = totals['word']
    reason_code = totals['cant_call_reason']
    trying, current = test['trial']['name'], test['current']['name']
    same_provider = test['trial']['provider'] == test['current']['provider']
    readable = receipt['counts_readable']
    cheaper = (receipt['cost_per_turn_new'] is not None and receipt['cost_per_turn_yours'] is not None
               and receipt['cost_per_turn_new'] < receipt['cost_per_turn_yours'])
    cheaper_or_faster = cheaper or (
        receipt['speed_new_s'] is not None and receipt['speed_yours_s'] is not None
        and receipt['speed_new_s'] < receipt['speed_yours_s'])
    if word == 'not_enough':
        reason = c(f'verdict.reason.{reason_code}', **_reason_fields(test))
    elif word == 'holds_up' and not cheaper_or_faster:
        reason = c('verdict.reason.holds_neither', trying=trying, current=current)
    else:
        reason = c(f'verdict.reason.{word}', trying=trying, current=current)
    if note := controller_stop_text(test, c):
        reason += '\n' + note
    caption = c('receipt.caption', trying=trying, current=current)
    if readable == 'so_far':
        caption += c.table['receipt.so_far']
    elif readable == 'no':
        caption += c('receipt.unreadable', reason=c(f'desktop.unreadable.{reason_code}'))
    buttons, quiet, note = _verdict_buttons(c, test, word, reason_code, same_provider, cheaper_or_faster)
    limit = switch_limit(test)
    if limit:
        buttons = [button for button in buttons if button['id'] not in {'switch', 'switch_all'}]
        quiet, note = None, c(limit)
    return {**presentation, 'word': presentation['advice'], 'reason': reason,
            'second_look': (c('fine.panel_second' if test.get('judge_mode') == 'backup_both' else 'fine.second', pairs=totals['pairs'])
                            if totals.get('second_look') else None),
            'caption': caption, 'faint': readable != 'yes', 'receipt': _receipt(c, receipt, totals),
            'sure': _sure(c, test), 'facts': _facts(c, test), 'buttons': buttons, 'quiet': quiet, 'note': note,
            'maths': _maths(c, test)}


def _reason_fields(test: dict) -> dict:
    totals = test['totals']
    unusable = sum(t['state'] == 'unusable' for t in test['turns'])
    return {'cap': test['cap'], 'pairs': totals['pairs'], 'provider': test['current']['provider_name'],
            'picked': test['picked'], 'unusable': unusable, 'judge': test['judge']['models'][0]['name'],
            'agree': totals['judge_agree'], 'current': test['current']['name'], 'varied': totals['varied']}


def _verdict_buttons(c: Copy, test: dict, word: str, reason: str | None, same_provider: bool,
                     cheaper_or_faster: bool) -> tuple[list, dict | None, str | None]:
    # trial's package init imports the plugin, and a merged checkout's copy loader
    # imports fixture_copy from this module. Importing panel at module level would
    # cycle before fixture_copy exists.
    from .trial.panel import panel_eligible

    current = test['current']['name']
    keep = _button(c, 'keep', 'button.keep', current=current)
    switch = _button(c, 'switch', 'button.switch') if same_provider else _button(c, 'switch_all', 'button.switch_all')
    note = None if same_provider else c('desktop.verdict.other_provider', trying=test['trial']['name'],
                                        provider=test['trial']['provider_name'])
    second = test['totals'].get('second_look', False)
    more = [] if second or panel_eligible(test) is False else [_button(c, 'run_more', 'button.more')]
    if word == 'better' or word == 'holds_up' and cheaper_or_faster:
        return [{**switch, 'primary': True}, keep], None, note
    if word == 'holds_up':
        return [{**keep, 'primary': True}, switch], None, note
    override = _button(c, 'anyway', 'switch.override.confirm')
    if word == 'worse':
        return [{**keep, 'primary': True}], override, None
    if word == 'too_close':
        quiet = override
        return more + [keep], quiet, None
    if reason in {'cap', 'rate_limited', 'panel_uncertain'}:
        return more + [keep], override, None
    return [keep], override, None


def _receipt(c: Copy, receipt: dict, totals: dict) -> list[dict]:
    cells = [{'key': 'result', 'label': c('receipt.label.result'),
              'value': [b(totals['won']), ' · ', b(totals['same']), ' · ', b(totals['lost'])],
              'sub': c('receipt.sub.result', pairs=totals['pairs']).split('\n'), 'accent_sub': False}]
    new, yours, mode = receipt['cost_per_turn_new'], receipt['cost_per_turn_yours'], receipt['cost_mode']
    sub = []
    if mode in {'list_price', 'plan'}:
        new_cost, yours_cost = money_turn(c, new), money_turn(c, yours)
        if receipt.get('plan_new'):
            new_cost = c('receipt.plan', cost=new_cost)
        if receipt.get('plan_yours'):
            yours_cost = c('receipt.plan', cost=yours_cost)
        value = _vs(c, new_cost, yours_cost)
        if receipt['saved_pct'] is not None:
            sub = [signed_pct(-receipt['saved_pct'])]
        elif receipt['more_pct'] is not None:
            sub = [signed_pct(receipt['more_pct'])]
    elif mode == 'unknown_price' and totals['pairs']:
        per = lambda side: thousands(round(receipt[f'tokens_{side}'] / totals['pairs']))
        value = _vs(c, per('new'), per('yours')) + [' ' + c('desktop.receipt.tokens')]
    else:
        value = [c('receipt.no_cost')]
    cells.append({'key': 'cost', 'label': c('receipt.label.cost'), 'value': value, 'sub': sub, 'accent_sub': True})
    if receipt['speed_new_s'] is None or receipt['speed_yours_s'] is None:
        cells.append({'key': 'speed', 'label': c('receipt.label.speed'), 'value': [c('receipt.no_speed')], 'sub': [], 'accent_sub': False})
    else:
        cells.append({'key': 'speed', 'label': c('receipt.label.speed'),
                      'value': _vs(c, speed(receipt['speed_new_s']), speed(receipt['speed_yours_s'])),
                      'sub': [c('receipt.sub.speed')], 'accent_sub': False})
    cells.append({'key': 'steps', 'label': c('receipt.label.steps'),
                  'value': _vs(c, f'{half_up(receipt["steps_new"])}', f'{half_up(receipt["steps_yours"])}'),
                  'sub': [c('receipt.sub.steps')], 'accent_sub': False})
    cells.append({'key': 'broken', 'label': c('receipt.label.broken'),
                  'value': _vs(c, receipt['broken_new'], receipt['broken_yours']), 'sub': [], 'accent_sub': False})
    return cells


def _sure(c: Copy, test: dict) -> str:
    totals = test['totals']
    if test.get('judge_mode') == 'backup_both' and totals.get('win_rate') is not None:
        return c('sure.bootstrap', low=math.floor(100 * totals['win_rate_ci95_low']),
                 high=math.ceil(100 * totals['win_rate_ci95_high']))
    word = totals['word']
    worst = c('sure.worst', count=totals['worst_per_100']) if totals['worst_per_100'] > 0 else c('sure.even')
    if word in {'better', 'worse'}:
        return luck_text(c, totals['p'])
    if word == 'holds_up':
        return worst
    if word == 'too_close':
        return ' '.join([c(f'sure.lean_{totals["lean"]}'), worst, c('sure.holds_bar')])
    if totals['pairs'] >= 20 and totals.get('cant_call_reason'):
        return c('verdict.reason.' + totals['cant_call_reason'], **_reason_fields(test))
    return c('sure.few')


def _unpriced_names(*rows, facts):
    if any('priced' in row for row in rows):
        return ', '.join(dict.fromkeys(row.get('name') or row.get('model') for row in rows if row.get('priced') is False))
    return ', '.join(facts.get('missing_price_names') or [])


def _pricing_notice(c: Copy, test: dict) -> str:
    return c('start.service_tier' if test.get('pricing_blocker') == 'service_tier' else 'trial.unpriceable')


def _facts(c: Copy, test: dict) -> list[str]:
    totals = test['totals']
    facts = []
    if totals['word'] == 'too_close' and test.get('more_estimate') is not None and not totals.get('second_look'):
        facts.append(c('trial.more_cost', cost=test['more_estimate']))
    if totals['word'] == 'not_enough':
        facts.append(_pricing_notice(c, test) if test.get('stop_reason') == 'unpriceable' else
                     c('desktop.facts.spent', spend=money_total(c, test['spend']), cap=money_total(c, test['cap'])))
        if test.get('more_estimate') is not None:
            facts.append(c('desktop.facts.more_cap' if test['more_estimate'] > test['cap'] else 'desktop.facts.more',
                           cost=money_total(c, test['more_estimate'])))
    if totals['reruns']:
        facts.append(c('desktop.facts.scale', varied=totals['varied'], reruns=totals['reruns']))
    if test.get('judge_mode') == 'backup_both':
        facts.append(c('fine.contestant'))
    disagreements = test.get('judge_disagreements')
    if isinstance(disagreements, dict):
        if disagreements.get('low_panel_agreement'):
            facts.append(c('fine.low_panel_agreement'))
        if disagreements.get('kappa') is not None:
            facts.append(c('maths.panel_kappa', method=disagreements.get('kappa_method') or '',
                           kappa=f'{disagreements["kappa"]:.3f}'))
    lookups = test.get('web_turns', test.get('web_lookup_turns', 0))
    if test['web_lookups'] and lookups:
        facts.append(c('desktop.facts.web_on', count=lookups))
    elif not test['web_lookups'] and lookups:
        facts.append(c('fine.web_off', count=lookups))
    facts.append(c('desktop.facts.copies'))
    return facts


def _maths(c: Copy, test: dict) -> dict:
    t = test['totals']
    r = t['receipt']
    few = t['ci_too_wide']
    alpha = 0.025 if t.get('second_look') else 0.05
    agree_key = 'desktop.maths.agree_backup' if test['judge_mode'] == 'backup_both' else 'desktop.maths.agree_of'
    rows = [
        {'label': c('desktop.maths.net'), 'value': [b(signed_pct(t['net_pct'])), ' ', {'k': c('desktop.maths.net_of', won=t['won'], lost=t['lost'], pairs=t['pairs'])}], 'warn': None},
        {'label': c('desktop.maths.ci'), 'value': [c('desktop.maths.ci_range', low=signed_pct(t['ci95_low']), high=signed_pct(t['ci95_high']))],
         'warn': c('desktop.maths.too_wide') if few else None},
        {'label': c('desktop.maths.sign'), 'value': [c('desktop.maths.p', p=sign_p_text(c, t['p'], alpha)), ' ', {'k': c('desktop.maths.sign_of', won=t['won'], lost=t['lost'], same=t['same'])}],
         'warn': c('desktop.maths.not_used') if few else None},
        {'label': c('desktop.maths.holds'), 'value': [c('desktop.maths.low_end', low=signed_pct(t['holds_low'])), ' ', {'k': c('desktop.maths.holds_of', side='97.5%' if t.get('second_look') else '95%')}],
         'warn': c('desktop.maths.not_used') if few else None},
        {'label': c('desktop.maths.agree'), 'value': [c('desktop.maths.agree_value', agree=t['judge_agree'], pairs=t['judge_pairs']), ' ', {'k': c(agree_key)}], 'warn': None},
        {'label': c('desktop.maths.fixes'), 'value': [c('desktop.maths.fixes_value', new=r['fixes_new']), ' ', {'k': c('desktop.maths.fixes_yours', yours=r['fixes_yours'])}], 'warn': None},
    ]
    where = [_where(c, r['where_won'], t['won'], won=True), _where(c, r['where_lost'], t['lost'], won=False)]
    if t['word'] in {'worse', 'too_close'}:
        where.reverse()
    rows += [row for row in where if row]
    level = t.get('bootstrap_level') or ('97.5' if t.get('second_look') else '95')
    backup_panel = test.get('judge_mode') == 'backup_both' and isinstance(test.get('judge_disagreements'), dict)
    method = c('maths.panel_method', level=level) if backup_panel else c('maths.method')
    return {'summary': c('desktop.maths.summary'), 'rows': rows, 'method': method}


def _where(c: Copy, where: dict, total: int, *, won: bool) -> dict | None:
    if not where or not where.get('kind'):
        return None
    words = c(f'desktop.kind.{where["kind"]}')
    text = c('desktop.maths.yours_did', what=words) if won else words[0].upper() + words[1:]
    return {'label': c('desktop.maths.where_won' if won else 'desktop.maths.where_lost'),
            'value': [text, ' ', {'k': c('desktop.maths.where_of', count=where['count'], total=total)}], 'warn': None}


# ---------- turn list ----------

def _result_turns(c: Copy, test: dict) -> dict:
    groups = {'lost': [], 'won': [], 'same': []}
    for turn in test['turns']:
        if not turn.get('usable'):
            continue
        result = turn['judgment']['result']
        groups[result].append({'id': turn['id'], 'tag': c(f'desktop.tag.{result}'), 'kind': result,
                               'text': _shown_prompt(c, turn['first_line']), 'day': day_short(turn.get('at')), 'open': True})
    differed = len(groups['lost']) + len(groups['won'])
    pairs = test['totals']['pairs']
    picks = judge3_ids(test)
    by_id = {t['id']: t for t in test['turns']}
    judge3 = None
    if len(picks) == 3:
        judge3 = {'title': c('desktop.judge3.title'), 'intro': c('desktop.judge3.intro', differed=differed),
                  'note': c('desktop.judge3.note'),
                  'rows': [{'id': tid, 'tag': c('desktop.judge3.pick', n=i + 1), 'kind': 'pick',
                            'text': _shown_prompt(c, by_id[tid]['first_line']), 'day': day_short(by_id[tid].get('at')), 'open': True}
                           for i, tid in enumerate(picks)]}
    return {'mode': 'result', 'title': c('desktop.turns.differed'),
            'count': c('desktop.turns.differed_count', differed=differed, pairs=pairs),
            'judge3_label': c('desktop.judge3.title') if judge3 else None,
            'groups': [{'key': key, 'label': c(f'desktop.group.{key}', count=len(rows)), 'folded': key == 'same', 'rows': rows}
                       for key, rows in groups.items() if rows],
            'judge3': judge3}


def judge3_ids(test: dict) -> list[str]:
    blind = test.get('blind', {})
    if blind.get('judge3'):
        return list(blind['judge3'])
    differed = [t['id'] for t in test['turns'] if t.get('usable') and t['judgment']['result'] != 'same']
    if len(differed) < 3:
        return []
    return random.Random(f'{test.get("seed", test["id"])}:judge3').sample(differed, 3)


def answer1_side(test: dict, turn_id: str) -> str:
    sides = test.get('blind', {}).get('sides', {})
    if turn_id in sides:
        return sides[turn_id]
    return random.Random(f'{test.get("seed", test["id"])}:{turn_id}').choice(('new', 'yours'))


# ---------- side by side ----------

def _shown_prompt(c: Copy, text: str) -> str:
    from .trial.picking import FOLLOW_UP

    suffix = re.escape(FOLLOW_UP) + '(?:' + re.escape('\n' + c('trial.tools_unavailable')) + ')?\n[0-9a-f]{32}$'
    return re.sub(suffix, '', text)


def build_turn(c: Copy, test: dict, turn_id: str) -> dict:
    turn = next((t for t in test['turns'] if t['id'] == turn_id), None)
    if turn is None:
        raise ValueError(f'no turn {turn_id}')
    finished = test['state'] in {'finished', 'cancelled', 'interrupted'}
    if not turn['runs']['new'] or not turn['runs']['yours']:
        raise ValueError(f'turn {turn_id} has no answers yet')
    picks = test.get('blind', {}).get('picks', {})
    revealed = finished and turn_id in picks
    pick = picks.get(turn_id)
    first = answer1_side(test, turn_id)
    sides = (first, 'yours' if first == 'new' else 'new')
    answers = []
    for n, side in enumerate(sides, start=1):
        run = turn['runs'][side][-1]
        name = None
        if revealed:
            model = test['trial'] if side == 'new' else test['current']
            name = [b(model['name']), ', ', {'i': c('desktop.sbs.trying' if side == 'new' else 'desktop.sbs.yours')}]
        answers.append({'n': n, 'title': c('desktop.sbs.answer', n=n), 'hidden': c('desktop.sbs.hidden'), 'name': name,
                        'steps': [_step(c, call) for call in run.get('tool_calls', [])],
                        'said_label': c('desktop.sbs.said'), 'said': run.get('summary', ''), 'picked': pick == n})
    source = _source_name(c, turn.get('source'))
    meta = (c('desktop.sbs.meta', date=date_day(turn['at']), source=source) if turn.get('at') is not None
            else c('desktop.sbs.meta_source', source=source))
    view = {'id': turn_id, 'test_id': test['id'], 'ask': _shown_prompt(c, turn.get('goal', turn['first_line'])),
            'meta': meta,
            'can_pick': finished and turn.get('usable', False), 'revealed': revealed, 'pick': pick,
            'answers': answers, 'judge': None, 'tally': None, 'at_the_time': None, 'judge3': None}
    if revealed:
        judgment = turn['judgment']
        winner = {'won': 'new', 'lost': 'yours'}.get(judgment['result'])
        if winner is None:
            view['judge'] = c('desktop.sbs.judge_same', reason=judgment['reason'])
        else:
            view['judge'] = c('desktop.sbs.judge_pick', n=sides.index(winner) + 1, reason=judgment['reason'])
        if turn.get('original_answer'):
            view['at_the_time'] = {'label': c('desktop.sbs.fold'), 'text': turn['original_answer']}
    if finished:
        view['tally'] = _tally(c, test, list(picks))
        ids = judge3_ids(test)
        if turn_id in ids:
            done = [tid for tid in ids if tid in picks]
            view['judge3'] = {'done': len(done), 'of': len(ids),
                              'tally': _tally(c, test, done) if len(done) == len(ids) else None}
    return view


def _tally(c: Copy, test: dict, turn_ids: list[str]) -> str | None:
    picks = test.get('blind', {}).get('picks', {})
    by_id = {t['id']: t for t in test['turns']}
    judged = [tid for tid in turn_ids if picks.get(tid) is not None and by_id[tid].get('usable')]
    if not judged:
        return None
    agreed = sum(_user_result(test, tid, picks[tid]) == by_id[tid]['judgment']['result'] for tid in judged)
    return c('desktop.tally', agreed=agreed, picked=len(judged))


def _user_result(test: dict, turn_id: str, pick: Any) -> str:
    if pick == 'same':
        return 'same'
    first = answer1_side(test, turn_id)
    side = first if pick == 1 else ('yours' if first == 'new' else 'new')
    return 'won' if side == 'new' else 'lost'


def _source_name(c: Copy, source: str | None) -> str:
    key = f'desktop.source.{source}'
    return c(key) if key in c.table else (source or c('desktop.source.unknown'))


STEP_VERBS = {'read_file': 'desktop.step.read', 'search_files': 'desktop.step.searched',
              'web_search': 'desktop.step.web_search', 'web_extract': 'desktop.step.fetched'}
STOP_KINDS = {'terminal': 'run', 'send_message': 'send', 'write_file': 'write', 'patch': 'write'}


def _target(args: dict) -> str:
    for key in ('path', 'command', 'query', 'url', 'to'):
        if args.get(key):
            return str(args[key])
    return ''


def _step(c: Copy, call: dict) -> dict:
    from .trial.safety import REPLAY_TOOLS

    name, args = call.get('name', ''), call.get('args', {}) or {}
    if call.get('status') in {'not_run', 'blocked'} or name not in REPLAY_TOOLS:
        kind = STOP_KINDS.get(name, 'other')
        if kind == 'run':
            return {'kind': 'stop', 'label': c('desktop.stop.run'), 'code': str(args.get('command', '')), 'note': c('desktop.stop.not_run')}
        if kind == 'send':
            text = str(args.get('message', '')).splitlines()[0] if args.get('message') else ''
            return {'kind': 'stop', 'label': c('desktop.stop.send', what=text, to=args.get('to', '')), 'code': None, 'note': c('desktop.stop.not_run')}
        if kind == 'write':
            return {'kind': 'stop', 'label': c('desktop.stop.write', path=args.get('path', '')), 'code': None, 'note': c('desktop.stop.not_run')}
        return {'kind': 'stop', 'label': c('desktop.stop.other', tool=name), 'code': _target(args) or None, 'note': c('desktop.stop.not_run')}
    verb = STEP_VERBS.get(name)
    target = _target(args)
    lead = c(verb) if verb else c('desktop.step.used', tool=name)
    answered = c('desktop.step.answered_note', note=call['note']) if call.get('note') else c('desktop.step.answered')
    parts = [lead + ' '] + ([{'code': target}, ' '] if target else []) + [{'k': '· ' + answered}]
    if call.get('broken'):
        parts.append({'k': ' · ' + c('desktop.step.broken')})
    return {'kind': 'ran', 'parts': _merge(parts)}


# ---------- switch panels ----------

def _trouble_note(c: Copy, switch: dict) -> str:
    if 'signals_note' in switch:
        return switch['signals_note']
    signals = ', '.join(c(f'desktop.signal.{s}') for s in switch.get('signals', ()) if f'desktop.signal.{s}' in c.table)
    return c('desktop.switch.counted', signals=signals) if signals else ''


def _of(count: int, turns: int, c: Copy) -> str:
    return c('desktop.switch.of', count=count, turns=turns)


def _meter(c: Copy, label: str, value: int, line: int, of_key: str = 'desktop.meter.of') -> dict:
    return {'label': label, 'value': str(value), 'of': c(of_key, line=line), 'frac': min(1.0, value / line) if line else 0.0}


def _log(c: Copy, switch: dict) -> dict:
    rows = []
    for event in sorted(switch.get('history', []), key=lambda e: str(e['at']), reverse=True):
        if 'text' in event:
            rows.append({'when': stamp(event['at']), 'text': event['text']})
            continue
        if f'desktop.log.{event.get("kind")}' not in c.table:
            continue
        manual = event.get('manual') is True
        fields = {k: v for k, v in event.items() if k not in {'at', 'kind', 'manual'}}
        if 'share' in fields:
            fields['share'] = c(SHARES[fields['share']])
        if 'word' in fields:
            fields['word'] = c(f'verdict.{fields["word"]}')
        text = c(f'desktop.log.{event["kind"]}', **fields)
        if manual:
            text = f'{text} · {c("desktop.log.manual")}'
        rows.append({'when': stamp(event['at']), 'text': text})
    return {'title': c('desktop.switch.log_title'), 'rows': rows}


def _gradual(c: Copy, switch: dict, source: Source) -> dict:
    new, old = switch['new']['name'], switch['old']['name']
    step, need = switch['step'], switch['step_turns']
    counts = switch['counts']
    paused = switch['phase'] == 'paused'
    done = switch['step_progress']
    if paused:
        heading = c('desktop.switch.paused', old=old)
        sub = switch.get('trip_text') or (switch['trips'][-1].get('text') if switch.get('trips') else None) or c('desktop.switch.paused_sub')
        buttons = [_button(c, 'resume', 'desktop.switch.resume'), _button(c, 'stop_switch', 'desktop.switch.stop')]
    else:
        heading = c('desktop.switch.heading', step=step, new=new, share=c(SHARES[step]))
        sub = c(f'desktop.switch.progress{step}', done=done, need=need)
        if switch.get('last_answered'):
            sub += ' ' + c('desktop.switch.last', model=switch['last_answered']['name'])
        buttons = ([_button(c, 'move_on', f'desktop.switch.move{step}')] if step < 3 else []) + [_button(c, 'undo', 'desktop.switch.undo')]
    nodes = [{'state': 'origin', 'title': old, 'sub': c('desktop.rail.origin')}]
    for s in (1, 2, 3):
        state = 'todo' if paused else 'done' if s < step else 'now' if s == step else 'todo'
        sub_text = (c('desktop.rail.done', need=need) if state == 'done' else
                    c('desktop.rail.now', done=done, need=need) if state == 'now' else c('desktop.rail.todo', need=need))
        nodes.append({'state': state, 'title': c(f'desktop.rail.step{s}'), 'sub': sub_text})
    nodes.append({'state': 'todo', 'title': c('desktop.rail.default'), 'sub': c('desktop.rail.stops')})
    rail = {'nodes': nodes, 'segments': 3, 'fill': None if paused else (step - 1) + min(1.0, done / need)}
    new_t, old_t = counts['new'], counts['old']
    rate = lambda k: 100 * k['trouble'] / k['turns'] if k['turns'] else 0.0
    gap = max(0, int(half_up(rate(new_t) - rate(old_t), 0)))
    trip = GRADUAL_TRIP
    meters = {'title': c('desktop.switch.meters_title'),
              'rows': [_meter(c, c('desktop.meter.trouble'), new_t['trouble'], trip['trouble']),
                       _meter(c, c('desktop.meter.gap', old=old), gap, trip['gap'], 'desktop.meter.of_points'),
                       _meter(c, c('desktop.meter.streak'), switch['failure_streak'], trip['streak'])],
              'rule': c('desktop.switch.rule', new=new, old=old)}
    trouble = {'title': c('desktop.switch.trouble_title'), 'cols': [c('desktop.col.model'), c('desktop.col.turns'), c('desktop.col.trouble')],
               'rows': [[new, str(new_t['turns']), _of(new_t['trouble'], new_t['turns'], c)],
                        [old, str(old_t['turns']), _of(old_t['trouble'], old_t['turns'], c)]],
               'note': _trouble_note(c, switch)}
    return {'kind': 'gradual', 'paused': paused, 'heading': heading, 'sub': sub, 'buttons': buttons, 'rail': rail,
            'trouble': trouble, 'meters': meters, 'log': _log(c, switch)}


def _watch(c: Copy, switch: dict, source: Source) -> dict:
    new, old = switch['new']['name'], switch['old']['name']
    counts = switch['counts']['new']
    tripped = switch['phase'] == 'paused'
    need = WATCH_TRIP['turns']
    if tripped:
        heading = c('desktop.watch.tripped', old=old)
        sub = switch.get('trip_text') or (switch['trips'][-1].get('text', '') if switch.get('trips') else '')
        buttons = [_button(c, 'switch_again', 'desktop.watch.again'), _button(c, 'stay', 'desktop.watch.stay', old=old)]
    else:
        key = 'desktop.watch.heading_clean' if counts['trouble'] == 0 else 'desktop.watch.heading'
        heading = c(key, new=new, done=counts['turns'], need=need, trouble=counts['trouble'])
        sub = c('desktop.watch.unverified' if switch['new']['provider'] == switch['old']['provider'] else 'desktop.watch.sub', new=new, old=old)
        buttons = [_button(c, 'undo', 'desktop.switch.undo')]
    nodes = [{'state': 'origin', 'title': old, 'sub': c('desktop.rail.origin')},
             {'state': 'todo' if tripped else 'now', 'title': c('desktop.rail.watching'), 'sub': c('desktop.rail.now', done=counts['turns'], need=need)},
             {'state': 'todo', 'title': c('desktop.rail.watch_over'), 'sub': c('desktop.rail.stops')}]
    rail = {'nodes': nodes, 'segments': 1, 'fill': None if tripped else min(1.0, counts['turns'] / need)}
    signals = _trouble_note(c, switch)
    trouble = {'title': c('desktop.watch.trouble_title'), 'cols': [c('desktop.col.model'), c('desktop.col.turns'), c('desktop.col.trouble')],
               'rows': [[new, str(counts['turns']), _of(counts['trouble'], counts['turns'], c)]],
               'note': ' '.join(filter(None, [c('desktop.watch.no_compare', old=old), signals]))}
    meters = {'title': c('desktop.watch.meters_title'),
              'rows': [_meter(c, c('desktop.meter.trouble'), counts['trouble'], WATCH_TRIP['trouble']),
                       _meter(c, c('desktop.meter.streak'), switch['failure_streak'], WATCH_TRIP['streak'])],
              'rule': c('desktop.watch.rule')}
    return {'kind': 'watch', 'paused': tripped, 'heading': heading, 'sub': sub, 'buttons': buttons, 'rail': rail,
            'trouble': trouble, 'meters': meters, 'log': _log(c, switch)}


# ---------- start card ----------

def build_models(c: Copy, source: Source) -> dict:
    models = sorted(source.models(), key=lambda m: m['name'].lower())
    return {'title': c('desktop.models.title'),
            'models': [{'slug': m['model'], 'name': m['name'], 'provider': m['provider_name']} for m in models]}


def _pick_judge(judges: list, judge: str):
    picked = next((row for row in judges if row['model'] == judge), None)
    if picked is not None:
        return picked
    matches = [row for row in judges if row['model'].endswith('::' + judge)]
    return matches[0] if len(matches) == 1 else None


def _start_judging(facts: dict, judge: str | None) -> dict:
    mode = facts['judge_mode']
    if mode == 'backup_both':
        if judge:
            raise ValueError('backup judging cannot be picked')
        return {'mode': mode, 'models': facts['judge_models'], 'source': 'auto', 'unreachable': None}
    auto = next(j for j in facts['judges'] if j.get('auto'))
    picked = _pick_judge(facts['judges'], judge) if judge else auto
    if picked is None:
        raise ValueError(f'no judge {judge} in the list')
    unreachable = {'judge': picked['name'], 'auto': auto['name']} if picked.get('unreachable') else None
    chosen = auto if unreachable else picked
    return {'mode': mode, 'models': [chosen], 'source': 'auto' if chosen is auto else 'user',
            'unreachable': unreachable}


def build_start(c: Copy, source: Source, model: str, judge: str | None = None, *, refresh: bool = False) -> dict:
    facts = source.start_facts(model)
    if facts is None:
        raise DesktopError('trial.model_unknown')
    trying, current = facts['trial'], facts['current']
    judges = facts['judges']
    if refresh and judge and _pick_judge(judges, judge) is None:
        judge = None
    judging = _start_judging(facts, judge)
    backup = judging['mode'] == 'backup_both'
    user = judging['source'] == 'user'
    chosen = judging['models'][0]
    notice = c('judge.pick_unreachable', **judging['unreachable']) if judging['unreachable'] else None
    unpriced = facts.get('priced') is False or any(j['cost'] is None for j in judging['models'])
    if unpriced:
        judge_cost = None
        estimate = None
        cost_value = [c('start.tokens', tokens=facts['tokens'])]
    else:
        judge_cost = sum(j['cost'] for j in judging['models'])
        estimate = facts['replay_estimate'] + judge_cost
        cost_value = [c('start.cost', estimate=estimate, cap=facts['cap'], judge_cost=judge_cost)]
        if facts.get('plan_tokens') and facts['plan_cost'] is not None:
            cost_value += [' ', c('start.plan', tokens=facts['plan_tokens'], cost=facts['plan_cost'])]
    providers = [trying['provider_name'], current['provider_name']] + [j['provider_name'] for j in judging['models']]
    unique = list(dict.fromkeys(providers))
    sent = c('desktop.start.only', provider=unique[0]) if len(unique) == 1 else ', '.join(unique)
    left = ', '.join(c(f'desktop.left.{k}', count=v) if f'desktop.left.{k}' in c.table
                     else f'{v} ' + c(f'pick.reason.{k}') for k, v in facts['left_out'].items()) or '0'
    judge_value = ([c('start.judge_contestant')] if backup else
                   c.rich('start.judge_user' if user else 'start.judge_line', judge=b(chosen['name'])))
    rows = [
        {'key': 'turns', 'label': c('desktop.start.label.turns'),
         'value': [c('start.turns', picked=facts['picked'], total=facts['total'], days=facts['days']), ' ', {'k': c('start.left_out', reasons=left)}]},
        {'key': 'judge', 'label': c('desktop.start.label.judge'), 'value': judge_value},
        {'key': 'sent', 'label': c('desktop.start.label.sent'), 'value': [c('start.sent', providers=sent, keys=facts['scrub']['keys'], emails=facts['scrub']['emails'])]},
        {'key': 'runs', 'label': c('desktop.start.label.runs'), 'value': [c('start.runs_on' if facts['web_lookups'] else 'start.runs_off')]},
        {'key': 'cost', 'label': c('desktop.start.label.cost'), 'value': cost_value},
    ]
    if facts.get('isolation_verified', False):
        rows.append({'key': 'wait', 'label': c('desktop.start.label.wait'), 'value': [c('start.wait', minutes=facts['minutes'])]})
    shown = judges[:5]
    options = [{'slug': j['model'], 'name': j['name'], 'auto': c('judge.list.auto_label') if j.get('auto') else None,
                'who': c('desktop.start.who', maker=j['maker'], provider=j['provider_name']),
                'cost': (_pricing_notice(c, facts) if facts.get('pricing_blocker') == 'service_tier' or j.get('cost') is None else
                         c('judge.list.option_cost', cost=j['cost'])), 'selected': j is chosen}
               for j in shown if not j.get('unreachable') or j.get('auto')]
    more = c.rich('desktop.start.more', command={'code': c('desktop.start.more_command')}) if len(judges) > 5 else None
    blocked = None
    if not facts.get('isolation_verified', False):
        blocked = c('trial.isolation_required')
    elif facts['picked'] < 20:
        blocked = c('start.too_few', picked=facts['picked'], days=facts['days'], command='/shadow', model=model)
    elif unpriced:
        missing = _unpriced_names(trying, current, *judging['models'], facts=facts) or trying.get('name') or model
        blocked = _pricing_notice(c, facts) if facts.get('pricing_blocker') == 'service_tier' else c('start.no_price', model=missing)
    elif estimate > facts['cap']:
        blocked = c('start.over_cap', estimate=estimate, cap=facts['cap'],
                    minimum=facts.get('minimum', estimate * 20 / facts['picked']),
                    command='/shadow', model=model, higher=math.ceil(estimate))
    buttons = ([] if blocked else [_button(c, 'start', 'button.start', primary=True)]) + [
        _button(c, 'see', 'desktop.start.see', picked=facts['picked']), _button(c, 'cancel', 'button.cancel')]
    card = {'title': c('start.title', trying=trying['name'], current=current['name']), 'id_line': f'{trying["provider"]} / {trying["model"]}',
            'model': model, 'judge_slug': chosen['model'] if user else None, 'rows': rows,
            'judge_mode': judging['mode'], 'judge_models': [j['model'] for j in judging['models']],
            'judge': None if backup else {'change': c('start.judge_change'), 'close': c('start.judge_close'), 'header': c('judge.list.header'),
                      'options': options, 'more': more, 'footer': c('judge.list.hidden_note', maker=facts['hidden_maker'])},
            'notice': notice, 'blocked': blocked, 'buttons': buttons, 'estimate': estimate}
    card['consent'] = hashlib.sha256(json.dumps(card, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return card


def consent_matches(params: dict, card: dict) -> bool:
    return all(params.get(key) == card[key] for key in ('judge_mode', 'judge_models', 'estimate', 'consent'))


# ---------- RPC surface ----------

class DesktopRpc:
    def __init__(self, source: Source, copy_table: Mapping[str, str]):
        self.source = source
        self.c = Copy(copy_table)
        if isinstance(source, FixtureSource):
            source.copy_table = copy_table

    def methods(self) -> dict[str, Callable[..., Any]]:
        handlers = {'page.state': self.page_state, 'turn.open': self.turn_open, 'turn.reveal': self.turn_reveal,
                    'start.models': self.start_models, 'start.card': self.start_card, 'settings.set': self.settings_set,
                    'act': self.act}
        return {name: self._handler(name, handler) for name, handler in handlers.items()}

    def _handler(self, name: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        def call(params, **kwargs):
            try:
                return handler(params, **kwargs)
            except DesktopError as error:
                failure = {'message': self.c(error.key, **error.fields)}
                if error.code:
                    failure['code'] = error.code
                return {'error': failure}
            except ValueError as error:
                message = str(error)
                user_copy = message in self.c.table.values() or message.startswith(('The judge', 'No model called', 'Shadow needs'))
                return {'error': {'message': message if user_copy else self.c('trial.invalid')}}
            except Exception:
                logging.getLogger(__name__).exception('shadow desktop rpc %s failed', name)
                return {'error': {'message': self.c('trial.unavailable')}}
        return call

    def page_state(self, params: dict, *, session_id: str | None = None, **_: Any) -> dict:
        return build_page(self.source, self.c, _text(params.get('test_id')) or None)

    def turn_open(self, params: dict, *, session_id: str | None = None, **_: Any) -> dict:
        return build_turn(self.c, self._test(params), _text(params.get('turn_id')))

    def turn_reveal(self, params: dict, *, session_id: str | None = None, **_: Any) -> dict:
        test = self._test(params)
        turn_id = _text(params.get('turn_id'))
        pick = params.get('pick')
        if pick not in (1, 2, 'same') or isinstance(pick, bool):
            raise ValueError('pick must be 1, 2 or "same"')
        if test['state'] in ACTIVE_TRIAL_STATES:
            raise ValueError('turns are revealed only after the test finishes')
        view = build_turn(self.c, test, turn_id)
        if view['revealed']:
            return view
        if not view['can_pick']:
            raise ValueError('only usable finished turns can be picked')
        self.source.save_pick(test['id'], turn_id, pick)
        return build_turn(self.c, self._test(params), turn_id)

    def start_models(self, params: dict, *, session_id: str | None = None, **_: Any) -> dict:
        return build_models(self.c, self.source)

    def start_card(self, params: dict, *, session_id: str | None = None, **_: Any) -> dict:
        return build_start(self.c, self.source, _text(params.get('model')), _text(params.get('judge')) or None)

    def settings_set(self, params: dict, *, session_id: str | None = None, **_: Any) -> dict:
        if not isinstance(params.get('web_lookups'), bool):
            raise ValueError('web_lookups must be true or false')
        self.source.set_web_lookups(params['web_lookups'])
        return build_page(self.source, self.c, _text(params.get('test_id')) or None)

    def act(self, params: dict, *, session_id: str | None = None, **_: Any) -> dict:
        action = params.get('action')
        if action not in ACTIONS:
            raise ValueError(f'unknown action {action!r}')
        if action == 'stop_test' and not params.get('test_id'):
            return {'page': build_page(self.source, self.c), 'notice': None}
        try:
            check_action_identity(self.source, action, params)
        except ValueError:
            return {'page': build_page(self.source, self.c), 'notice': None}
        if action == 'start':
            card = build_start(self.c, self.source, _text(params.get('model')), _text(params.get('judge')) or None,
                               refresh=bool(params.get('consent')))
            if not consent_matches(params, card):
                return {'page': build_page(self.source, self.c, _text(params.get('test_id')) or None),
                        'notice': None, 'start_card': card}
        if action in {'switch', 'switch_all', 'anyway'}:
            test = self.source.test(params['test_id']) if params.get('test_id') else None
            if test and test.get('totals'):
                from .trial.cards import switch_limit
                limit = switch_limit(test)
                if limit:
                    raise DesktopError(limit)
            if test and test.get('totals') and test['totals']['word'] not in {'better', 'holds_up'} and params.get('confirm_override') is not True:
                from .trial.presentation import verdict_presentation
                return {'page': build_page(self.source, self.c, test['id']), 'notice': None,
                        'confirm_override': {'message': self.c('switch.override.warning', headline=verdict_presentation(trial_view(test), self.c)['headline']),
                                             'confirm': self.c('switch.override.confirm'), 'cancel': self.c('switch.override.cancel')}}
        arguments = {k: v for k, v in params.items() if k != 'action'}
        if action == 'start':
            arguments['origin'] = {'session_id': session_id, 'platform': 'desktop'}
            arguments['_copy_table'] = self.c.table
        result = self.source.act(action, arguments)
        reply = {'page': build_page(self.source, self.c, result.get('test_id')), 'notice': result.get('notice')}
        if result.get('confirm_override') is not None:
            reply['confirm_override'] = result['confirm_override']
        if result.get('start_card') is not None:
            reply['start_card'] = result['start_card']
        return reply

    def _test(self, params: dict) -> dict:
        test = self.source.test(_text(params.get('test_id')))
        if test is None:
            raise DesktopError('trial.not_found', code='test_not_found')
        return trial_view(test)


def _text(value: Any) -> str:
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError('expected a short string')
    return value


def register(ctx: Any, source: Source, copy_table: Mapping[str, str] | None = None) -> DesktopRpc:
    if copy_table is None:
        from .common.copy import load_copy
        copy_table = load_copy()
    rpc = DesktopRpc(source, copy_table)
    for name, handler in rpc.methods().items():
        ctx.register_rpc(name, handler)
    return rpc


def page_changed(ctx: Any, session_id: str | None, *, mode: str, test_id: str | None = None,
                 switch_id: str | None = None) -> bool:
    if not session_id:
        return False
    return bool(ctx.emit_to_session(session_id, EVENT_CHANGED, {'mode': mode, 'test_id': test_id, 'switch_id': switch_id}))


# ---------- fixtures ----------

FIXTURES = Path(__file__).resolve().parents[1] / 'desktop' / 'fixtures'


class FixtureSource:
    """A whole Shadow state from ``desktop/fixtures/<scenario>.json``, held in memory. Writes stay in memory."""

    def __init__(self, scenario: str | Path, *, finalize_fixture: Callable[[dict], dict] | None = None):
        path = Path(scenario) if str(scenario).endswith('.json') else FIXTURES / f'{scenario}.json'
        self.world = json.loads(path.read_text())
        self.finalize_fixture = finalize_fixture

    def tests(self) -> list[dict]:
        return sorted(self.world['tests'], key=lambda t: str(t['created_at']), reverse=True)

    def test(self, test_id: str) -> dict | None:
        return next((t for t in self.world['tests'] if t['id'] == test_id), None)

    def active_switch(self) -> dict | None:
        switch = self.world.get('switch')
        if switch is None:
            return None
        origin = self.test(switch['test_id'])
        if 'counts' not in switch:
            from .switch.domain import SwitchRecord, is_active
            return switch if is_active(SwitchRecord.from_dict(switch)) else None
        word = origin['totals']['word'] if origin and origin.get('totals') else switch.get('test_word', 'better')
        return switch_view({**switch, 'test': {'id': switch['test_id'], 'word': word}}, origin)

    def settings(self) -> dict:
        return self.world['settings']

    def set_web_lookups(self, on: bool) -> None:
        self.world['settings']['web_lookups'] = on

    def save_pick(self, test_id: str, turn_id: str, pick: Any) -> None:
        blind = self.test(test_id).setdefault('blind', {})
        blind.setdefault('picks', {}).setdefault(turn_id, pick)

    def models(self) -> list[dict]:
        return self.world.get('start', {}).get('models', [])

    def start_facts(self, model: str) -> dict | None:
        start = self.world.get('start')
        if not start:
            return None
        trial = next((m for m in start['models'] if m['model'] == model), None)
        if trial is None:
            return None
        facts = start['facts']
        excluded = {trial['maker'], start['current']['maker']}
        judges = [{**j, 'auto': False} for j in facts['judges'] if j['maker'] not in excluded]
        auto = next((j for j in judges if not j.get('unreachable')), None)
        mode = 'third_maker' if auto else 'backup_both'
        if auto:
            auto['auto'] = True
            judges.remove(auto)
            judges.insert(0, auto)
        else:
            judges = []
        models = ([auto] if auto else
                  [{**m, 'cost': facts['backup_judge_costs'][m['model']]} for m in (trial, start['current'])])
        estimate = facts['replay_estimate'] if model == start['try'] else facts['replay_estimates'][model]
        hidden = [facts['hidden_maker']]
        if trial['maker'] != start['current']['maker']:
            hidden.append(trial['maker'])
        return {'plan_cost': 0.0, **facts, 'trial': trial, 'current': start['current'], 'judges': judges,
                'judge_mode': mode, 'judge_models': models, 'replay_estimate': estimate,
                'isolation_verified': True,
                'hidden_maker': ' / '.join(hidden), 'web_lookups': self.world['settings']['web_lookups']}

    def act(self, action: str, params: dict) -> dict:
        notices = self.world.get('notices', {})
        check_action_identity(self, action, params)
        if action == 'start':
            facts = self.start_facts(params.get('model'))
            if facts is None:
                raise ValueError('no such model')
            card = build_start(Copy(self.copy_table),
                               self, params['model'], params.get('judge'), refresh=bool(params.get('consent')))
            if not consent_matches(params, card):
                return {'start_card': card, 'test_id': params.get('test_id')}
            judging = _start_judging(facts, params.get('judge'))
            estimate = facts['replay_estimate'] + sum(j['cost'] for j in judging['models'])
            if facts['picked'] < 20 or estimate > facts['cap']:
                raise ValueError('start needs a valid estimate and at least 20 turns')
            if self.active_switch() or any(t['state'] in ACTIVE_TRIAL_STATES for t in self.tests()):
                raise ValueError('a test or switch is already running')
            test_id = next(f'fixture-{i}' for i in range(1, len(self.tests()) + 2) if self.test(f'fixture-{i}') is None)
            test = {**facts, 'id': test_id, 'created_at': datetime.now().isoformat(),
                    'judge': {'mode': judging['mode'], 'models': judging['models']},
                    'judge_mode': judging['mode'], 'judge_source': judging['source'], 'judge_disagreements': 0,
                    'state': 'running', 'spend': 0.0, 'turns': [], 'totals': None}
            self.world['tests'].append(test)
            return {'test_id': test['id']}
        if action in {'switch', 'switch_all', 'anyway'}:
            from .switch import Engine, Proposal, Route, Store
            from .trial.presentation import verdict_presentation
            test = self.test(params.get('test_id'))
            if test is None:
                raise ValueError('no such test')
            if self.active_switch() or any(t['state'] in ACTIVE_TRIAL_STATES for t in self.tests()):
                raise ValueError('a test or switch is already running')
            routes = [Route(**test['routes'][side])
                      for side in ('current', 'trial')]
            import sqlite3
            engine = Engine(Store(connection=sqlite3.connect(':memory:')), verified_pair=lambda *_: True, set_default=lambda _: None)
            record = engine.start(Proposal(test['id'], *routes, test['totals']['word'],
                                           verdict_presentation(trial_view(test))['headline']),
                                  confirm_override=params.get('confirm_override') is True)
            self.world['switch'] = record.to_dict()
            return {'notice': notices.get(action)}
        if action == 'undo' or action == 'stop_switch' or action == 'stay':
            self.world['switch'] = None
        elif action == 'move_on':
            self.world['switch']['step'] += 1
        elif action in {'resume', 'switch_again'} and self.world.get('switch'):
            switch = self.world['switch']
            switch.update(phase='watch' if switch['mode'] == 'all_at_once' else 'running',
                          step=1, step_start=0, failure_streak=0,
                          epoch=switch.get('epoch', 0) + 1, resumes=switch.get('resumes', 0) + 1)
            if 'counts' in switch:
                switch['step_progress'] = 0
                switch['counts'] = {side: {'turns': 0, 'trouble': 0} for side in ('new', 'old')}
            else:
                switch['turns'].clear()
        elif action == 'stop_test':
            self.test(params['test_id'])['state'] = 'cancelled'
        elif action in {'get_result', 'delete_test'}:
            test = self.test(params.get('test_id'))
            if test is None or test['state'] in ACTIVE_TRIAL_STATES:
                raise ValueError('no stopped test')
            if action == 'delete_test':
                self.world['tests'].remove(test)
            else:
                if test['state'] not in {'cancelled', 'interrupted'}:
                    raise ValueError('no stopped test')
                if sum(t['state'] == 'done' and t.get('usable', False) for t in test['turns']) < 20:
                    raise ValueError('at least 20 usable finished turns are required')
                if self.finalize_fixture is not None:
                    finalize = self.finalize_fixture
                else:
                    try:
                        from .trial.verdict import finalize
                    except ModuleNotFoundError as error:
                        if error.name not in _loaded_as("trial", "trial.verdict"):
                            raise
                        raise ValueError('the trial finalizer is unavailable') from error
                    _fill_missing_run_usage(test)
                finalize(test)
                return {'test_id': test['id']}
        return {'notice': notices.get(action), 'test_id': params.get('test_id') if action in {'keep'} else None}


def _fill_missing_run_usage(test: dict) -> None:
    """Progress fixtures omit usage. The trial finalizer reads it; do not let that KeyError escape."""
    for turn in test.get('turns') or []:
        for side in ('new', 'yours'):
            for run in (turn.get('runs') or {}).get(side) or []:
                if isinstance(run, dict):
                    run.setdefault('usage', {'total_tokens': 0, 'estimated': True})


def fixture_copy(extra_dirs: tuple[Path, ...] = ()) -> dict[str, str]:
    """Merge live copy tables, using Desktop snapshot files only when their live table is absent.

    Duplicate keys between live owners still fail, as in the shared loader.
    """
    merged: dict[str, str] = {}
    dirs = (Path(__file__).resolve().parent / 'copy',) + tuple(extra_dirs)
    snapshot = FIXTURES.parents[1] / 'tests' / 'fixtures' / 'desktop' / 'copy'
    live = [path for directory in dirs if directory.resolve() != snapshot.resolve()
            for path in sorted(directory.glob('*.json'))]
    live_names = {path.name for path in live}
    fallback = [path for directory in dirs if directory.resolve() == snapshot.resolve()
                for path in sorted(directory.glob('*.json')) if path.name not in live_names]
    for path in live + fallback:
        for key, value in json.loads(path.read_text()).items():
            if key in merged:
                raise ValueError(f'duplicate copy key: {key}')
            merged[key] = value
    return merged


def dump_states(out_dir: Path, copy_table: Mapping[str, str]) -> list[Path]:
    """Every fixture scenario as the RPC answers it, for the headless harness: page plus opened turns
    plus start cards, one JSON file per scenario."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for path in sorted(FIXTURES.glob('*.json')):
        source = FixtureSource(path)
        rpc = DesktopRpc(source, copy_table)
        page = rpc.page_state({})
        bundle: dict[str, Any] = {'page': page, 'turns': {}, 'revealed': {}, 'start': {}, 'models': None}
        tests = source.tests()
        test = next((t for t in tests if t['state'] == 'running'), tests[0] if tests else None)
        for tid in _openable(page):
            bundle['turns'][tid] = rpc.turn_open({'test_id': test['id'], 'turn_id': tid})
        for tid, pick in (source.world.get('harness', {}).get('reveal') or {}).items():
            bundle['revealed'][tid] = DesktopRpc(FixtureSource(path), copy_table).turn_reveal({'test_id': test['id'], 'turn_id': tid, 'pick': pick})
        if source.world.get('start'):
            bundle['models'] = rpc.start_models({})
            for judge in [None] + [j['model'] for j in source.world['start']['facts']['judges']]:
                bundle['start'][judge or ''] = rpc.start_card({'model': source.world['start']['try'], 'judge': judge})
        target = out_dir / f'{path.stem}.json'
        target.write_text(json.dumps(bundle, ensure_ascii=False, indent=1))
        written.append(target)
    return written


def _openable(page: dict) -> list[str]:
    turns = page.get('turns')
    if not turns:
        return []
    rows = [r for g in turns['groups'] for r in g['rows']] + (turns['judge3']['rows'] if turns['judge3'] else [])
    return list(dict.fromkeys(r['id'] for r in rows if r['open']))


if __name__ == '__main__':
    import sys
    extra = tuple(Path(p) for p in sys.argv[2:])
    for written in dump_states(Path(sys.argv[1]), fixture_copy(extra)):
        print(written)
