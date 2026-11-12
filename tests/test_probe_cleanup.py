import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from shadow.trial import probe
from test_setup_check import _job, _plugin, _Life, active_home
from test_probe_dispatch import hermes_dispatch


def _sessions(home, ids):
    db = sqlite3.connect(home / 'state.db')
    db.executescript('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL); CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);')
    for sid in ids:
        db.execute('INSERT INTO sessions VALUES (?, ?, ?)', (sid, 'cli', 1))
    db.commit()
    db.close()


def test_probe_never_creates_a_live_session_database(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    ctx.subagent_lifecycle = _Life(ctx)
    existed = []
    launch = ctx.subagent_lifecycle.launch

    def inspect(request):
        existed.append((tmp_path / 'home' / 'state.db').exists())
        return launch(request)

    ctx.subagent_lifecycle.launch = inspect
    probe.SetupProbe(plugin, job).run()
    assert existed == [False]
    assert not (tmp_path / 'home' / 'state.db').exists()


def test_parent_cleanup_preserves_concurrent_chat(tmp_path, monkeypatch):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    home = tmp_path / 'home'
    _sessions(home, ['existing'])

    def wait(*args, **kwargs):
        _sessions(home, ['user-concurrent'])
        return {'passed': False}

    monkeypatch.setattr(probe, 'wait_for_result', wait)
    plugin.launcher.popen = lambda *args, **kwargs: SimpleNamespace()
    result = probe.launch_setup(plugin.launcher, job, killer=lambda _: None)
    assert 'user-concurrent' in probe.session_fingerprints(home)
    assert result['passed'] is False
    assert result['unchanged'] is False


def test_owned_cleanup_uses_sessiondb_and_rejects_foreign_cascades(tmp_path, monkeypatch):
    _sessions(tmp_path, ['owned', 'user'])
    deleted = []

    class SessionDB:
        def __init__(self, db_path):
            assert db_path == tmp_path / 'state.db'
        def get_session_delete_targets(self, sid):
            return [sid, 'user']
        def delete_session(self, sid, **kwargs):
            deleted.append(sid)
        def close(self):
            pass

    monkeypatch.setitem(sys.modules, 'hermes_state', SimpleNamespace(SessionDB=SessionDB))
    with pytest.raises(ValueError):
        probe.delete_sessions(tmp_path, ['owned'])
    assert deleted == []
    assert set(probe.session_fingerprints(tmp_path)) == {'owned', 'user'}


def _controller_launch(tmp_path, monkeypatch):
    from shadow.trial.isolation import write_record
    from test_setup_check import IDENTITY, checks
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    ctx.subagent_lifecycle = _Life(ctx)
    def popen(*args, **kwargs):
        assert kwargs['env']['HERMES_HOME'] == job['home']
        probe.finish_setup(plugin, 'setup', probe.prompt_text(job), data_dir=tmp_path)
        return SimpleNamespace()
    plugin.launcher.popen = popen
    return plugin, job


def test_child_reports_evidence_without_publishing_verification(tmp_path, monkeypatch):
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    probe.write_job(tmp_path, job)
    probe.finish_setup(plugin, 'setup', probe.prompt_text(job), data_dir=tmp_path)
    result = json.loads(probe.result_path(tmp_path, job['id']).read_text())
    assert result['passed'] is True
    assert set(result['sessions']) == {'setup', 'replay-child'}
    assert plugin.launcher.isolation_verified is False


def test_parent_publishes_only_after_process_cleanup(tmp_path, monkeypatch):
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    stopped = []
    def killer(_process):
        stopped.append(True)
        assert plugin.launcher.isolation_verified is False
    result = probe.launch_setup(plugin.launcher, job, killer=killer)
    assert stopped == [True]
    assert result['passed'] is True
    assert plugin.launcher.isolation_verified is True


def test_parent_profile_failure_cannot_leave_a_passing_record(tmp_path, monkeypatch):
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    def killer(_process):
        (tmp_path / 'home' / 'MEMORY.md').write_text('Changed memory while the setup controller was exiting.')
    result = probe.launch_setup(plugin.launcher, job, killer=killer)
    assert result['passed'] is False
    assert result['unchanged'] is False
    assert plugin.launcher.isolation_verified is False


def test_failed_setup_revokes_a_previous_verification(tmp_path, monkeypatch):
    from shadow.trial.isolation import write_record
    from test_setup_check import IDENTITY, checks
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    write_record(tmp_path, IDENTITY, checks())
    assert plugin.launcher.isolation_verified is True
    monkeypatch.setattr(probe, 'wait_for_result', lambda *args, **kwargs: None)
    plugin.launcher.popen = lambda *args, **kwargs: SimpleNamespace()
    assert probe.launch_setup(plugin.launcher, job, killer=lambda _: None)['passed'] is False
    assert plugin.launcher.isolation_verified is False


def test_controller_without_result_reports_timeout_without_claiming_leaks(tmp_path, monkeypatch):
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    plugin.launcher.popen = lambda *args, **kwargs: SimpleNamespace()
    monkeypatch.setattr(probe, 'wait_for_result', lambda *args, **kwargs: None)
    result = probe.launch_setup(plugin.launcher, job, killer=lambda _: None)
    text = probe.failure_text(result)
    assert result.get('timed_out') is True
    assert 'timed out' in text
    assert all(line not in text for line in probe.DETAIL.values())


def test_missing_check_evidence_does_not_report_unobserved_leaks():
    text = probe.failure_text({'passed': False})
    assert 'could not complete' in text
    assert all(line not in text for line in probe.DETAIL.values())


def test_process_is_reaped_before_parent_can_publish(tmp_path, monkeypatch):
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    reaped = []
    controller = plugin.launcher.popen

    def popen(*args, **kwargs):
        controller(*args, **kwargs)
        return SimpleNamespace(pid=12345, poll=lambda: None,
                               wait=lambda **kwargs: reaped.append(kwargs['timeout']))

    plugin.launcher.popen = popen
    monkeypatch.setattr(probe.os, 'killpg', lambda *args: None)
    result = probe.launch_setup(plugin.launcher, job)
    assert reaped
    assert result['passed'] is True
    assert plugin.launcher.isolation_verified is True


def test_controller_consumes_its_job_before_running_the_child(tmp_path, monkeypatch):
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    probe.write_job(tmp_path, job)
    launch = plugin.ctx.subagent_lifecycle.launch

    def child(request):
        assert not probe.job_path(tmp_path, job['id']).exists()
        return launch(request)

    plugin.ctx.subagent_lifecycle.launch = child
    probe.finish_setup(plugin, 'setup', probe.prompt_text(job), data_dir=tmp_path)
    assert not probe.job_path(tmp_path, job['id']).exists()
    assert json.loads(probe.result_path(tmp_path, job['id']).read_text())['passed'] is True


@pytest.mark.parametrize('outcome', ['pass', 'timeout', 'spawn-error', 'wait-error'])
def test_parent_removes_all_setup_artifacts(tmp_path, monkeypatch, outcome):
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    if outcome == 'timeout':
        plugin.launcher.popen = lambda *args, **kwargs: SimpleNamespace()
        monkeypatch.setattr(probe, 'wait_for_result', lambda *args, **kwargs: None)
    elif outcome in ('spawn-error', 'wait-error'):
        def fail(*args, **kwargs):
            raise OSError('controller unavailable')
        if outcome == 'spawn-error':
            plugin.launcher.popen = fail
        else:
            monkeypatch.setattr(probe, 'wait_for_result', fail)
    if outcome.endswith('error'):
        with pytest.raises(OSError):
            probe.launch_setup(plugin.launcher, job, killer=lambda _: None)
    else:
        result = probe.launch_setup(plugin.launcher, job, killer=lambda _: None)
        assert result['passed'] is (outcome == 'pass')
    assert list(tmp_path.glob('setup-*.json')) == []
    assert list(tmp_path.glob('setup-*.txt')) == []


def test_timed_out_setup_hook_cannot_call_the_controller_provider(tmp_path, monkeypatch, hermes_dispatch):
    import threading
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    probe.write_job(tmp_path, job)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    run = probe.SetupProbe.run
    hook = plugin.pre_llm_call

    def slow_probe(self):
        entered.set()
        assert release.wait(2)
        return run(self)

    def controller_hook(**kwargs):
        try:
            return hook(**kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(probe.SetupProbe, 'run', slow_probe)
    monkeypatch.setattr(hermes_dispatch.plugins, 'load_config_readonly',
                        lambda: {'plugins': {'hook_callback_timeout': 0.01}})
    manager = hermes_dispatch.manager
    manager._hooks['pre_llm_call'] = [controller_hook]
    manager._middleware['llm_execution'] = [plugin.ctx.middleware['llm_execution']]
    sent = []
    request = {'model': job['model'], 'input': [
        {'role': 'user', 'content': [{'type': 'input_text', 'text': probe.prompt_text(job)}]},
    ]}
    try:
        assert manager.invoke_hook('pre_llm_call', session_id='setup', user_message=probe.prompt_text(job), conversation_history=[]) == []
        assert entered.wait(2)
        assert not finished.is_set()
        hermes_dispatch.middleware.run_llm_execution_middleware(
            request, lambda payload: sent.append(payload) or 'provider called', session_id='setup', model=job['model'],
        )
        assert sent == []
    finally:
        release.set()
        assert finished.wait(2)
    assert json.loads(probe.result_path(tmp_path, job['id']).read_text())['passed'] is False
    assert plugin.launcher.isolation_verified is False


@pytest.mark.parametrize('format', ['messages', 'input', 'input-text'])
@pytest.mark.parametrize('rewritten', [False, True])
def test_skipped_setup_hook_and_retries_cannot_call_provider(tmp_path, hermes_dispatch, format, rewritten):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    probe.write_job(tmp_path, job)
    text = probe.prompt_text(job)
    if format == 'messages':
        original = {'messages': [{'role': 'user', 'content': text}]}
    elif format == 'input':
        original = {'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}]}
    else:
        original = {'input': text}
    request = {'input': 'rewritten prompt'} if rewritten else original
    hermes_dispatch.manager._middleware['llm_execution'] = [ctx.middleware['llm_execution']]
    sent = []
    execute = hermes_dispatch.middleware.run_llm_execution_middleware
    execute(request, lambda payload: sent.append(payload), session_id='setup', original_request=original)
    execute({'input': 'retry without marker'}, lambda payload: sent.append(payload), session_id='setup')
    assert sent == []
    assert plugin.launcher.isolation_verified is False
    replay = {'input': 'Reply with the single word ready.'}
    execute(replay, lambda payload: sent.append(payload), session_id='replay-child')
    assert sent == [replay]


def test_setup_execution_guard_preserves_ordinary_provider_calls(tmp_path, hermes_dispatch):
    ctx, plugin = _plugin(tmp_path)
    request = {'model': 'user-model', 'messages': [{'role': 'user', 'content': 'Explain the layout.'}]}
    sent = []
    hermes_dispatch.manager._middleware['llm_execution'] = [ctx.middleware['llm_execution']]
    answer = hermes_dispatch.middleware.run_llm_execution_middleware(
        request, lambda payload: sent.append(payload) or 'ordinary answer', session_id='ordinary',
    )
    assert answer == 'ordinary answer'
    assert sent == [request]


@pytest.mark.parametrize('kind', ['malformed', 'missing-job', 'wrong-token'])
@pytest.mark.parametrize('hook_runs', [False, True])
def test_untrusted_setup_marker_does_not_disable_later_chat(tmp_path, hermes_dispatch, kind, hook_runs):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    text = probe.prompt_text(job)
    if kind == 'malformed':
        text = probe.SETUP_PREFIX + 'not-a-prompt'
    elif kind == 'wrong-token':
        probe.write_job(tmp_path, job)
        text = text.replace(job['controller'], 'untrusted-token')
    if hook_runs:
        assert plugin.pre_llm_call(session_id='user-chat', user_message=text)['action'] == 'block'
    hermes_dispatch.manager._middleware['llm_execution'] = [ctx.middleware['llm_execution']]
    execute = hermes_dispatch.middleware.run_llm_execution_middleware
    sent = []
    execute({'messages': [{'role': 'user', 'content': text}]}, lambda payload: sent.append(payload), session_id='user-chat')
    ordinary = {'messages': [{'role': 'user', 'content': 'Continue our ordinary conversation.'}]}
    execute(ordinary, lambda payload: sent.append(payload), session_id='user-chat')
    assert sent == [ordinary]


def test_old_untrusted_setup_marker_in_history_does_not_disable_new_turn(tmp_path, hermes_dispatch):
    ctx, plugin = _plugin(tmp_path)
    hermes_dispatch.manager._middleware['llm_execution'] = [ctx.middleware['llm_execution']]
    request = {'messages': [
        {'role': 'user', 'content': probe.SETUP_PREFIX + 'not-a-prompt'},
        {'role': 'assistant', 'content': 'The setup check did not pass.'},
        {'role': 'user', 'content': 'Continue our ordinary conversation.'},
    ]}
    sent = []
    hermes_dispatch.middleware.run_llm_execution_middleware(
        request, lambda payload: sent.append(payload), session_id='user-chat',
    )
    assert sent == [request]


def test_setup_prompt_is_private_before_the_controller_starts(tmp_path, monkeypatch):
    import io
    import os
    import stat
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    open_file = io.open
    modes = []

    def inspect_creation(file, mode='r', *args, **kwargs):
        handle = open_file(file, mode, *args, **kwargs)
        prompt = tmp_path / f'setup-{job["id"]}.txt'
        if 'w' in mode and prompt.exists() and os.path.samestat(os.fstat(handle.fileno()), prompt.stat()):
            modes.append(stat.S_IMODE(os.fstat(handle.fileno()).st_mode))
        return handle

    monkeypatch.setattr(io, 'open', inspect_creation)
    previous_umask = os.umask(0)
    try:
        probe.launch_setup(plugin.launcher, job, killer=lambda _: None)
    finally:
        os.umask(previous_umask)
    assert modes == [0o600]


@pytest.mark.parametrize('collision', ['job', 'prompt'])
def test_setup_refuses_artifact_collisions_without_deleting_existing_files(tmp_path, monkeypatch, collision):
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    path = probe.job_path(tmp_path, job['id']) if collision == 'job' else tmp_path / f'setup-{job["id"]}.txt'
    path.write_text('Keep this existing file.')
    with pytest.raises(FileExistsError):
        probe.launch_setup(plugin.launcher, job, killer=lambda _: None)
    assert path.read_text() == 'Keep this existing file.'


def test_timeout_keeps_observed_chat_change_visible(tmp_path, monkeypatch):
    plugin, job = _controller_launch(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    _sessions(home, ['existing'])

    def launch(*args, **kwargs):
        _sessions(home, ['unclaimed-setup-chat'])
        return SimpleNamespace()

    plugin.launcher.popen = launch
    monkeypatch.setattr(probe, 'wait_for_result', lambda *args, **kwargs: None)
    result = probe.launch_setup(plugin.launcher, job, killer=lambda _: None)
    text = probe.failure_text(result)
    assert result['timed_out'] is True and result['unchanged'] is False
    assert 'timed out' in text
    assert probe.DETAIL['unchanged'] in text
    assert 'unclaimed-setup-chat' in probe.session_fingerprints(home)
    assert all(line not in text for name, line in probe.DETAIL.items() if name != 'unchanged')


def test_empty_memory_pass_copy_survives_result_and_signed_record(tmp_path, monkeypatch):
    from shadow.common.models import Model
    from shadow.trial.isolation import read_record

    plugin, job = _controller_launch(tmp_path, monkeypatch)
    (tmp_path / 'home' / 'MEMORY.md').unlink()
    result = probe.launch_setup(plugin.launcher, job, killer=lambda _: None)
    assert result['passed'] is True
    assert result['no_saved_memory'] is True
    assert read_record(tmp_path)['no_saved_memory'] is True
    assert plugin.launcher.isolation_verified is True
    assert 'This profile has no saved memory.' in plugin.setup_tool({})
    assert 'A replay did not see your memory' not in plugin.setup_tool({})
    plugin.current = Model('openai-codex', 'gpt-6.1-sol')
    plugin.catalog = [plugin.current]
    plugin.setup_launch = lambda *_: result
    text = plugin.verify_isolation(terminal=True, yes=True)
    assert 'This profile has no saved memory.' in text
    assert 'A replay did not see your memory' not in text


def test_empty_memory_claim_is_covered_by_the_record_signature(tmp_path):
    from shadow.trial.isolation import read_record, record_path, write_record
    from test_setup_check import IDENTITY, checks, launcher

    write_record(tmp_path, IDENTITY, checks(), no_saved_memory=True)
    record = read_record(tmp_path)
    record['no_saved_memory'] = False
    record_path(tmp_path).write_text(json.dumps(record))
    assert launcher(tmp_path).isolation_verified is False
