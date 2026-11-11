import json
import sqlite3

import pytest

from shadow.switch import Engine, Store
from shadow.switch.domain import SwitchRecord, Turn, turn_key
from test_switch_safety import NEW, OLD


def legacy_record(record_id):
    session, task, turn, api = "private-session", "private-task", "private-turn", "private-turn:api:1"
    key = turn_key(session, task, turn)
    record = SwitchRecord(record_id, "test", OLD, NEW, "gradual", version=1, step=3,
                          assignments={session: "new"}, draining={session: "new"},
                          sessions={session: "cli"}, session_keys={"private-chat-key": session},
                          excluded=["private-helper"], last_turn={session: key},
                          chat_moves={session: "0:watch:new"})
    record.turns[key] = Turn(session, task, turn, "new", 0, response_model="new", finished=True,
                             signals=["tool_error"], requests={api: "new"},
                             observations=[f"ok:{api}:123", f"error:{api}:0:124"], sent=True)
    return record


def legacy_database(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE shadow_switch_records (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    for record_id in ("archived", "current"):
        record = legacy_record(record_id)
        connection.execute("INSERT INTO shadow_switch_records VALUES (?, ?)",
                           (record.id, json.dumps(record.to_dict())))
    connection.commit()
    return connection


def test_supplied_path_opens_lazily_and_persists_to_that_database(tmp_path):
    path = tmp_path / "explicit.db"
    store = Store(path=path, salt_path=tmp_path / "identity.salt")
    assert store.read() is None
    assert not path.exists()
    assert not store.salt_path.exists()
    with store.transaction(create=True) as holder:
        holder[0] = SwitchRecord("switch", "test", OLD, NEW, "gradual")
    assert path.exists()
    assert store.salt_path.exists()
    assert Store(path=path, salt_path=store.salt_path).read().id == "switch"


def test_legacy_read_migrates_every_record_and_keeps_counts_routing_and_deduplication(tmp_path):
    connection = legacy_database(tmp_path / "switch.db")
    store = Store(connection)
    record = store.read()
    assert record.version == 2
    assert record.counts("new").turns == 1
    assert record.counts("new").trouble == 1
    session = store.identity("private-session")
    task = store.identity("private-task")
    turn = store.identity("private-turn")
    api = store.identity("private-turn:api:1")
    key = turn_key(session, task, turn)
    assert record.assignments == {session: "new"}
    assert record.draining == {session: "new"}
    assert record.sessions == {session: "cli"}
    assert record.session_keys == {store.identity("private-chat-key"): session}
    assert record.excluded == [store.identity("private-helper")]
    assert record.last_turn == {session: key}
    assert record.chat_moves == {session: "0:watch:new"}
    assert record.turns[key].requests == {api: "new"}
    assert record.turns[key].observations == [f"ok:{api}:123", f"error:{api}:0:124"]
    serialized = connection.execute("SELECT data FROM shadow_switch_records").fetchall()
    assert len(serialized) == 2
    assert all(json.loads(row[0])["version"] == 2 for row in serialized)
    assert all("private-" not in row[0] for row in serialized)
    restarted = Engine(Store(connection), verified_pair=lambda old, new: True)
    event = dict(session_id="private-session", task_id="private-task", turn_id="private-turn",
                 provider=NEW.provider, base_url=NEW.base_url, api_mode=NEW.api_mode,
                 api_request_id="private-turn:api:1", response_model="new", started_at=123)
    before = restarted.record.to_dict()
    restarted.response(**event)
    assert restarted.record.to_dict() == before
    assert restarted.store.identity("private-session") == session
    next_turn = dict(session_id="private-session", task_id="private-task", turn_id="private-next")
    restarted.admit(**next_turn, platform="cli", model="old")
    routed = restarted.rewrite(**next_turn, provider=OLD.provider, base_url=OLD.base_url,
                               api_mode=OLD.api_mode, model="old", api_request_id="private-next:api:1",
                               request={"model": "old"})
    assert routed["request"]["model"] == "new"


def test_migration_survives_new_connection_and_does_not_hash_again(tmp_path):
    path = tmp_path / "switch.db"
    connection = legacy_database(path)
    before = Store(connection).read().to_dict()
    connection.close()
    restarted = Store(sqlite3.connect(path))
    assert restarted.read().to_dict() == before
    assert restarted.identity("private-session") in restarted.read().assignments


def test_new_database_keeps_identity_salt_only_in_private_file(tmp_path):
    from stat import S_IMODE

    path = tmp_path / 'switch.db'
    store = Store(path=path, salt_path=tmp_path / 'switch.salt')
    engine = Engine(store, verified_pair=lambda *_: True)
    from shadow.switch import Proposal
    engine.start(Proposal('saved', OLD, NEW, 'better'))
    assert not store.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='shadow_switch_salt'").fetchone()
    assert S_IMODE(store.salt_path.stat().st_mode) == 0o600
    assert store.salt_path.read_bytes() not in path.read_bytes()
    assert Store(path=path, salt_path=store.salt_path).identity('chat') == store.identity('chat')


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_legacy_database_salt_moves_to_file_without_changing_hashes(tmp_path, journal_mode):
    import hashlib
    import hmac
    from shadow.switch.domain import SwitchRecord

    path = tmp_path / 'switch.db'
    salt = b'fixture-legacy-identity-salt-12345'
    expected = hmac.new(salt, b'chat', hashlib.sha256).hexdigest()
    record = SwitchRecord('saved', 'test', OLD, NEW, 'gradual', assignments={expected: 'new'})
    connection = sqlite3.connect(path)
    connection.execute(f'PRAGMA journal_mode={journal_mode}')
    connection.execute('CREATE TABLE shadow_switch_salt (salt BLOB NOT NULL)')
    connection.execute('INSERT INTO shadow_switch_salt VALUES (?)', (salt,))
    connection.execute('CREATE TABLE shadow_switch_records (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    connection.execute('INSERT INTO shadow_switch_records VALUES (?, ?)', ('saved', json.dumps(record.to_dict())))
    connection.commit()
    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    store = Store(connection)
    assert store.read().assignments == {expected: 'new'}
    assert (tmp_path / 'switch.salt').read_bytes() == salt
    assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='shadow_switch_salt'").fetchone()
    assert salt not in path.read_bytes()
    connection.close()
    restarted = Store(sqlite3.connect(path))
    assert restarted.identity('chat') == expected
    assert restarted.read().assignments == {expected: 'new'}
