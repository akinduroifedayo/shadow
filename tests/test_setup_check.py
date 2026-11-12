import json
from concurrent.futures import Future
from types import SimpleNamespace
import sys
import threading

import pytest

from test_probe_dispatch import hermes_dispatch

from fixtures import FakeContext
from shadow.common.storage import Store
from shadow.trial.controller import ControllerLauncher
from shadow.trial.isolation import CHECK_NAMES, Identity, config_can_verify, matches, read_record, write_record
from shadow.trial.plugin import TrialPlugin, register
from shadow.trial.probe import SetupProbe, new_job


IDENTITY = Identity('9.9.9', 'abcdef1234567890', '0.1.0', 'fixture-hash', 'demo')


def checks(passed=True):
    return {name: True for name in CHECK_NAMES} if passed else {name: name != 'memory' for name in CHECK_NAMES}


def launcher(tmp_path, identity=IDENTITY):
    return ControllerLauncher('demo', tmp_path, identity=lambda: identity)


def test_passing_record_verifies_and_a_missing_one_does_not(tmp_path):
    made = launcher(tmp_path)
    assert made.isolation_verified is False
    write_record(tmp_path, IDENTITY, checks())
    assert made.isolation_verified is True
    assert matches(read_record(tmp_path), IDENTITY, 'demo')


def test_stale_hermes_or_plugin_hash_fails_closed(tmp_path):
    write_record(tmp_path, IDENTITY, checks())
    stale_hermes = Identity(IDENTITY.hermes_version, 'other-commit', IDENTITY.plugin_version, IDENTITY.plugin_hash, 'demo')
    stale_files = Identity(IDENTITY.hermes_version, IDENTITY.hermes_commit, IDENTITY.plugin_version, 'other-hash', 'demo')
    assert launcher(tmp_path, stale_hermes).isolation_verified is False
    assert launcher(tmp_path, stale_files).isolation_verified is False


def test_tampered_record_fails_closed(tmp_path):
    write_record(tmp_path, IDENTITY, checks())
    path = tmp_path / 'isolation.json'
    body = json.loads(path.read_text())
    body['checks']['memory'] = False
    path.write_text(json.dumps(body))
    assert launcher(tmp_path).isolation_verified is False
    body['checks']['memory'] = True
    body['digest'] = '0' * 64
    path.write_text(json.dumps(body))
    assert launcher(tmp_path).isolation_verified is False
    body['passed'] = False
    path.write_text(json.dumps(body))
    assert launcher(tmp_path).isolation_verified is False


def test_config_and_constructor_cannot_verify(tmp_path):
    made = ControllerLauncher('demo', tmp_path, isolation_verified=True, identity=lambda: IDENTITY)
    assert made.isolation_verified is False
    assert config_can_verify({'plugins': {'entries': {'shadow': {'settings': {'isolation_verified': True}}}}}) is False


_active_dispatch = None


def _plugin(tmp_path):
    ctx = FakeContext()
    plugin = TrialPlugin(ctx, store=Store(tmp_path / 'trial.sqlite'), launcher=launcher(tmp_path))
    register(ctx, plugin=plugin)
    if _active_dispatch is not None:
        _active_dispatch.register(plugin.guard)
        plugin.guard_registrations = dict(zip(('veto', 'middleware'), _active_dispatch.registrations))
    return ctx, plugin


@pytest.fixture(autouse=True)
def active_home(tmp_path, monkeypatch, hermes_dispatch):
    from hermes_cli import config
    monkeypatch.setattr(config, 'load_config_readonly', lambda: {})
    monkeypatch.setattr(sys.modules[__name__], '_active_dispatch', hermes_dispatch)
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(get_hermes_home=lambda: tmp_path / 'home'))


