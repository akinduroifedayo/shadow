import json
from pathlib import Path

import pytest

from shadow.trial.safety import ReplayGuard

STOP = 'Not run: this is a test. Stop here and say what you were about to do and why.'


@pytest.mark.parametrize('tool', ['terminal', 'write_file', 'send_message', 'browser_navigate', 'memory', 'delegate_task', 'other_plugin_invented'])
@pytest.mark.parametrize('crash', [False, True])
def test_disallowed_tools_never_execute_and_end_replay(tmp_path, tool, crash):
    guard = ReplayGuard()
    guard.add_controller('controller')
    guard.expect('token', tmp_path)
    guard.subagent_start(parent_session_id='controller', child_session_id='child', child_goal='token')
    effects = []
    def actual(args):
        effects.append(args)
        (tmp_path / 'side_effect').touch()
    def middleware(**kw):
        if crash:
            raise RuntimeError('fixture crash')
        return guard.tool_execution(**kw)
    def authorized(args):
        directive = guard.pre_tool_call(tool_name=tool, args=args, session_id='child')
        return directive['message'] if directive else actual(args)
    try:
        result = middleware(tool_name=tool, args={}, session_id='child', next_call=authorized)
    except RuntimeError:
        result = authorized({})
    assert STOP in result
    assert effects == []
    assert not (tmp_path / 'side_effect').exists()
    assert guard.pre_tool_call(tool_name='shadow_read', args={'path': '.'}, session_id='child')['action'] == 'block'


def test_veto_admits_scoped_reads_and_blocks_every_other_real_name(tmp_path):
    (tmp_path / 'notes.txt').write_text('scoped fixture contents')
    outside = tmp_path.parent / 'outside-notes.txt'
    outside.write_text('outside fixture')
    guard = ReplayGuard()
    guard.add_controller('controller')
    guard.expect('token', tmp_path)
    guard.subagent_start(parent_session_id='controller', child_session_id='child', child_goal='token')
    admitted = (
        ('read_file', {'path': 'notes.txt'}),
        ('search_files', {'pattern': 'scoped', 'path': '.'}),
        ('skill_view', {'name': 'notes'}),
        ('skill_view', {'name': 'notes', 'file_path': 'notes.txt'}),
        ('todo', {'todos': []}),
        ('todo_list', {}),
        ('web_search', {'query': 'fixture'}),
        ('web_extract', {'urls': ['https://example.test']}),
        ('clarify', {'questions': []}),
        ('shadow_read', {'path': 'notes.txt'}),
    )
    for tool, args in admitted:
        assert guard.pre_tool_call(tool_name=tool, args=args, session_id='child') is None, tool
        assert guard.children['child'].stopped is False
    for tool, args in (
        ('read_file', {'path': str(outside)}),
        ('read_file', {'path': '.env'}),
        ('search_files', {'pattern': 'scoped', 'path': '..'}),
        ('skill_view', {'file_path': str(outside)}),
    ):
        blocked = guard.pre_tool_call(tool_name=tool, args=args, session_id='child')
        assert blocked['action'] == 'block' and blocked['message'] == STOP, tool
        assert guard.children['child'].stopped is False, tool
    assert guard.pre_tool_call(tool_name='read_file', args={'path': 'notes.txt'}, session_id='child') is None
    write = guard.pre_tool_call(tool_name='write_file', args={'path': 'notes.txt'}, session_id='child')
    assert write['action'] == 'block' and write['message'] == STOP
    assert guard.children['child'].stopped is True
    assert guard.pre_tool_call(tool_name='read_file', args={'path': 'notes.txt'}, session_id='child')['action'] == 'block'


def test_middleware_itself_answers_without_downstream(tmp_path):
    guard = ReplayGuard()
    guard.add_controller('parent')
    guard.expect('goal-token', tmp_path)
    guard.subagent_start(parent_session_id='parent', child_session_id='child', child_goal='goal-token')
    result = guard.tool_execution(tool_name='made_up', args={}, session_id='child', next_call=lambda _: pytest.fail('tool ran'))
    assert STOP in result


def test_unidentified_controller_child_is_denied(tmp_path):
    guard = ReplayGuard()
    guard.add_controller('controller')
    assert guard.pre_llm_call(session_id='lost', parent_session_id='controller')['action'] == 'block'
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='lost', parent_session_id='controller')['action'] == 'block'


