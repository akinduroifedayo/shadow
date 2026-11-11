import importlib.util
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from test_engine import setup_engine


def hermes_reference():
    reference = os.environ.get('SHADOW_HERMES_REF')
    if not reference:
        pytest.skip('SHADOW_HERMES_REF is not configured')
    return Path(reference)


def require_hermes_reference():
    source = hermes_reference()
    if not (source / 'toolsets.py').is_file():
        pytest.skip('Hermes reference checkout is absent')
    return source


@pytest.mark.parametrize('reference', [None, ''])
def test_unconfigured_hermes_reference_skips(monkeypatch, reference):
    if reference is None:
        monkeypatch.delenv('SHADOW_HERMES_REF', raising=False)
    else:
        monkeypatch.setenv('SHADOW_HERMES_REF', reference)
    with pytest.raises(pytest.skip.Exception):
        hermes_reference()


def test_missing_hermes_reference_skips_and_honors_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv('SHADOW_HERMES_REF', str(tmp_path / 'missing'))
    with pytest.raises(pytest.skip.Exception):
        require_hermes_reference()
    checkout = tmp_path / 'ref'
    checkout.mkdir()
    (checkout / 'toolsets.py').write_text('MARKER = True\n')
    monkeypatch.setenv('SHADOW_HERMES_REF', str(checkout))
    assert require_hermes_reference() == checkout


@pytest.fixture
def offered_tools(monkeypatch):
    source = require_hermes_reference()
    spec = importlib.util.spec_from_file_location('trial_hermes_toolsets', source / 'toolsets.py')
    toolsets = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(toolsets)
    monkeypatch.setitem(__import__('sys').modules, 'toolsets', toolsets)
    config = ModuleType('tools.delegate_tool_config')
    config._get_inherit_mcp_toolsets = lambda: True
    monkeypatch.setitem(__import__('sys').modules, 'tools.delegate_tool_config', config)
    spec = importlib.util.spec_from_file_location('trial_child_toolsets', source / 'tools/delegate_tool_toolsets.py')
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    parent = SimpleNamespace(enabled_toolsets=['hermes-cli', 'shadow_replay', 'mcp-fixture'], disabled_toolsets=[])
    def offered(request):
        enabled, disabled = resolver._resolve_child_toolsets(parent, request.get('allowed_toolsets'), 'leaf')
        names = {name for ts in enabled for name in toolsets.resolve_toolset(ts, include_registry=False)}
        names -= {name for ts in disabled for name in toolsets.resolve_toolset(ts, include_registry=False)}
        if 'mcp-fixture' in enabled:
            names.add('mcp_fixture_write')
        return names
    return offered


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
def test_every_inherited_real_tool_is_independently_vetoed(tmp_path, offered_tools, layer):
    ctx, engine_store, engine, record = setup_engine(tmp_path, 20)
    engine.attach(record, 'controller')
    engine.step('controller')
    request = ctx.subagent_lifecycle.launches[0]
    offered = offered_tools(request)
    assert {'terminal', 'write_file', 'patch', 'browser_exec', 'mcp_fixture_write'} <= offered
    effects = []
    def execute(args):
        effects.append(args)
        (tmp_path / 'effect').touch()
        return 'executed'
    guard = engine.guard
    admitted = {'read_file', 'search_files', 'skill_view', 'todo', 'todo_list', 'web_search', 'web_extract', 'clarify'}
    for index, tool in enumerate(sorted(offered)):
        token, sid = f'fixture-{index:03d}-end', f'replay-{index}'
        guard.expect(token, tmp_path)
        guard.subagent_start(parent_session_id='controller', child_session_id=sid, child_goal=token)
        if layer == 'veto':
            args = {'path': '.'} if tool in {'read_file', 'search_files'} else {}
            directive = guard.pre_tool_call(tool_name=tool, args=args, session_id=sid)
            if tool in admitted:
                assert directive is None, tool
                assert guard.children[sid].stopped is False, tool
                continue
            result = directive['message'] if directive and directive['action'] == 'block' else execute({})
        else:
            result = guard.tool_execution(tool_name=tool, args={}, session_id=sid, next_call=execute)
        assert guard.stop_message in result, tool
    assert not effects and not (tmp_path / 'effect').exists()


