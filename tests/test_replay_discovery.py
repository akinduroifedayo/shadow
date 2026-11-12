import json
import sys
from types import SimpleNamespace

import pytest

from test_probe_dispatch import dispatch_case, hermes_dispatch


def test_child_describes_deferred_read_then_reads_without_running_real_tool(tmp_path, monkeypatch, hermes_dispatch):
    monkeypatch.setitem(sys.modules, 'snowballstemmer', SimpleNamespace(
        stemmer=lambda _: SimpleNamespace(stemWord=lambda word: word)))
    monkeypatch.setitem(sys.modules, 'tools.connectors.search', SimpleNamespace(
        connections_in_scope=lambda _: False, connector_entries_by_group=lambda *_a, **_kw: {},
        connectors_unavailable=lambda *_a, **_kw: pytest.fail('remote discovery attempted'),
        remote_schemas_for=lambda *_a, **_kw: ({}, None)))
    from tools import registry as registry_module, tool_search

    guard, tokens = dispatch_case(tmp_path, hermes_dispatch)
    registry = registry_module.ToolRegistry()
    monkeypatch.setattr(registry_module, 'registry', registry)
    schema = {'name': 'shadow_read', 'description': 'Read a scoped file.',
              'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}
    registry.register('shadow_read', 'shadow_replay', schema, lambda _: pytest.fail('real tool ran'))
    definitions = [{'type': 'function', 'function': schema}]
    config = tool_search.ToolSearchConfig.from_raw({'enabled': 'on'})

    def dispatch(name, args, handler):
        directive = guard.pre_tool_call(tool_name=name, args=args, session_id='real-child')
        assert directive is None, directive
        return guard.tool_execution(tool_name=name, args=args, next_call=handler,
                                    original_args=args, session_id='real-child')

    described = json.loads(dispatch('tool_describe', {'names': ['shadow_read']},
        lambda args: tool_search.dispatch_tool_describe(args, current_tool_defs=definitions, config=config)))
    assert described['tools']['shadow_read']['parameters'] == schema['parameters']
    read = json.loads(dispatch('read_file', {'path': 'notes.txt'}, lambda _: pytest.fail('real read ran')))
    assert tokens['scope'] in read['text']
    assert not guard.children['real-child'].stopped


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
@pytest.mark.parametrize('tool', ['tool_search', 'tool_call', 'connectors__x__y'])
def test_child_action_discovery_remains_blocked_and_stopped_child_cannot_describe(tmp_path, hermes_dispatch, layer, tool):
    guard, _ = dispatch_case(tmp_path, hermes_dispatch)
    first = ('tool_describe', {'names': [tool]}) if tool == 'connectors__x__y' else (tool, {})
    for name, args in (first, ('tool_describe', {'names': ['shadow_read']})):
        if layer == 'veto':
            directive = guard.pre_tool_call(tool_name=name, args=args, session_id='real-child')
            assert directive['message'] == guard.stop_message
        else:
            result = guard.tool_execution(tool_name=name, args=args, session_id='real-child',
                                          next_call=lambda _: pytest.fail('blocked tool ran'))
            assert guard.stop_message in result
    assert guard.children['real-child'].stopped


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
@pytest.mark.parametrize('names', [
    ['shadow_read', 'connectors__x__y'], ['terminal'], ['shadow_read', 'terminal'],
    ['tool_describe'], ['shadow_read', 'tool_describe'], [], None, 'shadow_read', [None], [['shadow_read']],
])
def test_child_cannot_describe_any_action_or_malformed_name(tmp_path, hermes_dispatch, layer, names):
    guard, _ = dispatch_case(tmp_path, hermes_dispatch)
    args = {'names': names}
    if layer == 'veto':
        assert guard.pre_tool_call(tool_name='tool_describe', args=args, session_id='real-child')['action'] == 'block'
    else:
        result = guard.tool_execution(tool_name='tool_describe', args=args, session_id='real-child',
                                      next_call=lambda _: pytest.fail('unsafe description ran'))
        assert guard.stop_message in result
    assert guard.children['real-child'].stopped


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
def test_child_can_describe_multiple_replay_tools(tmp_path, hermes_dispatch, layer):
    guard, _ = dispatch_case(tmp_path, hermes_dispatch)
    args = {'names': ['shadow_read', 'read_file', 'search_files', 'web_search', 'shadow_clarify']}
    if layer == 'veto':
        assert guard.pre_tool_call(tool_name='tool_describe', args=args, session_id='real-child') is None
    else:
        result = guard.tool_execution(tool_name='tool_describe', args=args, session_id='real-child',
                                      next_call=lambda _: 'replay schemas')
        assert result == 'replay schemas'
    assert not guard.children['real-child'].stopped


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
def test_unidentified_child_cannot_describe(tmp_path, hermes_dispatch, layer):
    guard, _ = dispatch_case(tmp_path, hermes_dispatch)
    if layer == 'veto':
        assert guard.pre_tool_call(tool_name='tool_describe', args={}, session_id='unknown',
                                   parent_session_id='controller')['action'] == 'block'
    else:
        result = guard.tool_execution(tool_name='tool_describe', args={}, session_id='unknown',
            parent_session_id='controller', next_call=lambda _: pytest.fail('unknown child described'))
        assert guard.stop_message in result
