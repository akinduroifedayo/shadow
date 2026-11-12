import math
import textwrap
from decimal import Decimal, ROUND_CEILING

from ..common.copy import copy
from ..common.models import Model
from ..common.controller_stop import controller_stop_text
from .usage import stored_price
from .judge import judge_error_text
from .panel import panel_eligible
from .verdict import half_up
from .presentation import verdict_presentation, terminal_rate_bar, terminal_model_table


def money(value):
    if value is None:
        return copy('receipt.no_cost')
    return copy('receipt.under_cent') if value < .001 else f'${value:.3f}' if value < 1 else f'${value:.2f}'


def speed(value):
    if value is None:
        return copy('receipt.no_speed')
    return f'{half_up(value):.1f}s' if value < 60 else f'{int(value // 60)}m {int(value % 60)}s'


def _wrap_groups(groups, width=80):
    lines, line = [], ''
    for group in groups:
        if line and len(line) + 3 + len(group) > width:
            lines.append(line)
            line = group
        else:
            line += (' | ' if line else '') + group
    return '\n'.join([*lines, line])


def judge_line(record, start=False):
    if record['judge_mode'] == 'backup_both':
        return copy('start.judge_contestant') if start else copy('fine.contestant')
    model = Model(**record['judge']['models'][0])
    key = 'start.judge_user' if record['judge_source'] == 'user' else 'start.judge_line'
    if not start:
        key = 'verdict.judge_your_pick' if record['judge_source'] == 'user' else 'fine.judge'
    return copy(key, judge=model.name)


def missing_price_names(record):
    models = [Model(**record['trial']), Model(**record['current'])]
    models.extend(Model(**row) for row in record['judge']['models'])
    return ', '.join(dict.fromkeys(model.name for model in models if stored_price(record.get('prices', {}), model) is None))


def pricing_notice(record):
    return copy('start.service_tier' if record.get('pricing_blocker') == 'service_tier' else 'trial.unpriceable')


def run_message(record, surface):
    fields = {'id': record['id'], 'done': sum(t['usable'] for t in record['turns']),
              'picked': record['picked'], 'spend': record['spend'], 'cap': record['cap']}
    if record.get('stop_reason') == 'unpriceable':
        return copy('trial.' + surface + '_unpriceable', pricing=pricing_notice(record), **fields)
    key = 'stopped_usable' if surface == 'stopped' and fields['done'] >= 20 else surface
    return copy('trial.' + key, **fields)


def render_start(record, *, cli=False, isolation_verified=False):
    current, trial = Model(**record['current']), Model(**record['trial'])
    command = 'hermes shadow' if cli else '/shadow'
    providers = ', '.join(dict.fromkeys(m['provider'] for m in [record['trial'], record['current'], *record['judge']['models']]))
    fields = {'trying': trial.name, 'current': current.name, 'picked': record['picked'], 'total': record['total'], 'days': record['days'],
              'verb': 'compare' if record['intent'] == 'compare' else 'try'}
    lines = [copy('start.title', **fields), f'{trial.provider} / {trial.model}', '', copy('start.turns', **fields),
             copy('start.left_out', reasons=', '.join(f'{v} {copy("pick.reason." + k)}' for k,v in record['left_out'].items()) or '0'),
             judge_line(record, True)]
    if record.get('second_look'):
        lines.append(copy('fine.panel_second' if record.get('judge_mode') == 'backup_both' else 'fine.second', pairs=record['picked']))
    if record['judge']['notice']:
        lines.append(record['judge']['notice'])
    lines += [copy('start.sent', providers=providers, keys=record['scrub_counts'].get('key', 0), emails=record['scrub_counts'].get('email', 0)),
              copy('start.runs_on' if record['web_lookups'] else 'start.runs_off')]
    prediction = record['estimate']
    if prediction and prediction.get('total') is not None:
        lines.append(copy('start.cost', estimate=prediction['total'], cap=record['cap'], judge_cost=prediction['judge']))
        if prediction['plan_tokens']:
            lines.append(copy('start.plan', tokens=prediction['plan_tokens'], cost=prediction['plan_cost']))
    elif prediction and prediction.get('tokens'):
        lines.append(copy('start.tokens', tokens=prediction['tokens']))
        if record.get('pricing_blocker') == 'service_tier':
            lines.append(pricing_notice(record))
        else:
            missing = missing_price_names(record)
            if missing:
                lines.append(copy('start.no_price', model=missing))
    else:
        lines.append(copy('trial.unpriceable'))
    if record['picked'] < 20:
        lines.append(copy('start.too_few', command=command, model=trial.model, **fields))
    elif prediction and prediction.get('total') is not None and prediction['total'] > record['cap']:
        lines.append(copy('start.over_cap', estimate=prediction['total'], cap=record['cap'], minimum=prediction['total'] * 20 / record['picked'], higher=math.ceil(prediction['total']), command=command, model=trial.model, verb=fields['verb']))
    if isolation_verified:
        lines.append(copy('start.wait', minutes=max(1, record['picked'])))
    else:
        lines.append(copy('trial.isolation_required'))
    if cli:
        lines.append(copy('start.preview', picked=record['picked']))
    if record['can_start'] and isolation_verified:
        lines.append(f'[{copy("button.start")}] [{copy("button.cancel")}]')
    return '\n'.join(textwrap.fill(line, 72) if cli and line else line for line in lines)