def test_ordinary_turn_is_untouched():
    guard = ReplayGuard()
    assert guard.pre_llm_call(session_id='ordinary') is None
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='ordinary') is None
    assert guard.tool_execution(tool_name='terminal', args={}, session_id='ordinary', next_call=lambda _: 'normal') == 'normal'


@pytest.mark.parametrize('observer', ['start', 'llm'])
def test_quarantined_parent_keeps_descendants_blocked_without_tool_parent_ids(observer):
    guard = ReplayGuard()
    guard.unidentified.add('orphan')
    if observer == 'start':
        guard.subagent_start(parent_session_id='orphan', child_session_id='nested')
    else:
        assert guard.pre_llm_call(session_id='nested', parent_session_id='orphan')['action'] == 'block'
    assert guard.pre_llm_call(session_id='nested')['action'] == 'block'
    assert guard.pre_tool_call(tool_name='terminal', args={}, session_id='nested')['action'] == 'block'
    assert STOP in guard.tool_execution(tool_name='terminal', args={}, session_id='nested',
                                        next_call=lambda _: pytest.fail('real tool ran'))


def test_nested_child_and_recovery_when_start_observer_is_lost(tmp_path):
    guard = ReplayGuard()
    guard.add_controller('controller')
    guard.expect('recovery-token', tmp_path)
    guard.pre_llm_call(session_id='child', parent_session_id='controller', messages=[{'role': 'user', 'content': 'A request recovery-token'}])
    assert guard.children['child'].cwd == tmp_path
    guard.subagent_start(parent_session_id='child', child_session_id='nested', child_goal='recovery-token')
    assert guard.pre_tool_call(session_id='nested', tool_name='terminal', args={})['action'] == 'block'


def test_recovery_uses_real_hermes_user_message_field(tmp_path):
    guard = ReplayGuard()
    guard.add_controller('controller')
    guard.expect('recovery-token', tmp_path)
    guard.pre_llm_call(session_id='child', parent_session_id='controller', user_message='A request recovery-token', conversation_history=[])
    assert guard.children['child'].cwd == tmp_path


def test_controller_cannot_execute_other_tools():
    guard = ReplayGuard()
    guard.add_controller('controller')
    assert guard.pre_tool_call(session_id='controller', tool_name='terminal', args={})['action'] == 'block'
    assert guard.pre_tool_call(session_id='controller', tool_name='shadow_trial_step', args={}) is None


def test_exclusive_controller_still_reaches_its_own_step():
    guard = ReplayGuard()
    guard.add_controller('controller', exclusive=True)
    seen = []
    result = guard.tool_execution(tool_name='shadow_trial_step', args={}, session_id='controller', next_call=lambda args: seen.append(args) or 'ok')
    assert result == 'ok' and seen == [{}]
    denied = guard.tool_execution(tool_name='terminal', args={}, session_id='controller', next_call=lambda _: pytest.fail('tool ran'))
    assert 'error' in json.loads(denied) and STOP not in denied


def test_unknown_tool_session_fails_closed_in_dedicated_controller():
    guard = ReplayGuard()
    guard.add_controller('controller', exclusive=True)
    assert guard.pre_tool_call(session_id='unknown', tool_name='terminal', args={})['action'] == 'block'
    assert STOP in guard.tool_execution(session_id='unknown', tool_name='terminal', args={}, next_call=lambda _: pytest.fail('tool ran'))


