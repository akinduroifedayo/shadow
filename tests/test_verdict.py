import math

import pytest

from shadow.trial.verdict import format_signed_pct, shown_pct, statistics_for


@pytest.mark.parametrize('w,s,l,word,p,upper', [
    (15,11,4,'better',.019,None), (10,18,2,'better',.039,None),
    (14,11,5,'better',.064,-.066), (9,19,2,'better',.065,-.052),
    (6,19,5,'holds_up',1,.147), (3,24,3,'holds_up',1,.136),
    (0,30,0,'holds_up',1,.051), (4,22,4,'too_close',1,.154),
    (4,20,6,'too_close',.754,.232), (2,19,9,'worse',.065,.385),
    (3,9,18,'worse',.0015,None), (7,8,4,'not_enough',.55,None),
])
def test_every_spec_table_row(w,s,l,word,p,upper):
    stats = statistics_for(w,s,l)
    assert stats['word'] == word
    assert f'{stats["p"]:.2g}' == f'{p:.2g}'
    if upper is not None:
        assert round(stats['U'], 3) == upper


@pytest.mark.parametrize('w,s,l,p,worst,luck', [
    (15,11,4,'0.020',None,'about 2 times in 100'), (10,18,2,'0.039',None,'about 4 times in 100'),
    (14,11,5,'0.064',None,'about 7 times in 100'), (9,19,2,'0.066',None,'about 7 times in 100'),
    (6,19,5,'1.0',15,None), (3,24,3,'1.0',14,None),
    (0,30,0,'1.0',6,None), (4,22,4,'1.0',16,None),
    (4,20,6,'0.76',24,None), (2,19,9,'0.066',None,'about 7 times in 100'),
    (3,9,18,'0.0015',None,'about 2 times in 1,000'), (7,8,4,'0.55',None,None),
])
def test_spec_numbers_round_cautiously_on_the_card(tmp_path,w,s,l,p,worst,luck):
    from test_cards import card_record
    from shadow.trial.cards import render_verdict
    text = render_verdict(card_record(tmp_path, (w,s,l)), stats=True)
    assert f'Sign test              {p}' in text
    if luck is not None:
        assert f'If the two were really even, luck would give a gap this big {luck}.' in text
        assert 'Worst case this test leaves open' not in text
    if worst is not None:
        expected = f'it loses {worst} more turns than it wins, per 100.' if worst else 'about even with yours.'
        assert expected in text


@pytest.mark.parametrize('w,s,l,net,low,high,holds', [(15,11,4,36,9,60,13), (6,19,5,3,-19,25,-15), (3,9,18,-50,-71,-23,-67), (4,20,6,-6,-27,14,-24), (7,8,4,15,-18,47,-13)])
def test_design_golden_maths(w,s,l,net,low,high,holds):
    stats = statistics_for(w,s,l)
    assert (stats['net_pct'],stats['ci95_low'],stats['ci95_high'],stats['holds_low']) == (net,low,high,holds)
    assert stats['holds_low'] == -stats['worst_per_100']


def test_rule_order_and_second_look():
    assert statistics_for(19,0,0)['word'] == 'not_enough'
    assert statistics_for(20,10,0,judge_agree=20)['cant_call_reason'] == 'judge_unsure'
    assert statistics_for(20,10,0,judge_agree=21,reruns=10,varied=6)['cant_call_reason'] == 'runs_varied'
    assert statistics_for(20,10,0,judge_agree=21,reruns=10,varied=5)['word'] == 'better'
    assert statistics_for(10,18,2)['word'] == 'better'
    assert statistics_for(10,18,2,second_look=True)['word'] == 'better'
    assert statistics_for(9,19,2)['word'] == 'better'
    assert statistics_for(9,19,2,second_look=True)['word'] == 'holds_up'
    assert statistics_for(6,19,5)['word'] == 'holds_up'
    assert statistics_for(6,19,5,second_look=True)['word'] == 'too_close'
    assert statistics_for(2,18,10,second_look=True)['word'] == 'worse'
    assert statistics_for(2,9,9,second_look=True)['word'] == 'too_close'


