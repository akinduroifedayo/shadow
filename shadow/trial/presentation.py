"""Saved verdict measurements shared by terminal and Desktop renderers."""
import math

from ..common.copy import copy
from ..common.models import Model


ADVICE_KEYS = {
    'better': 'verdict.advice.switch',
    'worse': 'verdict.advice.keep',
    'holds_up': 'verdict.advice.safe',
    'too_close': 'verdict.advice.close',
    'not_enough': 'verdict.advice.close',
}


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
    if rate is None and totals['pairs']:
        rate = (totals['won'] + .5 * totals['same']) / totals['pairs']
        # The legacy matched-pair interval is stored on the net rate scale.
        if 'ci95_low' in totals and 'ci95_high' in totals:
            low, high = (totals['ci95_low'] + 100) / 200, (totals['ci95_high'] + 100) / 200
        level = '95'
    bar = None
    if rate is not None and low is not None and high is not None:
        point = round(100 * rate)
        lower, upper = max(0, math.floor(100 * low)), min(100, math.ceil(100 * high))
        bar = {'point': point, 'low': lower, 'high': upper, 'midpoint': c('verdict.rate.midpoint'),
               **{key: totals[key] for key in ('won', 'same', 'lost')},
               'label': c('verdict.rate.label', point=point, low=lower, high=upper, level=level)}
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
    return {'headline': headline, 'advice': c(ADVICE_KEYS[totals['word']]),
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
