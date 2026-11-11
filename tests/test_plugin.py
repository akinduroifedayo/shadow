import argparse
import importlib.util
import json
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext, selection, set_judge_pick
from shadow.common.storage import Store
from shadow.trial.cards import render_start, render_verdict, receipt_columns
from shadow.trial.controller import ControllerLauncher, IsolationRequired
from shadow.trial.engine import TrialEngine
from shadow.trial.plugin import CommandError, TrialPlugin, register
from shadow.trial.safety import ReplayGuard
from shadow.trial.verdict import finalize


class FixtureLauncher:
    isolation_verified = True

    def __init__(self):
        self.launched = []

    def launch(self, record):
        self.launched.append(record['id'])
        return 123


def service(tmp_path):
    ctx = FakeContext()
    changed = threading.Event()
    class ProgressStore(Store):
        def save(self, record):
            super().save(record)
            changed.set()
    def wait_for_change(_):
        assert changed.wait(10)
        changed.clear()
    plugin = TrialPlugin(ctx, store=ProgressStore(tmp_path / 'trial.sqlite'), current=CURRENT,
                         catalog=[CURRENT, TRIAL, OUTSIDE], launcher=FixtureLauncher(),
                         selector=lambda **kw: selection(tmp_path, kw.get('count', 30)), watch_wait=wait_for_change,
                         config_reader=lambda: {'delegation': {'inherit_mcp_toolsets': False}})
    register(ctx, plugin=plugin)
    return ctx, plugin


def finish_controller(ctx, plugin, record):
    ctx.hook('pre_llm_call', session_id='controller', user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}')
    for _ in range(256):
        if not json.loads(plugin.step({}, session_id='controller'))['continue']:
            return 123
    pytest.fail('fixture controller exceeded its step bound')


def test_registered_plugin_idle_full_turn_creates_no_data(tmp_path):
    ctx = FakeContext()
    plugin = TrialPlugin(ctx, store=Store(tmp_path / 'trial.sqlite'))
    register(ctx, plugin=plugin)
    ctx.hook('on_session_start', session_id='ordinary', source='cli')
    ctx.hook('pre_llm_call', session_id='ordinary', user_message='Please explain the code.', conversation_history=[])
    ctx.hook('post_api_request', session_id='ordinary', api_request_id='r1', response_model=CURRENT.model, usage={'prompt_tokens': 50})
    ctx.hook('post_tool_call', session_id='ordinary', tool_name='terminal', args={})
    ctx.hook('on_session_end', session_id='ordinary', completed=True)
    assert not ctx.capture and not ctx.messages and plugin.engine is None
    assert not (tmp_path / 'trial.sqlite').exists()
    assert ctx.hook('pre_llm_call', session_id='ordinary', user_message=[{'type': 'text', 'text': 'An ordinary attachment.'}]) == [None]


def test_chat_start_controller_and_results_end_to_end(tmp_path):
    ctx, plugin = service(tmp_path)
    ctx.answers = ['start']
    text = ctx.commands['shadow']('try gpt-6-luna --judge claude-sonnet-4-5', session_id='origin')
    assert 'Try GPT-6 Luna' in text
    record = plugin.store.get()
    assert record['state'] == 'queued' and record['judge_source'] == 'user'
    ctx.hook('pre_llm_call', session_id='controller', user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}', conversation_history=[])
    for _ in range(150):
        result = json.loads(ctx.tools['shadow_trial_step']({}, session_id='controller'))
        if not result['continue']:
            break
    saved = plugin.store.get(record['id'])
    assert saved['state'] == 'finished' and saved['totals']['pairs'] == 30
    assert len(ctx.messages) <= 3
    result = ctx.commands['shadow'](f'results {record["id"]} --stats')
    assert 'Net win rate' in result and 'your pick.' in result
    assert result.count('Broken calls') <= 1
    assert len(receipt_columns(saved)) == 5
    assert saved['totals']['receipt']['cost_per_turn_new'] < saved['totals']['receipt']['cost_per_turn_yours']


