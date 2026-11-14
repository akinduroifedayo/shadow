import pytest

from fixtures import FakeContext
from shadow import register
from shadow.switch import register as register_switch
from test_hermes_plugin_loader import _install_host


@pytest.mark.parametrize('registration', [register, register_switch])
def test_registration_uses_distinct_command_names(tmp_path, monkeypatch, registration):
    _install_host(monkeypatch, tmp_path)
    calls = {'chat': [], 'cli': []}

    class Context(FakeContext):
        def register_command(self, name, *args, **kwargs):
            calls['chat'].append(name)
            return super().register_command(name, *args, **kwargs)

        def register_cli_command(self, name, *args, **kwargs):
            calls['cli'].append(name)
            return super().register_cli_command(name, *args, **kwargs)

    ctx = Context()
    registration(ctx)
    assert calls == {'chat': ['shadow-trial'], 'cli': ['shadow-trial']}
    assert set(ctx.commands) == {'shadow-trial'}
    assert set(ctx.cli) == {'shadow-trial'}
