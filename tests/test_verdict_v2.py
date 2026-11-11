import pytest

from shadow.trial.cards import render_verdict
from shadow.trial.presentation import ADVICE_KEYS, verdict_presentation
from test_cards import card_record


@pytest.mark.parametrize('word,tag', [
    ('better', 'Switch-worthy'), ('worse', 'Keep your current model'),
    ('holds_up', 'Safe to switch'), ('too_close', 'Too close to call'),
    ('not_enough', 'Too close to call'),
])
def test_every_verdict_has_a_small_advice_tag(tmp_path, word, tag):
    record = card_record(tmp_path)
    record['totals']['word'] = word
    assert verdict_presentation(record)['advice'] == tag
    assert set(ADVICE_KEYS) == {'better', 'worse', 'holds_up', 'too_close', 'not_enough'}


def test_numbers_lead_and_interval_bar_fits_terminal(tmp_path):
    record = card_record(tmp_path, (1, 21, 8))
    record['totals'].update(win_rate=.38, win_rate_ci95_low=.25, win_rate_ci95_high=.49)
    text = render_verdict(record, cli=True)
    assert text.splitlines()[0] == 'Won 1, lost 8, tied 21 of 30 turns · 61% cheaper per turn'
    assert 'win rate 38% (95% CI 25–49%)' in text
    assert any('│' in line and '█' in line and '▒' in line for line in text.splitlines())
    assert all(len(line) <= 80 for line in text.splitlines())


def test_unknown_cost_and_latency_are_omitted(tmp_path):
    record = card_record(tmp_path, (20, 0, 10))
    receipt = record['totals']['receipt']
    receipt.update(cost_mode='unknown_price', cost_per_turn_new=None, speed_new_s=None)
    view = verdict_presentation(record)
    assert view['headline'] == 'Won 20, lost 10 of 30 turns'
    assert 'Cost per turn' not in view['table']['columns']
    assert 'Speed per turn' not in view['table']['columns']


def test_more_expensive_headline_uses_recorded_percentage(tmp_path):
    record = card_record(tmp_path)
    record['totals']['receipt'].update(saved_pct=None, more_pct=27)
    assert verdict_presentation(record)['headline'].endswith('27% more expensive per turn')


@pytest.mark.parametrize('saved_pct,more_pct', [(0, None), (None, 0), (0, 0)])
def test_equal_cost_omits_the_headline_cost_clause(tmp_path, saved_pct, more_pct):
    record = card_record(tmp_path)
    record['totals']['receipt'].update(cost_per_turn_new=.02, cost_per_turn_yours=.02,
                                      saved_pct=saved_pct, more_pct=more_pct)
    assert verdict_presentation(record)['headline'] == 'Won 15, lost 4, tied 11 of 30 turns'


@pytest.mark.parametrize('rate', [.49, .5, .51])
def test_point_at_midpoint_cell_keeps_a_visible_midpoint(tmp_path, rate):
    record = card_record(tmp_path, (10, 10, 10))
    record['totals'].update(win_rate=rate, win_rate_ci95_low=.35, win_rate_ci95_high=.65)
    text = render_verdict(record, cli=True)
    bar, axis = text.splitlines()[1:3]
    assert bar[20] == '╋'
    assert axis[20:25] == '│ 50%'
    assert all(len(line) <= 80 for line in text.splitlines())