def receipt_columns(record):
    stats = record['totals']
    receipt = stats['receipt']
    a, b = money(receipt['cost_per_turn_new']), money(receipt['cost_per_turn_yours'])
    if receipt.get('plan_new'):
        a = copy('receipt.plan', cost=a)
    if receipt.get('plan_yours'):
        b = copy('receipt.plan', cost=b)
    costs = f'{a} vs {b}'
    cost_sub = ''
    if receipt['saved_pct'] is not None:
        cost_sub = f'-{receipt["saved_pct"]}%'
    elif receipt['more_pct'] is not None:
        cost_sub = f'+{receipt["more_pct"]}%'
    if receipt['cost_mode'] == 'unknown_price':
        costs = copy('receipt.tokens', new=receipt['tokens_new'], yours=receipt['tokens_yours'])
        cost_sub = ''
    elif receipt['cost_mode'] == 'not_reported':
        costs, cost_sub = copy('receipt.no_cost'), copy('receipt.estimated')
    return [
        {'key': 'result', 'label': copy('receipt.label.result'), 'value': f'{stats["won"]} · {stats["same"]} · {stats["lost"]}', 'sub': copy('receipt.sub.result', pairs=stats['pairs'])},
        {'key': 'cost', 'label': copy('receipt.label.cost'), 'value': costs, 'sub': cost_sub},
        {'key': 'speed', 'label': copy('receipt.label.speed'), 'value': f'{speed(receipt["speed_new_s"])} vs {speed(receipt["speed_yours_s"])}', 'sub': copy('receipt.sub.speed')},
        {'key': 'steps', 'label': copy('receipt.label.steps'), 'value': f'{receipt["steps_new"]:.1f} vs {receipt["steps_yours"]:.1f}', 'sub': copy('receipt.sub.steps')},
        {'key': 'broken', 'label': copy('receipt.label.broken'), 'value': f'{receipt["broken_new"]} vs {receipt["broken_yours"]}', 'sub': ''},
    ]


def _signed(value):
    from shadow.trial.verdict import format_signed_pct
    return format_signed_pct(value)


def p_text(p, alpha):
    if p < .001:
        return copy('maths.p_small')
    exponent = math.floor(math.log10(p)) - 1
    quantum = Decimal(10) ** exponent
    rounded = Decimal(str(p)).quantize(quantum, rounding=ROUND_CEILING)
    if p < alpha <= rounded:
        return copy('maths.p_near', alpha=alpha)
    return f'{rounded:.{max(0, -exponent)}f}'


def switch_limit(record):
    if record.get('switch_limit'):
        return record['switch_limit']
    if record.get('route_conflicts'):
        return 'trial.switch_routes_conflict'
    routes = record.get('routes', {})
    if set(routes) != {'current', 'trial'} or any(not all(key in route for key in ('provider', 'model', 'base_url', 'api_mode')) for route in routes.values()):
        return 'trial.switch_routes_missing'
    old, new = routes['current'], routes['trial']
    if old['provider'] == new['provider']:
        if old['base_url'] != new['base_url']:
            return 'trial.switch_incompatible'
        if old['api_mode'] != new['api_mode']:
            return 'trial.switch_incompatible'
    return None


def switch_notice(record):
    if record['totals']['word'] in {'better', 'holds_up'}:
        return switch_limit(record)
    return None


