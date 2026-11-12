"""Explain a missing setup result when a gateway may still own the chat."""

from ..switch.doctor import hermes_command


def missing_setup_result(home, profile):
    try:
        from hermes_cli.gateway_runtime import discover_gateway_endpoint

        if discover_gateway_endpoint(home).state != 'absent':
            return {'passed': False, 'incomplete': True,
                    'reason_code': 'gateway_restart_needed',
                    'restart_command': hermes_command(profile) + ' gateway restart'}
    except Exception:
        pass
    return {'passed': False, 'timed_out': True}
