"""The Desktop page's RPC answers, using merged copy or isolated-checkout snapshots."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from shadow import desktop_rpc as d

SNAPSHOT = Path(__file__).parent / 'fixtures' / 'desktop' / 'copy'
try:
    from shadow.common.copy import load_copy
except ModuleNotFoundError as error:
    if error.name not in {'shadow.common', 'shadow.common.copy'}:
        raise
    COPY = d.fixture_copy((SNAPSHOT,))
else:
    COPY = load_copy()
SCENARIOS = sorted(p.stem for p in d.FIXTURES.glob('*.json'))


def rpc(scenario):
    return d.DesktopRpc(d.FixtureSource(scenario), COPY)


def text(rich):
    return ''.join(part if isinstance(part, str) else next(iter(part.values())) for part in rich)


def page(scenario, **params):
    return rpc(scenario).page_state(params, session_id=None)


def cells(verdict):
    return {cell['key']: (text(cell['value']), cell['sub']) for cell in verdict['receipt']}


def test_blind_turn_carries_no_names_judge_call_or_original_answer():
    r = rpc('result')
    test = r.source.test('b71c')
    for tid in [t['id'] for t in test['turns'] if t.get('usable')]:
        view = r.turn_open({'test_id': 'b71c', 'turn_id': tid}, session_id=None)
        blob = json.dumps(view)
        for leak in ('Hermes 4 70B', 'Hermes 4 405B', 'trying', 'yours'):
            assert leak not in blob, (tid, leak)
        turn = next(t for t in test['turns'] if t['id'] == tid)
        assert turn['judgment']['reason'] not in blob
        assert view['judge'] is None and view['at_the_time'] is None and not view['revealed']
        assert all(a['name'] is None for a in view['answers'])


def test_reveal_names_both_sides_and_gives_the_judge_reason():
    r = rpc('result')
    view = r.turn_reveal({'test_id': 'b71c', 'turn_id': 't01', 'pick': 2}, session_id=None)
    assert [text(a['name']) for a in view['answers']] == ['Hermes 4 70B, trying', 'Hermes 4 405B, yours']
    assert view['judge'] == 'The judge picked Answer 2: it keeps the 3 newest as asked; Answer 1 would delete all of them.'
    assert view['answers'][1]['picked'] and view['at_the_time']['label'] == 'The answer you got at the time'
    assert view['tally'] == 'You and the judge agreed on 1 of 1.'
    assert view['judge3'] == {'done': 1, 'of': 3, 'tally': None}


def test_judge3_tally_after_the_third_pick():
    r = rpc('result')
    r.turn_reveal({'test_id': 'b71c', 'turn_id': 't01', 'pick': 2}, session_id=None)
    r.turn_reveal({'test_id': 'b71c', 'turn_id': 't07', 'pick': 2}, session_id=None)
    view = r.turn_reveal({'test_id': 'b71c', 'turn_id': 't04', 'pick': 1}, session_id=None)
    assert view['judge3'] == {'done': 3, 'of': 3, 'tally': 'You and the judge agreed on 2 of 3.'}


def test_sides_stay_put_when_reopened():
    first = rpc('result-better').turn_open({'test_id': 'a3f2', 'turn_id': 't09'}, session_id=None)
    again = rpc('result-better').turn_open({'test_id': 'a3f2', 'turn_id': 't09'}, session_id=None)
    assert first['answers'] == again['answers']


def test_running_test_never_reveals():
    r = rpc('progress')
    view = r.turn_open({'test_id': 'a3f2', 'turn_id': 't01'}, session_id=None)
    assert not view['can_pick'] and view['tally'] is None
    with pytest.raises(ValueError):
        r.turn_reveal({'test_id': 'a3f2', 'turn_id': 't01', 'pick': 1}, session_id=None)


def test_progress_list_shows_states_not_results():
    p = page('progress')
    assert p['mode'] == 'progress' and p['verdict'] is None
    rows = p['turns']['groups'][0]['rows']
    assert [r['tag'] for r in rows] == ['Done'] * 5 + ['', 'Judging', 'Running', 'Running', 'Waiting', '']
    assert rows[5]['text'] == '… 13 more done' and rows[-1]['text'] == '… 8 more waiting'
    assert not any(word in json.dumps(p['turns']) for word in ('Won', 'Lost', 'Same'))
    assert p['progress']['heading'] == 'Testing: 18 of 30 turns done'
    assert p['progress']['sub'] == '$0.71 of $5.00 spent · about 10 minutes left · you can keep working'
    assert p['progress']['frac'] == 18 / 30
    assert p['status'] == 'Shadow · test 18 of 30'
    assert p['top']['try'] == {'label': 'Try a model', 'disabled': True, 'tip': 'Test a3f2 is still running.'}


GOLDEN = {
    'result-better': ('Better', 'Hermes 4 70B did better than your Hermes 4 405B on your turns.',
                      'If the two were really even, luck would give a gap this big about 2 times in 100.',
                      '15 · 11 · 4', '$0.014 vs $0.036', '8.6s vs 13.9s', '3.2 vs 3.8', '0 vs 1',
                      ['+36%', '+9% to +60%', 'p = 0.020', 'low end +13%']),
    'result': ('Holds up', 'Hermes 4 70B kept up with your Hermes 4 405B on your turns.',
               'Worst case this test leaves open: it loses 15 more turns than it wins, per 100.',
               '6 · 19 · 5', '$0.014 vs $0.036', '8.8s vs 13.7s', '3.4 vs 3.6', '1 vs 1',
               ['+3%', '−19% to +25%', 'p = 1.0', 'low end −15%']),
    'result-worse': ('Worse', 'Hermes 4 70B did worse than your Hermes 4 405B on your turns.',
                     'If the two were really even, luck would give a gap this big about 2 times in 1,000.',
                     '3 · 9 · 18', '$0.014 vs $0.036', '7.9s vs 13.8s', '4.6 vs 3.7', '7 vs 1',
                     ['−50%', '−71% to −23%', 'p = 0.0015', 'low end −67%']),
    'result-too-close': ('Too close to call', "These turns couldn't separate Hermes 4 70B from your Hermes 4 405B.",
                         'It leans toward the current model, but a gap this small is easily luck. Worst case this test leaves open: it loses '
                         '24 more turns than it wins, per 100. To call it a tie, the worst case must be 15 or fewer.',
                         '4 · 20 · 6', '$0.014 vs $0.036', '8.9s vs 13.6s', '3.5 vs 3.4', '1 vs 0',
                         ['−6%', '−27% to +14%', 'p = 0.76', 'low end −24%']),
    'result-not-enough': ('Not enough to go on', 'Stopped at your $5.00 cap after 19 usable turns. Shadow needs 20.',
                          'Too few turns to read a result from.',
                          '7 · 8 · 4', '$0.068 vs $0.172', '10.2s vs 15.4s', '3.1 vs 3.3', '1 vs 1',
                          ['+15%', '−18% to +47%', 'p = 0.55', 'low end −13%']),
}


@pytest.mark.parametrize('scenario', sorted(GOLDEN))
def test_verdict_strings_match_the_design_goldens(scenario):
    word, reason, sure, result, cost, speed, steps, broken, maths = GOLDEN[scenario]
    v = page(scenario)['verdict']
    tag = {'Better': 'Safe to switch', 'Holds up': 'Switch saves money', 'Worse': 'Stay on your current model'}.get(word, 'Too close to call')
    assert (v['word'], v['reason'], v['sure']) == (tag, reason, sure)
    assert v['headline'].startswith('Won ')
    assert v['table']['columns'][0] == 'Model'
    got = cells(v)
    assert [got[k][0] for k in ('result', 'cost', 'speed', 'steps', 'broken')] == [result, cost, speed, steps, broken]
    assert [text(row['value']).split(' ·')[0] for row in v['maths']['rows'][:4]] == maths


def test_every_receipt_number_equals_its_record():
    for scenario in GOLDEN:
        source = d.FixtureSource(scenario)
        test = source.tests()[0]
        t, r = test['totals'], test['totals']['receipt']
        got = cells(rpc(scenario).page_state({}, session_id=None)['verdict'])
        assert got['result'][0] == f"{t['won']} · {t['same']} · {t['lost']}"
        assert got['result'][1] == ['won · same · lost', f"of {t['pairs']} pairs"]
        assert got['cost'][0] == f"${r['cost_per_turn_new']:.3f} vs ${r['cost_per_turn_yours']:.3f}"
        assert got['cost'][1] == [f"−{r['saved_pct']}%"]
        assert got['speed'][0] == f"{r['speed_new_s']}s vs {r['speed_yours_s']}s"
        assert got['steps'][0] == f"{r['steps_new']} vs {r['steps_yours']}"
        assert got['broken'][0] == f"{r['broken_new']} vs {r['broken_yours']}"


def test_not_enough_is_faint_and_says_so():
    v = page('result-not-enough')['verdict']
    assert v['faint'] and v['caption'] == 'Hermes 4 70B vs your Hermes 4 405B, per turn · so far, too few to read'
    assert [row['warn'] for row in v['maths']['rows'][1:4]] == ['· too wide to use: under 20 pairs', '· not used under 20 pairs', '· not used under 20 pairs']
    assert v['facts'][:2] == ['Spent $5.00 of $5.00', '30 more turns would cost about $7.90, so they need a higher cap']


@pytest.mark.parametrize('scenario,buttons,quiet', [
    ('result-better', [('Switch gradually', True), ('Keep Hermes 4 405B', False)], None),
    ('result', [('Switch gradually', True), ('Keep Hermes 4 405B', False)], None),
    ('result-worse', [('Keep Hermes 4 405B', True)], 'Switch anyway'),
    ('result-too-close', [('Run 30 more', False), ('Keep Hermes 4 405B', False)], 'Switch anyway'),
    ('result-not-enough', [('Run 30 more', False), ('Keep Hermes 4 405B', False)], 'Switch anyway'),
])
def test_desktop_button_matrix(scenario, buttons, quiet):
    v = page(scenario)['verdict']
    assert [(b['label'], b['primary']) for b in v['buttons']] == buttons
    assert (v['quiet'] or {}).get('label') == quiet


def test_where_lost_leads_on_worse_and_too_close():
    for scenario, first in (('result-worse', 'Where it lost'), ('result-too-close', 'Where it lost'), ('result-better', 'Where it won')):
        assert page(scenario)['verdict']['maths']['rows'][6]['label'] == first


def test_backup_judging_is_labelled_on_the_page():
    p = page('result-backup')
    assert 'judged blind by both models (backup judging)' in text(p['top']['sub'])
    assert 'fine.contestant' in COPY
    assert COPY['fine.contestant'] in p['verdict']['facts']
    assert text(p['verdict']['maths']['rows'][4]['value']) == '26 of 30 pairs · both models judged both orders and agreed'


def test_result_list_orders_lost_won_same_and_folds_same():
    turns = page('result')['turns']
    assert [(g['label'], g['folded']) for g in turns['groups']] == [('Lost · 5', False), ('Won · 6', False), ('Same · 19', True)]
    assert (turns['title'], turns['count']) == ('Where they differed', '11 of 30')
    assert [r['text'] for r in turns['judge3']['rows']] == [
        'Clean up the old backup folders on the NAS, keep the last 3', 'Rename the photos in ~/Desktop/trip by date taken',
        'Why is the nightly cron for the price scraper not firing?']
    assert {r['kind'] for r in turns['judge3']['rows']} == {'pick'}
    assert turns['judge3']['intro'] == 'Picked at random from the 11 that differed'


def test_side_by_side_steps_mark_what_was_not_run():
    view = rpc('result').turn_open({'test_id': 'b71c', 'turn_id': 't01'}, session_id=None)
    first, second = view['answers']
    assert first['steps'][0]['kind'] == 'ran'
    assert text(first['steps'][0]['parts']) == 'Searched /Volumes/nas/backups · answered inside the test, 9 folders'
    assert second['steps'][0] == first['steps'][0]
    assert first['steps'][1] == {'kind': 'stop', 'label': 'It was about to run:', 'code': 'rmdir /Volumes/nas/backups/2026-*',
                                 'note': 'Not run in a test. The replay stopped here.'}
    assert second['steps'][2]['code'] == 'ls -1td /Volumes/nas/backups/*/ | tail -n +4 | xargs rm -rf'
    assert view['meta'] == 'Tue, Nov 3 · from Hermes Desktop'


@pytest.mark.parametrize('status', ['error', 'unknown'])
def test_legacy_guard_error_shows_the_stop_box(status):
    r = rpc('result')
    call = r.source.test('b71c')['turns'][0]['runs']['new'][-1]['tool_calls'][1]
    call.clear()
    call.update(name='terminal', args={'command': 'rmdir /Volumes/nas/backups/2026-*'}, status=status, broken=False)
    view = r.turn_open({'test_id': 'b71c', 'turn_id': 't01'}, session_id=None)
    step = view['answers'][0]['steps'][1]
    assert step == {'kind': 'stop', 'label': 'It was about to run:',
                    'code': 'rmdir /Volumes/nas/backups/2026-*',
                    'note': 'Not run in a test. The replay stopped here.'}
    assert 'ran for real' not in json.dumps(view['answers'])


@pytest.mark.parametrize('status', ['ok', 'error', 'unknown'])
def test_answered_reads_do_not_claim_a_guard_veto(status):
    r = rpc('result')
    call = r.source.test('b71c')['turns'][0]['runs']['new'][-1]['tool_calls'][0]
    call.clear()
    call.update(name='read_file', args={'path': 'notes.txt'}, status=status, broken=False)
    view = r.turn_open({'test_id': 'b71c', 'turn_id': 't01'}, session_id=None)
    step = view['answers'][0]['steps'][0]
    assert step['kind'] == 'ran'
    assert text(step['parts']) == 'Read notes.txt · answered inside the test'
    assert 'ran for real' not in json.dumps(view['answers'])


def test_switch_panel_numbers():
    p = page('switch')
    s = p['switch']
    assert p['mode'] == 'switch' and p['status'] == 'Shadow · step 2 of 3 · half the time'
    assert s['heading'] == 'Step 2 of 3: Hermes 4 70B answers half the time'
    assert s['sub'] == '11 of 20 of its turns done toward all. Last turn answered by Hermes 4 70B.'
    assert [b['label'] for b in s['buttons']] == ['Move to all now', 'Undo'] and not any(b['primary'] for b in s['buttons'])
    assert [(m['label'], m['value'], m['of']) for m in s['meters']['rows']] == [
        ('Turns with trouble', '1', 'of 4'), ('Gap above Hermes 4 405B', '2', 'of 20 points'), ('Failed requests in a row', '0', 'of 3')]
    assert s['trouble']['rows'] == [['Hermes 4 70B', '31', '1 of 31'], ['Hermes 4 405B', '211', '3 of 211']]
    assert s['rail']['fill'] == pytest.approx(1.55) and [n['state'] for n in s['rail']['nodes']] == ['origin', 'done', 'now', 'todo', 'todo']
    assert [r['when'] for r in s['log']['rows']] == ['Nov 12, 2:14 PM', 'Nov 12, 9:02 AM', 'Nov 10, 4:20 PM']
    assert s['log']['rows'][2]['text'] == 'Started at 1 time in 10 from test b71c (Holds up, won 6, same 19, lost 5)'
    assert text(p['top']['sub']) == 'Switch from test b71c (Holds up) · started Nov 10, 4:20 PM · Nous Portal'
    assert p['top']['picker']['current'] == ''


def test_paused_switch_shows_the_way_back_and_the_tripped_meter_at_its_line():
    p = page('switch-paused')
    s = p['switch']
    assert s['heading'] == "You're back on Hermes 4 405B. The switch is paused."
    assert [b['label'] for b in s['buttons']] == ['Resume at 1 time in 10', 'Stop the switch']
    assert s['rail']['fill'] is None and s['meters']['rows'][0]['frac'] == 1.0
    assert p['status'] == 'Shadow · paused, on Hermes 4 405B'


def test_watch_panel():
    p = page('watch')
    s = p['switch']
    assert p['mode'] == 'watch' and p['status'] == 'Shadow · watching Qwen 3.5 72B · 9 of 20'
    assert s['heading'] == 'Watching Qwen 3.5 72B: 9 of 20 turns, no trouble yet'
    assert [(m['value'], m['of']) for m in s['meters']['rows']] == [('0', 'of 5'), ('0', 'of 3')]
    assert s['trouble']['note'].startswith('No comparison with Hermes 4 405B here')
    assert text(p['top']['sub']) == 'Switch from test f20d (Better) · Nov 10, 4:40 PM · all at once, because Qwen 3.5 72B is on OpenRouter'
    tripped = page('watch-tripped')['switch']
    assert tripped['heading'] == "You're back on Hermes 4 405B, and it's your default again."
    assert [b['label'] for b in tripped['buttons']] == ['Switch again', 'Stay on Hermes 4 405B']


def test_empty_state_is_idle_and_carries_the_tagline():
    p = page('empty')
    assert p['mode'] == 'empty' and p['status'] is None and text(p['top']['sub']) == 'Your own work decides.'
    assert p['empty']['title'] == 'No tests yet' and p['empty']['web']['title'] == 'Web lookups in tests: On'
    assert [b['label'] for b in p['empty']['buttons']] == ['Try a model', 'Run the setup check']


def test_start_card_and_judge_change():
    r = rpc('result')
    model = r.source.world['start']['try']
    card = r.start_card({'model': model}, session_id=None)
    rows = {row['key']: text(row['value']) for row in card['rows']}
    assert card['title'] == 'Try Hermes 4 70B against your Hermes 4 405B'
    assert rows['judge'] == 'Judged blind by DeepSeek V3.2 (auto). It compares each pair without knowing which model wrote which.'
    assert rows['cost'] == 'About $1.90, capped at $5.00. The judge is about $0.40.'
    assert rows['sent'].startswith('Nous Portal only: both models and the judge.')
    assert [(o['name'], o['auto'], o['cost'], o['selected']) for o in card['judge']['options']] == [
        ('DeepSeek V3.2', '(auto)', 'about $0.40', True), ('Kimi K2', None, 'about $0.55', False),
        ('Qwen 3.5 72B', None, 'about $0.35', False), ('GLM 4.6', None, 'about $0.45', False)]
    assert card['judge']['footer'] == "Hermes models aren't listed. The judge has to come from a maker that made neither model being compared."
    picked = r.start_card({'model': model, 'judge': 'qwen/qwen-3.5-72b'}, session_id=None)
    rows = {row['key']: text(row['value']) for row in picked['rows']}
    assert rows['judge'].startswith('Judged blind by Qwen 3.5 72B, your pick.')
    assert rows['cost'] == 'About $1.85, capped at $5.00. The judge is about $0.35.'
    assert rows['sent'].startswith('Nous Portal, OpenRouter:')
    assert [(b['label'], b['primary']) for b in picked['buttons']] == [
        ('Start', True), ('Compare', False), ('See the 30', False), ('Cancel', False)]


def test_unreachable_pick_falls_back_to_auto_and_says_so():
    source = d.FixtureSource('result')
    source.world['start']['facts']['judges'][1]['unreachable'] = True
    card = d.DesktopRpc(source, COPY).start_card({'model': source.world['start']['try'], 'judge': 'moonshot/kimi-k2'}, session_id=None)
    assert card['notice'] == "Kimi K2 can't be reached. Judged blind by DeepSeek V3.2 (auto) instead."
    assert next(o for o in card['judge']['options'] if o['selected'])['name'] == 'DeepSeek V3.2'


def test_model_picker_is_alphabetical_with_no_marks():
    names = [m['name'] for m in rpc('empty').start_models({}, session_id=None)['models']]
    assert names == sorted(names, key=str.lower)


@pytest.mark.parametrize('other_blocker', ['cap', 'too_few', 'no_price'])
def test_isolation_message_takes_priority_over_other_start_blockers(other_blocker):
    r = rpc('empty')
    original = r.source.start_facts
    changes = {'cap': {'cap': 0}, 'too_few': {'picked': 0}, 'no_price': {'priced': False, 'tokens': 1000}}
    r.source.start_facts = lambda model: {**original(model), **changes[other_blocker], 'isolation_verified': False}

    card = r.start_card({'model': r.source.world['start']['try']})

    assert card['blocked'] == COPY['trial.isolation_required']
    assert 'start' not in [button['id'] for button in card['buttons']]
    assert 'wait' not in [row['key'] for row in card['rows']]


def test_missing_isolation_fact_never_offers_start():
    r = rpc('empty')
    original = r.source.start_facts

    def omit_isolation(model):
        facts = original(model)
        facts.pop('isolation_verified', None)
        return facts

    r.source.start_facts = omit_isolation
    card = r.start_card({'model': r.source.world['start']['try']})

    assert card['blocked'] == COPY['trial.isolation_required']
    assert 'start' not in [button['id'] for button in card['buttons']]
    assert 'wait' not in [row['key'] for row in card['rows']]


def test_settings_and_actions_round_trip():
    r = rpc('switch')
    assert r.settings_set({'web_lookups': False}, session_id=None)['top']['web']['word'] == 'Off'
    with pytest.raises(ValueError):
        r.settings_set({'web_lookups': 'no'}, session_id=None)
    with pytest.raises(ValueError):
        r.act({'action': 'rm -rf'}, session_id=None)
    assert r.act({'action': 'undo', **r.page_state({})['switch']['identity']}, session_id=None)['page']['mode'] == 'result'


def test_register_names_and_session_event():
    class Ctx:
        def __init__(self):
            self.rpc, self.events = {}, []

        def register_rpc(self, name, handler):
            self.rpc[name] = handler

        def emit_to_session(self, session_id, event, payload=None):
            self.events.append((session_id, event, payload))
            return True

    ctx = Ctx()
    d.register(ctx, d.FixtureSource('empty'), COPY)
    assert sorted(ctx.rpc) == sorted(['page.state', 'turn.open', 'turn.reveal', 'start.models', 'start.card', 'settings.set', 'act'])
    assert ctx.rpc['page.state']({}, session_id='local-1')['mode'] == 'empty'
    assert d.page_changed(ctx, 'local-1', mode='progress', test_id='a3f2')
    assert ctx.events == [('local-1', 'page.changed', {'mode': 'progress', 'test_id': 'a3f2', 'switch_id': None})]
    assert not d.page_changed(ctx, None, mode='progress')


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_every_scenario_renders_and_is_json(scenario, tmp_path):
    json.dumps(page(scenario), allow_nan=False)


def test_dump_states_covers_every_scenario(tmp_path):
    written = d.dump_states(tmp_path, COPY)
    assert sorted(p.stem for p in written) == SCENARIOS


def test_desktop_keys_are_namespaced_and_unique():
    own = json.loads((Path(d.__file__).parent / 'copy' / 'desktop.json').read_text())
    assert all(key.startswith('desktop.') for key in own)
    assert not set(own) & set(d.fixture_copy((SNAPSHOT,))) - set(own)


def test_real_copy_table_has_every_key_once_merged(tmp_path):
    loader = pytest.importorskip('shadow.common.copy')
    d.dump_states(tmp_path, loader.load_copy())


@pytest.mark.parametrize('usable', [18, 22])
def test_cancel_preserves_finished_turns_without_automatic_verdict(usable):
    source = d.FixtureSource('progress')
    running = source.tests()[0]
    complete = [t for t in running['turns'] if t['state'] == 'done']
    waiting = [t for t in running['turns'] if t['state'] == 'waiting']
    while len(complete) < usable:
        item = {**deepcopy(complete[0]), 'id': waiting.pop(0)['id']}
        complete.append(item)
    running['turns'] = complete[:usable] + waiting[:30 - usable]
    for i, turn in enumerate(complete):
        turn['judgment']['result'] = 'won' if i < 6 else 'lost' if i < 11 else 'same'
    r = d.DesktopRpc(source, COPY)
    stopped = r.act({'action': 'stop_test', 'test_id': r.source.tests()[0]['id']})['page']
    assert stopped['mode'] == 'stopped' and stopped['verdict'] is None
    assert running['totals'] is None
    assert stopped == r.page_state({})
    assert str(usable) in stopped['stopped']['summary']
    assert {b['id'] for b in stopped['stopped']['buttons']} == (
        {'get_result', 'delete_test'} if usable >= 20 else {'turns', 'delete_test'})
    rows = stopped['turns']['groups'][0]['rows']
    assert len(rows) == usable and all(t['open'] for t in rows)
    opened = r.turn_open({'test_id': running['id'], 'turn_id': rows[0]['id']})
    assert not opened['revealed']
    if usable >= 20:
        saved = deepcopy(d.FixtureSource('result').tests()[0]['totals'])
        source.finalize_fixture = lambda record: record.update(totals=saved)
        result = r.act({'action': 'get_result', 'test_id': running['id']})['page']
        assert result['mode'] == 'result'
        assert running['state'] == 'cancelled'
        assert running['totals'] == saved
        assert r.page_state({})['mode'] == 'result'
    else:
        with pytest.raises(ValueError):
            r.act({'action': 'get_result', 'test_id': running['id']})
        assert r.page_state({})['mode'] == 'stopped'
    assert r.act({'action': 'delete_test', 'test_id': running['id']})['page']['mode'] == 'empty'


@pytest.mark.parametrize('state', ['cancelled'])
def test_unfinalized_stopped_record_without_totals_stays_readable(state):
    source = d.FixtureSource('progress')
    test = source.tests()[0]
    test['state'] = state
    del test['totals']
    stopped = d.DesktopRpc(source, COPY).page_state({})
    assert stopped['mode'] == 'stopped' and stopped['verdict'] is None
    assert len(stopped['turns']['groups'][0]['rows']) == 18


def test_get_result_is_only_available_for_usable_stopped_turns():
    source = d.FixtureSource('result-better')
    test = source.tests()[0]
    r = d.DesktopRpc(source, COPY)
    with pytest.raises(ValueError):
        r.act({'action': 'get_result', 'test_id': test['id']})
    test['state'], test['totals'] = 'cancelled', None
    for turn in test['turns'][19:]:
        turn['state'], turn['usable'] = 'unusable', False
    stopped = r.page_state({})
    assert {b['id'] for b in stopped['stopped']['buttons']} == {'turns', 'delete_test'}
    with pytest.raises(ValueError):
        r.act({'action': 'get_result', 'test_id': test['id']})
    assert test['totals'] is None


@pytest.mark.parametrize('scenario', ['switch', 'watch', 'progress'])
def test_history_picker_can_always_return_to_current_activity(scenario):
    source = d.FixtureSource(scenario)
    history = d.FixtureSource('result').tests()[0]
    if source.test(history['id']) is None:
        source.world['tests'].append(history)
    r = d.DesktopRpc(source, COPY)
    current = r.page_state({})
    assert current['top']['picker']['current'] == ''
    assert current['top']['picker']['options'][0] == {'id': '', 'label': current['status']}
    past = r.page_state({'test_id': history['id']})
    assert past['mode'] == 'result' and past['top']['picker']['current'] == history['id']
    assert past['top']['picker']['options'][0]['id'] == ''
    assert r.page_state({'test_id': ''})['mode'] == current['mode']


@pytest.mark.parametrize('word', ['better', 'holds_up'])
@pytest.mark.parametrize('other_provider', [False, True])
@pytest.mark.parametrize('cheaper', [False, True])
def test_switch_action_matches_quality_before_cost_or_speed(word, other_provider, cheaper):
    source = d.FixtureSource('result-better' if word == 'better' else 'result')
    test = source.tests()[0]
    test['trial']['provider'] = 'openrouter' if other_provider else test['current']['provider']
    test['trial']['provider_name'] = 'OpenRouter' if other_provider else test['current']['provider_name']
    receipt = test['totals']['receipt']
    receipt.update(cost_per_turn_new=.014 if cheaper else .05, saved_pct=61 if cheaper else None,
                   more_pct=None if cheaper else 39, speed_new_s=20)
    buttons = d.DesktopRpc(source, COPY).page_state({})['verdict']['buttons']
    switch = next(b for b in buttons if b['id'] == ('switch_all' if other_provider else 'switch'))
    assert switch['primary'] is (word == 'better' or cheaper)
    assert next(b for b in buttons if b['id'] == 'keep')['primary'] is (word == 'holds_up' and not cheaper)


def test_holds_up_equal_cost_and_speed_keeps_keep_primary():
    source = d.FixtureSource('result')
    receipt = source.tests()[0]['totals']['receipt']
    receipt.update(cost_per_turn_new=.036, saved_pct=0, more_pct=0, speed_new_s=13.7)
    buttons = d.DesktopRpc(source, COPY).page_state({})['verdict']['buttons']
    assert [(b['id'], b['primary']) for b in buttons] == [('keep', True), ('switch', False)]


def test_no_third_maker_discloses_both_judges_costs_and_saves_backup_start():
    source = d.FixtureSource('empty')
    source.world['start']['facts']['judges'] = []
    model = source.world['start']['try']
    facts = source.start_facts(model)
    assert facts['judge_mode'] == 'backup_both'
    assert [j['model'] for j in facts['judge_models']] == [model, facts['current']['model']]
    r = d.DesktopRpc(source, COPY)
    card = r.start_card({'model': model})
    rows = {row['key']: text(row['value']) for row in card['rows']}
    assert rows['judge'] == COPY['start.judge_contestant']
    assert rows['cost'] == 'About $2.25, capped at $5.00. The judge is about $0.75.'
    assert card['judge'] is None and card['judge_slug'] is None
    assert card['judge_mode'] == 'backup_both'
    assert card['judge_models'] == [model, facts['current']['model']]
    assert rows['sent'].startswith('Nous Portal only:')
    started = r.act(start_params(r, model))['page']
    test = source.test(started['test_id'])
    assert started['mode'] == 'progress'
    assert test['judge_mode'] == 'backup_both' and test['judge_source'] == 'auto'
    assert [m['model'] for m in test['judge']['models']] == card['judge_models']
    assert test['totals'] is None


def test_backup_judging_lists_every_destination_and_cannot_be_picked():
    source = d.FixtureSource('empty')
    source.world['start']['facts']['judges'] = []
    model = 'qwen/qwen-3.5-72b'
    r = d.DesktopRpc(source, COPY)
    card = r.start_card({'model': model})
    sent = next(row for row in card['rows'] if row['key'] == 'sent')
    assert text(sent['value']).startswith('OpenRouter, Nous Portal:')
    with pytest.raises(ValueError):
        r.start_card({'model': model, 'judge': model})
    with pytest.raises(ValueError):
        r.act({'action': 'start', 'model': model, 'judge': model})
    assert source.tests() == []


MODEL_START_COSTS = {
    'nousresearch/hermes-4-70b-fp8-2026-08-14': ('DeepSeek V3.2', '$1.90'),
    'deepseek/deepseek-v3.2': ('Kimi K2', '$1.75'),
    'moonshot/kimi-k2': ('DeepSeek V3.2', '$2.05'),
    'qwen/qwen-3.5-72b': ('DeepSeek V3.2', '$1.75'),
    'zhipu/glm-4.6': ('DeepSeek V3.2', '$1.80'),
}


@pytest.mark.parametrize('model', MODEL_START_COSTS)
def test_every_offered_model_has_eligible_judges_and_its_own_estimate(model):
    r = rpc('empty')
    offered = {m['slug'] for m in r.start_models({})['models']}
    assert offered == set(MODEL_START_COSTS) and model in offered
    card = r.start_card({'model': model})
    facts = r.source.start_facts(model)
    excluded = {facts['trial']['maker'], facts['current']['maker']}
    by_model = {j['model']: j for j in facts['judges']}
    assert all(by_model[o['slug']]['maker'] not in excluded for o in card['judge']['options'])
    expected_judge, expected_cost = MODEL_START_COSTS[model]
    assert next(o['name'] for o in card['judge']['options'] if o['selected']) == expected_judge
    rows = {row['key']: text(row['value']) for row in card['rows']}
    assert rows['cost'].startswith(f'About {expected_cost},')
    assert expected_judge in rows['judge']
    expected_destinations = ('Nous Portal, OpenRouter:' if facts['trial']['provider'] == 'openrouter'
                             else 'Nous Portal only:')
    assert rows['sent'].startswith(('OpenRouter, Nous Portal:' if facts['trial']['provider'] == 'openrouter'
                                    else expected_destinations))
    started = r.act(start_params(r, model))['page']
    record = r.source.test(started['test_id'])
    assert record['trial']['model'] == model
    assert record['judge']['models'][0]['maker'] not in excluded
    assert record['judge']['models'][0]['name'] == expected_judge
    assert record['judge_source'] == 'auto'


def test_contestant_and_same_maker_judges_are_refused_before_start():
    source = d.FixtureSource('empty')
    source.world['start']['facts']['judges'].append({**source.world['start']['current'], 'cost': .45})
    r = d.DesktopRpc(source, COPY)
    model = 'deepseek/deepseek-v3.2'
    card = r.start_card({'model': model})
    assert 'DeepSeek' in card['judge']['footer'] and 'Hermes' in card['judge']['footer']
    for judge in (model, source.world['start']['current']['model']):
        with pytest.raises(ValueError):
            r.start_card({'model': model, 'judge': judge})
        with pytest.raises(ValueError):
            r.act({'action': 'start', 'model': model, 'judge': judge})
    assert source.tests() == []


def test_deleting_an_older_record_never_reuses_an_existing_start_id():
    r = rpc('result-better')
    model = r.source.world['start']['try']
    first = r.act(start_params(r, model))['page']['test_id']
    r.act({'action': 'stop_test', 'test_id': first})
    r.act({'action': 'delete_test', 'test_id': 'a3f2'})
    second = r.act(start_params(r, model))['page']['test_id']
    assert first != second
    assert len({t['id'] for t in r.source.tests()}) == 2


def test_unreachable_auto_uses_next_eligible_judge_then_backup_if_none_reachable():
    source = d.FixtureSource('empty')
    judges = source.world['start']['facts']['judges']
    judges[0]['unreachable'] = True
    r = d.DesktopRpc(source, COPY)
    model = source.world['start']['try']
    card = r.start_card({'model': model, 'judge': judges[0]['model']})
    assert card['judge_models'] == ['moonshot/kimi-k2']
    assert card['notice'] == "DeepSeek V3.2 can't be reached. Judged blind by Kimi K2 (auto) instead."
    for judge in judges:
        judge['unreachable'] = True
    backup = r.start_card({'model': model})
    assert backup['judge_mode'] == 'backup_both' and backup['judge'] is None
    assert backup['judge_models'] == [model, source.world['start']['current']['model']]


def start_params(r, model, **extra):
    card = r.start_card({'model': model, **extra})
    return {'action': 'start', 'model': model, 'judge': card['judge_slug'],
            **{key: card[key] for key in ('judge_mode', 'judge_models', 'estimate', 'consent')}}


@pytest.mark.parametrize('intent', ['switch', 'compare'])
def test_confirmed_fixture_desktop_start_begins_a_running_trial(intent):
    r = rpc('empty')
    model = r.source.world['start']['try']
    assert r.source.world['tests'] == []
    card = r.start_card({'model': model})
    assert any(button['id'] == 'start' for button in card['buttons'])

    reply = r.act({**start_params(r, model), 'intent': intent})

    assert len(r.source.world['tests']) == 1
    record = r.source.world['tests'][0]
    assert record['state'] == 'running'
    assert record['intent'] == intent
    assert record['trial']['model'] == model
    assert reply['page']['test_id'] == record['id']
    assert reply['page']['mode'] == 'progress'
    assert reply['notice'] is None
    assert 'start_card' not in reply


@pytest.mark.parametrize('change', ['auto_unreachable', 'backup', 'cost', 'web'])
def test_start_refreshes_consent_without_running_when_card_changes(change):
    r = rpc('empty')
    model = r.source.world['start']['try']
    params = start_params(r, model)
    facts = r.source.world['start']['facts']
    if change == 'auto_unreachable':
        facts['judges'][0]['unreachable'] = True
    elif change == 'backup':
        facts['judges'] = []
    elif change == 'cost':
        facts['replay_estimate'] += 1
    else:
        r.source.set_web_lookups(False)
    reply = r.act(params)
    assert r.source.tests() == []
    refreshed = r.start_card({'model': model})
    assert reply['start_card'] == refreshed
    assert refreshed['consent'] != params['consent']
    assert r.act(start_params(r, model))['page']['mode'] == 'progress'


def test_start_without_consent_returns_card_and_does_not_run():
    r = rpc('empty')
    reply = r.act({'action': 'start', 'model': r.source.world['start']['try']})
    assert reply['start_card']['title']
    assert not r.source.tests()


@pytest.mark.parametrize('action', ['move_on', 'undo', 'resume', 'stop_switch', 'switch_again', 'stay'])
@pytest.mark.parametrize('changed', ['id', 'phase', 'step', 'epoch'])
def test_switch_actions_refresh_stale_display_identity_without_acting(action, changed):
    r = rpc('switch')
    switch = r.source.world['switch']
    params = {'action': action, **r.page_state({})['switch']['identity']}
    switch[changed] = {'id': 'new-switch', 'phase': 'done', 'step': 3, 'epoch': 1}[changed]
    before = deepcopy(r.source.world)
    assert r.methods()['act'](params) == {'page': r.page_state({}), 'notice': None}
    assert r.source.world == before


def test_stop_targets_only_the_displayed_trial_and_refuses_replaced_work():
    r = rpc('progress')
    test = r.source.tests()[0]
    stale_id = test['id']
    test['id'] = 'new-test'
    assert r.methods()['act']({'action': 'stop_test', 'test_id': stale_id}) == {
        'page': r.page_state({}), 'notice': None}
    assert test['state'] == 'running'
    r.act({'action': 'stop_test', 'test_id': test['id']})
    assert test['state'] == 'cancelled'


def test_switch_panel_supplies_identity_and_source_enforces_it():
    r = rpc('switch')
    panel = r.page_state({})['switch']
    assert panel['identity'] == {'switch_id': 's1', 'phase': 'running', 'step': 2, 'epoch': 0}
    with pytest.raises(ValueError):
        r.source.act('move_on', {})
    r.source.act('move_on', panel['identity'])
    assert r.source.world['switch']['step'] == 3


@pytest.mark.parametrize('phase', [None, 'done'])
def test_undo_without_identity_cannot_change_an_inactive_switch(phase):
    r = rpc('switch' if phase else 'result')
    if phase:
        r.source.world['switch']['phase'] = phase
    before = deepcopy(r.source.world)
    assert r.methods()['act']({'action': 'undo'}) == {'page': r.page_state({}), 'notice': None}
    assert r.source.world == before


@pytest.mark.parametrize('usable', [18, 22])
def test_stopped_summary_uses_the_usable_count(usable):
    r = rpc('result')
    record = r.source.tests()[0]
    record['state'], record['totals'] = 'cancelled', None
    for turn in record['turns'][usable:]:
        turn['state'], turn['usable'] = 'unusable', False
    stopped = r.page_state({'test_id': record['id']})['stopped']
    assert f'{usable} finished turns' in stopped['summary']
    assert str(usable) in stopped['buttons'][0]['label']


@pytest.mark.parametrize('on', [True, False])
def test_web_lookup_fine_print_reads_the_trial_record_field(on):
    r = rpc('result')
    record = r.source.tests()[0]
    record.pop('web_lookup_turns', None)
    record.update(web_turns=7, web_lookups=on)
    facts = r.page_state({})['verdict']['facts']
    expected = ('Web lookups ran for real on 7 turns, same pages for both' if on else
                'Web lookups were off, so 7 turns that needed one stopped there, the same way for both models.')
    assert expected in facts


def test_revealed_pick_is_immutable_and_retries_return_the_saved_view():
    r = rpc('result')
    params = {'test_id': 'b71c', 'turn_id': 't01', 'pick': 2}
    saved = r.turn_reveal(params)
    before = deepcopy(r.source.world)
    for pick in (1, 'same', 2):
        assert r.turn_reveal({**params, 'pick': pick}) == saved
        assert r.source.world == before
    assert r.turn_open(params) == saved


@pytest.mark.parametrize('extra', [{}, {'pick': None}])
def test_reveal_requires_a_pick_before_exposing_the_judge(extra):
    r = rpc('result')
    params = {'test_id': 'b71c', 'turn_id': 't01'}
    before = deepcopy(r.source.world)
    with pytest.raises(ValueError):
        r.turn_reveal({**params, **extra})
    assert r.source.world == before
    view = r.turn_open(params)
    assert not view['revealed'] and view['judge'] is None


def test_settings_change_keeps_the_selected_historical_result():
    r = rpc('result')
    older = deepcopy(r.source.tests()[0])
    older.update(id='older-test', created_at='2026-10-01T16:12:00')
    r.source.world['tests'].append(older)
    page = r.settings_set({'web_lookups': False, 'test_id': older['id']})
    assert page['test_id'] == older['id']
    assert page['top']['picker']['current'] == older['id']
    assert page['top']['web']['word'] == 'Off'


@pytest.mark.parametrize('boundary', ['rpc', 'source'])
def test_consent_refresh_keeps_the_viewed_historical_result(boundary):
    r = rpc('result')
    older = deepcopy(r.source.tests()[0])
    older.update(id='older-test', created_at='2026-10-01T16:12:00')
    r.source.world['tests'].append(older)
    params = {**start_params(r, r.source.world['start']['try']), 'test_id': older['id']}
    r.source.set_web_lookups(False)
    before = deepcopy(r.source.tests())
    if boundary == 'rpc':
        reply = r.act(params)
        assert reply['page']['test_id'] == older['id']
        assert reply['page']['top']['picker']['current'] == older['id']
    else:
        reply = r.source.act('start', {k: v for k, v in params.items() if k != 'action'})
        assert reply['test_id'] == older['id']
    assert reply['start_card']['consent'] != params['consent']
    assert r.source.tests() == before


@pytest.mark.parametrize('change', ['removed', 'backup'])
@pytest.mark.parametrize('boundary', ['rpc', 'source'])
def test_disappearing_user_judge_refreshes_consent_instead_of_trapping_start(change, boundary):
    r = rpc('empty')
    model = r.source.world['start']['try']
    params = start_params(r, model, judge='moonshot/kimi-k2')
    assert params['judge'] == 'moonshot/kimi-k2'
    facts = r.source.world['start']['facts']
    facts['judges'] = [] if change == 'backup' else [j for j in facts['judges'] if j['model'] != params['judge']]
    reply = (r.act(params) if boundary == 'rpc' else
             r.source.act('start', {k: v for k, v in params.items() if k != 'action'}))
    assert r.source.tests() == []
    card = reply['start_card']
    assert card == r.start_card({'model': model})
    assert card['consent'] != params['consent']
    assert r.act(start_params(r, model))['page']['mode'] == 'progress'