class _Life:
    def __init__(self, ctx, leak='', *, model=None, copies=1, timeout=False, summary='ready', system=''):
        self.ctx = ctx
        self.leak = leak
        self.model = model
        self.copies = copies
        self.timeout = timeout
        self.summary = summary
        self.system = system
        self.waited = False
        self.release = threading.Event()
        self.future = Future()

    def launch(self, request):
        session_id = 'replay-child'
        self.ctx.hook('subagent_start', parent_session_id='setup', child_session_id=session_id, child_goal=request['goal'])

        def child():
            self.release.wait()
            try:
                goal = request['goal'] + self.leak
                self.ctx.hook('pre_llm_call', session_id=session_id, parent_session_id='setup', user_message=goal, conversation_history=[{'role': 'user', 'content': goal}])
                self.ctx.hook('pre_api_request', session_id=session_id, request_messages=[{'role': 'user', 'content': goal}], system_prompt=self.system)
                for index in range(self.copies):
                    self.ctx.hook('post_api_request', session_id=session_id, api_request_id=f'call-{index}', provider=request['provider'], response_model=self.model or request['model'], base_url='https://chatgpt.com/backend-api/codex', api_mode='codex_responses', usage={'prompt_tokens': 8, 'completion_tokens': 1})
                self.future.set_result(None)
            except BaseException as exc:
                self.future.set_exception(exc)

        self.thread = threading.Thread(target=child)
        self.thread.start()
        return session_id

    def wait(self, handle, *, timeout_seconds):
        self.waited = True
        if self.timeout:
            return SimpleNamespace(completed=False, timed_out=True)
        self.release.set()
        self.future.result(timeout=timeout_seconds)
        return SimpleNamespace(completed=True, timed_out=False)

    def result(self, handle):
        return SimpleNamespace(ready=True, terminal_state='SUCCEEDED', summary=self.summary)

    def cancel(self, handle, *, reason):
        self.release.set()
        self.thread.join(timeout=2)
        self.future.result(timeout=2)


def _job(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'MEMORY.md').write_text('The user keeps a long private note about the workshop layout.\n')
    job = new_job(home=home, workspace=tmp_path / 'workspace', provider='openai-codex', model='gpt-6.1-sol', profile='demo')
    job['controller_session'] = 'setup'
    return job


