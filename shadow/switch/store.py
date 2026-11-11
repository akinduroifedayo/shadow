from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import hmac
import os
import secrets
from pathlib import Path
import sqlite3
import tempfile
from threading import RLock

from .domain import SwitchRecord, turn_key


class Store:
    def __init__(self, connection: sqlite3.Connection | None = None, *, path: Path | None = None,
                 salt_path: Path | None = None, connection_factory=None):
        self.connection = connection
        self.connection_factory = connection_factory
        self.path = path
        self.lock = RLock()
        self.salt_path = salt_path or (path.with_suffix(".salt") if path else None)
        if self.salt_path is None and connection is not None:
            self._locate_salt_file(connection)
        self._salt = None
        self._temporary_salt = secrets.token_bytes(32)
        self._identity_epoch = 0
        self._migration_version = None
        self._salt_cleanup_pending = False

    def _locate_salt_file(self, connection):
        filename = next((row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"), "")
        if filename:
            self.salt_path = Path(filename).with_suffix(".salt")

    def _load_salt(self):
        if self._salt is not None:
            return
        salt = None
        if self.salt_path is not None and self.salt_path.exists():
            salt = self.salt_path.read_bytes()
        elif self._exists():
            connection = self._open()
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='shadow_switch_salt'").fetchone()
            row = connection.execute("SELECT salt FROM shadow_switch_salt").fetchone() if exists else None
            salt = bytes(row[0]) if row else None
        if salt is not None:
            self._adopt_salt(salt)

    def _adopt_salt(self, salt):
        if salt != (self._salt or self._temporary_salt):
            self._identity_epoch += 1
        self._salt = salt

    @property
    def identity_epoch(self):
        with self.lock:
            self._load_salt()
            return self._identity_epoch

    def identity(self, value):
        if not value:
            return value
        with self.lock:
            self._load_salt()
            salt = self._salt or self._temporary_salt
            return hmac.new(salt, str(value).encode(), hashlib.sha256).hexdigest()

    def _save_salt(self, connection):
        self._load_salt()
        salt = self._salt or self._temporary_salt
        if self.salt_path is not None:
            self.salt_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.salt_path.exists():
                fd, temporary = tempfile.mkstemp(dir=self.salt_path.parent, prefix=".switch-salt-")
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(salt)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(temporary, self.salt_path)
                    except FileExistsError:
                        pass
                finally:
                    Path(temporary).unlink()
            salt = self.salt_path.read_bytes()
        self._adopt_salt(salt)
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='shadow_switch_salt'").fetchone():
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("DROP TABLE shadow_switch_salt")
            self._salt_cleanup_pending = True

    def _checkpoint_salt_migration(self, connection):
        if self._salt_cleanup_pending:
            busy, _, _ = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            self._salt_cleanup_pending = bool(busy)

    def _exists(self) -> bool:
        return self.connection is not None or (self.path is not None and self.path.exists())

    def _open(self) -> sqlite3.Connection:
        if self.connection is None:
            if self.connection_factory is not None:
                self.connection = self.connection_factory()
            elif self.path is not None:
                self.connection = sqlite3.connect(self.path)
            else:
                from plugins.plugin_storage import plugin_db
                self.connection = plugin_db("shadow", "switch.db")
        if self.salt_path is None:
            self._locate_salt_file(self.connection)
        return self.connection

    def _migrate(self, connection):
        version = connection.execute("PRAGMA data_version").fetchone()[0]
        if version == self._migration_version:
            return
        owns_transaction = not connection.in_transaction
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        try:
            self._save_salt(connection)
            rows = connection.execute("SELECT id, data FROM shadow_switch_records").fetchall()
            for record_id, serialized in rows:
                data = json.loads(serialized)
                if data.get("version", 1) >= 2:
                    continue
                self._save_salt(connection)
                for name in ("assignments", "draining", "sessions", "chat_moves"):
                    data[name] = {self.identity(key): value for key, value in data.get(name, {}).items()}
                data["session_keys"] = {self.identity(key): self.identity(value)
                                        for key, value in data.get("session_keys", {}).items()}
                data["excluded"] = [self.identity(value) for value in data.get("excluded", [])]
                turns = {}
                keys = {}
                for old_key, turn in data["turns"].items():
                    for name in ("session_id", "task_id", "turn_id"):
                        turn[name] = self.identity(turn[name])
                    key = turn_key(turn["session_id"], turn["task_id"], turn["turn_id"])
                    keys[old_key] = key
                    requests = turn.get("requests", {})
                    observations = []
                    for observation in turn.get("observations", []):
                        for request_id in sorted(requests, key=len, reverse=True):
                            prefix = next((f"{kind}:{request_id}:" for kind in ("ok", "error")
                                           if observation.startswith(f"{kind}:{request_id}:")), None)
                            if prefix is not None:
                                kind = prefix.split(":", 1)[0]
                                observation = f"{kind}:{self.identity(request_id)}:{observation[len(prefix):]}"
                                break
                        observations.append(observation)
                    turn["requests"] = {self.identity(key): value for key, value in requests.items()}
                    turn["observations"] = observations
                    turns[key] = turn
                data["turns"] = turns
                data["last_turn"] = {self.identity(key): keys.get(value, self.identity(value))
                                     for key, value in data.get("last_turn", {}).items()}
                data["version"] = 2
                connection.execute("UPDATE shadow_switch_records SET data=? WHERE id=?",
                                   (json.dumps(data, separators=(",", ":")), record_id))
            if owns_transaction:
                connection.commit()
                self._checkpoint_salt_migration(connection)
                self._migration_version = version
        except BaseException:
            if owns_transaction:
                connection.rollback()
            raise

    def _read(self, connection) -> SwitchRecord | None:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='shadow_switch_records'").fetchone():
            return None
        self._migrate(connection)
        row = connection.execute("SELECT data FROM shadow_switch_records ORDER BY rowid DESC LIMIT 1").fetchone()
        return SwitchRecord.from_dict(json.loads(row[0])) if row else None

    def read(self) -> SwitchRecord | None:
        with self.lock:
            return self._read(self._open()) if self._exists() else None

    @contextmanager
    def transaction(self, *, create=False):
        with self.lock:
            if not create and not self._exists():
                yield None
                return
            connection = self._open()
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._save_salt(connection)
                connection.execute("CREATE TABLE IF NOT EXISTS shadow_switch_records (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
                record = self._read(connection)
                holder = [record]
                yield holder
                if holder[0] is not None:
                    record = holder[0]
                    connection.execute("INSERT INTO shadow_switch_records VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                                       (record.id, json.dumps(record.to_dict(), separators=(",", ":"))))
                connection.commit()
                self._checkpoint_salt_migration(connection)
            except BaseException:
                connection.rollback()
                raise

    def row_count(self) -> int:
        with self.lock:
            if not self._exists():
                return 0
            connection = self._open()
            if self._read(connection) is None:
                return 0
            return connection.execute("SELECT count(*) FROM shadow_switch_records").fetchone()[0]
