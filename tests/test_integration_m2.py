"""Combined-package checks for the switch, trial, and desktop lines."""
import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext, selection
from shadow.common.copy import copy, load_copy
from shadow.common.models import Model
from shadow.desktop_rpc import DesktopRpc, FixtureSource
from shadow.switch import Engine, Proposal, Store
from shadow.switch.copy import short_name, status
from shadow.switch.domain import SwitchRecord, is_active
from shadow.switch.plugin import SwitchPlugin
from test_plugin import service
from test_switch_safety import NEW, OLD
from test_switch_states import turn


def test_trial_uses_switch_is_active_instead_of_a_watching_literal(tmp_path, monkeypatch):
    ctx, plugin = service(tmp_path)
    engine = Engine(Store(sqlite3.connect(':memory:')))
    record = SwitchRecord('switch-1', 'saved', OLD, NEW, 'all_at_once', phase='watch')
    with engine.store.transaction(create=True) as holder:
        holder[0] = record
    ctx.shadow_switch = SimpleNamespace(engine=engine)
    plugin.selector = lambda **_: pytest.fail('an active watch must refuse before turn selection')
    assert is_active(engine.record)
    assert ctx.commands['shadow'](f'try {TRIAL.model}') == 'A switch is in progress. Finish it or undo it first.'

    with engine.store.transaction() as holder:
        holder[0].phase = 'watching'
    assert not is_active(engine.record)
    plugin.selector = lambda **kw: selection(tmp_path, kw.get('count', 30))
    text = ctx.commands['shadow'](f'try {TRIAL.model}')
    assert 'A switch is in progress' not in text
    assert 'Try GPT-6 Luna' in text

    with engine.store.transaction() as holder:
        holder[0].phase = 'watch'
    monkeypatch.setattr('shadow.switch.is_active', lambda _record: False)
    refused = ctx.commands['shadow'](f'try {TRIAL.model}')
    assert 'A switch is in progress' not in refused


def test_arm_labels_use_the_switch_short_name_on_trial_and_desktop():
    old, new = short_name('old'), short_name('new')
    assert Model('fixture', 'old').name == old
    assert Model('fixture', 'new').name == new
    from shadow.desktop_rpc import model_view
    assert model_view({'provider': 'fixture', 'model': 'old'})['name'] == old
    assert model_view({'provider': 'fixture', 'model': 'new'})['name'] == new
    table = load_copy()
    assert '{old}' in table['switch.status.paused']
    assert '{new}' in table['switch.status.watch']
    assert '{old}' in table['desktop.switch.paused']
    assert 'Old' not in table['switch.status.paused']
    assert 'New' not in table['desktop.switch.heading']
    record = SwitchRecord('switch-1', 'b71c', OLD, NEW, 'gradual', phase='paused')
    source = FixtureSource('result')
    source.world['switch'] = record.to_dict()
    page = DesktopRpc(source, table).page_state({})
    assert page['switch']['heading'] == copy('desktop.switch.paused', old=old)
    assert page['status'] == copy('desktop.status.paused', old=old)
    assert old in status(record)
    other = 'old' if old != 'old' else 'Old'
    assert other not in page['switch']['heading']
    assert other not in status(record)


def test_desktop_step_rows_come_from_panel_history(tmp_path):
    engine = Engine(Store(sqlite3.connect(':memory:')), verified_pair=lambda *_: True)
    engine.start(Proposal('b71c', OLD, NEW, 'better'), switch_id='switch-1', step_turns=3)
    turn(engine, 'one')
    engine.advance()
    for index in range(3):
        turn(engine, f'auto-{index}')
    service_switch = SwitchPlugin(None, engine, config_reader=lambda: {})
    panel = service_switch.panel()['history']
    with engine.store.transaction() as holder:
        holder[0].step_turns = 99
        for event in holder[0].history:
            event['decoy_turns'] = 99
    source = FixtureSource('result')
    source.world['switch'] = engine.record.to_dict()
    rows = DesktopRpc(source, load_copy()).page_state({})['switch']['log']['rows']
    texts = [row['text'] for row in rows]
    steps = [event for event in panel if event['kind'] in {'step2', 'step3'}]
    assert [(event['kind'], event['turns'], event['manual'], event['reason']) for event in steps] == [
        ('step2', 1, True, None), ('step3', 3, False, None)]
    assert texts[0] == 'Moved to all the time after 3 of its turns with no trip'
    assert texts[1] == 'Moved to half the time after 1 of its turns with no trip · you moved it on'
    assert 'after 99' not in ' '.join(texts)
    assert 'you moved it on' not in texts[0]
    assert any(text.startswith('Started at 1 time in 10') for text in texts)


