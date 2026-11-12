import argparse
import json
import re
import unicodedata
from pathlib import Path

import pytest

from fixtures import OUTSIDE, TRIAL
from shadow import desktop_rpc as d
from shadow.common.copy import load_copy
from shadow.common.storage import Store
from shadow.trial.cards import render_start, render_verdict
from test_cards import card_record
from test_plugin import service


COSTS = {
    'less': (.01, .02, 50, None),
    'saving_ten_percent': (.018, .02, 10, None),
    'sub_percent': (.01992, .02, 0, None),
    'same': (.02, .02, 0, 0),
    'more': (.03, .02, None, 50),
    'unknown': (None, .02, None, None),
    'free': (0, 0, None, None),
    'small_premium': (.0201, .02, None, 1),
    'estimated': (.01, .02, 50, None),
    'saving_nine_percent': (.0182, .02, 9, None),
    'plan': (.01, .02, 50, None),
    'plan_new': (.01, .02, 50, None),
    'plan_yours': (.01, .02, 50, None),
    'plan_new_premium': (.03, .02, None, 50),
    'plan_yours_premium': (.03, .02, None, 50),
    'modest_premium': (.021, .02, None, 5),
    'sub_percent_current': (.02008, .02, None, 1),
}
TAGS = {
    'switch': {
        'better': ['Safe to switch'] * 4 + ['{trial} came out ahead'] * 2 + ['Safe to switch'] * 2 + ['{trial} came out ahead'] + ['Safe to switch'] * 3 + ['{trial} came out ahead', 'Safe to switch'] + ['{trial} came out ahead'] * 2 + ['Safe to switch'],
        'worse': ['Stay on your current model'] * len(COSTS),
        'holds_up': ['Switch saves money'] * 3 + ['Safe to switch'] + ['Even match'] * 2 + ['Safe to switch'] * 2 + ['Even match', 'Switch saves money'] + ['Safe to switch'] * 2 + ['Even match', 'Safe to switch'] + ['Even match'] * 2 + ['Safe to switch'],
        'too_close': ['Too close to call'] * len(COSTS),
        'not_enough': ['Too close to call'] * len(COSTS),
    },
    'compare': {
        'better': ['{trial} came out ahead'] * len(COSTS),
        'worse': ['{current} came out ahead'] * len(COSTS),
        'holds_up': ['{trial} matched it for less'] * 3 + ['Even match', '{current} matched it for less'] + ['Even match'] * 2 + ['{current} matched it for less', 'Even match', '{trial} matched it for less'] + ['Even match'] * 5 + ['{current} matched it for less'] * 2,
        'too_close': ['Too close to call'] * len(COSTS),
        'not_enough': ['Too close to call'] * len(COSTS),
    },
}
CASES = [(intent, outcome, cost, tags[index])
         for intent, outcomes in TAGS.items() for outcome, tags in outcomes.items()
         for index, cost in enumerate(COSTS)]


def desktop_verdict(record):
    source = d.FixtureSource('empty')
    source.world['tests'] = [record]
    return d.DesktopRpc(source, load_copy()).page_state({})['verdict']


@pytest.mark.parametrize('intent,outcome,cost,expected', CASES)
def test_tags_on_terminal_and_desktop(tmp_path, intent, outcome, cost, expected):
    record = card_record(tmp_path)
    record['intent'] = intent
    record['totals'].update(word=outcome, cant_call_reason='few_usable' if outcome == 'not_enough' else None)
    new, yours, saved, more = COSTS[cost]
    record['totals']['receipt'].update(plan_new=False, plan_yours=False, cost_per_turn_new=new, cost_per_turn_yours=yours,
                                       saved_pct=saved, more_pct=more,
                                       cost_mode='not_reported' if cost == 'estimated' else 'unknown_price' if new is None else 'list_price')
    if cost == 'plan':
        record['totals']['receipt'].update(cost_mode='plan', plan_new=True, plan_yours=True)
    elif cost.startswith('plan_'):
        side = 'new' if cost.startswith('plan_new') else 'yours'
        record['totals']['receipt']['plan_' + side] = True
    names = d.trial_view(record)
    expected = expected.format(trial=names['trial']['name'], current=names['current']['name'])
    terminal = render_verdict(record, cli=True)
    payload = desktop_verdict(record)
    assert '[' + expected + ']' in terminal
    assert payload['word'] == expected
    hint = 'Run more turns to separate them.' if outcome == 'too_close' else None
    assert payload['advice_hint'] == hint
    assert ('Run more turns to separate them.' in terminal) == bool(hint)
    if intent == 'compare':
        assert 'switch' not in payload['word'].lower()
        assert all(not button['primary'] for button in payload['buttons'] if button['id'] in {'switch', 'switch_all'})
        assert not payload['note'] or 'switch' not in payload['note'].lower()
        assert 'so the switch' not in terminal


