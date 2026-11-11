from copy import deepcopy

import pytest

from fixtures import CURRENT, TRIAL
from test_cards import card_record
from shadow.trial.cards import render_verdict
from shadow.trial.verdict import finalize


def panel_record(tmp_path, votes, *, mode='backup_both', threshold=70):
    n = len(votes[0])
    record = card_record(tmp_path, (n, 0, 0))
    record['judge_policy'] = {'version': 2, 'swap_consistency_min': threshold,
                              'bootstrap_seed': 42, 'bootstrap_samples': 4000}
    record['judge_mode'] = mode
    if mode == 'backup_both':
        from dataclasses import asdict
        record['judge']['models'] = [asdict(TRIAL), asdict(CURRENT)]
    elif len(votes) > 1:
        record['judge']['models'] *= len(votes)
    for index, turn in enumerate(record['turns']):
        turn['judgment']['decisions'] = []
        for judge in votes:
            first, second = judge[index]
            turn['judgment']['decisions'].append({'orders': [
                {'normalized_pick': first}, {'normalized_pick': second}],
                'agree': first == second, 'pick': first if first == second else 'same'})
    return record


@pytest.mark.parametrize('threshold,eligible', [(70, [0]), (71, [])])
def test_swap_eligibility_is_per_judge_and_configurable(tmp_path, threshold, eligible):
    votes = [[('1', '1')] * 14 + [('1', '2')] * 6]
    record = panel_record(tmp_path, votes, threshold=threshold)
    finalize(record)
    data = record['judge_disagreements']
    assert data['eligible_judges'] == eligible
    assert data['judges'][0]['swap_consistency'] == .7
    if not eligible:
        assert record['totals']['word'] == 'not_enough'
        assert record['totals']['cant_call_reason'] == 'judge_ineligible'
        assert 'often enough to trust' in render_verdict(record)
        assert 'no judge to trust' in render_verdict(record)


def test_run_30_more_stays_when_the_interval_includes_half_and_not_when_no_judge_is_trustworthy(tmp_path):
    from shadow.common.copy import copy
    from shadow.trial.cards import result_actions
    from shadow.trial.panel import panel_eligible

    split = panel_record(tmp_path, [[('1', '1')] * 20, [('2', '2')] * 20])
    finalize(split)
    assert split['totals']['cant_call_reason'] == 'panel_uncertain'
    assert panel_eligible(split) is True
    assert split['totals']['win_rate_ci95_low'] <= .5 <= split['totals']['win_rate_ci95_high']
    assert 'more' in {choice['value'] for choice in result_actions(split)}
    assert copy('button.more') in render_verdict(split)

    weak = panel_record(tmp_path, [[('1', '1')] * 20, [('2', '2')] * 13 + [('1', '2')] * 7])
    finalize(weak)
    assert weak['totals']['cant_call_reason'] == 'judge_ineligible'
    assert panel_eligible(weak) is False
    assert 'more' not in {choice['value'] for choice in result_actions(weak)}
    assert copy('button.more') not in render_verdict(weak)

    close = card_record(tmp_path, (4, 20, 6))
    assert close['totals']['word'] == 'too_close'
    assert panel_eligible(close) is None
    assert 'more' in {choice['value'] for choice in result_actions(close)}


def test_cross_judge_disagreement_never_changes_swap_eligibility(tmp_path):
    record = panel_record(tmp_path, [[('1', '1')] * 20, [('2', '2')] * 20], mode='backup_both')
    finalize(record)
    data = record['judge_disagreements']
    assert data['eligible_judges'] == [0, 1]
    assert data['count'] == 20 and len(data['turn_ids']) == 20
    assert data['kappa'] == 0 and data['low_panel_agreement']
    assert record['totals']['same'] == 20
    assert record['totals']['cant_call_reason'] == 'panel_uncertain'
    text = render_verdict(record)
    assert text.count('Backup judging:') == 1
    assert 'both picked the same winner' in text
    assert 'read this result loosely' in text
    assert 'The likely range still includes an even split.' in text
    assert 'bootstrap interval' not in text
    stats = render_verdict(record, stats=True)
    assert '95% bootstrap interval' in stats
    assert 'A call needs the whole range above or below 50%.' in stats
    assert 'cohen kappa' in stats