def test_unidentified_inference_fails_closed_in_dedicated_controller():
    guard = ReplayGuard()
    guard.add_controller('controller', exclusive=True)
    assert guard.pre_llm_call(session_id='unknown', user_message='unidentified')['action'] == 'block'
    assert guard.pre_llm_call(session_id='controller') is None


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'duplicate'])
@pytest.mark.parametrize('layer', ['veto', 'middleware'])
def test_registered_blockers_survive_copy_damage(tmp_path, monkeypatch, damage, layer):
    from importlib import import_module

    from fixtures import FakeContext
    from shadow.common.storage import Store
    from shadow.trial.plugin import TrialPlugin, register

    table = import_module('shadow.common.copy')
    directory = tmp_path / 'copy'
    directory.mkdir()
    for source in (Path(table.__file__).parents[1] / 'copy').glob('*.json'):
        (directory / source.name).write_text(source.read_text())
    load = table.load_copy
    monkeypatch.setattr(table, 'load_copy', lambda: load(directory))
    ctx = FakeContext()
    plugin = register(ctx, TrialPlugin(ctx, store=Store(tmp_path / 'trial.sqlite')))
    plugin.guard.add_controller('controller', exclusive=True)
    plugin.guard.expect('fixture-token', tmp_path)
    plugin.guard.subagent_start(parent_session_id='controller', child_session_id='child', child_goal='fixture-token')
    for path in directory.glob('*.json'):
        if damage == 'missing':
            path.unlink()
        elif damage == 'corrupt':
            path.write_text('{')
        else:
            path.write_text('{"duplicate": "first", "duplicate": "second"}')
    effects = []
    def actual(args):
        effects.append(args)
        return 'executed'
    if layer == 'veto':
        directive, = ctx.hook('pre_tool_call', tool_name='terminal', args={}, session_id='child')
        result = directive['message'] if directive and directive['action'] == 'block' else actual({})
    else:
        result = ctx.middleware['tool_execution'](tool_name='terminal', args={}, session_id='child', next_call=actual)
    assert STOP in result
    assert effects == []


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
def test_blockers_refuse_malformed_scope_arguments_without_raising(tmp_path, layer):
    guard = ReplayGuard()
    guard.add_controller('controller', exclusive=True)
    guard.expect('fixture-token', tmp_path)
    guard.subagent_start(parent_session_id='controller', child_session_id='child', child_goal='fixture-token')
    if layer == 'veto':
        result = guard.pre_tool_call(tool_name='shadow_read', args={'path': object()}, session_id='child')
        assert result['action'] == 'block' and result['message'] == STOP
    else:
        guard.children = None
        result = guard.tool_execution(tool_name='shadow_read', args={}, session_id='child', next_call=lambda _: pytest.fail('tool ran'))
        assert json.loads(result) == {'error': STOP}


def test_identified_replay_admission_is_independent_of_prior_turn_stop(tmp_path):
    guard = ReplayGuard()
    guard.add_controller('controller', exclusive=True)
    guard.expect('fixture-token', tmp_path)
    guard.subagent_start(parent_session_id='controller', child_session_id='child', child_goal='fixture-token')
    for turn in range(3):
        assert guard.pre_llm_call(session_id='child', user_message=f'Fixture user turn {turn}') is None
        for tool in ('terminal', 'shadow_read', 'shadow_clarify'):
            directive = guard.pre_tool_call(session_id='child', tool_name=tool, args={})
            assert directive['action'] == 'block' and directive['message'] == STOP


@pytest.mark.parametrize('session', ['ordinary', 'replay'])
def test_middleware_preserves_downstream_errors(tmp_path, session):
    guard = ReplayGuard()
    if session == 'replay':
        guard.add_controller('controller')
        guard.expect('fixture-token', tmp_path)
        guard.subagent_start(parent_session_id='controller', child_session_id=session, child_goal='fixture-token')
    error = RuntimeError('fixture downstream error')
    def downstream(args):
        raise error
    with pytest.raises(RuntimeError) as raised:
        guard.tool_execution(tool_name='shadow_read', args={}, session_id=session, next_call=downstream)
    assert raised.value is error


def test_controller_inference_veto_uses_registration_copy(tmp_path, monkeypatch):
    from fixtures import FakeContext
    from shadow.common.storage import Store
    from shadow.trial.plugin import TrialPlugin, register
    ctx = FakeContext()
    plugin = register(ctx, TrialPlugin(ctx, store=Store(tmp_path / 'trial.sqlite')))
    def unavailable(*args, **kwargs):
        raise ValueError('fixture unavailable copy')
    monkeypatch.setattr('shadow.trial.plugin.copy', unavailable)
    reply, = ctx.hook('pre_llm_call', session_id='unknown-controller', user_message='shadow-controller:malformed')
    assert reply == {'action': 'block', 'message': plugin.guard.controller_stop_message}
    assert reply['message'] != STOP
    assert not ctx.capture