@pytest.mark.parametrize('outcome,expected', [('better', 'Safe to switch'), ('holds_up', 'Safe to switch'),
                                             ('worse', 'Stay on your current model')])
def test_old_records_read_as_switch_on_both_surfaces(tmp_path, outcome, expected):
    record = card_record(tmp_path)
    record.pop('intent', None)
    record['totals']['word'] = outcome
    store = Store(tmp_path / 'legacy.sqlite')
    store.save(record)
    loaded = store.get(record['id'])
    assert loaded['intent'] == 'switch'
    assert store.list()[0]['intent'] == 'switch'
    assert '[' + expected + ']' in render_verdict(loaded, cli=True)
    assert desktop_verdict(loaded)['word'] == expected


@pytest.mark.parametrize('new,current', [(.01, .009), (.02, .018), (.03, .027), (.02008, .02)])
def test_compare_recognizes_any_lower_current_model_cost(tmp_path, new, current):
    record = card_record(tmp_path)
    record['intent'] = 'compare'
    record['totals']['word'] = 'holds_up'
    record['totals']['receipt'].update(cost_mode='list_price', plan_new=False, plan_yours=False,
                                       cost_per_turn_new=new, cost_per_turn_yours=current,
                                       saved_pct=None, more_pct=12)
    expected = desktop_verdict(record)['table']['rows'][1][0] + ' matched it for less'
    assert '[' + expected + ']' in render_verdict(record, cli=True)
    assert desktop_verdict(record)['word'] == expected


@pytest.mark.parametrize('outcome,fallback', [('better', '{trial} came out ahead'), ('holds_up', 'Even match')])
@pytest.mark.parametrize('new,more,same_band', [(.0202, 1, True), (.02021, 2, False), (.0222, 11, False)])
def test_switch_premium_tolerance_stops_at_one_percent(tmp_path, outcome, fallback, new, more, same_band):
    record = card_record(tmp_path)
    record['intent'] = 'switch'
    record['totals']['word'] = outcome
    record['totals']['receipt'].update(cost_mode='list_price', plan_new=False, plan_yours=False,
                                       cost_per_turn_new=new, cost_per_turn_yours=.02,
                                       saved_pct=None, more_pct=more)
    payload = desktop_verdict(record)
    expected = 'Safe to switch' if same_band else fallback.format(trial=payload['table']['rows'][0][0])
    assert '[' + expected + ']' in render_verdict(record, cli=True)
    assert payload['word'] == expected


@pytest.mark.parametrize('terminal', [False, True])
@pytest.mark.parametrize('verb,intent', [('try', 'switch'), ('compare', 'compare')])
def test_start_commands_store_intent(tmp_path, capsys, terminal, verb, intent):
    ctx, plugin = service(tmp_path)
    ctx.answers = ['cancel']
    if terminal:
        setup, handler = ctx.cli['shadow']
        parser = argparse.ArgumentParser()
        setup(parser)
        text = handler(parser.parse_args([verb, TRIAL.model, '--judge', OUTSIDE.model]))
    else:
        text = ctx.commands['shadow'](f'{verb} {TRIAL.model} --judge {OUTSIDE.model}')
    assert 'Try GPT-6 Luna' in (capsys.readouterr().out if terminal else text)
    record = plugin.store.get()
    assert record['intent'] == intent
    assert record['judge_source'] == 'user'
    assert 'compare' in plugin.ctx.shadow_command_routes


def test_compare_start_recovery_commands_keep_intent(tmp_path):
    record = card_record(tmp_path)
    record.update(intent='compare', picked=19)
    assert 'hermes shadow compare ' in render_start(record, cli=True)
    record.update(picked=30, cap=.01)
    assert 'hermes shadow compare ' in render_start(record, cli=True)


