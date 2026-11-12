from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from shadow.trial import probe
from test_probe_dispatch import hermes_dispatch
from test_setup_check import _job, _plugin


@pytest.fixture
def gateway_home(tmp_path, monkeypatch, hermes_dispatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path / 'home')


@pytest.mark.parametrize('profile,command', [('default', 'hermes gateway restart'),
                                            ('demo', 'hermes -p demo gateway restart')])
@pytest.mark.parametrize('state', ['ready', 'starting', 'inaccessible'])
def test_missing_hooks_report_restart(monkeypatch, profile, command, state):
    from shadow.trial.probe_gateway import missing_setup_result
    homes = []
    def discover(home):
        homes.append(home)
        return SimpleNamespace(state=state)
    monkeypatch.setitem(sys.modules, 'hermes_cli.gateway_runtime',
                        SimpleNamespace(discover_gateway_endpoint=discover))
    result = missing_setup_result('/profile', profile)
    assert homes == ['/profile']
    assert result['reason_code'] == 'gateway_restart_needed'
    assert result['passed'] is False
    assert command in probe.failure_text(result)


def test_absent_gateway_keeps_timeout(monkeypatch):
    from shadow.trial.probe_gateway import missing_setup_result
    monkeypatch.setitem(sys.modules, 'hermes_cli.gateway_runtime',
                        SimpleNamespace(discover_gateway_endpoint=lambda home: SimpleNamespace(state='absent')))
    assert missing_setup_result('/profile', 'demo') == {'passed': False, 'timed_out': True}


@pytest.mark.parametrize('profile,command', [('default', 'hermes gateway restart'),
                                            ('demo', 'hermes -p demo gateway restart')])
@pytest.mark.parametrize('saved_session', [False, True])
def test_setup_without_hooks_uses_gateway_hint_after_launch(tmp_path, monkeypatch, gateway_home, profile, command, saved_session):
    from test_probe_cleanup import _sessions

    _, plugin = _plugin(tmp_path)
    job = _job(tmp_path)
    job['profile'] = profile
    launched = []
    def launch(*args, **kwargs):
        launched.append(args)
        if saved_session:
            _sessions(Path(job['home']), ['unguarded-setup'])
        return SimpleNamespace()
    plugin.launcher.popen = launch
    monkeypatch.setattr(probe, 'wait_for_result', lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, 'hermes_cli.gateway_runtime',
                        SimpleNamespace(discover_gateway_endpoint=lambda home: SimpleNamespace(state='ready')))
    result = probe.launch_setup(plugin.launcher, job, killer=lambda _: None)
    assert len(launched) == 1
    assert result['reason_code'] == 'gateway_restart_needed'
    assert result.get('unchanged', True) is not saved_session
    assert command in probe.failure_text(result)
    assert ('unguarded-setup' in probe.session_fingerprints(job['home'])) is saved_session
    assert plugin.launcher.isolation_verified is False


@pytest.mark.parametrize('crash', [False, True])
def test_resident_gateway_runs_distinct_setup_jobs_but_refuses_replayed_job(tmp_path, monkeypatch, gateway_home, crash):
    _, plugin = _plugin(tmp_path)
    first = _job(tmp_path)
    second = probe.new_job(home=first['home'], workspace=first['workspace'],
                           provider=first['provider'], model=first['model'], profile=first['profile'])
    ran, blocked = [], []

    def run(self):
        ran.append(self.job['id'])
        if crash:
            raise RuntimeError('offline setup failure')
        return {'passed': False, 'incomplete': True}

    def launch(argv, **kwargs):
        message = Path(argv[argv.index('--query-file') + 1]).read_text()
        blocked.append(plugin.pre_llm_call(session_id=f'setup-{len(blocked)}', user_message=message))
        return SimpleNamespace()

    monkeypatch.setattr(probe.SetupProbe, 'run', run)
    plugin.launcher.popen = launch
    for job in (first, second, first):
        result = probe.launch_setup(plugin.launcher, job, timeout=0.05, killer=lambda _: None)
        assert result['passed'] is False and result['incomplete'] is True
        assert result.get('timed_out') is not True
    assert ran == [first['id'], second['id']]
    assert all(result['action'] == 'block' for result in blocked)
    assert plugin.launcher.isolation_verified is False


def test_resident_gateway_refuses_overlapping_setup_job(tmp_path, monkeypatch, gateway_home):
    _, plugin = _plugin(tmp_path)
    first = _job(tmp_path)
    second = probe.new_job(home=first['home'], workspace=first['workspace'],
                           provider=first['provider'], model=first['model'], profile=first['profile'])
    ran = []

    def run(self):
        ran.append(self.job['id'])
        probe.write_job(tmp_path, second)
        answer = plugin.pre_llm_call(session_id='overlapping', user_message=probe.prompt_text(second))
        assert answer['action'] == 'block'
        assert 'could not complete' in answer['message']
        return {'passed': False, 'incomplete': True}

    monkeypatch.setattr(probe.SetupProbe, 'run', run)
    probe.write_job(tmp_path, first)
    plugin.pre_llm_call(session_id='first', user_message=probe.prompt_text(first))
    assert ran == [first['id']]
    result = probe.wait_for_result(plugin.launcher, second, timeout=0.05, pause=lambda _: None)
    assert result['incomplete'] is True and result['passed'] is False
    assert result['model'] is None