def test_probe_passes_when_the_replay_is_isolated(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    ctx.subagent_lifecycle = _Life(ctx)
    job = _job(tmp_path)
    result = SetupProbe(plugin, job).run()
    assert result['passed'] is True, result
    assert all(result[name] is True for name in CHECK_NAMES)
    assert not (tmp_path / 'workspace' / 'scope' / 'ran.txt').exists()
    assert 'workshop layout' in (tmp_path / 'home' / 'MEMORY.md').read_text()


def test_probe_fails_when_the_replay_sees_this_chat(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    ctx.subagent_lifecycle = _Life(ctx, leak=job['live'])
    result = SetupProbe(plugin, job).run()
    assert result['passed'] is False
    assert result['live_context'] is False
    assert not (tmp_path / 'isolation.json').exists()


def test_verify_command_names_the_model_and_does_not_call_until_yes(tmp_path, monkeypatch):
    ctx, plugin = _plugin(tmp_path)
    plugin.current = __import__('shadow.common.models', fromlist=['Model']).Model('openai-codex', 'gpt-6.1-sol')
    plugin.catalog = [plugin.current]
    called = []
    plugin.setup_launch = lambda *args, **kwargs: called.append(args) or {'passed': False}
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    (tmp_path / 'home').mkdir()
    declined = ctx.commands['shadow']('doctor --verify')
    assert 'openai-codex / gpt-6.1-sol' in declined
    assert 'Nothing was called.' in declined
    assert called == []
    plugin.setup_launch = lambda launcher, job: write_record(launcher.directory, launcher.identity, checks()) or {'passed': True}
    accepted = ctx.commands['shadow']('doctor --verify --yes')
    assert 'The setup check passed' in accepted
    assert plugin.launcher.isolation_verified is True


def test_one_check_at_a_time(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    assert ctx.commands['shadow']('doctor --verify --verify-pair') == 'Run one check at a time.'


def test_probe_requires_bounded_completion_and_one_valid_answer(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    ctx.subagent_lifecycle = _Life(ctx)
    result = SetupProbe(plugin, _job(tmp_path)).run()
    assert ctx.subagent_lifecycle.waited is True
    assert result['model'] is True


@pytest.mark.parametrize('options', [
    {'timeout': True}, {'summary': ''}, {'model': 'gpt-6-luna'}, {'copies': 0}, {'copies': 2},
])
def test_probe_refuses_incomplete_or_wrong_child_answers(tmp_path, options):
    ctx, plugin = _plugin(tmp_path)
    ctx.subagent_lifecycle = _Life(ctx, **options)
    result = SetupProbe(plugin, _job(tmp_path)).run()
    assert result['passed'] is False
    assert result.get('model') is (None if options.get('timeout') else False)


def test_probe_accepts_dated_model_identity(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    ctx.subagent_lifecycle = _Life(ctx, model='gpt-6.1-sol-2026-11-01')
    result = SetupProbe(plugin, _job(tmp_path)).run()
    assert result['model'] is True


@pytest.mark.parametrize('check,source', [
    ('memory', 'system'), ('live_context', 'system'), ('other_sessions', 'system'),
    ('memory', 'history'), ('other_sessions', 'history'),
])
def test_probe_refuses_final_request_context_leaks(tmp_path, check, source):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    leaked = {
        'memory': 'The user keeps a long private note about the workshop layout.',
        'live_context': job['live'], 'other_sessions': job['other'],
    }[check]
    ctx.subagent_lifecycle = _Life(ctx, system=leaked if source == 'system' else '', leak=leaked if source == 'history' else '')
    result = SetupProbe(plugin, job).run()
    assert result[check] is False
    assert result['passed'] is False


def test_probe_accepts_an_empty_memory_profile_without_writing_memory(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    (tmp_path / 'home' / 'MEMORY.md').unlink()
    ctx.subagent_lifecycle = _Life(ctx)
    result = SetupProbe(plugin, job).run()
    assert result['memory'] is True
    assert result['passed'] is True
    assert result['no_saved_memory'] is True
    assert result['unchanged'] is True
    assert list((tmp_path / 'home').iterdir()) == []


@pytest.mark.parametrize('system', [
    'You have persistent memory, carried across sessions',
    'You have a persistent user profile, carried across sessions',
    'MEMORY (your personal notes) [1%]\nUnexpected memory.',
    'USER PROFILE (who the user is) [1%]\nUnexpected profile.',
])
def test_empty_memory_profile_refuses_memory_guidance_and_blocks(tmp_path, system):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    (tmp_path / 'home' / 'MEMORY.md').unlink()
    ctx.subagent_lifecycle = _Life(ctx, system=system)
    result = SetupProbe(plugin, job).run()
    assert result['memory'] is False
    assert result['passed'] is False


@pytest.mark.parametrize('text', ['', '   \n', 'Private fact.', 'User is Ife.', '12345678'])
def test_memory_source_distinguishes_empty_and_short_saved_memory(tmp_path, text):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    (tmp_path / 'home' / 'MEMORY.md').write_text(text)
    ctx.subagent_lifecycle = _Life(ctx)
    result = SetupProbe(plugin, job).run()
    assert result['memory'] is True
    assert result['passed'] is True
    assert result['no_saved_memory'] is (not text.strip())


@pytest.mark.parametrize('layer', ['veto', 'middleware'])
def test_setup_refuses_a_bypassed_blocking_callback(tmp_path, monkeypatch, layer):
    ctx, plugin = _plugin(tmp_path)
    if layer == 'veto':
        monkeypatch.setattr(plugin.guard, 'pre_tool_call', lambda **kwargs: None)
    else:
        monkeypatch.setattr(plugin.guard, 'tool_execution', lambda **kwargs: kwargs['next_call'](kwargs['args']))
    ctx.subagent_lifecycle = _Life(ctx)
    result = SetupProbe(plugin, _job(tmp_path)).run()
    assert result[layer] is False
    assert result['passed'] is False


def test_verify_uses_hermes_context_home_over_process_environment(tmp_path, monkeypatch):
    from shadow.common.models import Model
    ctx, plugin = _plugin(tmp_path)
    plugin.current = Model('openai-codex', 'gpt-6.1-sol')
    plugin.catalog = [plugin.current]
    seen = []
    plugin.setup_launch = lambda launcher, job: seen.append(job) or {'passed': False}
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'different-profile'))
    ctx.commands['shadow']('doctor --verify --yes')
    assert seen[0]['home'] == str(tmp_path / 'home')


def test_controller_refuses_job_for_a_different_active_home(tmp_path, monkeypatch):
    from shadow.trial.probe import finish_setup, prompt_text, write_job
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    write_job(tmp_path, job)
    calls = []
    ctx.subagent_lifecycle.launch = lambda request: calls.append(request)
    monkeypatch.setattr(sys.modules['hermes_constants'], 'get_hermes_home', lambda: tmp_path / 'different-profile')
    finish_setup(plugin, 'setup', prompt_text(job), data_dir=tmp_path)
    assert calls == []
    assert plugin.launcher.isolation_verified is False


def test_probe_refuses_child_with_a_different_active_home(tmp_path, monkeypatch):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    ctx.subagent_lifecycle = _Life(ctx)
    monkeypatch.setattr(sys.modules['hermes_constants'], 'get_hermes_home', lambda:
        tmp_path / ('home' if threading.current_thread() is threading.main_thread() else 'different-profile'))
    result = SetupProbe(plugin, job).run()
    assert result['passed'] is False


@pytest.mark.parametrize('hook_timeout,expected_wait', [(30, 25), (10, 5), (0, 60)])
def test_setup_child_wait_reserves_hook_cleanup_time(tmp_path, monkeypatch, hermes_dispatch, hook_timeout, expected_wait):
    import shadow.trial.probe as probe
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    clock = [100.0]
    snapshot = probe.profile_snapshot

    def slow_snapshot(home):
        clock[0] += 2
        return snapshot(home)

    monkeypatch.setattr(probe.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(probe, 'profile_snapshot', slow_snapshot)
    from hermes_cli import config
    monkeypatch.setattr(config, 'load_config_readonly',
                        lambda: {'plugins': {'hook_callback_timeout': hook_timeout}})
    ctx.subagent_lifecycle = _Life(ctx)
    wait = ctx.subagent_lifecycle.wait
    budgets = []

    def observe_wait(handle, *, timeout_seconds):
        budgets.append(timeout_seconds)
        return wait(handle, timeout_seconds=timeout_seconds)

    ctx.subagent_lifecycle.wait = observe_wait
    assert probe.SetupProbe(plugin, job).run()['passed'] is True
    assert budgets == [expected_wait]


def test_setup_child_timeout_has_its_own_diagnostic(tmp_path):
    import shadow.trial.probe as probe
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    ctx.subagent_lifecycle = _Life(ctx, timeout=True)
    probe.write_job(tmp_path, job)
    result = probe.finish_setup(plugin, 'setup', probe.prompt_text(job), data_dir=tmp_path)
    saved = json.loads(probe.result_path(tmp_path, job['id']).read_text())
    assert saved['passed'] is False
    assert 'timed out' in result['message'].lower()
    assert 'timed out' in probe.failure_text(saved).lower()
    assert 'did not run on the model' not in result['message']


def test_setup_missing_response_model_fails_without_an_exception(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    ctx.subagent_lifecycle = _Life(ctx)
    hook = ctx.hook

    def missing_model(name, **event):
        if name == 'post_api_request':
            event['response_model'] = None
        return hook(name, **event)

    ctx.hook = missing_model
    result = SetupProbe(plugin, job).run()
    assert result['passed'] is False
    assert result['model'] is False
    assert ctx.subagent_lifecycle.thread.is_alive() is False
    assert plugin.setup_active is None


@pytest.mark.parametrize('stop', ['home', 'no-child', 'extra-child'])
def test_skipped_setup_checks_remain_unknown_in_the_result_file(tmp_path, monkeypatch, stop):
    from shadow.trial import probe

    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    ctx.subagent_lifecycle = _Life(ctx)
    if stop == 'home':
        monkeypatch.setattr(probe, 'active_home', lambda: tmp_path / 'another-home')
    else:
        wait = ctx.subagent_lifecycle.wait

        def child_count(handle, *, timeout_seconds):
            terminal = wait(handle, timeout_seconds=timeout_seconds)
            if stop == 'no-child':
                plugin.guard.children.clear()
            else:
                plugin.guard.children['extra-child'] = plugin.guard.children['replay-child']
            return terminal

        ctx.subagent_lifecycle.wait = child_count
    result = SetupProbe(plugin, job).run()
    assert result['incomplete'] is True and result['passed'] is False
    assert all(result.get(name) is None for name in (*CHECK_NAMES, 'model') if name != 'unchanged')
    probe.write_result(tmp_path, job['id'], result, sessions=[])
    saved = json.loads(probe.result_path(tmp_path, job['id']).read_text())
    assert saved['memory'] is None and saved['model'] is None
    text = probe.failure_text(saved)
    assert 'could not complete' in text
    assert all(line not in text for name, line in probe.DETAIL.items() if name != 'unchanged')


@pytest.mark.parametrize('refused', ['already-ran', 'wrong-home'])
def test_authenticated_refused_job_writes_an_incomplete_result(tmp_path, monkeypatch, refused):
    from shadow.trial import probe

    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    probe.write_job(tmp_path, job)
    if refused == 'already-ran':
        plugin.setup_used.add(job['id'])
    else:
        monkeypatch.setattr(probe, 'active_home', lambda: tmp_path / 'another-home')
    response = probe.finish_setup(plugin, 'setup', probe.prompt_text(job), data_dir=tmp_path)
    saved = json.loads(probe.result_path(tmp_path, job['id']).read_text())
    assert saved['incomplete'] is True and saved['passed'] is False
    assert saved['timed_out'] is not True
    assert saved['memory'] is None and saved['model'] is None
    assert 'could not complete' in response['message']
    assert plugin.launcher.isolation_verified is False


def test_result_round_trip_preserves_unknown_and_observed_checks(tmp_path):
    from shadow.trial import probe

    checks = {**probe.blank_checks(), 'memory': False, 'live_context': True,
              'passed': False, 'incomplete': True}
    probe.write_result(tmp_path, 'unknown', checks, sessions=[])
    saved = json.loads(probe.result_path(tmp_path, 'unknown').read_text())
    assert saved['memory'] is False and saved['live_context'] is True
    assert saved['secrets'] is None and saved['model'] is None
    text = probe.failure_text(saved)
    assert probe.DETAIL['memory'] in text
    assert probe.DETAIL['secrets'] not in text


@pytest.mark.parametrize('terminal', [False, True])
def test_setup_confirmation_describes_check_and_prints_restart_hint(tmp_path, capsys, terminal):
    from shadow.common.models import Model

    ctx, plugin = _plugin(tmp_path)
    plugin.current = Model('openai-codex', 'gpt-6.1-sol')
    plugin.catalog = [plugin.current]
    plugin.setup_launch = lambda *args: {'passed': False, 'incomplete': True,
                                        'reason_code': 'gateway_restart_needed',
                                        'restart_command': 'hermes -p demo gateway restart'}
    text = plugin.verify_isolation(terminal=terminal, yes=True)
    shown = text + capsys.readouterr().out
    assert 'isolate a replay' in shown
    assert 'hermes -p demo gateway restart' in text
    assert 'Setup check reason: gateway_restart_needed.' in text
    assert 'could not complete' in text


def test_short_saved_memory_still_detects_a_leak(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    (tmp_path / 'home' / 'MEMORY.md').write_text('Private fact.')
    ctx.subagent_lifecycle = _Life(ctx, system='Private fact.')
    result = SetupProbe(plugin, job).run()
    assert result['memory'] is False
    assert result['passed'] is False


def test_symlinked_memory_does_not_claim_an_empty_profile(tmp_path):
    from shadow.trial.probe import memory_snippet

    home = tmp_path / 'home'
    home.mkdir()
    (home / 'MEMORY.md').symlink_to(tmp_path / 'missing-memory')
    assert memory_snippet(home) is None


def test_symlinked_memory_does_not_claim_an_empty_profile_or_a_leak(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    note = tmp_path / 'home' / 'MEMORY.md'
    note.unlink()
    note.symlink_to(tmp_path / 'missing-memory')
    ctx.subagent_lifecycle = _Life(ctx)
    result = SetupProbe(plugin, job).run()
    assert result['memory'] is None
    assert result['incomplete'] is True
    assert result['passed'] is False
    assert result.get('no_saved_memory') is not True
    from shadow.trial.probe import failure_text
    assert 'A replay could see your memory.' not in failure_text(result)


def test_empty_memory_verification_expires_when_saved_memory_appears(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    note = tmp_path / 'home' / 'MEMORY.md'
    note.unlink()
    ctx.subagent_lifecycle = _Life(ctx)
    result = SetupProbe(plugin, job).run()
    assert result['passed'] is True and result['no_saved_memory'] is True
    write_record(tmp_path, IDENTITY, {name: result[name] for name in CHECK_NAMES},
                 no_saved_memory=result['no_saved_memory'])
    assert plugin.launcher.isolation_verified is True
    note.write_text('User is Ife.')
    assert plugin.launcher.isolation_verified is False
    shown = ctx.commands['shadow']('doctor')
    assert 'isolation are not verified' in shown
    assert 'This profile has no saved memory.' not in shown


@pytest.mark.parametrize('no_saved_memory', [False, True])
def test_doctor_qualifies_only_an_empty_memory_verification(tmp_path, no_saved_memory):
    ctx, plugin = _plugin(tmp_path)
    (tmp_path / 'home').mkdir()
    write_record(tmp_path, IDENTITY, checks(), no_saved_memory=no_saved_memory)
    shown = ctx.commands['shadow']('doctor')
    assert 'Replay context and memory isolation was checked on' in shown
    assert ('This profile has no saved memory.' in shown) is no_saved_memory
    assert ('The replay received no memory guidance or memory blocks.' in shown) is no_saved_memory


def test_empty_memory_verification_fails_closed_when_active_home_is_unknown(tmp_path, monkeypatch):
    write_record(tmp_path, IDENTITY, checks(), no_saved_memory=True)
    (tmp_path / 'home').mkdir()
    made = launcher(tmp_path)
    assert made.isolation_verified is True
    monkeypatch.setattr('shadow.trial.probe.active_home', lambda: None)
    assert made.isolation_verified is False


@pytest.mark.parametrize('provider', ['honcho', 'mem0', '  HONCHO  '])
def test_external_memory_provider_is_incomplete_without_claiming_a_leak_or_launching(tmp_path, monkeypatch, provider):
    from hermes_cli import config
    from shadow.trial.probe import failure_text

    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    (tmp_path / 'home' / 'MEMORY.md').unlink()
    monkeypatch.setattr(config, 'load_config_readonly', lambda: {'memory': {'provider': provider}})
    ctx.subagent_lifecycle = _Life(ctx)
    launch = ctx.subagent_lifecycle.launch
    launched = []

    def observe_launch(request):
        launched.append(request)
        return launch(request)

    ctx.subagent_lifecycle.launch = observe_launch
    result = SetupProbe(plugin, job).run()
    assert result['passed'] is False and result['incomplete'] is True
    assert result['memory'] is None
    assert result.get('no_saved_memory') is not True
    assert result['reason_code'] == 'memory_provider_unverifiable'
    assert launched == []
    shown = failure_text(result)
    assert 'could not complete' in shown
    assert 'A replay could see your memory.' not in shown
    assert 'no saved memory' not in shown


@pytest.mark.parametrize('provider', [None, '', 'default', 'builtin', 'built-in', 'none', '  BUILT-IN  ', ' NONE '])
def test_core_memory_provider_aliases_can_verify_an_empty_profile(tmp_path, monkeypatch, provider):
    from hermes_cli import config

    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    (tmp_path / 'home' / 'MEMORY.md').unlink()
    monkeypatch.setattr(config, 'load_config_readonly', lambda: {'memory': {'provider': provider}})
    ctx.subagent_lifecycle = _Life(ctx)
    result = SetupProbe(plugin, job).run()
    assert result['passed'] is True
    assert result['memory'] is True and result['no_saved_memory'] is True


@pytest.mark.parametrize('text', ['Ife', '1234567', 'a\nb\n'])
def test_tiny_saved_memory_is_incomplete_without_claiming_a_leak(tmp_path, text):
    from shadow.trial.probe import failure_text

    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    (tmp_path / 'home' / 'MEMORY.md').write_text(text)
    ctx.subagent_lifecycle = _Life(ctx)
    result = SetupProbe(plugin, job).run()
    assert result['passed'] is False and result['incomplete'] is True
    assert result['memory'] is None
    assert result.get('no_saved_memory') is not True
    assert result['reason_code'] == 'memory_source_unverifiable'
    assert 'A replay could see your memory.' not in failure_text(result)


@pytest.mark.parametrize('fallback', [False, True])
def test_unreadable_memory_configuration_is_incomplete_without_launching(tmp_path, monkeypatch, fallback):
    from hermes_cli import config
    from hermes_cli.config_read_errors import FailedConfigRead
    from shadow.trial.probe import failure_text

    ctx, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    error = OSError('fixture configuration unavailable')

    def unreadable():
        if fallback:
            return FailedConfigRead({'memory': {}}, error=error)
        raise error

    monkeypatch.setattr(config, 'load_config_readonly', unreadable)
    ctx.subagent_lifecycle = _Life(ctx)
    result = SetupProbe(plugin, job).run()
    assert result['passed'] is False and result['incomplete'] is True
    assert result['memory'] is None
    assert result['reason_code'] == 'memory_provider_unverifiable'
    assert 'A replay could see your memory.' not in failure_text(result)
    assert not hasattr(ctx.subagent_lifecycle, 'thread')


@pytest.mark.parametrize('no_saved_memory', [False, True])
def test_external_provider_revokes_an_existing_verification(tmp_path, monkeypatch, no_saved_memory):
    from hermes_cli import config

    (tmp_path / 'home').mkdir()
    write_record(tmp_path, IDENTITY, checks(), no_saved_memory=no_saved_memory)
    made = launcher(tmp_path)
    assert made.isolation_verified is True
    monkeypatch.setattr(config, 'load_config_readonly', lambda: {'memory': {'provider': 'honcho'}})
    assert made.isolation_verified is False