def test_cli_parser_and_yes_flow(tmp_path, capsys):
    ctx, plugin = service(tmp_path)
    plugin.launcher.launch = lambda record: finish_controller(ctx, plugin, record)
    setup, handler = ctx.cli['shadow']
    parser = argparse.ArgumentParser()
    setup(parser)
    handler(parser.parse_args(['try', TRIAL.model, '--yes', '--judge', OUTSIDE.model]))
    assert 'Try GPT-6 Luna' in capsys.readouterr().out
    assert plugin.store.get()['state'] == 'finished'
    with pytest.raises(CommandError):
        plugin.parser(terminal=True).parse_args(['results', '--judge', OUTSIDE.model])


def test_no_start_for_over_cap_unknown_price_or_isolation(tmp_path):
    ctx, plugin = service(tmp_path)
    ctx.answers = ['start']
    text = ctx.commands['shadow']('try gpt-6-luna --budget .01')
    assert 'over your $0.01 cap' in text and '[Start]' not in text
    assert not plugin.launcher.launched
    plugin.launcher = ControllerLauncher(data_dir=tmp_path)
    text = ctx.commands['shadow']('try gpt-6-luna')
    assert '[Start]' not in text
    assert not ctx.capture


def test_named_judge_refusals_send_nothing_and_create_no_record(tmp_path):
    ctx, plugin = service(tmp_path)
    for judge, expected in [(CURRENT.model, "The judge can't be one"), ('gpt-4.1', 'The judge has to come'), ('no-such-model', 'No model called')]:
        plugin.catalog.append(__import__('shadow.common.models', fromlist=['Model']).Model('openai-codex', 'gpt-4.1'))
        text = ctx.commands['shadow'](f'try {TRIAL.model} --judge {judge}')
        assert expected in text
    assert not ctx.capture and not plugin.launcher.launched
    assert plugin.store.list() == []


@pytest.mark.parametrize('missing', ['llm.allow_provider_override', 'llm.allow_model_override'])
def test_doctor_names_each_missing_permission_without_writes(tmp_path, missing):
    from copy import deepcopy
    ctx, plugin = service(tmp_path)
    entry = {'allow_gateway_injection': True, 'llm': {'allow_provider_override': True, 'allow_model_override': True}}
    target = entry
    parts = missing.split('.')
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = False
    config = {'plugins': {'entries': {'shadow': entry}}}
    original = deepcopy(config)
    plugin.config_reader = lambda: config
    text = plugin.command('doctor')
    setting = 'plugins.entries.shadow.' + missing
    assert f'Missing: {setting}' in text
    fix = 'hermes plugins enable shadow'
    assert fix in text
    assert text.count('Missing: plugins.entries.shadow.') == 1 and config == original
    assert not ctx.capture and not ctx.messages
    assert not (tmp_path / 'trial.sqlite').exists()


def test_doctor_distinguishes_backup_judging_from_a_third_maker(tmp_path):
    ctx, plugin = service(tmp_path)
    plugin.catalog = [CURRENT, TRIAL]
    plugin.config_reader = lambda: {'plugins': {'entries': {'shadow': {
        'allow_gateway_injection': True,
        'llm': {'allow_provider_override': True, 'allow_model_override': True}}}}}
    text = plugin.command('doctor')
    assert plugin.availability() == (True, True)
    assert 'Third-maker judge: unavailable' in text
    assert 'Fix: hermes model' in text
    assert 'Missing at least one other model' not in text
    assert not ctx.capture and not ctx.messages
    assert not (tmp_path / 'trial.sqlite').exists()


