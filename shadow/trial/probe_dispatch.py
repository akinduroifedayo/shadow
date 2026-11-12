"""Prove Shadow guard callbacks without invoking a real tool."""

from dataclasses import replace


def run_dispatch_checks(guard, session_id, scope, tokens):
    checks = {'veto': False, 'middleware': False, 'read_only': False, 'secrets': False}
    original = guard.children.get(session_id)
    if original is None:
        checks['reason_code'] = 'read_only_child_missing'
        return checks
    if original.cwd != scope.resolve():
        checks['reason_code'] = 'read_only_scope_mismatch'
        return checks

    def exercise(tool_name, args, *, layer):
        attempts = []

        def inert_executor(final_args):
            attempts.append(final_args)
            return 'setup inert executor reached'

        guard.children[session_id] = replace(original, stopped=False)
        identity = {'session_id': session_id, 'task_id': '', 'tool_call_id': 'shadow-setup-proof',
                    'turn_id': '', 'api_request_id': '', 'conversation_id': ''}
        if layer == 'veto':
            directive = guard.pre_tool_call(
                tool_name=tool_name, args=args, middleware_trace=[], **identity)
            result = (directive['message'] if directive and directive.get('action') == 'block'
                      else inert_executor(args))
        else:
            result = guard.tool_execution(
                tool_name=tool_name, args=args, next_call=inert_executor,
                original_args=args, **identity)
        return str(result), attempts

    try:
        veto, attempts = exercise('terminal', {'command': 'true'}, layer='veto')
        checks['veto'] = guard.stop_message in veto and not attempts
        middleware, attempts = exercise('terminal', {'command': 'true'}, layer='middleware')
        checks['middleware'] = guard.stop_message in middleware and not attempts
        allowed_veto, veto_attempts = exercise('read_file', {'path': 'notes.txt'}, layer='veto')
        allowed, attempts = exercise('read_file', {'path': 'notes.txt'}, layer='middleware')
        read_conditions = {
            'read_only_veto_executor_not_reached': bool(veto_attempts),
            'read_only_veto_result_mismatch': allowed_veto == 'setup inert executor reached',
            'read_only_scope_token_missing': tokens['scope'] in allowed,
            'read_only_middleware_executor_reached': not attempts,
        }
        failures = [reason for reason, passed in read_conditions.items() if not passed]
        checks['read_only'] = not failures
        if failures:
            checks['reason_code'] = ','.join(failures)
        checks['secrets'] = True
        for path, token in (('.env', tokens['secret']), ('../outside.txt', tokens['outside'])):
            veto, veto_attempts = exercise('read_file', {'path': path}, layer='veto')
            answered, attempts = exercise('read_file', {'path': path}, layer='middleware')
            checks['secrets'] &= bool(
                guard.stop_message in veto and not veto_attempts and not attempts and token not in answered
            )
        return checks
    finally:
        guard.children[session_id] = original