@pytest.mark.parametrize('tool', ['terminal', 'write_file'])
@pytest.mark.parametrize('layer', ['veto', 'middleware'])
def test_real_action_produces_a_judged_plan_without_execution(tmp_path, offered_tools, tool, layer):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    original = ctx.subagent_lifecycle.launch
    effects = []
    def launch(request):
        assert tool in offered_tools(request)
        handle = original(request)
        if layer == 'veto':
            directive = engine.guard.pre_tool_call(tool_name=tool, args={}, session_id=handle)
            if not directive:
                effects.append(tool)
        else:
            engine.guard.tool_execution(tool_name=tool, args={}, session_id=handle, next_call=lambda _: effects.append(tool))
        assert engine.guard.pre_llm_call(session_id=handle) is None
        ctx.subagent_lifecycle.results[handle].summary += ' I was about to update the requested file.'
        return handle
    ctx.subagent_lifecycle.launch = launch
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    saved = store.get(record['id'])
    assert saved['turns'][0]['usable']
    for side in ('new', 'yours'):
        run = saved['turns'][0]['runs'][side][-1]
        assert run['end_state'] == 'stopped_at_action'
        assert 'about to update' in run['summary']
    assert ctx.llm.calls and 'about to update' in ctx.llm.calls[0]['input'][0]['text']
    assert not effects


def test_read_file_returns_scoped_content_and_the_run_continues(tmp_path):
    from shadow.trial.reads import SafeReads
    (tmp_path / 'notes.txt').write_text('scoped fixture contents\nsecond line')
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.guard.reads = SafeReads(engine.guard, web_fetch=lambda args: 'shared fixture page')
    original = ctx.subagent_lifecycle.launch
    seen = []

    def launch(request):
        handle = original(request)
        result = engine.guard.tool_execution(tool_name='read_file', args={'path': 'notes.txt', 'offset': 1, 'limit': 1},
                                             session_id=handle, next_call=lambda _: pytest.fail('real read_file ran'))
        seen.append(result)
        assert engine.guard.children[handle].stopped is False
        for tool, args in (
            ('search_files', {'pattern': 'scoped fixture', 'path': '.'}),
            ('skill_view', {'name': 'notes', 'file_path': 'notes.txt'}),
            ('todo', {'todos': [{'id': '1', 'content': 'Check the notes', 'status': 'pending'}]}),
            ('todo_list', {}),
            ('web_search', {'query': 'fixture lookup'}),
            ('web_extract', {'urls': ['https://example.test/page']}),
            ('clarify', {'questions': [{'question': 'Which file?'}]}),
        ):
            answered = engine.guard.tool_execution(tool_name=tool, args=args, session_id=handle,
                                                   next_call=lambda _: pytest.fail(tool + ' ran'))
            assert engine.guard.children[handle].stopped is False, tool
            seen.append(answered)
        return handle

    ctx.subagent_lifecycle.launch = launch
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    assert 'scoped fixture contents' in seen[0] and 'second line' not in seen[0]
    assert 'scoped fixture' in seen[1] and 'scoped fixture contents' in seen[2]
    assert 'Check the notes' in seen[3] and 'Check the notes' in seen[4]
    assert 'shared fixture page' in seen[5] and 'shared fixture page' in seen[6]
    assert 'Nobody is available' in seen[7]
    saved = store.get(record['id'])
    assert saved['turns'][0]['usable']
    for side in ('new', 'yours'):
        assert saved['turns'][0]['runs'][side][-1]['end_state'] != 'stopped_at_action'
    engine.guard.expect('veto-token', tmp_path)
    engine.guard.subagent_start(parent_session_id='controller', child_session_id='veto-child', child_goal='veto-token')
    assert engine.guard.pre_tool_call(tool_name='read_file', args={'path': 'notes.txt'}, session_id='veto-child') is None
    assert engine.guard.children['veto-child'].stopped is False
    assert 'scoped fixture contents' not in engine.guard.tool_execution(
        tool_name='terminal', args={'command': 'cat notes.txt'}, session_id='veto-child',
        next_call=lambda _: pytest.fail('terminal ran'))
    assert engine.guard.children['veto-child'].stopped is True


