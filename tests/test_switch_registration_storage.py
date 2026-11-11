from concurrent.futures import ThreadPoolExecutor
import sqlite3
import sys
from types import ModuleType

import pytest

from shadow.switch import Proposal, register
from test_switch_plugin import Context, config
from test_switch_safety import NEW, OLD, identity, request


@pytest.fixture
def storage_environment(monkeypatch, tmp_path):
    modules = {name: ModuleType(name) for name in (
        "hermes_constants", "hermes_cli", "hermes_cli.config", "plugins", "plugins.plugin_storage",
        "gateway", "gateway.session_context")}
    profile = [tmp_path]
    opened = []

    def plugin_db(name, filename):
        path = profile[0] / "plugin-data" / name / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        opened.append(path)
        return sqlite3.connect(path, check_same_thread=False)

    modules["hermes_constants"].get_hermes_home = lambda: profile[0]
    modules["hermes_cli.config"].read_config_doc = lambda path: config()
    modules["plugins.plugin_storage"].plugin_db = plugin_db
    modules["gateway.session_context"].get_session_env = lambda key, default="": default
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return profile, opened


def test_registration_and_idle_turn_never_create_storage(storage_environment):
    profile, opened = storage_environment
    ctx = Context()
    service = register(ctx)
    assert opened == []
    ctx.hooks["on_session_start"](session_id="conversation", platform="cli")
    ctx.hooks["pre_llm_call"](**identity(), model="old", platform="cli")
    assert request(service.engine) is None
    ctx.hooks["post_api_request"](**identity(), response_model="old", api_request_id="a")
    ctx.hooks["api_request_error"](**identity(), api_request_id="a")
    ctx.hooks["post_tool_call"](**identity(), status="error")
    ctx.hooks["on_session_end"](**identity(), platform="cli", completed=True, failed=False)
    ctx.hooks["pre_command"](command="retry", session_key="conversation", args_raw="")
    ctx.hooks["post_compaction"](session_id="next", old_session_id="conversation")
    ctx.hooks["subagent_start"](child_session_id="helper")
    assert service.engine.record is None
    assert service.engine.store.row_count() == 0
    assert opened == []
    assert not (profile[0] / "plugin-data").exists()


def test_first_switch_persists_and_restart_uses_hermes_connection(storage_environment):
    profile, opened = storage_environment
    service = register(Context())
    service.engine.verified_pair = lambda old, new: True
    record = service.engine.start(Proposal("saved-test", OLD, NEW, "better"), switch_id="saved-switch")
    expected_path = profile[0] / "plugin-data" / "shadow" / "switch.db"
    assert opened == [expected_path]
    assert expected_path.exists()
    assert (expected_path.parent / "switch.salt").exists()
    session = service.engine.store.identity("conversation")
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(lambda: service.engine.record.id).result() == record.id
    restarted = register(Context())
    assert opened == [expected_path]
    assert restarted.engine.record.id == "saved-switch"
    assert restarted.engine.record.test_id == "saved-test"
    assert restarted.engine.store.identity("conversation") == session
    assert opened == [expected_path, expected_path]


def test_profile_change_cannot_redirect_pending_storage(storage_environment):
    profile, opened = storage_environment
    original_home = profile[0]
    service = register(Context())
    service.engine.verified_pair = lambda old, new: True
    profile[0] = original_home / "other-profile"
    with pytest.raises(RuntimeError):
        service.engine.start(Proposal("saved-test", OLD, NEW, "better"))
    assert opened == []
    assert not (original_home / "plugin-data").exists()
    assert not (profile[0] / "plugin-data").exists()
