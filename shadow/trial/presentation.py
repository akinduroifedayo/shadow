"""Saved verdict measurements shared by terminal and Desktop renderers."""
import math

from ..common.copy import copy
from ..common.models import Model
from .panel import panel_eligible
from .verdict import format_rate_pct, shown_pct


_CLOSE = ('verdict.advice.close', None)
_SAFE = ('verdict.advice.safe', None)
_KEEP = ('verdict.advice.keep', None)
_AHEAD_NEW = ('verdict.advice.ahead', 'trial')
_AHEAD_CURRENT = ('verdict.advice.ahead', 'current')
_EVEN = ('verdict.advice.even', None)
_SAVES = ('verdict.advice.saves', None)
_LESS_NEW = ('verdict.advice.less', 'trial')
_LESS_CURRENT = ('verdict.advice.less', 'current')
_COST_BANDS = ('less', 'same_or_lower', 'more', 'small_premium', 'unknown')
ADVICE_TAGS = {
    ('switch', 'better'): {
        'less': _SAFE, 'same_or_lower': _SAFE, 'more': _AHEAD_NEW, 'small_premium': _SAFE, 'unknown': _AHEAD_NEW},
    ('switch', 'holds_up'): {
        'less': _SAVES, 'same_or_lower': _SAFE, 'more': _EVEN, 'small_premium': _SAFE, 'unknown': _EVEN},
    ('switch', 'worse'): dict.fromkeys(_COST_BANDS, _KEEP),
    ('switch', 'too_close'): dict.fromkeys(_COST_BANDS, _CLOSE),
    ('switch', 'not_enough'): dict.fromkeys(_COST_BANDS, _CLOSE),
    ('compare', 'better'): dict.fromkeys(_COST_BANDS, _AHEAD_NEW),
    ('compare', 'worse'): dict.fromkeys(_COST_BANDS, _AHEAD_CURRENT),
    ('compare', 'holds_up'): {
        'less': _LESS_NEW, 'same_or_lower': _EVEN, 'more': _LESS_CURRENT, 'small_premium': _LESS_CURRENT, 'unknown': _EVEN},
    ('compare', 'too_close'): dict.fromkeys(_COST_BANDS, _CLOSE),
    ('compare', 'not_enough'): dict.fromkeys(_COST_BANDS, _CLOSE),
}


def cost_comparison(receipt):
    new, current = receipt['cost_per_turn_new'], receipt['cost_per_turn_yours']
    if receipt['cost_mode'] in {'unknown_price', 'not_reported'} or new is None or current is None:
        return 'unknown'
    if receipt.get('plan_yours') and not receipt.get('plan_new'):
        return 'unknown'
    if receipt['cost_mode'] == 'plan' or receipt.get('plan_new'):
        return 'same_or_lower'
    if new < current:
        return 'less'
    if new == current:
        return 'same_or_lower'
    return 'small_premium' if receipt['more_pct'] is not None and receipt['more_pct'] <= 1 else 'more'


def verdict_tag(intent, outcome, comparison, trial, current, c=copy):
    key, side = ADVICE_TAGS[intent, outcome][comparison]
    model = {'trial': trial, 'current': current}.get(side)
    return c(key, model=model)


def verdict_presentation(record, c=copy):
    from .cards import money, speed

    totals = record['totals']
    receipt = totals['receipt']
    ties = c('verdict.headline.ties', same=totals['same']) if totals['same'] else ''
    headline = c('verdict.headline.counts', ties=ties, **totals)
    priced = receipt['cost_mode'] not in {'unknown_price', 'not_reported'} and all(
        receipt.get('cost_per_turn_' + side) is not None for side in ('new', 'yours'))
    if priced:
        if receipt.get('saved_pct') is not None and receipt['saved_pct'] > 0:
            headline += c('verdict.headline.cheaper', pct=receipt['saved_pct'])
        elif receipt.get('more_pct') is not None and receipt['more_pct'] > 0:
            headline += c('verdict.headline.more', pct=receipt['more_pct'])
    rate = totals.get('win_rate')
    low, high = totals.get('win_rate_ci95_low'), totals.get('win_rate_ci95_high')
    level = totals.get('bootstrap_level', '95')
    lower = upper = None
    if rate is None and totals['pairs']:
        rate = (totals['won'] + .5 * totals['same']) / totals['pairs']
        keep = totals.get('word') in {'better', 'worse'}
        if 'interval_low' in totals and 'interval_high' in totals:
            lower = max(0, 50 + shown_pct(totals['interval_low'] / 2, upper=False, keep_off_zero=keep))
            upper = min(100, 50 + shown_pct(totals['interval_high'] / 2, upper=True, keep_off_zero=keep))
        elif 'ci95_low' in totals and 'ci95_high' in totals:
            low, high = (totals['ci95_low'] + 100) / 200, (totals['ci95_high'] + 100) / 200
        level = '97.5' if totals.get('second_look') else '95'
    bar = None
    if rate is not None and (lower is not None or (low is not None and high is not None)):
        point = round(100 * rate)
        if lower is None:
            lower, upper = max(0, math.floor(100 * low)), min(100, math.ceil(100 * high))
        bar = {'point': point, 'low': lower, 'high': upper, 'midpoint': c('verdict.rate.midpoint'),
               **{key: totals[key] for key in ('won', 'same', 'lost')},
               'label': c('verdict.rate.label', point=point, low=format_rate_pct(lower), high=format_rate_pct(upper), level=level)}
    columns = [c('verdict.table.model')]
    if rate is not None:
        columns.append(c('verdict.table.rate'))
    if priced:
        columns.append(c('receipt.label.cost'))
    timed = all(receipt.get('speed_' + side + '_s') is not None for side in ('new', 'yours'))
    if timed:
        columns.append(c('verdict.table.speed'))
    rows = []
    for side, model, model_rate in [('new', record['trial'], rate), ('yours', record['current'], 1 - rate if rate is not None else None)]:
        row = [model.get('name') or Model(model['provider'], model['model']).name]
        if rate is not None:
            row.append(f'{round(100 * model_rate)}%')
        if priced:
            cost = money(receipt['cost_per_turn_' + side])
            row.append(c('receipt.plan', cost=cost) if receipt.get('plan_' + side) else cost)
        if timed:
            row.append(speed(receipt['speed_' + side + '_s']))
        rows.append(row)
    return {'headline': headline,
            'advice': verdict_tag(record['intent'], totals['word'], cost_comparison(receipt), rows[0][0], rows[1][0], c),
            'advice_hint': (c('verdict.advice.close_hint') if totals['word'] == 'too_close'
                            and not totals['second_look'] and panel_eligible(record) is not False else None),
            'bar': bar, 'table': {'columns': columns, 'rows': rows}}


def terminal_rate_bar(bar, width=41):
    cells = ['░'] * width
    position = lambda pct: round(pct * (width - 1) / 100)
    for index in range(position(bar['low']), position(bar['high']) + 1):
        cells[index] = '▒'
    midpoint, point = position(50), position(bar['point'])
    cells[midpoint] = '│'
    cells[point] = '╋' if point == midpoint else '█'
    return ''.join(cells) + '\n' + copy('verdict.rate.axis')


def terminal_model_table(table):
    rows = [table['columns'], *table['rows']]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return '\n'.join('  '.join(value.ljust(width) for value, width in zip(row, widths)).rstrip() for row in rows)