@pytest.mark.parametrize('section', ['agent', 'delegation'])
@pytest.mark.parametrize('injected_models', [True, False])
def test_start_reads_profile_tier_from_hermes_readonly_config(tmp_path, monkeypatch, section, injected_models):
    from copy import deepcopy
    ctx, plugin = service(tmp_path)
    config = {section: {'service_tier': 'priority'},
              'model': {'provider': CURRENT.provider, 'default': CURRENT.model},
              'plugins': {'entries': {'shadow': {'settings': {
                  'models': [{'provider': m.provider, 'model': m.model} for m in (CURRENT, TRIAL, OUTSIDE)]}}}}}
    original = deepcopy(config)
    loaded = []
    module = ModuleType('hermes_cli.config')
    def read_config():
        loaded.append(True)
        return config
    module.load_config_readonly = read_config
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', module)
    plugin.config_reader = None
    if not injected_models:
        from shadow.common.discovery import ModelCatalog
        plugin.current = plugin.catalog = None
        monkeypatch.setattr('shadow.trial.plugin.discover_models',
                            lambda **_: ModelCatalog.from_models(CURRENT, [CURRENT, TRIAL, OUTSIDE]))
    ctx.answers = ['cancel']

    ctx.commands['shadow'](f'try {TRIAL.model} --turns 20', session_id='origin')

    record = plugin.store.get()
    assert loaded and config == original
    assert not record['can_start'] and record['estimate']['total'] is None
    assert not plugin.launcher.launched and not ctx.subagent_lifecycle.launches and not ctx.llm.calls


def test_start_config_read_failure_never_assumes_normal_tier(tmp_path, monkeypatch):
    ctx, plugin = service(tmp_path)
    module = ModuleType('hermes_cli.config')
    def read_config():
        raise OSError('fixture config unavailable')
    module.load_config_readonly = read_config
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', module)
    plugin.config_reader = None
    ctx.answers = ['cancel']

    ctx.commands['shadow'](f'try {TRIAL.model} --turns 20', session_id='origin')

    assert plugin.store.list() == []
    assert not plugin.launcher.launched and not ctx.subagent_lifecycle.launches and not ctx.llm.calls


@pytest.mark.parametrize('turns,limit', [(20,168), (30,248), (60,488)])
def test_controller_launch_is_separate_finite_and_gated(tmp_path, turns, limit, monkeypatch):
    monkeypatch.setattr('shadow.trial.probe.memory_provider_unverifiable', lambda: False)
    record = {'id': 'f123', 'controller_token': 'fixture-token', 'turns': [None] * turns}
    with pytest.raises(IsolationRequired):
        ControllerLauncher(data_dir=tmp_path).launch(record)
    calls = []
    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return type('Process', (), {'pid': 12})()
    from shadow.trial.isolation import CHECK_NAMES, Identity, write_record
    identity = Identity('9.9.9', 'abcdef1234567890', '0.1.0', 'fixture-hash', 'demo')
    write_record(tmp_path, identity, {name: True for name in CHECK_NAMES})
    assert ControllerLauncher('demo', tmp_path, isolation_verified=True, identity=lambda: identity, popen=popen).launch(record) == 12
    argv, kw = calls[0]
    assert argv[:5] == ['hermes', '-p', 'demo', 'chat', '--query-file'] and '-Q' in argv
    assert f'Use at most {limit} calls.' in Path(argv[5]).read_text()
    assert argv[argv.index('--max-turns') + 1] == str(limit)
    assert kw['start_new_session']