def test_ineligible_contestant_judge_does_not_award_the_turn(tmp_path):
    record = panel_record(tmp_path, [[('1', '1')] * 20,
                                   [('2', '2')] * 13 + [('1', '2')] * 7])
    finalize(record)
    assert record['judge_disagreements']['eligible_judges'] == [0]
    assert record['turns'][0]['judgment']['result'] == 'same'
    assert record['totals']['same'] == 20
    assert record['totals']['word'] == 'not_enough'
    assert record['totals']['cant_call_reason'] == 'judge_ineligible'


def test_contestant_fallback_requires_both_eligible_judges(tmp_path):
    record = panel_record(tmp_path, [[('1', '1')] * 20,
                                   [('2', '2')] * 13 + [('1', '2')] * 7], mode='backup_both')
    finalize(record)
    assert record['totals']['same'] == 20 and record['totals']['word'] == 'not_enough'


def test_win_plus_tie_is_a_win_but_counts_as_swap_inconsistent(tmp_path):
    votes = [('1', '1')] * 14 + [('same', '1')] * 6
    record = panel_record(tmp_path, [votes, votes])
    finalize(record)
    assert record['totals']['won'] == 20
    assert record['judge_disagreements']['judges'][0]['consistent'] == 14


@pytest.mark.parametrize('wins,ties,losses,word', [(20, 0, 0, 'better'), (0, 0, 20, 'worse'),
                                                (0, 20, 0, 'not_enough'), (6, 19, 5, 'not_enough')])
def test_seeded_bootstrap_gates_on_excluding_half(tmp_path, wins, ties, losses, word):
    votes = [('1', '1')] * wins + [('same', 'same')] * ties + [('2', '2')] * losses
    record = panel_record(tmp_path, [votes, votes])
    finalize(record)
    stats = deepcopy(record['totals'])
    assert stats['word'] == word
    assert stats['win_rate'] == (wins + ties / 2) / (wins + ties + losses)
    if word == 'not_enough':
        assert stats['win_rate_ci95_low'] <= .5 <= stats['win_rate_ci95_high']
    finalize(record)
    assert record['totals'] == stats
    total = wins + ties + losses
    unanimous = wins == total or losses == total or ties == total
    assert record['judge_disagreements']['kappa'] is None if unanimous else record['judge_disagreements']['kappa'] == 1


def test_fleiss_kappa_stays_diagnostic_for_three_raters():
    from shadow.trial.panel import agreement_kappa
    kappa, method, undefined = agreement_kappa([('1', '1', '2')] * 20)
    assert method == 'fleiss'
    assert kappa == pytest.approx(-.5)
    assert undefined is None


def test_single_judge_finalize_keeps_a_stored_loss(tmp_path):
    record = panel_record(tmp_path, [[('1', '1')] * 20], mode='third_maker')
    record['turns'][0]['judgment']['result'] = 'lost'
    record['turns'][0]['judgment']['failure_result'] = 'lost'
    finalize(record)
    assert record['turns'][0]['judgment']['result'] == 'lost'
    assert record['totals']['lost'] == 1
    assert record['totals']['word'] == 'better'


def test_configured_threshold_is_saved_and_invalid_values_are_refused(tmp_path):
    from fixtures import FakeContext, OUTSIDE, selection
    from shadow.common.storage import Store
    from shadow.trial.engine import TrialEngine
    from shadow.trial.safety import ReplayGuard
    ctx = FakeContext()
    ctx.config['judge_swap_consistency_min'] = 65
    store = Store(tmp_path / 'policy.sqlite')
    engine = TrialEngine(ctx, store, ReplayGuard())
    record = engine.prepare(selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    assert store.get(record['id'])['judge_policy']['swap_consistency_min'] == 65
    for invalid in (-1, 101, '70', True, float('nan')):
        ctx.config['judge_swap_consistency_min'] = invalid
        with pytest.raises(ValueError):
            engine.prepare(selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])