def test_receipt_counts_failed_attempt_calls_and_broken_calls():
    from shadow.trial.verdict import receipt_for
    failed = {'cost': .02, 'usage': {'total_tokens': 100, 'estimated': False},
              'duration_s': 1, 'tool_calls': [{'name': 'bad_call'}], 'broken_calls': 1}
    finished = {'cost': .01, 'usage': {'total_tokens': 50, 'estimated': False},
                'duration_s': 2, 'tool_calls': [{'name': 'read'}, {'name': 'search'}], 'broken_calls': 0}
    turns = [{'usable': True, 'runs': {'new': [failed, finished], 'yours': [finished]},
              'judgment': {'result': 'same', 'reason_kind': 'missed_request'}}]
    receipt = receipt_for(turns)
    assert receipt['cost_per_turn_new'] == .03 and receipt['tokens_new'] == 150
    assert receipt['steps_new'] == 3.0 and receipt['broken_new'] == 1
    assert receipt['speed_new_s'] == 3


def test_trial_7fde6e45_interval_decides_worse():
    stats = statistics_for(2, 9, 9)
    assert stats['word'] == 'worse'
    assert (stats['ci95_low'], stats['ci95_high']) == (-60, -3)
    assert stats['p'] == pytest.approx(.0654296875)


@pytest.mark.parametrize('intent,advice', [
    ('switch', 'Stay on your current model'),
    ('compare', 'GPT-6.1 Sol came out ahead'),
])
def test_trial_7fde6e45_card_advice(tmp_path, intent, advice):
    from test_cards import card_record
    from shadow import desktop_rpc as desktop
    from shadow.common.copy import load_copy
    from shadow.trial.cards import render_verdict
    from shadow.trial.presentation import verdict_presentation

    record = card_record(tmp_path, (2, 9, 9))
    record['intent'] = intent
    card = render_verdict(record, stats=True)
    assert f'[{advice}]' in card
    assert '-60% to -3%' in card
    assert 'about 7 times in 100' in card
    assert verdict_presentation(record)['advice'] == advice
    view = desktop._verdict(desktop.Copy(load_copy()), desktop.trial_view(record))
    assert view['word'] == advice


@pytest.mark.parametrize('second_look', [False, True])
@pytest.mark.parametrize('guard', [None, 'unpriceable', 'judge_unreachable', 'cap', 'rate_limited', 'judge_unsure', 'runs_varied'])
def test_single_judge_tags_match_the_shown_interval(second_look, guard):
    from desktop.dev.fixture_results import stats as fixture_stats

    z = 2.241 if second_look else 1.96
    for n in range(61):
        options = {'second_look': second_look, 'judge_agree': n, 'reruns': 0, 'varied': 0}
        if guard in {'unpriceable', 'cap', 'rate_limited'}:
            options.update(stop_reason=guard, judge_unreachable=True)
        if guard in {'unpriceable', 'judge_unsure', 'runs_varied'}:
            options.update(reruns=10, varied=6)
        if guard in {'unpriceable', 'judge_unsure'}:
            options['judge_agree'] = 0
        if guard == 'judge_unreachable':
            options['judge_unreachable'] = True
        if guard == 'unpriceable':
            reason = 'unpriceable'
        elif n < 20:
            reason = guard if guard in {'judge_unreachable', 'cap', 'rate_limited'} else 'few_usable'
        elif guard in {'judge_unsure', 'runs_varied'}:
            reason = guard
        else:
            reason = None
        for won in range(n + 1):
            for lost in range(n - won + 1):
                same = n - won - lost
                result = statistics_for(won, same, lost, **options)
                assert fixture_stats(won, same, lost, **options) == result
                d = (lost - won) / (n + 2)
                se = math.sqrt(lost + won + 1 - (lost - won) ** 2 / (n + 2)) / (n + 2)
                low, high = -d - z * se, -d + z * se
                off_zero = result['word'] in {'better', 'worse'}
                assert result['ci95_low'] == shown_pct(low, upper=False, keep_off_zero=off_zero)
                assert result['ci95_high'] == shown_pct(high, upper=True, keep_off_zero=off_zero)
                if reason:
                    assert result['word'] == 'not_enough'
                    assert result['cant_call_reason'] == reason
                    continue
                assert (result['word'] == 'better') == (low > 0)
                assert (result['word'] == 'worse') == (high < 0)
                if result['word'] == 'better':
                    assert result['ci95_high'] > 0
                    assert result['ci95_low'] > 0
                elif result['word'] == 'worse':
                    assert result['ci95_low'] < 0
                    assert result['ci95_high'] < 0
                else:
                    assert result['word'] in {'holds_up', 'too_close'}
                    assert low <= 0 <= high
                    assert result['ci95_low'] <= 0 <= result['ci95_high']
                    upper = d + (1.96 if second_look else 1.645) * se
                    assert (result['word'] == 'holds_up') == (upper <= .15)


