import random
from collections import Counter

from .judge import resolve_orders


def agreement_kappa(votes):
    n = len(votes)
    judges = len(votes[0]) if n else 0
    method = 'cohen' if judges == 2 else 'fleiss' if judges > 2 else None
    if judges < 2:
        return None, method, 'fewer_than_two_judges'
    if judges == 2:
        observed = sum(row[0] == row[1] for row in votes) / n
        left, right = (Counter(row[j] for row in votes) for j in (0, 1))
        chance = sum(left[pick] * right[pick] for pick in ('1', 'same', '2')) / n ** 2
    else:
        observed = sum(sum(count * (count - 1) for count in Counter(row).values())
                       / (judges * (judges - 1)) for row in votes) / n
        totals = Counter(pick for row in votes for pick in row)
        chance = sum((count / (n * judges)) ** 2 for count in totals.values())
    if chance >= 1:
        return None, method, 'constant_unanimous_votes'
    return (observed - chance) / (1 - chance), method, None


def bootstrap_interval(scores, *, seed, samples, tail=.025):
    if not scores:
        return None, None
    rng = random.Random(seed)
    n = len(scores)
    rates = sorted(sum(rng.choices(scores, k=n)) / n for _ in range(samples))
    index = int(tail * (samples - 1))
    return rates[index], rates[-1 - index]


def aggregate_panel(record, turns):
    policy = record['judge_policy']
    threshold = policy['swap_consistency_min']
    judges = []
    matrix = []
    for turn in turns:
        decisions = turn['judgment'].get('decisions', [])
        matrix.append([resolve_orders(d['orders']) for d in decisions])
    for index, model in enumerate(record['judge']['models']):
        decisions = [turn['judgment'].get('decisions', [])[index]
                     for turn in turns if len(turn['judgment'].get('decisions', [])) > index]
        consistent = sum(d['orders'][0]['normalized_pick'] == d['orders'][1]['normalized_pick'] for d in decisions)
        rate = consistent / len(turns) if turns else 0
        judges.append({'index': index, 'provider': model['provider'], 'model': model['model'],
                       'consistent': consistent, 'pairs': len(turns), 'swap_consistency': rate,
                       'eligible': bool(turns) and len(decisions) == len(turns) and 100 * rate >= threshold})
    eligible = [judge['index'] for judge in judges if judge['eligible']]
    votes = [[row[index] for index in eligible] for row in matrix]
    for turn, row in zip(turns, votes):
        if record['judge_mode'] == 'backup_both':
            pick = row[0] if len(row) == 2 and row[0] == row[1] else 'same'
        else:
            score = sum({'1': 1, 'same': .5, '2': 0}[pick] for pick in row)
            pick = '1' if score > len(row) / 2 else '2' if score < len(row) / 2 else 'same'
        result = {'1': 'won', '2': 'lost', 'same': 'same'}[pick]
        result = turn['judgment'].get('failure_result') or result
        turn['judgment']['result'] = result
    disagreements = [turn['id'] for turn, row in zip(turns, votes) if len(set(row)) > 1]
    kappa, method, undefined = agreement_kappa(votes)
    record['judge_disagreements'] = {'count': len(disagreements), 'turn_ids': disagreements,
                                    'judges': judges, 'eligible_judges': eligible,
                                    'kappa': kappa, 'kappa_method': method, 'kappa_undefined': undefined,
                                    'low_panel_agreement': kappa is not None and kappa < .41}
    scores = [{'won': 1, 'same': .5, 'lost': 0}[turn['judgment']['result']] for turn in turns]
    second_look = bool(record.get('second_look'))
    low, high = bootstrap_interval(scores, seed=policy['bootstrap_seed'], samples=policy['bootstrap_samples'],
                                   tail=.0125 if second_look else .025)
    return {'win_rate': sum(scores) / len(scores) if scores else None,
            'win_rate_ci95_low': low, 'win_rate_ci95_high': high,
            'bootstrap_level': '97.5' if second_look else '95',
            'eligible': bool(eligible) and (record['judge_mode'] != 'backup_both' or len(eligible) == 2),
            'judge_agree': sum(judge['consistent'] for judge in judges),
            'judge_pairs': sum(judge['pairs'] for judge in judges)}


def panel_eligible(record):
    """The saved panel's eligible flag.

    None when this result has no judge panel. False when no judge was
    trustworthy: a second look cannot repair that. True when the panel can
    still be read, including when its interval includes an even split.
    """
    panel = record.get('judge_disagreements')
    if isinstance(panel, dict) and 'eligible_judges' in panel:
        chosen = panel['eligible_judges']
        if record.get('judge_mode') == 'backup_both':
            return len(chosen) == 2
        return bool(chosen)
    if (record.get('totals') or {}).get('cant_call_reason') == 'judge_ineligible':
        return False
    return None