def _panel_record(reason, *, backup=False):
    source = FixtureSource('result')
    record = source.tests()[0]
    record['judge_policy'] = {'version': 2, 'swap_consistency_min': 70}
    if backup:
        record['judge_mode'] = 'backup_both'
    record['totals'].update(word='not_enough', cant_call_reason=reason, second_look=False,
                            win_rate=0.62, win_rate_ci95_low=0.48, win_rate_ci95_high=0.71)
    record['judge_disagreements'] = {'count': 4, 'kappa': 0.22, 'kappa_method': 'cohen',
                                     'low_panel_agreement': True,
                                     'eligible_judges': [] if reason == 'judge_ineligible' else [0, 1]}
    return source, record


def _verdict(source, record):
    return DesktopRpc(source, load_copy()).page_state({'test_id': record['id']})['verdict']


@pytest.mark.parametrize('reason', ['panel_uncertain', 'judge_ineligible'])
def test_single_judge_page_keeps_the_ordinary_lines_when_a_policy_is_saved(reason):
    source, record = _panel_record(reason)
    verdict = _verdict(source, record)
    assert verdict['sure'] == copy('verdict.reason.' + reason)
    assert 'likely wins' not in verdict['sure']
    record['totals']['second_look'] = True
    second = _verdict(source, record)
    assert second['second_look'] == copy('fine.second', pairs=record['totals']['pairs'])
    assert 'judged the same way' not in second['second_look']
    assert second['maths']['method'] == copy('maths.method')
    facts = ' '.join(verdict['facts'])
    assert 'Backup judging:' not in facts
    assert copy('fine.low_panel_agreement') in facts
    labels = [button['label'] for button in verdict['buttons']]
    if reason == 'panel_uncertain':
        assert labels == ['Run 30 more', 'Keep Hermes 4 405B']
    else:
        assert 'Run 30 more' not in labels


@pytest.mark.parametrize('reason', ['panel_uncertain', 'judge_ineligible'])
def test_desktop_backup_page_keeps_the_contestant_sentence_and_panel_lines(reason):
    source, record = _panel_record(reason, backup=True)
    verdict = _verdict(source, record)
    assert verdict['sure'] == copy('sure.bootstrap', low=48, high=71)
    assert 'luck would' not in verdict['sure']
    facts = ' '.join(verdict['facts'])
    assert copy('fine.contestant') in facts
    assert copy('fine.low_panel_agreement') in facts
    assert '0.220 (cohen kappa)' in facts
    assert verdict['maths']['method'] == copy('maths.panel_method', level='95')
    assert 'Sign test:' not in verdict['maths']['method']
    record['totals'].update(second_look=True, bootstrap_level='97.5')
    second = _verdict(source, record)
    assert second['second_look'] == copy('fine.panel_second', pairs=record['totals']['pairs'])
    assert 'The bar is higher' not in second['second_look']
    assert second['maths']['method'] == copy('maths.panel_method', level='97.5')
    record['totals'].update(second_look=False, bootstrap_level='95')
    labels = [button['label'] for button in _verdict(source, record)['buttons']]
    if reason == 'panel_uncertain':
        assert labels == ['Run 30 more', 'Keep Hermes 4 405B']
    else:
        assert labels == ['Keep Hermes 4 405B']