@pytest.mark.parametrize('counts,first_word,second_word,second_interval', [
    ((10, 18, 2), 'better', 'better', (1, 49)),
    ((2, 18, 10), 'worse', 'worse', (-49, -1)),
    ((9, 19, 2), 'better', 'holds_up', (-1, 45)),
    ((2, 9, 9), 'worse', 'too_close', (-64, 1)),
])
def test_second_look_card_uses_the_wider_interval_and_level(tmp_path, counts, first_word, second_word, second_interval):
    from test_cards import card_record
    from shadow import desktop_rpc as desktop
    from shadow.common.copy import load_copy
    from shadow.trial.cards import render_verdict
    from shadow.trial.verdict import finalize

    record = card_record(tmp_path, counts)
    for second_look, word, level in [(False, first_word, '95'), (True, second_word, '97.5')]:
        record['second_look'] = second_look
        finalize(record)
        assert record['totals']['word'] == word
        if second_look:
            assert (record['totals']['ci95_low'], record['totals']['ci95_high']) == second_interval
        for cli in (False, True):
            card = render_verdict(record, cli=cli, stats=True)
            assert f'{level}% interval' in card
            assert f'({level}% CI' in card
            if second_look:
                assert '95% interval' not in card
                assert '(95% CI' not in card
        view = desktop._verdict(desktop.Copy(load_copy()), desktop.trial_view(record))
        assert view['maths']['rows'][1]['label'] == f'{level}% interval'
        assert f'({level}% CI' in view['bar']['label']


@pytest.mark.parametrize('second_look,advice,interval,word', [
    (False, 'Stay on your current model', '-60% to -3%', 'worse'),
    (True, 'Too close to call', '-64% to +1%', 'too_close'),
])
def test_saved_single_judge_cards_refresh_the_old_rule(tmp_path, second_look, advice, interval, word):
    from test_cards import card_record
    from test_plugin import service
    from shadow import desktop_rpc as desktop
    from shadow.common.copy import load_copy
    from shadow.desktop_source import ProfileSource

    _, plugin = service(tmp_path)
    record = card_record(tmp_path, (2, 9, 9))
    record['second_look'] = second_look
    record['totals'].update(word='too_close', cant_call_reason='too_close', second_look=second_look,
                            ci95_low=-60, ci95_high=-3)
    for turn in record['turns']:
        for runs in turn['runs'].values():
            for run in runs:
                run.pop('usage')
    plugin.store.save(record)
    card = plugin.command(f'results {record["id"]} --stats', terminal=True)
    assert f'[{advice}]' in card
    assert interval in card
    view = desktop.DesktopRpc(ProfileSource(plugin), load_copy()).page_state({'test_id': record['id']})['verdict']
    assert view['word'] == advice
    assert plugin.proposal(record['id']).verdict == word
    refreshed = plugin.verdict_view(plugin.store.get(record['id']))
    assert refreshed['totals']['receipt'] == record['totals']['receipt']
    assert plugin.store.get(record['id'])['totals']['ci95_high'] == -3


@pytest.mark.parametrize('second_look', [False, True])
def test_saved_backup_judging_keeps_its_panel_decision(tmp_path, second_look):
    from copy import deepcopy
    from test_panel import panel_record
    from test_plugin import service
    from shadow.trial.verdict import finalize

    _, plugin = service(tmp_path)
    votes = [('1', '1')] * 2 + [('same', 'same')] * 9 + [('2', '2')] * 9
    record = panel_record(tmp_path, [votes, votes])
    record['second_look'] = second_look
    finalize(record)
    totals, disagreements = deepcopy(record['totals']), deepcopy(record['judge_disagreements'])
    plugin.store.save(record)
    view = plugin.verdict_view(plugin.store.get(record['id']))
    assert view['totals'] == totals
    assert view['judge_disagreements'] == disagreements
    assert plugin.proposal(record['id']).verdict == totals['word']


