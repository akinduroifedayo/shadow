from pathlib import Path

import pytest

from shadow.trial.isolation import write_record
from shadow.trial.probe import SetupProbe
from test_probe_dispatch import hermes_dispatch
from test_setup_check import _Life, _job, _plugin, active_home, checks


@pytest.mark.parametrize('symlinked', [False, True])
def test_verify_uses_disposable_read_scope_outside_profile(tmp_path, symlinked):
    ctx, plugin = _plugin(tmp_path)
    home = Path(_job(tmp_path)['home'])
    data = tmp_path / '.hermes' / 'profiles' / 'shadow-demo' / 'plugin_data' / 'shadow'
    data.mkdir(parents=True)
    if symlinked:
        alias = tmp_path / 'profile-alias'
        alias.symlink_to(data, target_is_directory=True)
        data = alias
    plugin.launcher.data_dir = data
    plugin._current_names = lambda: ('openai-codex', 'gpt-6.1-sol')
    ctx.subagent_lifecycle = _Life(ctx)
    workspaces = []

    def launch(launcher, job):
        workspaces.append(Path(job['workspace']))
        job['controller_session'] = 'setup'
        result = SetupProbe(plugin, job).run()
        assert result['read_only'] is True, result
        assert result['passed'] is True, result
        assert home.is_dir()
        write_record(launcher.directory, launcher.identity, checks())
        return result

    plugin.setup_launch = launch
    text = ctx.commands['shadow']('doctor --verify --yes')
    assert 'The setup check passed' in text
    assert workspaces and all(not path.exists() for path in workspaces)
    assert not (data / 'setup-workspace').exists()


def test_verify_removes_read_scope_on_launch_failure(tmp_path):
    ctx, plugin = _plugin(tmp_path)
    _job(tmp_path)
    plugin._current_names = lambda: ('openai-codex', 'gpt-6.1-sol')
    workspaces = []

    def launch(_launcher, job):
        workspace = Path(job['workspace'])
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'probe-file').touch()
        workspaces.append(workspace)
        raise OSError('synthetic launch failure')

    plugin.setup_launch = launch
    assert 'did not pass' in ctx.commands['shadow']('doctor --verify --yes')
    assert workspaces and all(not path.exists() for path in workspaces)
