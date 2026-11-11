import math
import statistics
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP

from .judge import REASONS
from .panel import aggregate_panel


def sign_p(won, lost):
    n = won + lost
    return min(1.0, 2 * sum(math.comb(n, k) for k in range(max(won, lost), n + 1)) / 2 ** n) if n else 1.0


def statistics_for(won, same, lost, *, judge_agree=None, varied=0, reruns=0, second_look=False, stop_reason=None, judge_unreachable=False):
    if any(type(v) is not int or v < 0 for v in (won, same, lost, varied, reruns)):
        raise ValueError('invalid verdict counts')
    n = won + same + lost
    agree = n if judge_agree is None else judge_agree
    if type(agree) is not int or not 0 <= agree <= n:
        raise ValueError('invalid judge agreement')
    wp, lp, np = won + .5, lost + .5, n + 2
    d = (lp - wp) / np
    se = math.sqrt(lp + wp - (lp - wp) ** 2 / np) / np
    upper = d + (1.96 if second_look else 1.645) * se
    p = sign_p(won, lost)
    alpha = .025 if second_look else .05
    reason = None
    if stop_reason == 'unpriceable':
        word, reason = 'not_enough', 'unpriceable'
    elif n < 20 and judge_unreachable and stop_reason not in {'cap', 'rate_limited'}:
        word, reason = 'not_enough', 'judge_unreachable'
    elif n < 20:
        word, reason = 'not_enough', stop_reason if stop_reason in {'cap', 'rate_limited'} else 'few_usable'
    elif agree / n < .7:
        word, reason = 'not_enough', 'judge_unsure'
    elif reruns >= 10 and varied >= 6:
        word, reason = 'not_enough', 'runs_varied'
    elif p < alpha and won > lost:
        word = 'better'
    elif p < alpha and lost > won:
        word = 'worse'
    elif upper <= .15:
        word = 'holds_up'
    else:
        word, reason = 'too_close', 'too_close'
    return {'word': word, 'cant_call_reason': reason, 'won': won, 'same': same, 'lost': lost, 'pairs': n,
            'p': p, 'U': upper, 'net_pct': math.trunc(100 * (won - lost) / n) if n else 0,
            'ci95_low': math.floor(100 * (-d - 1.96 * se)), 'ci95_high': math.ceil(100 * (-d + 1.96 * se)),
            'ci_too_wide': n < 20, 'holds_low': math.floor(-100 * upper), 'worst_per_100': math.ceil(100 * upper),
            'judge_agree': agree, 'judge_pairs': n, 'reruns': reruns, 'varied': varied, 'second_look': second_look,
            'lean': 'better' if won - lost >= 2 else 'worse' if lost - won >= 2 else 'neither'}


def half_up(value, places=1):
    return float(Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))


def receipt_for(turns, *, plan_new=False, plan_yours=False):
    usable = [t for t in turns if t.get('usable')]
    n = len(usable)
    receipt = {'pairs': n, 'plan_new': plan_new, 'plan_yours': plan_yours}
    for side in ('new', 'yours'):
        runs = [r for t in usable for r in t['runs'][side]]
        costs = [sum(r['cost'] for r in t['runs'][side] if r.get('cost') is not None) for t in usable]
        unknown = any(r.get('cost') is None for t in usable for r in t['runs'][side])
        receipt['cost_per_turn_' + side] = None if unknown or not n else sum(costs) / n
        receipt['tokens_' + side] = sum(r['usage']['total_tokens'] for t in usable for r in t['runs'][side])
        times = [sum(r['duration_s'] for r in t['runs'][side]) for t in usable
                 if all(r.get('duration_s') is not None for r in t['runs'][side])]
        receipt['speed_' + side + '_s'] = statistics.median(times) if len(times) == n and n else None
        receipt['steps_' + side] = half_up(sum(len(r.get('tool_calls', [])) for r in runs) / n) if n else 0.0
        receipt['broken_' + side] = sum(r.get('broken_calls', 0) for r in runs)
        receipt['fixes_' + side] = sum(t['judgment'].get('fixes_' + side) in {'minor', 'major'} for t in usable)
    a, b = receipt['cost_per_turn_new'], receipt['cost_per_turn_yours']
    receipt['saved_pct'] = math.floor(100 * (1 - a / b)) if a is not None and b and a < b else None
    receipt['more_pct'] = math.ceil(100 * (a / b - 1)) if a is not None and b and a >= b else None
    receipt['cost_mode'] = 'unknown_price' if a is None or b is None else 'plan' if plan_new and plan_yours else 'list_price'
    if any(r['usage']['estimated'] for t in usable for side in ('new', 'yours') for r in t['runs'][side]):
        receipt['cost_mode'] = 'not_reported'
    for outcome in ('won', 'lost'):
        kinds = Counter(t['judgment']['reason_kind'] for t in usable if t['judgment']['result'] == outcome)
        kind = max(REASONS, key=lambda k: (kinds[k], -REASONS.index(k))) if kinds else None
        receipt['where_' + outcome] = {'kind': kind, 'count': kinds[kind] if kind else 0}
    return receipt


def finalize(record):
    usable = [t for t in record['turns'] if t.get('usable')]
    panel = aggregate_panel(record, usable) if record.get('judge_mode') == 'backup_both' and record.get('judge_policy') else None
    if panel is None:
        for turn in usable:
            failure = turn['judgment'].get('failure_result')
            if failure:
                turn['judgment']['result'] = failure
    counts = Counter(t['judgment']['result'] for t in usable)
    judge_calls = [c for c in record.get('usage_calls', []) if c.get('kind') == 'judge']
    judge_unreachable = bool(judge_calls) and all(c.get('error') == 'request_failed' for c in judge_calls)
    stats = statistics_for(counts['won'], counts['same'], counts['lost'], judge_agree=None if panel else sum(t['judgment']['agree'] for t in usable),
                           varied=record.get('varied', 0), reruns=record.get('reruns', 0), second_look=record.get('second_look', False), stop_reason=record.get('stop_reason'),
                           judge_unreachable=judge_unreachable)
    if panel:
        stats.update({key: value for key, value in panel.items() if key != 'eligible'})
        if stats['pairs'] >= 20 and stats['cant_call_reason'] != 'unpriceable':
            if not panel['eligible']:
                stats.update(word='not_enough', cant_call_reason='judge_ineligible')
            elif stats['cant_call_reason'] != 'runs_varied':
                if panel['win_rate_ci95_low'] > .5:
                    stats.update(word='better', cant_call_reason=None)
                elif panel['win_rate_ci95_high'] < .5:
                    stats.update(word='worse', cant_call_reason=None)
                else:
                    stats.update(word='not_enough', cant_call_reason='panel_uncertain')
    receipt = receipt_for(record['turns'], plan_new=record['trial'].get('plan', False), plan_yours=record['current'].get('plan', False))
    receipt['counts_readable'] = 'no' if stats['cant_call_reason'] in {'judge_unsure', 'judge_ineligible', 'runs_varied'} else 'so_far' if stats['pairs'] < 20 else 'yes'
    stats['receipt'] = receipt
    record['totals'] = stats
    if panel is None:
        record['judge_disagreements'] = sum(t['judgment']['disagreement'] for t in usable)
    return record
