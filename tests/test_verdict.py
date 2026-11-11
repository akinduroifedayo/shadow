import pytest

from shadow.trial.verdict import statistics_for


@pytest.mark.parametrize('w,s,l,word,p,upper', [
    (15,11,4,'better',.019,None), (10,18,2,'better',.039,None),
    (14,11,5,'holds_up',.064,-.066), (9,19,2,'holds_up',.065,-.052),
    (6,19,5,'holds_up',1,.147), (3,24,3,'holds_up',1,.136),
    (0,30,0,'holds_up',1,.051), (4,22,4,'too_close',1,.154),
    (4,20,6,'too_close',.754,.232), (2,19,9,'too_close',.065,.385),
    (3,9,18,'worse',.0015,None), (7,8,4,'not_enough',.55,None),
])
def test_every_spec_table_row(w,s,l,word,p,upper):
    stats = statistics_for(w,s,l)
    assert stats['word'] == word
    assert f'{stats["p"]:.2g}' == f'{p:.2g}'
    if upper is not None:
        assert round(stats['U'], 3) == upper


@pytest.mark.parametrize('w,s,l,p,worst', [
    (15,11,4,'0.020',None), (10,18,2,'0.039',None),
    (14,11,5,'0.064',0), (9,19,2,'0.066',0),
    (6,19,5,'1.0',15), (3,24,3,'1.0',14),
    (0,30,0,'1.0',6), (4,22,4,'1.0',16),
    (4,20,6,'0.76',24), (2,19,9,'0.066',39),
    (3,9,18,'0.0015',None), (7,8,4,'0.55',None),
])
def test_spec_numbers_round_cautiously_on_the_card(tmp_path,w,s,l,p,worst):
    from test_cards import card_record
    from shadow.trial.cards import render_verdict
    text = render_verdict(card_record(tmp_path, (w,s,l)), stats=True)
    assert f'Sign test              {p}' in text
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
    assert statistics_for(10,18,2,second_look=True)['word'] == 'holds_up'
    assert statistics_for(6,19,5)['word'] == 'holds_up'
    assert statistics_for(6,19,5,second_look=True)['word'] == 'too_close'
    assert statistics_for(2,18,10,second_look=True)['word'] == 'too_close'


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