def test_refresh_uses_the_same_panel_guard_as_finalize(tmp_path):
    from test_cards import card_record
    from shadow.trial.verdict import refresh_statistics

    record = card_record(tmp_path, (2, 9, 9))
    record['judge_mode'] = 'backup_both'
    record.pop('judge_policy', None)
    record['totals'].update(word='too_close', cant_call_reason='too_close')
    assert refresh_statistics(record)['totals']['word'] == 'worse'
    record['judge_policy'] = {'version': 2, 'swap_consistency_min': 70,
                              'bootstrap_seed': 1, 'bootstrap_samples': 100}
    assert refresh_statistics(record)['totals']['word'] == 'too_close'

def test_printed_interval_uses_decimals_when_a_whole_percent_would_be_zero():
    stats = statistics_for(8, 10, 2)
    assert stats['word'] == 'better'
    assert 0 < stats['ci95_low'] < 1
    assert stats['ci95_high'] > 0
    assert format_signed_pct(stats['ci95_low']) != '0%'
    stats = statistics_for(2, 10, 8)
    assert stats['word'] == 'worse'
    assert stats['ci95_low'] < 0
    assert -1 < stats['ci95_high'] < 0
    assert format_signed_pct(stats['ci95_high']) != '0%'


@pytest.mark.parametrize('second_look', [False, True])
def test_printed_bounds_never_contradict_better_or_worse(tmp_path, second_look):
    from test_cards import card_record
    from shadow import desktop_rpc as desktop
    from shadow.common.copy import load_copy
    from shadow.trial.cards import render_verdict
    from shadow.trial.verdict import finalize

    for n in (20, 30, 60):
        for won in range(n + 1):
            for lost in range(n - won + 1):
                same = n - won - lost
                stats = statistics_for(won, same, lost, second_look=second_look)
                if stats['word'] not in {'better', 'worse'}:
                    continue
                low, high = stats['ci95_low'], stats['ci95_high']
                assert format_signed_pct(low) != '0%' and format_signed_pct(high) != '0%'
                bar_low = 50 + shown_pct(stats['interval_low'] / 2, upper=False, keep_off_zero=True)
                bar_high = 50 + shown_pct(stats['interval_high'] / 2, upper=True, keep_off_zero=True)
                if stats['word'] == 'better':
                    assert low > 0 and high > 0
                    assert format_signed_pct(low).startswith('+') and format_signed_pct(high).startswith('+')
                    assert bar_low > 50 and bar_high > 50
                else:
                    assert low < 0 and high < 0
                    assert format_signed_pct(low).startswith('-') and format_signed_pct(high).startswith('-')
                    assert bar_low < 50 and bar_high < 50

    for counts, word in [((8, 10, 2), 'better'), ((2, 10, 8), 'worse')]:
        record = card_record(tmp_path, counts)
        record['second_look'] = second_look
        finalize(record)
        if record['totals']['word'] != word:
            continue
        low, high = record['totals']['ci95_low'], record['totals']['ci95_high']
        interval = f'{format_signed_pct(low)} to {format_signed_pct(high)}'
        card = render_verdict(record, stats=True)
        assert interval in card
        view = desktop._verdict(desktop.Copy(load_copy()), desktop.trial_view(record))
        shown = view['maths']['rows'][1]['value'][0]
        assert desktop.signed_pct(low) in shown and desktop.signed_pct(high) in shown
        assert desktop.signed_pct(low) != '0%' and desktop.signed_pct(high) != '0%'
        _assert_bar_stays_off_even(render_verdict(record), view['bar'], word)


def _assert_bar_stays_off_even(card, bar, word):
    import re
    assert bar['label'] in card
    match = re.search(r'CI ([0-9.]+)–([0-9.]+)%', bar['label'])
    assert match, bar['label']
    low, high = float(match.group(1)), float(match.group(2))
    assert low == bar['low'] and high == bar['high']
    if word == 'better':
        assert low > 50 and high > 50
    else:
        assert low < 50 and high < 50
