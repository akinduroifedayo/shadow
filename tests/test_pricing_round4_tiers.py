import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, selection, set_judge_pick
from shadow.common.copy import copy
from shadow.trial.cards import render_start
from shadow.trial.engine import TrialEngine
from shadow.trial.verdict import finalize
from test_plugin import completed_service, service


@pytest.mark.parametrize('recover', [False, True])
def test_tier_change_before_launch_refuses_without_spend(tmp_path, recover):
    ctx, plugin = service(tmp_path)
    record = TrialEngine(ctx, plugin.store, plugin.guard).prepare(
        selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    record.update(state='queued', spend=0.42)
    plugin.store.save(record)
    if recover:
        plugin.start_controller(record)
        plugin.launcher.is_alive = lambda pid: False
    launched = list(plugin.launcher.launched)
    plugin.config_reader = lambda: {'agent': {'service_tier': 'fast'}}

    if recover:
        plugin.status(plugin.store.get(record['id']))
    else:
        plugin.start_controller(record)

    saved = plugin.store.get(record['id'])
    assert plugin.launcher.launched == launched
    assert saved['state'] == 'interrupted'
    assert saved['stop_reason'] == 'unpriceable'
    assert not saved['can_start']
    assert saved['spend'] == 0.42
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls
    plugin.status(saved)
    assert plugin.launcher.launched == launched


@pytest.mark.parametrize('second_look', [False, True])
def test_tier_change_while_start_card_is_open_refuses_start(tmp_path, second_look):
    if second_look:
        ctx, plugin, parent = completed_service(tmp_path)
        for index, turn in enumerate(parent['turns']):
            set_judge_pick(turn, '1' if index < 4 else '2' if index < 10 else 'same')
        finalize(parent)
        plugin.store.save(parent)
        plugin.selector = lambda **kw: selection(tmp_path, 90)
    else:
        ctx, plugin = service(tmp_path)
    original_choice = ctx.ask_choice
    ctx.answers = ['more', 'start'] if second_look else ['start']
    calls = len(ctx.llm.calls)
    launches = len(ctx.subagent_lifecycle.launches)

    def choose(question, choices):
        answer = original_choice(question, choices)
        if answer == 'start':
            plugin.config_reader = lambda: {'agent': {'service_tier': 'priority'}}
        return answer

    ctx.ask_choice = choose
    command = f'results {parent["id"]}' if second_look else f'try {TRIAL.model} --turns 20'
    text = plugin.command(command)

    saved = plugin.store.get()
    assert saved['state'] == 'interrupted'
    assert saved['stop_reason'] == 'unpriceable'
    assert saved['spend'] == (parent['spend'] if second_look else 0)
    assert not plugin.launcher.launched
    assert len(ctx.llm.calls) == calls
    assert len(ctx.subagent_lifecycle.launches) == launches
    assert 'service tier' in text
    assert 'Pick another model' not in text
    if second_look:
        assert not plugin.store.get(parent['id']).get('second_look_id')
        plugin.command('cancel')
        plugin.config_reader = lambda: {'delegation': {'inherit_mcp_toolsets': False}}
        ctx.ask_choice = original_choice
        ctx.answers = ['more', 'start']
        plugin.command(command)
        followup = plugin.store.get()
        assert followup['state'] == 'finished'
        assert followup['parent_id'] == parent['id']
        assert plugin.store.get(parent['id'])['second_look_id'] == followup['id']


def test_tier_blocks_start_with_tier_copy_and_judge_picker_without_dollars(tmp_path):
    ctx, plugin = service(tmp_path)
    plugin.config_reader = lambda: {'agent': {'service_tier': 'fast'}}
    ctx.answers = ['judge', '0', 'cancel']
    plugin.command(f'try {TRIAL.model} --turns 20')

    record = plugin.store.get()
    text = render_start(record, isolation_verified=True)
    assert 'service tier' in text
    assert "Shadow doesn't have a price for" not in text
    assert 'Pick another model' not in text
    assert '[Start]' not in text
    choices = ctx.questions[1][1]
    assert choices
    assert all('$' not in choice['label'] and 'service tier' in choice['label'] for choice in choices)
    assert not plugin.launcher.launched


@pytest.mark.parametrize('surface', ['status', 'progress', 'stopped'])
def test_unpriceable_spend_is_not_presented_as_complete(tmp_path, surface, capsys):
    ctx, plugin = service(tmp_path)
    record = TrialEngine(ctx, plugin.store, plugin.guard).prepare(
        selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    record.update(state='interrupted', stop_reason='unpriceable', spend=0.42)
    plugin.store.save(record)
    if surface == 'status':
        text = plugin.status(record)
    elif surface == 'stopped':
        text = plugin.stopped(record)
    else:
        plugin.watch(record['id'], terminal=True)
        text = capsys.readouterr().out
    assert copy('trial.unpriceable') in text
    assert '$0.42 of $5.00 spent' not in text


def test_unpriceable_verdict_names_the_stop_instead_of_failed_models(tmp_path):
    from shadow.trial.cards import render_verdict
    from shadow.trial.verdict import finalize

    ctx, plugin = service(tmp_path)
    record = TrialEngine(ctx, plugin.store, plugin.guard).prepare(
        selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    record.update(state='finished', stop_reason='unpriceable')
    finalize(record)
    text = render_verdict(record)
    assert record['totals']['cant_call_reason'] == 'unpriceable'
    assert copy('trial.unpriceable') in text
    assert 'both models failed' not in text


def test_desktop_tier_block_does_not_blame_a_model_or_show_judge_prices(monkeypatch):
    from shadow import desktop_rpc as desktop
    from shadow.common.copy import load_copy

    source = desktop.FixtureSource('empty')
    facts = source.start_facts(source.world['start']['try'])
    facts.update(priced=False, tokens=12345, pricing_blocker='service_tier')
    monkeypatch.setattr(source, 'start_facts', lambda _: facts)
    card = desktop.build_start(desktop.Copy(load_copy()), source, facts['trial']['model'])
    assert 'service tier' in card['blocked']
    assert 'Pick another model' not in card['blocked']
    assert all('$' not in option['cost'] for option in card['judge']['options'])
    assert not any(button['id'] == 'start' for button in card['buttons'])


def test_desktop_unpriceable_progress_and_result_label_unknown_spend():
    from shadow import desktop_rpc as desktop
    from shadow.common.copy import load_copy

    c = desktop.Copy(load_copy())
    for scenario in ('progress', 'result-not-enough'):
        source = desktop.FixtureSource(scenario)
        test = source.tests()[0]
        test['stop_reason'] = 'unpriceable'
        if scenario == 'progress':
            text = desktop._progress(c, test)['sub']
        else:
            text = ' '.join(desktop._facts(c, test))
        assert copy('trial.unpriceable') in text


@pytest.mark.parametrize('surface', ['status', 'progress', 'stopped'])
def test_tier_interruption_keeps_tier_explanation(tmp_path, surface, capsys):
    ctx, plugin = service(tmp_path)
    record = TrialEngine(ctx, plugin.store, plugin.guard).prepare(
        selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    record.update(state='interrupted', stop_reason='unpriceable', pricing_blocker='service_tier')
    plugin.store.save(record)
    if surface == 'progress':
        plugin.watch(record['id'], terminal=True)
        text = capsys.readouterr().out
    else:
        text = getattr(plugin, surface)(record)
    assert copy('start.service_tier') in text
    assert 'Pick another model' not in text


def test_config_read_failure_at_launch_refuses_without_spend(tmp_path):
    ctx, plugin = service(tmp_path)
    record = TrialEngine(ctx, plugin.store, plugin.guard).prepare(
        selection(tmp_path, 20), CURRENT, TRIAL, [OUTSIDE])
    record['state'] = 'queued'
    plugin.store.save(record)

    def unavailable():
        raise OSError('fixture config unavailable')

    plugin.config_reader = unavailable
    plugin.start_controller(record)
    saved = plugin.store.get(record['id'])
    assert not plugin.launcher.launched
    assert saved['state'] == 'interrupted' and saved['stop_reason'] == 'unpriceable'
    assert saved['spend'] == 0


@pytest.mark.parametrize('backup', [False, True])
def test_unpriceable_stop_prevents_a_definite_verdict_after_twenty_pairs(tmp_path, backup):
    from shadow.trial.verdict import finalize
    from test_plugin import completed_service

    _, _, record = completed_service(tmp_path)
    assert record['totals']['pairs'] >= 20
    if backup:
        record['judge_mode'] = 'backup_both'
    record['stop_reason'] = 'unpriceable'
    finalize(record)
    assert record['totals']['word'] == 'not_enough'
    assert record['totals']['cant_call_reason'] == 'unpriceable'
