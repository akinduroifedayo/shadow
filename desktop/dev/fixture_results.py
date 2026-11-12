"""Fixture-only result finalization from saved turn measurements. No provider calls."""
import math
from collections import Counter
from statistics import median
from decimal import Decimal, ROUND_HALF_UP




def shown_pct(rate, *, upper, keep_off_zero=False):
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


def stats(won, same, lost, judge_agree, *, reruns=10, varied=2, stop_reason=None, second_look=False, judge_unreachable=False):
    n = won + same + lost
    wp, lp, n2 = won + .5, lost + .5, n + 2
    d = (lp - wp) / n2
    se = math.sqrt(lp + wp - (lp - wp) ** 2 / n2) / n2
    upper = d + (1.96 if second_look else 1.645) * se
    k, m = max(won, lost), won + lost
    p = min(1.0, 2 * sum(math.comb(m, i) for i in range(k, m + 1)) / 2 ** m) if m else 1.0
    z = 2.241 if second_look else 1.96
    low, high = -d - z * se, -d + z * se
    reason = None
    if stop_reason == 'unpriceable':
        word, reason = 'not_enough', 'unpriceable'
    elif n < 20 and judge_unreachable and stop_reason not in {'cap', 'rate_limited'}:
        word, reason = 'not_enough', 'judge_unreachable'
    elif n < 20:
        word, reason = 'not_enough', stop_reason if stop_reason in {'cap', 'rate_limited'} else 'few_usable'
    elif judge_agree / n < .7:
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
    return {'word': word, 'cant_call_reason': reason, 'won': won, 'same': same, 'lost': lost, 'pairs': n, 'p': p, 'U': upper,
            'net_pct': math.trunc(100 * (won - lost) / n) if n else 0,
            'interval_low': low, 'interval_high': high,
            'ci95_low': shown_pct(low, upper=False, keep_off_zero=off_zero),
            'ci95_high': shown_pct(high, upper=True, keep_off_zero=off_zero), 'ci_too_wide': n < 20, 'holds_low': math.floor(-100 * upper),
            'worst_per_100': math.ceil(100 * upper), 'judge_agree': judge_agree, 'judge_pairs': n, 'reruns': reruns,
            'varied': varied, 'second_look': second_look,
            'lean': 'better' if won - lost >= 2 else 'worse' if lost - won >= 2 else 'neither'}


def receipt(n, *, cost, speed, steps, broken, fixes, where_won, where_lost, readable='yes'):
    new_c, old_c = cost
    saved = math.floor(100 * (1 - new_c / old_c)) if new_c < old_c else None
    more = math.ceil(100 * (new_c / old_c - 1)) if new_c >= old_c else None
    return {'pairs': n, 'cost_per_turn_new': new_c, 'cost_per_turn_yours': old_c, 'tokens_new': 1370 * n, 'tokens_yours': 3530 * n,
            'speed_new_s': speed[0], 'speed_yours_s': speed[1], 'steps_new': steps[0], 'steps_yours': steps[1],
            'broken_new': broken[0], 'broken_yours': broken[1], 'fixes_new': fixes[0], 'fixes_yours': fixes[1],
            'saved_pct': saved, 'more_pct': more, 'cost_mode': 'list_price',
            'where_won': {'kind': where_won[0], 'count': where_won[1]}, 'where_lost': {'kind': where_lost[0], 'count': where_lost[1]},
            'counts_readable': readable}



def totals_for(test):
    turns = [t for t in test['turns'] if t['state'] == 'done' and t.get('usable')]
    n = len(turns)
    if n < 20:
        raise ValueError('at least 20 usable finished turns are required')
    counts = Counter(t['judgment']['result'] for t in turns)
    totals = stats(counts['won'], counts['same'], counts['lost'],
                   sum(t['judgment']['agree'] for t in turns), reruns=test.get('reruns', 0),
                   varied=test.get('varied', 0), second_look=test.get('second_look', False))
    measured = {'pairs': n}
    for side in ('new', 'yours'):
        all_runs = [run for t in turns for run in t['runs'][side]]
        runs = [t['runs'][side][-1] for t in turns]
        costs = [r.get('cost') for r in all_runs]
        times = [r.get('duration_s') for r in runs]
        tokens = [r.get('usage', {}).get('total_tokens') for r in all_runs]
        measured['cost_per_turn_' + side] = sum(costs) / n if all(c is not None for c in costs) else None
        measured['tokens_' + side] = sum(tokens) if all(t is not None for t in tokens) else None
        measured['speed_' + side + '_s'] = median(times) if all(t is not None for t in times) else None
        steps = sum(len(r.get('tool_calls', [])) for r in runs) / n
        measured['steps_' + side] = float(Decimal(str(steps)).quantize(Decimal('.1'), rounding=ROUND_HALF_UP))
        measured['broken_' + side] = sum(r.get('broken_calls', 0) for r in runs)
        measured['fixes_' + side] = sum(t['judgment'].get('fixes_' + side) in {'minor', 'major'} for t in turns)
    a, b = measured['cost_per_turn_new'], measured['cost_per_turn_yours']
    measured['saved_pct'] = math.floor(100 * (1 - a / b)) if a is not None and b and a < b else None
    measured['more_pct'] = math.ceil(100 * (a / b - 1)) if a is not None and b and a >= b else None
    plan = test['trial'].get('plan') or test['current'].get('plan')
    measured['cost_mode'] = 'unknown_price' if a is None or b is None else 'plan' if plan else 'list_price'
    if measured['cost_mode'] == 'unknown_price' and any(measured['tokens_' + side] is None for side in ('new', 'yours')):
        measured['cost_mode'] = 'not_reported'
    if any(run.get('usage', {}).get('estimated') for t in turns for side in ('new', 'yours') for run in t['runs'][side]):
        measured['cost_mode'] = 'not_reported'
    for result in ('won', 'lost'):
        reasons = Counter(t['judgment']['reason_kind'] for t in turns if t['judgment']['result'] == result)
        kind, count = reasons.most_common(1)[0] if reasons else (None, 0)
        measured['where_' + result] = {'kind': kind, 'count': count}
    measured['counts_readable'] = 'no' if totals['cant_call_reason'] in {'judge_unsure', 'runs_varied'} else 'yes'
    totals['receipt'] = measured
    return totals
