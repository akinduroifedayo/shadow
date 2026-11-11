def controller_stop_text(record, render):
    keys = {'controller_exited': 'trial.controller_exited',
            'controller_failed': 'trial.controller_failed', 'step_error': 'trial.controller_failed',
            'controller_interrupted': 'trial.controller_interrupted',
            'plugin_reloaded': 'trial.plugin_reloaded'}
    key = keys.get(record.get('stop_reason'))
    details = record.get('controller_stop_details') or {}
    reason = details.get('turn_exit_reason') or details.get('error_type') or 'unknown'
    return render(key, model=record['trial']['model'], reason=reason) if key else ''
