"""Refresh the process that owns classic Hermes chat before a setup controller is submitted."""

import logging
import sys


logger = logging.getLogger(__name__)


def refresh_setup_gateway(home):
    """Return a bounded failure code, or None for a refreshed or provably absent owner."""
    runner = sys.modules.get('gateway.run')
    if runner is not None and runner._gateway_runner_ref() is not None:
        return None
    try:
        from hermes_cli.gateway_runtime import discover_gateway_endpoint
        from hermes_cli.plugins_activation import reload_running_gateway

        answer = reload_running_gateway()
        if answer is None:
            reason = (None if discover_gateway_endpoint(home).state == 'absent'
                      else 'gateway_refresh_unconfirmed')
        elif not isinstance(answer, dict) or answer.get('reloaded') is not True:
            reason = 'gateway_refresh_refused'
        else:
            activations = answer.get('activations')
            activation = next((item for item in activations
                               if isinstance(item, dict) and item.get('key') == 'shadow'), None) \
                if isinstance(activations, list) else None
            hooks = (activation.get('activated_now') or {}).get('hooks', []) if activation else []
            reason = (None if 'shadow' in answer.get('plugins', []) and activation
                      and not activation.get('missing_dependencies') and 'pre_llm_call' in hooks
                      else 'gateway_shadow_unavailable')
    except Exception:
        reason = 'gateway_refresh_error'
    if reason:
        logger.warning('Shadow setup check could not start reason=%s', reason)
    return reason