def test_disguised_plugin_import_uses_relative_imports(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('installed_shadow_fixture', root / '__init__.py', submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert callable(module.register)


@pytest.mark.parametrize('setting', [None, True, False, 'false'])
def test_controller_inherits_profile_tools_with_both_blockers(tmp_path, setting):
    ctx, plugin = service(tmp_path)
    ctx.answers = ['start']
    ctx.commands['shadow'](f'try {TRIAL.model}')
    record = plugin.store.get()
    plugin.config_reader = lambda: {'delegation': {'inherit_mcp_toolsets': setting}}
    replies = ctx.hook('pre_llm_call', session_id='controller', user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}')
    assert not any(r and r.get('action') == 'block' for r in replies)
    assert plugin.engine.record['state'] == 'running'
    assert plugin.guard.pre_tool_call(session_id='controller', tool_name='terminal', args={})['action'] == 'block'


def test_launcher_cannot_overwrite_a_finished_controller_record(tmp_path):
    ctx, plugin = service(tmp_path)
    plugin.launcher.launch = lambda record: finish_controller(ctx, plugin, record)
    ctx.answers = ['start']
    ctx.commands['shadow'](f'try {TRIAL.model}')
    assert plugin.store.get()['state'] == 'finished'


def test_cli_waits_for_fixture_verdict_and_prints_start_once(tmp_path, capsys):
    ctx, plugin = service(tmp_path)
    plugin.launcher.launch = lambda record: finish_controller(ctx, plugin, record)
    text = plugin.command(f'try {TRIAL.model} --yes', terminal=True)
    output = capsys.readouterr().out + text
    assert output.count('Try GPT-6 Luna') == 1
    assert 'Won 30' in output and 'Replays are close copies' in output


def test_start_card_fewer_turns_recomputes_estimate(tmp_path):
    ctx, plugin = service(tmp_path)
    ctx.answers = ['fewer', 'start']
    ctx.commands['shadow'](f'try {TRIAL.model}')
    record = plugin.store.get()
    assert record['picked'] == 20 and record['state'] == 'queued'
    assert plugin.launcher.launched == [record['id']]
    assert '20 of your 30' in ctx.questions[-1][0]


def test_start_card_preview_can_drop_a_turn_before_start(tmp_path):
    ctx, plugin = service(tmp_path)
    ctx.answers = ['preview', 't0', 'start']
    ctx.commands['shadow'](f'try {TRIAL.model}')
    record = plugin.store.get()
    assert record['picked'] == 29 and record['state'] == 'queued'
    assert 't0' not in {t['id'] for t in record['turns']}


def test_change_judge_hides_contestant_makers_and_updates_start_card(tmp_path):
    from shadow.common.models import Model
    other = Model('anthropic', 'claude-opus-4-6')
    plugin_ctx, plugin = service(tmp_path)
    plugin.catalog.append(other)
    plugin_ctx.answers = ['judge', '1', 'start']
    plugin_ctx.commands['shadow'](f'try {TRIAL.model}')
    picker_text, choices = plugin_ctx.questions[1]
    assert 'Judge for this test' in picker_text and "OpenAI models aren't listed" in picker_text
    assert all('GPT' not in choice['label'] for choice in choices)
    assert '(auto)' in choices[0]['label']
    assert 'your pick.' in plugin_ctx.questions[-1][0]
    record = plugin.store.get()
    assert record['judge_source'] == 'user'
    assert record['judge']['models'][0]['model'] == other.model


def test_status_reports_a_dead_controller_without_relaunching(tmp_path):
    ctx, plugin = service(tmp_path)
    record = TrialEngine(ctx, plugin.store, plugin.guard).prepare(selection(tmp_path), CURRENT, TRIAL, [OUTSIDE])
    record['state'] = 'queued'
    plugin.store.save(record)
    plugin.start_controller(record)
    record = plugin.store.get(record['id'])
    assert record['controller_pid'] == 123
    plugin.launcher.is_alive = lambda pid: False
    assert 'stopped' in plugin.command('status')
    assert len(plugin.launcher.launched) == 1
    assert plugin.store.get()['stop_reason'] == 'controller_exited'


def test_cancelled_controller_prompt_cannot_start_inference(tmp_path):
    ctx, plugin = service(tmp_path)
    ctx.answers = ['start']
    plugin.command(f'try {TRIAL.model}')
    record = plugin.store.get()
    plugin.command('cancel')
    replies = ctx.hook('pre_llm_call', session_id='controller', user_message=f'shadow-controller:{record["id"]}:{record["controller_token"]}')
    assert any(r and r.get('action') == 'block' for r in replies)
    assert plugin.engine is None and not ctx.capture


def test_status_stops_abandoned_startup_and_rejects_old_token(tmp_path):
    ctx, plugin = service(tmp_path)
    record = TrialEngine(ctx, plugin.store, plugin.guard).prepare(selection(tmp_path), CURRENT, TRIAL, [OUTSIDE])
    record['state'] = 'queued'
    plugin.store.save(record)
    old_claim = plugin.store.claim_controller(record['id'], None)
    plugin.launcher.is_alive = lambda pid: False
    plugin.command('status')
    saved = plugin.store.get()
    assert plugin.launcher.launched == []
    assert saved['state'] == 'interrupted' and saved['stop_reason'] == 'controller_exited'
    responses = ctx.hook('pre_llm_call', session_id='stale', user_message=f'shadow-controller:{record["id"]}:{old_claim}')
    assert any(r and r.get('action') == 'block' for r in responses)
    assert plugin.engine is None


def completed_service(tmp_path):
    ctx, plugin = service(tmp_path)
    plugin.launcher.launch = lambda record: finish_controller(ctx, plugin, record)
    ctx.answers = ['start']
    plugin.command(f'try {TRIAL.model}')
    return ctx, plugin, plugin.store.get()


def test_lost_child_identity_stops_without_retry_or_estimated_charge(tmp_path):
    ctx, plugin = service(tmp_path)
    engine = plugin.engine = TrialEngine(ctx, plugin.store, plugin.guard)
    record = engine.prepare(selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    engine.attach(record, 'controller')
    failed = []

    def launch(request):
        failed.append(request['goal'])
        replies = ctx.hook('pre_llm_call', session_id='lost', parent_session_id='controller')
        assert replies[0]['action'] == 'block'
        return 'lost'

    ctx.subagent_lifecycle.launch = launch
    reply = json.loads(ctx.tools['shadow_trial_step']({}, session_id='controller'))
    saved = plugin.store.get(record['id'])
    run = saved['turns'][0]['runs']['new'][0]

    assert not reply['continue']
    assert saved['state'] == 'finished' and saved['stop_reason'] == 'unpriceable'
    assert saved['spend'] == 0
    assert run['attempt'] == 1 and not run['ok']
    assert run['usage']['estimated'] and run['cost'] is None
    assert len(failed) == 1
    assert ctx.subagent_lifecycle.cancelled == ['lost']
    assert not ctx.llm.calls
    replies = ctx.hook('pre_llm_call', session_id='lost', parent_session_id='controller', user_message=failed[0])
    assert replies[0]['action'] == 'block'


def test_result_keep_and_lost_turn_actions_use_saved_data_only(tmp_path):
    ctx, plugin, record = completed_service(tmp_path)
    turn = record['turns'][0]
    set_judge_pick(turn, '2')
    turn['runs']['new'][-1]['where_stopped'] = 'stopped at an action'
    finalize(record)
    plugin.store.save(record)
    calls = len(ctx.capture)
    ctx.answers = ['keep']
    assert 'Nothing changed.' in plugin.command(f'results {record["id"]}')
    ctx.answers = ['turns']
    text = plugin.command(f'results {record["id"]}')
    assert turn['first_line'] in text and 'Not run in a test.' in text
    assert len(ctx.capture) == calls


def test_result_second_look_keeps_old_pairs_and_replays_thirty_new_turns(tmp_path):
    ctx, plugin, record = completed_service(tmp_path)
    for index, turn in enumerate(record['turns']):
        set_judge_pick(turn, '1' if index < 4 else '2' if index < 10 else 'same')
    finalize(record)
    plugin.store.save(record)
    assert record['totals']['word'] == 'too_close'
    assert record['totals']['cant_call_reason'] == 'too_close'
    plugin.selector = lambda **kw: selection(tmp_path, 90)
    ctx.answers = ['more', 'start']
    plugin.command(f'results {record["id"]}')
    followup = plugin.store.get()
    assert followup['state'] == 'finished'
    assert followup['second_look'] and followup['parent_id'] == record['id']
    assert followup['totals']['pairs'] == 60
    assert [t['id'] for t in followup['turns'][:30]] == [t['id'] for t in record['turns']]
    assert len({t['id'] for t in followup['turns']}) == 60
    assert len(ctx.subagent_lifecycle.launches) == 120
    assert followup['spend'] > record['spend']
    assert 'Run 30 more' not in plugin.command(f'results {followup["id"]}')
    assert plugin.store.get(record['id'])['second_look_id'] == followup['id']


def test_watch_progress_uses_injected_events_without_polling_sleep(tmp_path, monkeypatch):
    ctx, plugin = service(tmp_path)
    engine = TrialEngine(ctx, plugin.store, plugin.guard)
    record = engine.prepare(selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    record['state'] = 'running'
    plugin.store.save(record)
    pauses = []
    def advance(seconds):
        pauses.append(seconds)
        saved = plugin.store.get(record['id'])
        saved.update(state='cancelled', stop_reason='cancelled')
        plugin.store.save(saved)
    plugin.watch_wait = advance
    monkeypatch.setattr('shadow.trial.plugin.time.sleep', lambda _: pytest.fail('real polling sleep'))
    plugin.watch(record['id'])
    assert pauses == [1]
    assert len(ctx.messages) == 1
    assert plugin.store.get(record['id'])['state'] == 'cancelled'


def test_chat_judge_picker_displays_an_unpriced_option_without_crashing(tmp_path):
    from shadow.common.models import Model

    ctx, plugin = service(tmp_path)
    plugin.catalog.append(Model('unknown-proxy', 'claude-opus-4-6'))
    ctx.answers = ['judge', '1', 'cancel']

    ctx.commands['shadow'](f'try {TRIAL.model}')

    choices = ctx.questions[1][1]
    assert 'price' in choices[1]['label']
    record = plugin.store.get()
    assert not record['can_start']
    assert record['estimate']['judge'] is None
    assert '[Start]' not in ctx.questions[-1][0]


@pytest.mark.parametrize('state', ['queued', 'running', 'interrupted'])
def test_legacy_recovery_blocks_first_controller_request(tmp_path, monkeypatch, state):
    from shadow.trial import usage

    ctx, plugin = service(tmp_path)
    engine = TrialEngine(ctx, plugin.store, plugin.guard)
    record = engine.prepare(selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    record.pop('prices')
    record['state'] = state
    plugin.store.save(record)
    record = plugin.store.get(record['id'])

    def unavailable(*args, **kwargs):
        raise AssertionError('legacy recovery must not resolve prices')
    monkeypatch.setattr(usage, 'price_for', unavailable)
    marker = f'shadow-controller:{record["id"]}:{record["controller_token"]}'

    decision = plugin.pre_llm_call(session_id='controller', user_message=marker)

    assert decision['action'] == 'block'
    saved = plugin.store.get(record['id'])
    assert saved['stop_reason'] == 'unpriceable' and saved['state'] == 'finished'
    assert not saved['can_start']
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls
    assert 'controller' not in plugin.guard.controllers
    assert plugin.pre_llm_call(session_id='controller', user_message=marker)['action'] == 'block'


@pytest.mark.parametrize('terminal', [False, True])
def test_legacy_refusal_leaves_a_renderable_result_for_existing_watchers(tmp_path, monkeypatch, terminal):
    from shadow.common.copy import copy
    from shadow.trial import usage

    ctx, plugin = service(tmp_path)
    engine = TrialEngine(ctx, plugin.store, plugin.guard)
    record = engine.prepare(selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    record.pop('prices')
    record['state'] = 'interrupted'
    plugin.store.save(record)
    record = plugin.store.get(record['id'])

    def unavailable(*args, **kwargs):
        raise AssertionError('legacy recovery must not resolve prices')
    monkeypatch.setattr(usage, 'price_for', unavailable)
    marker = f'shadow-controller:{record["id"]}:{record["controller_token"]}'
    assert plugin.pre_llm_call(session_id='controller', user_message=marker)['action'] == 'block'

    result = plugin.watch(record['id'], terminal=terminal)

    text = result if terminal else ctx.messages[-1]
    assert copy('verdict.advice.close') in text
    assert plugin.store.get(record['id'])['totals']['pairs'] == 0
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls
