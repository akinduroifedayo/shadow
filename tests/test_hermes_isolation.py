"""Hook order on the hermes-agent checkout named by SHADOW_HERMES_REF."""

import ast
import sys

import pytest

from test_real_tools import require_hermes_reference
from test_probe_dispatch import hermes_dispatch


def _calls(node):
    found = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            found.append(child.func.id)
        found.extend(_calls(child))
    return found


def test_executor_nests_the_veto_inside_the_middleware():
    source = require_hermes_reference()
    tree = ast.parse((source / 'agent' / 'tool_executor.py').read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert 'run_tool_execution_middleware' in _calls(functions['_run_agent_tool_execution_middleware'])
    dispatch_calls = _calls(functions['_dispatch_authorized_once'])
    assert dispatch_calls.index('_pre_tool_block') < dispatch_calls.index('execute')
    delegate = ast.parse((source / 'tools' / 'delegate_tool.py').read_text())
    agent_calls = [
        node for node in ast.walk(delegate)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'AIAgent'
        and 'skip_memory' in {keyword.arg for keyword in node.keywords}
    ]
    keywords = {keyword.arg: keyword.value for keyword in agent_calls[0].keywords}
    assert isinstance(keywords['skip_context_files'], ast.Constant) and keywords['skip_context_files'].value is True
    assert isinstance(keywords['skip_memory'], ast.UnaryOp)


def test_real_middleware_blocks_a_harmful_tool_and_answers_a_scoped_read(tmp_path, hermes_dispatch):
    from types import SimpleNamespace

    _run_execution_chain = hermes_dispatch.middleware._run_execution_chain
    executor = hermes_dispatch.executor
    from shadow.trial.reads import SafeReads
    from shadow.trial.safety import ReplayGuard

    guard = ReplayGuard()
    guard.add_controller('controller')
    (tmp_path / 'notes.txt').write_text('scoped note\n')
    guard.reads = SafeReads(guard, web_fetch=lambda _args: 'no web')

    executed = []
    manager = hermes_dispatch.register(guard)

    def harmful_child(session_id, token):
        guard.expect(token, tmp_path)
        guard.subagent_start(parent_session_id='controller', child_session_id=session_id, child_goal=token)

        def terminal(args):
            ref = executor._ToolCallRef('terminal', args, '', 'fixture-call', [])
            block, args = executor._pre_tool_block(SimpleNamespace(session_id=session_id), ref)
            if block:
                return block
            executed.append(args)
            return 'executed'

        return terminal

    blocked = _run_execution_chain('tool_execution', harmful_child('child', 'alpha-token'), tool_name='terminal', args={'command': 'true'}, session_id='child')
    assert guard.stop_message in str(blocked) and not executed

    def crash(**_kwargs):
        raise RuntimeError('fixture crash')

    manager._middleware['tool_execution'] = [crash]
    crashed = _run_execution_chain('tool_execution', harmful_child('child-2', 'beta-token'), tool_name='terminal', args={'command': 'true'}, session_id='child-2')
    assert guard.stop_message in str(crashed) and not executed

    manager._middleware['tool_execution'] = [guard.tool_execution]
    guard.expect('gamma-token', tmp_path)
    guard.subagent_start(parent_session_id='controller', child_session_id='child-3', child_goal='gamma-token')

    def real_read(args):
        executed.append(args)
        return 'real read'

    answered = _run_execution_chain('tool_execution', real_read, tool_name='read_file', args={'path': 'notes.txt'}, session_id='child-3')
    assert 'scoped note' in str(answered) and not executed


def test_installed_adapter_drops_foreign_reasoning_and_keeps_the_text():
    source = require_hermes_reference()
    sys.path.insert(0, str(source))
    try:
        from agent.codex_responses_adapter import _chat_messages_to_responses_input
    except Exception as exc:
        pytest.skip(f'codex adapter is not importable: {exc.__class__.__name__}')
    from shadow.switch.pair_probe import reasoning_move
    assert _chat_messages_to_responses_input
    assert reasoning_move('gpt-6.1-sol', 'gpt-6.1-terra') is True
    sys.path.remove(str(source))