def _veto_then_middleware(guard, tool, args, session_id):
    """Hermes registry order at 7f3ac61295: pre_tool_call, then tool_execution.

    model_tools.handle_function_call runs _pre_dispatch_guards (model_tools.py:966)
    before _execute_tool (model_tools.py:981). Direct invoke_tool does the same
    (agent/agent_runtime_helpers.py:2496, then :2548) unless the caller already
    checked the veto. The agent loop is the other way around: tool_executor.py
    runs the middleware at line 811, and pre_tool_call only inside that
    middleware's next_call (_pre_tool_block, line 705).
    """
    directive = guard.pre_tool_call(tool_name=tool, args=args, session_id=session_id)
    if directive and directive.get('action') == 'block':
        return directive['message']
    return guard.tool_execution(tool_name=tool, args=args, session_id=session_id,
                                next_call=lambda _: pytest.fail(tool + ' ran'))


def test_veto_then_middleware_returns_scoped_read_and_the_run_continues(tmp_path):
    from shadow.trial.reads import SafeReads
    (tmp_path / 'notes.txt').write_text('scoped fixture contents\nsecond line')
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.guard.reads = SafeReads(engine.guard, web_fetch=lambda args: 'shared fixture page')
    original = ctx.subagent_lifecycle.launch
    seen = []

    def launch(request):
        handle = original(request)
        result = _veto_then_middleware(engine.guard, 'read_file', {'path': 'notes.txt', 'offset': 1, 'limit': 1}, handle)
        seen.append(result)
        assert engine.guard.children[handle].stopped is False
        return handle

    ctx.subagent_lifecycle.launch = launch
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    assert seen and all('scoped fixture contents' in result and 'second line' not in result for result in seen)
    saved = store.get(record['id'])
    assert saved['turns'][0]['usable']
    for side in ('new', 'yours'):
        assert saved['turns'][0]['runs'][side][-1]['end_state'] != 'stopped_at_action'


def test_write_after_a_scoped_read_is_still_blocked_and_stops_the_replay(tmp_path):
    from shadow.trial.reads import SafeReads
    (tmp_path / 'notes.txt').write_text('scoped fixture contents\nsecond line')
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    engine.guard.reads = SafeReads(engine.guard)
    original = ctx.subagent_lifecycle.launch

    def launch(request):
        handle = original(request)
        read = _veto_then_middleware(engine.guard, 'read_file', {'path': 'notes.txt'}, handle)
        assert 'scoped fixture contents' in read
        assert engine.guard.children[handle].stopped is False
        blocked = _veto_then_middleware(engine.guard, 'write_file', {'path': 'notes.txt', 'content': 'changed'}, handle)
        assert engine.guard.stop_message in blocked
        assert engine.guard.children[handle].stopped is True
        assert (tmp_path / 'notes.txt').read_text() == 'scoped fixture contents\nsecond line'
        return handle

    ctx.subagent_lifecycle.launch = launch
    engine.attach(record, 'controller')
    engine.step('controller')
    engine.step('controller')
    saved = store.get(record['id'])
    assert saved['turns'][0]['usable']
    for side in ('new', 'yours'):
        run = saved['turns'][0]['runs'][side][-1]
        assert run['end_state'] == 'stopped_at_action'
        assert 'scoped fixture contents' not in run['summary']


def test_controller_keeps_profile_tools(tmp_path, monkeypatch):
    monkeypatch.setattr('shadow.trial.probe.memory_provider_unverifiable', lambda: False)
    from shadow.trial.controller import ControllerLauncher
    calls = []
    from test_setup_check import IDENTITY, checks
    from shadow.trial.isolation import write_record
    write_record(tmp_path, IDENTITY, checks())
    launcher = ControllerLauncher('demo', data_dir=tmp_path, identity=lambda: IDENTITY,
                                  popen=lambda argv, **kw: calls.append(argv) or SimpleNamespace(pid=1))
    launcher.launch({'id': 'fixture', 'controller_token': 'fixture', 'turns': []})
    assert '-t' not in calls[0] and '--toolsets' not in calls[0]


@pytest.mark.parametrize('web', [False, True])
def test_unavailable_tool_disclosure_tracks_disabled_web(tmp_path, web):
    from shadow.common.copy import copy
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    record['web_lookups'] = web
    engine.attach(record, 'controller')
    engine.step('controller')
    for request in ctx.subagent_lifecycle.launches:
        assert (copy('trial.tools_unavailable') in request['goal']) is (not web)