def render_verdict(record, *, cli=False, stats=False, interactive=False):
    data = record['totals']
    level = '97.5' if data['second_look'] else '95'
    current, trial = Model(**record['current']), Model(**record['trial'])
    judge = Model(**record['judge']['models'][0])
    command = 'hermes shadow' if cli else '/shadow'
    fields = {'trying': trial.name, 'current': current.name, 'judge': judge.name, 'pairs': data['pairs'], 'picked': record['picked'],
              'unusable': record['picked'] - data['pairs'], 'cap': record['cap'], 'provider': current.provider, 'agree': data['judge_agree'], 'varied': data['varied']}
    reason = data['cant_call_reason'] or data['word']
    receipt = data['receipt']
    cheaper = receipt['saved_pct'] is not None and receipt['saved_pct'] > 0
    faster = receipt['speed_new_s'] is not None and receipt['speed_yours_s'] is not None and receipt['speed_new_s'] < receipt['speed_yours_s']
    if data['word'] == 'holds_up' and not cheaper and not faster:
        reason = 'holds_neither'
    view = verdict_presentation(record)
    lines = [view['headline']]
    if view['bar']:
        lines += [terminal_rate_bar(view['bar']), view['bar']['label']]
    lines += [terminal_model_table(view['table']), '[' + view['advice'] + ']']
    if view['advice_hint']:
        lines.append(view['advice_hint'])
    reason_group = 'verdict.reason.compare.' if record['intent'] == 'compare' and data['word'] != 'not_enough' else 'verdict.reason.'
    lines.append(copy(reason_group + reason, **fields))
    if error := judge_error_text(record):
        lines.append(error)
    if note := controller_stop_text(record, copy):
        lines.append(note)
    if record.get('stop_reason') == 'unpriceable':
        lines.append(pricing_notice(record))
    if data['second_look']:
        lines.append(copy('fine.panel_second' if record.get('judge_mode') == 'backup_both' else 'fine.second', pairs=data['pairs']))
    caption = copy('receipt.caption', trying=trial.name, current=current.name)
    if receipt['counts_readable'] == 'so_far':
        caption += copy('receipt.so_far')
    elif receipt['counts_readable'] == 'no':
        caption += copy('receipt.unreadable', reason=copy('counts.' + data['cant_call_reason']))
    columns = receipt_columns(record)
    cost = columns[1]['value'] + (f' ({columns[1]["sub"]})' if columns[1]['sub'] else '')
    groups = [copy('receipt.result_text', **data), cost, columns[2]['value'], copy('receipt.steps_text', new=f'{receipt["steps_new"]:.1f}', yours=f'{receipt["steps_yours"]:.1f}'), copy('receipt.broken_text', new=receipt['broken_new'], yours=receipt['broken_yours'])]
    lines.extend(['', caption, _wrap_groups(groups)])
    if record.get('judge_mode') == 'backup_both' and data.get('win_rate') is not None:
        lines.append(copy('sure.bootstrap', low=math.floor(100 * data['win_rate_ci95_low']), high=math.ceil(100 * data['win_rate_ci95_high'])))
    elif data['word'] in {'better', 'worse'}:
        p = data['p']
        luck = copy('sure.luck_hundred', count=math.ceil(100*p)) if p >= .01 else copy('sure.luck_thousand', count=math.ceil(1000*p)) if p >= .001 else copy('sure.luck_small')
        lines.append(copy('sure.luck', luck=luck))
    elif data['word'] in {'holds_up', 'too_close'}:
        if data['word'] == 'too_close':
            lines.append(copy('sure.lean_' + data['lean']))
        lines.append(copy('sure.worst', count=data['worst_per_100']) if data['worst_per_100'] > 0 else copy('sure.even'))
        if data['word'] == 'too_close':
            lines.append(copy('sure.holds_bar'))
    elif data['pairs'] < 20:
        lines.append(copy('sure.few'))
    if data['reruns']:
        lines.append(copy('fine.scale', reruns=data['reruns'], varied=data['varied']))
    lines += [judge_line(record), copy('fine.copies')]
    if isinstance(record['judge_disagreements'], dict) and record['judge_disagreements']['low_panel_agreement']:
        lines.append(copy('fine.low_panel_agreement'))
    if not record['web_lookups']:
        lines.append(copy('fine.web_off', count=record.get('web_stopped', 0)))
    elif record.get('web_turns'):
        lines.append(copy('fine.web_on', count=record['web_turns']))
    estimate = (record.get('estimate') or {}).get('total')
    if data['word'] == 'too_close' and not data['second_look'] and estimate is not None:
        lines.append(copy('trial.more_cost', cost=estimate))
    notice = switch_notice(record)
    if notice:
        lines.append(copy(notice))
    elif record['intent'] == 'switch' and data['word'] in {'better', 'holds_up'} and current.provider != trial.provider:
        lines.append(copy('trial.switch_watch', trying=trial.name, provider=trial.provider))
    lines.append(copy('hint.stats', command=command, id=record['id']))
    if stats:
        rows = [('maths.net', _signed(data['net_pct'])), ('maths.ci', f'{_signed(data["ci95_low"])} to {_signed(data["ci95_high"])}' + (' · ' + copy('maths.too_wide') if data['ci_too_wide'] else '')),
                ('maths.sign', p_text(data['p'], .025 if data['second_look'] else .05) + (' · ' + copy('maths.not_used') if data['pairs'] < 20 else '')),
                ('maths.holds', copy('maths.holds_value', low=_signed(data['holds_low']), level='97.5' if data['second_look'] else '95')),
                ('maths.agree', copy('maths.panel_agree_value', agree=data['judge_agree'], pairs=data['judge_pairs'], threshold=record['judge_policy']['swap_consistency_min']) if record.get('judge_mode') == 'backup_both' else copy('maths.agree_value', agree=data['judge_agree'], pairs=data['judge_pairs'])),
                ('maths.fixes', copy('maths.fixes_value', new=receipt['fixes_new'], yours=receipt['fixes_yours']))]
        for outcome in ('won', 'lost'):
            where = receipt['where_' + outcome]
            if where['kind']:
                rows.append(('maths.where_' + outcome, copy('maths.reason_count', reason=copy('reason.' + where['kind']), count=where['count'], total=data[outcome])))
        lines.extend(f'{copy(label, level=level):22} {value}' for label,value in rows)
        if record.get('judge_mode') == 'backup_both' and isinstance(record.get('judge_disagreements'), dict):
            panel = record['judge_disagreements']
            if data.get('win_rate') is not None:
                lines.append(copy('maths.bootstrap', low=math.floor(100 * data['win_rate_ci95_low']), high=math.ceil(100 * data['win_rate_ci95_high']), level=data.get('bootstrap_level', '95')))
            if panel['kappa'] is not None:
                lines.append(copy('maths.panel_kappa', method=panel['kappa_method'], kappa=f'{panel["kappa"]:.3f}'))
            lines.append(copy('maths.panel_method', level=data.get('bootstrap_level', '95')))
        else:
            lines.append(copy('maths.method'))
    if cli:
        if not interactive and any(c['value'] == 'switch' for c in result_actions(record)):
            lines.append(f'{command} switch {record["id"]}')
    else:
        lines.append(' '.join(f'[{c["label"]}]' for c in result_actions(record)))
    output = []
    for line in lines:
        output.append(textwrap.fill(line, 72) if cli and line and '\n' not in line and line not in {view['headline'], terminal_model_table(view['table']), groups[0], _wrap_groups(groups)} and not any(line.startswith(copy(k, level=level)) for k in ('maths.net','maths.ci','maths.sign','maths.holds','maths.agree','maths.fixes')) else line)
    return '\n'.join(output)