def test_panel_and_bootstrap_survive_storage_and_refinalization(tmp_path):
    from shadow.common.storage import Store
    votes = [('1', '1')] * 14 + [('2', '2')] * 6
    record = panel_record(tmp_path, [votes, votes])
    finalize(record)
    store = Store(tmp_path / 'panel.sqlite')
    store.save(record)
    loaded = store.get(record['id'])
    finalize(loaded)
    assert loaded['totals'] == record['totals']
    assert loaded['judge_disagreements'] == record['judge_disagreements']


def test_second_look_panel_uses_the_stricter_percentiles(tmp_path):
    from shadow.trial.panel import bootstrap_interval
    votes = [('1', '1')] * 15 + [('2', '2')] * 5
    first = panel_record(tmp_path, [votes, votes])
    second = panel_record(tmp_path, [votes, votes])
    second['second_look'] = True
    finalize(first)
    finalize(second)
    low, high = bootstrap_interval([1] * 15 + [0] * 5, seed=42, samples=4000, tail=.0125)
    assert second['totals']['win_rate_ci95_low'] == pytest.approx(low)
    assert second['totals']['win_rate_ci95_high'] == pytest.approx(high)
    assert second['totals']['win_rate_ci95_low'] < first['totals']['win_rate_ci95_low']
    assert second['totals']['win_rate_ci95_high'] > first['totals']['win_rate_ci95_high']
    assert first['totals']['word'] == 'better'
    assert second['totals']['word'] == 'not_enough'
    assert second['totals']['cant_call_reason'] == 'panel_uncertain'
    assert first['totals']['bootstrap_level'] == '95'
    assert second['totals']['bootstrap_level'] == '97.5'
    assert 'stricter bar' in render_verdict(second)
    assert '97.5% bootstrap interval' in render_verdict(second, stats=True)
    assert 'seeded 97.5% bootstrap interval' in render_verdict(second, stats=True)
    assert 'seeded 95%' not in render_verdict(second, stats=True)
    assert 'seeded 95% bootstrap interval' in render_verdict(first, stats=True)
    from shadow.trial.cards import render_start
    start = render_start(second)
    assert start.count('Backup judging:') == 1
    assert 'in both orders' in start


def test_single_judge_with_saved_policy_still_reaches_holds_up(tmp_path):
    record = card_record(tmp_path, (6, 19, 5))
    record['judge_mode'] = 'third_maker'
    record['judge_policy'] = {'version': 2, 'swap_consistency_min': 70,
                              'bootstrap_seed': 42, 'bootstrap_samples': 4000}
    finalize(record)
    assert record['totals']['word'] == 'holds_up'
    assert record['totals']['cant_call_reason'] is None
    text = render_verdict(record, stats=True)
    assert text.splitlines()[0].startswith('Won 6, lost 5, tied 19 of 30 turns')
    assert '[Safe to switch]' in text
    assert 'kept up' in text
    assert 'Sign test' in text
    assert 'bootstrap' not in text.lower()
    assert 'judge panel' not in text.lower()


def test_cohen_kappa_uses_separate_judge_marginals(tmp_path):
    record = panel_record(tmp_path, [[('1', '1')] * 10 + [('2', '2')] * 10,
                                   [('1', '1')] * 8 + [('2', '2')] * 12])
    finalize(record)
    assert record['judge_disagreements']['kappa_method'] == 'cohen'
    assert record['judge_disagreements']['kappa'] == pytest.approx(.8)


@pytest.mark.parametrize('mode', ['third_maker', 'backup_both'])
def test_failure_result_overrides_the_vote_in_every_mode(tmp_path, mode):
    votes = [('1', '1')] * 20
    record = panel_record(tmp_path, [votes, votes] if mode == 'backup_both' else [votes], mode=mode)
    record['turns'][0]['judgment']['failure_result'] = 'lost'
    finalize(record)
    assert record['turns'][0]['judgment']['result'] == 'lost'
    assert record['totals']['lost'] == 1
    assert record['totals']['won'] == 19


def test_contestant_failure_cannot_bypass_both_judges_requirement(tmp_path):
    record = panel_record(tmp_path, [[('same', 'same')] * 20, [('same', 'same')] * 20], mode='backup_both')
    record['turns'][0]['judgment']['failure_result'] = 'lost'
    finalize(record)
    assert record['turns'][0]['judgment']['result'] == 'lost'
    assert record['totals']['lost'] == 1
    assert record['totals']['same'] == 19