def test_tag_copy_excludes_banned_words(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checked = 0
    banned = re.compile(r'\b(?:maths|better|worse|holds up|worth switching|winner|crushed|upgrade)\b|Switch-worthy', re.I)
    for path in (Path(__file__).resolve().parents[1] / 'shadow' / 'copy').rglob('*.json'):
        for key, value in json.loads(path.read_text()).items():
            if key.startswith('verdict.advice.'):
                checked += 1
                assert not banned.search(value), (path, key, value)
                assert not any(unicodedata.category(char) == 'So' for char in value), (path, key, value)

    assert checked > 0


@pytest.mark.parametrize('reason', ['few_usable', 'cap', 'judge_unsure', 'panel_uncertain'])
def test_insufficient_evidence_keeps_existing_desktop_actions(tmp_path, reason):
    record = card_record(tmp_path)
    record['totals'].update(word='not_enough', cant_call_reason=reason)
    record['intent'] = 'switch'
    switch = desktop_verdict(record)
    record['intent'] = 'compare'
    compare = desktop_verdict(record)
    assert compare['reason'] == switch['reason']
    assert compare['buttons'] == switch['buttons']
    assert compare['quiet'] == switch['quiet']


@pytest.mark.parametrize('outcome', ['better', 'holds_up', 'worse', 'too_close', 'not_enough'])
def test_cross_provider_compare_does_not_recommend_a_switch(tmp_path, outcome):
    record = card_record(tmp_path)
    record['intent'] = 'compare'
    record['trial']['provider'] = 'openrouter'
    record['routes']['trial']['provider'] = 'openrouter'
    record['totals'].update(word=outcome, cant_call_reason='few_usable' if outcome == 'not_enough' else None)
    payload = desktop_verdict(record)
    assert payload['note'] is None
    assert not any(button['primary'] for button in payload['buttons'] if button['id'] == 'switch_all')
    assert 'so the switch' not in render_verdict(record, cli=True)


@pytest.mark.parametrize('intent', ['switch', 'compare'])
@pytest.mark.parametrize('second,panel,expected', [
    (False, None, True), (True, None, False),
    (False, {'eligible_judges': []}, False),
    (False, {'eligible_judges': [0]}, True),
])
def test_extra_turn_hint_requires_an_available_second_look(tmp_path, intent, second, panel, expected):
    record = card_record(tmp_path, (4, 20, 6))
    record['intent'] = intent
    record['totals']['second_look'] = second
    if panel is not None:
        record['judge_disagreements'] = dict(panel, low_panel_agreement=False)
    terminal = render_verdict(record, cli=True)
    payload = desktop_verdict(record)
    assert ('Run more turns to separate them.' in terminal) == expected
    assert payload['advice_hint'] == ('Run more turns to separate them.' if expected else None)
    assert any(button['id'] == 'run_more' for button in payload['buttons']) == expected


@pytest.mark.parametrize('outcome,no_advantage,reason', [
    ('better', False, '{trial} led {current} on these turns.'),
    ('worse', False, '{current} led {trial} on these turns.'),
    ('holds_up', False, '{trial} and {current} were within the quality tie margin on these turns.'),
    ('holds_up', True, '{trial} and {current} were within the quality tie margin on these turns, with no cost or speed advantage for {trial}.'),
    ('too_close', False, "These turns couldn't separate {trial} from {current}."),
])
def test_compare_reasons_name_both_models_without_an_incumbent(tmp_path, outcome, no_advantage, reason):
    record = card_record(tmp_path)
    record['intent'] = 'compare'
    record['totals'].update(word=outcome, cant_call_reason=None)
    if no_advantage:
        receipt = record['totals']['receipt']
        receipt.update(cost_per_turn_new=receipt['cost_per_turn_yours'], saved_pct=0,
                       speed_new_s=receipt['speed_yours_s'])
    payload = desktop_verdict(record)
    expected = reason.format(trial=payload['table']['rows'][0][0], current=payload['table']['rows'][1][0])
    assert expected in ' '.join(render_verdict(record, cli=True).split())
    assert payload['reason'] == expected
    assert not re.search(r"\b(?:your|better|worse)\b", payload['reason'], re.I)


@pytest.mark.parametrize('lean,toward', [('better', 'the trial model'), ('worse', 'the current model'), ('neither', None)])
def test_too_close_receipt_explains_the_tie_rule_in_current_copy(tmp_path, lean, toward):
    record = card_record(tmp_path, (4, 20, 6))
    record['totals']['lean'] = lean
    terminal = render_verdict(record, cli=True)
    sure = desktop_verdict(record)['sure']
    rule = 'To call it a tie, the worst case must be 15 or fewer.'
    assert rule in terminal and rule in sure
    if toward:
        assert f'It leans toward {toward}, but a gap this small is easily luck.' in terminal
        assert f'It leans toward {toward}, but a gap this small is easily luck.' in sure
    assert not re.search(r'\b(?:better|worse|holds up)\b', terminal, re.I)
    assert not re.search(r'\b(?:better|worse|holds up)\b', sure, re.I)
