"""Prove registered Hermes guard paths without invoking a real tool."""

from dataclasses import replace
from types import SimpleNamespace


def run_dispatch_checks(guard, session_id, scope, tokens):
    from agent.tool_executor import _ToolCallRef, _pre_tool_block
    from hermes_cli.middleware import run_tool_execution_middleware
    from hermes_cli.plugins import _delivery_manager

    checks = {'veto': False, 'middleware': False, 'read_only': False, 'secrets': False}
    manager = _delivery_manager()
    registered_veto = guard.pre_tool_call in manager._hooks.get('pre_tool_call', ())
    registered_middleware = guard.tool_execution in manager._middleware.get('tool_execution', ())
    original = guard.children.get(session_id)
    if original is None:
        checks['reason_code'] = 'read_only_child_missing'
        return checks
    if original.cwd != scope.resolve():
        checks['reason_code'] = 'read_only_scope_mismatch'
        return checks
    agent = SimpleNamespace(session_id=session_id)

    def exercise(tool_name, args, *, layer):
        attempts = []

        def inert_executor(final_args):
            attempts.append(final_args)
            return 'setup inert executor reached'

        guard.children[session_id] = replace(original, stopped=False)
        if layer == 'veto':
            ref = _ToolCallRef(tool_name, args, '', 'shadow-setup-proof', [])
            block, final_args = _pre_tool_block(agent, ref)
            result = block if block else inert_executor(final_args)
        else:
            result = run_tool_execution_middleware(tool_name, args, inert_executor,
                                                   session_id=session_id)
        return str(result), attempts

    try:
        veto, attempts = exercise('terminal', {'command': 'true'}, layer='veto')
        checks['veto'] = registered_veto and guard.stop_message in veto and not attempts
        middleware, attempts = exercise('terminal', {'command': 'true'}, layer='middleware')
        checks['middleware'] = registered_middleware and guard.stop_message in middleware and not attempts
        allowed_veto, veto_attempts = exercise('read_file', {'path': 'notes.txt'}, layer='veto')
        allowed, attempts = exercise('read_file', {'path': 'notes.txt'}, layer='middleware')
        read_conditions = {
            'read_only_veto_unregistered': registered_veto,
            'read_only_middleware_unregistered': registered_middleware,
            'read_only_veto_executor_not_reached': bool(veto_attempts),
            'read_only_veto_result_mismatch': allowed_veto == 'setup inert executor reached',
            'read_only_scope_token_missing': tokens['scope'] in allowed,
            'read_only_middleware_executor_reached': not attempts,
        }
        failures = [reason for reason, passed in read_conditions.items() if not passed]
        checks['read_only'] = not failures
        if failures:
            checks['reason_code'] = ','.join(failures)
        checks['secrets'] = registered_veto and registered_middleware
        for path, token in (('.env', tokens['secret']), ('../outside.txt', tokens['outside'])):
            veto, veto_attempts = exercise('read_file', {'path': path}, layer='veto')
            answered, attempts = exercise('read_file', {'path': path}, layer='middleware')
            checks['secrets'] &= bool(
                guard.stop_message in veto and not veto_attempts and not attempts and token not in answered
            )
        return checks
    finally:
        guard.children[session_id] = original