def test_unknown_price_keeps_the_token_line_and_refuses_start(tmp_path):
    from shadow.common.storage import Store as TrialStore
    from shadow.trial.cards import render_start
    from shadow.trial.engine import TrialEngine
    from shadow.trial.safety import ReplayGuard

    unlisted = Model('openai-codex', 'gpt-unlisted', plan=True)
    engine = TrialEngine(FakeContext(), TrialStore(tmp_path / 'trial.sqlite'), ReplayGuard())
    record = engine.prepare(selection(tmp_path, 20), CURRENT, unlisted, [OUTSIDE])
    assert not record['can_start']
    assert record['estimate']['priced'] is False
    assert record['estimate']['total'] is None
    assert record['estimate']['contestants'] is None
    tokens = record['estimate']['tokens']
    assert type(tokens) is int and tokens > 0
    line = copy('start.tokens', tokens=tokens)
    reason = copy('start.no_price', model=unlisted.name)
    text = render_start(record, isolation_verified=True)
    assert line in text and reason in text and '$' not in text and '[Start]' not in text

    class Facts:
        def start_facts(self, model):
            return {'trial': {'model': unlisted.model, 'name': unlisted.name, 'provider': unlisted.provider,
                              'provider_name': unlisted.provider},
                    'current': {'model': CURRENT.model, 'name': CURRENT.name, 'provider': CURRENT.provider,
                                'provider_name': CURRENT.provider},
                    'judges': [{'model': OUTSIDE.model, 'name': OUTSIDE.name, 'maker': 'Anthropic',
                                'provider_name': OUTSIDE.provider, 'cost': 0.2, 'auto': True}],
                    'judge_mode': 'third_maker', 'picked': 20, 'total': 20, 'days': 7, 'left_out': {},
                    'scrub': {'keys': 0, 'emails': 0}, 'web_lookups': True, 'cap': 5, 'minutes': 20,
                    'isolation_verified': True,
                    'hidden_maker': 'OpenAI', 'replay_estimate': 9.5, 'priced': False, 'tokens': tokens}

    card = DesktopRpc(Facts(), load_copy()).start_card({'model': unlisted.model})
    cost = next(row['value'] for row in card['rows'] if row['key'] == 'cost')
    shown = ''.join(part if isinstance(part, str) else next(iter(part.values())) for part in cost)
    assert shown == line
    assert card['blocked'] == reason
    assert not any(button['id'] == 'start' for button in card['buttons'])
    known = engine.prepare(selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE], cap=.01)
    assert known['estimate']['priced'] is True and known['estimate']['total'] > .01
    assert not known['can_start']


def test_desktop_registration_reads_saved_tests_from_the_profile_data_dir(tmp_path, monkeypatch):
    import shadow
    db_path = tmp_path / 'plugin-data' / 'shadow' / 'trial.sqlite'
    db_path.parent.mkdir(parents=True)

    def open_db(*_args):
        return sqlite3.connect(db_path, check_same_thread=False)

    names = ('hermes_constants', 'hermes_cli', 'hermes_cli.config', 'plugins', 'plugins.plugin_storage',
             'gateway', 'gateway.session_context')
    modules = {name: ModuleType(name) for name in names}
    modules['hermes_constants'].get_hermes_home = lambda: str(tmp_path)
    modules['hermes_cli.config'].read_config_doc = lambda _path: {}
    modules['plugins.plugin_storage'].plugin_db = open_db
    modules['plugins.plugin_storage'].plugin_data_dir = lambda _name: db_path.parent
    modules['gateway.session_context'].get_session_env = lambda _key, default='': default
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    ctx = FakeContext()
    shadow.register(ctx)
    fixture = Path(__file__).parents[1] / 'desktop' / 'fixtures' / 'result.json'
    saved = deepcopy(json.loads(fixture.read_text())['tests'][0])
    saved['id'] = 'saved-from-profile'
    saved['created_at'] = 1_700_000_000.0
    ctx.shadow_trial.store.save(saved)
    assert db_path.exists() and db_path.stat().st_size > 0
    page = ctx.rpc['page.state']({})
    assert page['mode'] == 'result'
    assert page['test_id'] == 'saved-from-profile'
    assert page['verdict']['word']