def result_actions(record):
    data, receipt = record['totals'], record['totals']['receipt']
    current, trial = Model(**record['current']), Model(**record['trial'])
    choices = {
        'keep': copy('button.keep', current=current.name),
        'switch': copy('button.switch' if current.provider == trial.provider else 'button.switch_all'),
        'turns': copy('button.lost', lost=data['lost']),
        'more': copy('button.more'),
    }
    if data['word'] not in {'better', 'holds_up'}:
        choices['switch'] = copy('switch.override.confirm')
    more = [] if data['second_look'] or record.get('second_look_id') or panel_eligible(record) is False else ['more']
    if data['word'] in {'better', 'holds_up'}:
        cheaper = receipt['saved_pct'] is not None and receipt['saved_pct'] > 0
        faster = receipt['speed_new_s'] is not None and receipt['speed_yours_s'] is not None and receipt['speed_new_s'] < receipt['speed_yours_s']
        order = ['switch', 'turns', 'keep'] if data['word'] == 'better' or cheaper or faster else ['keep', 'switch', 'turns']
    elif data['word'] == 'worse':
        order = ['turns', 'keep']
    elif data['word'] == 'too_close':
        order = more + ['turns', 'keep']
    else:
        choices['turns'] = copy('button.turns', count=data['pairs'])
        order = (more if data['cant_call_reason'] in {'cap', 'rate_limited', 'panel_uncertain'} else []) + ['turns', 'keep']
    if record['intent'] == 'compare':
        order = [name for name in order if name != 'switch']
    if 'switch' not in order:
        order.append('switch')
    return [{'value': name, 'label': choices[name]} for name in order if name != 'switch' or not switch_limit(record)]


def render_turns(record, *, cli=False):
    lost_only = record['totals']['word'] != 'not_enough'
    turns = [t for t in record['turns'] if t.get('usable') and (not lost_only or t['judgment']['result'] == 'lost')]
    lines = [copy('turns.lost_title', lost=len(turns), trying=Model(**record['trial']).name) if lost_only else copy('button.turns', count=len(turns))]
    for index, turn in enumerate(turns[:8], 1):
        lines.append(f'{index}. "{turn["first_line"]}"')
        lines.append('   ' + turn['judgment']['reason'])
        if any(run.get('where_stopped') for side in ('new', 'yours') for run in turn['runs'][side]):
            lines.append('   ' + copy('turns.not_run'))
    if len(turns) > 8:
        lines.append(copy('turns.more', count=len(turns) - 8))
    return '\n'.join(textwrap.fill(line, 72) if cli else line for line in lines)
