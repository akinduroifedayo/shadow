import sys
from types import ModuleType

import pytest

from shadow.trial.probe import hook_callback_timeout


@pytest.mark.parametrize('plugins,expected', [
    (None, 30), ({}, 30), ([], 30),
    ({'hook_callback_timeout': None}, 30),
    ({'hook_callback_timeout': 'invalid'}, 30),
    ({'hook_callback_timeout': -1}, 30),
    ({'hook_callback_timeout': 0}, 0),
    ({'hook_callback_timeout': '10.5'}, 10.5),
    ({'hook_callback_timeout': 1000}, 600),
])
def test_setup_timeout_uses_public_profile_config(monkeypatch, plugins, expected):
    config = ModuleType('hermes_cli.config')
    config.load_config_readonly = lambda: {'plugins': plugins}
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', config)
    assert hook_callback_timeout() == expected


def test_setup_timeout_uses_default_when_config_is_unavailable(monkeypatch):
    config = ModuleType('hermes_cli.config')
    def unavailable():
        raise OSError('config unavailable')
    config.load_config_readonly = unavailable
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', config)
    assert hook_callback_timeout() == 30