def _register_both(tmp_path, monkeypatch, env=None):
    import shadow
    env = {} if env is None else env
    names = ('hermes_constants', 'hermes_cli', 'hermes_cli.config', 'plugins', 'plugins.plugin_storage',
             'gateway', 'gateway.session_context')
    modules = {name: ModuleType(name) for name in names}
    modules['hermes_constants'].get_hermes_home = lambda: str(tmp_path)
    modules['hermes_cli.config'].read_config_doc = lambda _path: {}
    modules['plugins.plugin_storage'].plugin_db = lambda *_args: sqlite3.connect(':memory:')
    modules['plugins.plugin_storage'].plugin_data_dir = lambda _name: tmp_path
    modules['gateway.session_context'].get_session_env = lambda key, default='': env.get(key, default)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    ctx = FakeContext()
    shadow.register(ctx)
    return ctx


def test_ordinary_pre_llm_call_reaches_switch_admit_and_fails_closed(tmp_path, monkeypatch, caplog):
    ctx = _register_both(tmp_path, monkeypatch)
    admitted = []
    engine = ctx.shadow_switch.engine
    original = engine.admit

    def spy(*args, **event):
        admitted.append(event)
        return original(*args, **event)

    engine.admit = spy
    assert ctx.shadow_trial.engine is None
    with caplog.at_level('DEBUG', logger='shadow.switch'):
        result = ctx.hook('pre_llm_call', session_id='ordinary',
                          user_message='Please explain the code.', conversation_history=[])
    assert result == [None, None]
    assert admitted and 'task_id' not in admitted[0]
    assert engine.record is None
    assert any('failed closed' in record.message for record in caplog.records)


def test_admit_still_runs_when_session_recovery_raises(tmp_path, monkeypatch):
    from shadow.switch import Proposal
    from test_switch_states import CROSS

    now = [100.0]
    monkeypatch.setattr('shadow.switch.engine.monotonic', lambda: now[0])
    env = {'HERMES_SESSION_ID': 'watch-chat', 'HERMES_SESSION_KEY': 'cli:watch-chat'}
    ctx = _register_both(tmp_path, monkeypatch, env)
    engine = ctx.shadow_switch.engine

    def fail_writer(_route):
        raise RuntimeError('fixture config unavailable')

    engine.set_default = fail_writer
    with pytest.raises(RuntimeError, match='fixture config unavailable'):
        engine.start(Proposal('saved', OLD, CROSS, 'better'))
    now[0] += 30
    ctx.hook('pre_llm_call', session_id='watch-chat', task_id='task-1', turn_id='turn-1',
             model=CROSS.model, platform='cli', user_message='hello')
    saved = list(engine.record.turns.values())
    assert len(saved) == 1
    assert saved[0].turn_id == engine.store.identity('turn-1')


def test_progress_fixture_finalizes_without_usage_keyerror():
    from copy import deepcopy
    source = FixtureSource('progress')
    running = source.tests()[0]
    done = [turn for turn in running['turns'] if turn['state'] == 'done']
    waiting = [turn for turn in running['turns'] if turn['state'] == 'waiting']
    while len(done) < 22:
        item = deepcopy(done[0])
        item['id'] = waiting.pop(0)['id']
        done.append(item)
    running['turns'] = done[:22] + waiting[:8]
    for index, turn in enumerate(done[:22]):
        turn['judgment']['result'] = 'won' if index < 6 else 'lost' if index < 11 else 'same'
        assert 'usage' not in turn['runs']['new'][0]
    source.finalize_fixture = None
    rpc = DesktopRpc(source, load_copy())
    rpc.act({'action': 'stop_test', 'test_id': running['id']})
    page = rpc.act({'action': 'get_result', 'test_id': running['id']})['page']
    assert page['mode'] == 'result'
    assert running['totals']['pairs'] == 22
