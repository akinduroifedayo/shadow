import sqlite3
import sys

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


def test_storage_uses_distinct_name(tmp_path, monkeypatch):
    from shadow.common.storage import plugin_data_dir
    from shadow.switch.store import Store

    _install_host(monkeypatch, tmp_path)
    storage = sys.modules['plugins.plugin_storage']
    calls = []

    def data_dir(name):
        calls.append(('directory', name))
        return tmp_path / 'plugin-data' / name

    def database(name, filename='data.db'):
        calls.append(('database', name, filename))
        return sqlite3.connect(':memory:')

    monkeypatch.setattr(storage, 'plugin_data_dir', data_dir)
    monkeypatch.setattr(storage, 'plugin_db', database)
    ctx = FakeContext()
    trial = register(ctx)
    trial.store._db()
    assert plugin_data_dir() == tmp_path / 'plugin-data' / 'shadow-trial'
    ctx.shadow_switch.engine.store._open()
    standalone = Store()
    standalone._open()
    assert calls == [
        ('database', 'shadow-trial', 'data.db'),
        ('directory', 'shadow-trial'),
        ('database', 'shadow-trial', 'switch.db'),
        ('database', 'shadow-trial', 'switch.db'),
    ]
    trial.store.close()
    ctx.shadow_switch.engine.store.connection.close()
    standalone.connection.close()
