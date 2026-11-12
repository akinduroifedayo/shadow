import math
import statistics
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP

from .judge import REASONS
from .panel import aggregate_panel



def shown_pct(rate, *, upper, keep_off_zero=False):
    """Outward percent bound. For a Better/Worse call, add decimals if a whole
    percent would print as 0, so the printed range stays on the same side of even."""
    whole = math.ceil(100 * rate) if upper else math.floor(100 * rate)
    if not keep_off_zero or whole != 0 or rate == 0:
        return whole
    places = 1
    while True:
        scale = 10 ** (2 + places)
        stepped = math.ceil(rate * scale) if upper else math.floor(rate * scale)
        shown = stepped / (10 ** places)
        if shown != 0 and (shown > 0) == (rate > 0):
            return shown
        places += 1


def format_signed_pct(value, minus='-'):
    if not value:
        return '0%'
    sign = '+' if value > 0 else minus
    magnitude = abs(value)
    if isinstance(value, int) or float(magnitude).is_integer():
        body = str(int(magnitude))
    else:
        body = f'{magnitude:.8f}'.rstrip('0').rstrip('.')
    return f'{sign}{body}%'


def format_rate_pct(value):
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return f'{value:.8f}'.rstrip('0').rstrip('.')


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
    z = 2.241 if second_look else 1.96
    low, high = -d - z * se, -d + z * se
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
    elif low > 0:
        word = 'better'
    elif high < 0:
        word = 'worse'
    elif upper <= .15:
        word = 'holds_up'
    else:
        word, reason = 'too_close', 'too_close'
    off_zero = word in {'better', 'worse'}
    return {'word': word, 'cant_call_reason': reason, 'won': won, 'same': same, 'lost': lost, 'pairs': n,
            'p': p, 'U': upper, 'net_pct': math.trunc(100 * (won - lost) / n) if n else 0,
            'interval_low': low, 'interval_high': high,
            'ci95_low': shown_pct(low, upper=False, keep_off_zero=off_zero),
            'ci95_high': shown_pct(high, upper=True, keep_off_zero=off_zero),
            'ci_too_wide': n < 20, 'holds_low': math.floor(-100 * upper), 'worst_per_100': math.ceil(100 * upper),
            'judge_agree': agree, 'judge_pairs': n, 'reruns': reruns, 'varied': varied, 'second_look': second_look,
            'lean': 'better' if won - lost >= 2 else 'worse' if lost - won >= 2 else 'neither'}


def uses_panel(record):
    return record.get('judge_mode') == 'backup_both' and record.get('judge_policy')


def refresh_statistics(record):
    if 'totals' not in record or uses_panel(record):
        return record
    totals = record['totals']
    stats = statistics_for(totals['won'], totals['same'], totals['lost'],
                           judge_agree=totals['judge_agree'], varied=totals['varied'], reruns=totals['reruns'],
                           second_look=totals['second_look'], stop_reason=record.get('stop_reason'),
                           judge_unreachable=totals['cant_call_reason'] == 'judge_unreachable')
    return {**record, 'totals': {**totals, **stats}}


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
    panel = aggregate_panel(record, usable) if uses_panel(record) else None
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
    off_zero = stats['word'] in {'better', 'worse'}
    stats['ci95_low'] = shown_pct(stats['interval_low'], upper=False, keep_off_zero=off_zero)
    stats['ci95_high'] = shown_pct(stats['interval_high'], upper=True, keep_off_zero=off_zero)
    receipt = receipt_for(record['turns'], plan_new=record['trial'].get('plan', False), plan_yours=record['current'].get('plan', False))
    receipt['counts_readable'] = 'no' if stats['cant_call_reason'] in {'judge_unsure', 'judge_ineligible', 'runs_varied'} else 'so_far' if stats['pairs'] < 20 else 'yes'
    stats['receipt'] = receipt
    record['totals'] = stats
    if panel is None:
        record['judge_disagreements'] = sum(t['judgment']['disagreement'] for t in usable)
    return record
