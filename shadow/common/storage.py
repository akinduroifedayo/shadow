import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .scrub import Scrubber


def plugin_data_dir():
    from plugins.plugin_storage import plugin_data_dir as data_dir
    return data_dir('shadow')


def plugin_db():
    from plugins.plugin_storage import plugin_db as database
    return database('shadow')


def parse_trial_record(record):
    record.setdefault('intent', 'switch')
    if record['intent'] not in {'switch', 'compare'}:
        raise ValueError('invalid trial intent')
    return record


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.connection = None
        self.lock = threading.RLock()

    def _db(self):
        if self.connection is None:
            self.connection = sqlite3.connect(self.path, check_same_thread=False) if self.path else plugin_db()
            self.connection.execute('CREATE TABLE IF NOT EXISTS trial_records (id TEXT PRIMARY KEY, created REAL NOT NULL, record TEXT NOT NULL)')
            self.connection.execute('CREATE TABLE IF NOT EXISTS trial_cancellations (id TEXT PRIMARY KEY)')
            self.connection.execute('CREATE TABLE IF NOT EXISTS trial_controllers (id TEXT PRIMARY KEY, claim TEXT NOT NULL, pid INTEGER, restarts INTEGER NOT NULL)')
            self.connection.execute('CREATE TABLE IF NOT EXISTS trial_second_looks (id TEXT PRIMARY KEY, followup_id TEXT NOT NULL)')
            self.connection.execute('CREATE TABLE IF NOT EXISTS trial_replay_children (run_id TEXT NOT NULL, session_id TEXT NOT NULL, PRIMARY KEY (run_id, session_id))')
            if 'owner_pid' not in {row[1] for row in self.connection.execute('PRAGMA table_info(trial_controllers)')}:
                self.connection.execute('ALTER TABLE trial_controllers ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0')
            if 'stop_reason' not in {row[1] for row in self.connection.execute('PRAGMA table_info(trial_controllers)')}:
                self.connection.execute('ALTER TABLE trial_controllers ADD COLUMN stop_reason TEXT')
            if 'stop_details' not in {row[1] for row in self.connection.execute('PRAGMA table_info(trial_controllers)')}:
                self.connection.execute('ALTER TABLE trial_controllers ADD COLUMN stop_details TEXT')
            self.connection.commit()
        return self.connection

    def save(self, record):
        with self.lock:
            safe = Scrubber().scrub(record)
            for source, target in zip(record.get('turns', []), safe.get('turns', [])):
                if 'cwd' in source:
                    target['cwd'] = str(Path(source['cwd']).resolve(strict=True))
            db = self._db()
            if record.get('state') == 'cancelled':
                db.execute('INSERT OR IGNORE INTO trial_cancellations VALUES (?)', (record['id'],))
            db.execute('INSERT INTO trial_records VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET record=excluded.record', (record['id'], record['created_at'], json.dumps(safe, sort_keys=True, allow_nan=False)))
            db.commit()

    def get(self, run_id=None):
        with self.lock:
            db = self._db()
            row = db.execute('SELECT record FROM trial_records WHERE id=?', (run_id,)).fetchone() if run_id else db.execute('SELECT record FROM trial_records ORDER BY created DESC LIMIT 1').fetchone()
            return self._record(row[0]) if row else None

    def _record(self, text):
        record = parse_trial_record(json.loads(text))
        if self._db().execute('SELECT 1 FROM trial_cancellations WHERE id=?', (record['id'],)).fetchone():
            record['state'] = 'cancelled'
            if record.get('stop_reason') != 'unpriceable':
                record['stop_reason'] = 'cancelled'
        controller = self._db().execute('SELECT pid, restarts, claim, owner_pid, stop_reason, stop_details FROM trial_controllers WHERE id=?', (record['id'],)).fetchone()
        if controller:
            record.update(controller_pid=controller[0], restarts=controller[1], controller_token=controller[2], controller_owner_pid=controller[3])
            if controller[4] and record.get('state') != 'cancelled':
                record['state'] = 'interrupted'
                if record.get('stop_reason') != 'unpriceable':
                    record['stop_reason'] = controller[4]
                if controller[5]:
                    record['controller_stop_details'] = json.loads(controller[5])
        second_look = self._db().execute('SELECT followup_id FROM trial_second_looks WHERE id=?', (record['id'],)).fetchone()
        if second_look:
            record['second_look_id'] = second_look[0]
        children = self._db().execute('SELECT session_id FROM trial_replay_children WHERE run_id=? ORDER BY session_id', (record['id'],)).fetchall()
        if children:
            record['replay_child_session_ids'] = list(dict.fromkeys(record.get('replay_child_session_ids', []) + [row[0] for row in children]))
        return record

    def remember_replay_child(self, run_id, session_id):
        with self.lock:
            db = self._db()
            db.execute('INSERT OR IGNORE INTO trial_replay_children (run_id, session_id) SELECT ?, ? WHERE EXISTS (SELECT 1 FROM trial_records WHERE id=?)',
                       (run_id, Scrubber().scrub(session_id), run_id))
            db.commit()

    def claim_controller(self, run_id, expected_pid, *, expected_claim=None):
        with self.lock:
            db = self._db()
            try:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT record FROM trial_records WHERE id=?', (run_id,)).fetchone()
                record = self._record(row[0]) if row else None
                previous = db.execute('SELECT pid, restarts, claim FROM trial_controllers WHERE id=?', (run_id,)).fetchone()
                if not record or record['state'] not in {'queued', 'running'} or (previous and (previous[0] != expected_pid or previous[1] >= 3 or previous[0] is None and previous[2] != expected_claim)):
                    db.rollback()
                    return None
                claim = secrets.token_hex(12)
                db.execute('INSERT OR REPLACE INTO trial_controllers (id, claim, pid, restarts, owner_pid) VALUES (?, ?, NULL, ?, ?)', (run_id, claim, previous[1] + 1 if previous else 0, os.getpid()))
                db.commit()
                return claim
            except BaseException:
                db.rollback()
                raise

    def controller_started(self, run_id, claim, pid):
        with self.lock:
            db = self._db()
            db.execute('UPDATE trial_controllers SET pid=? WHERE id=? AND claim=?', (pid, run_id, claim))
            db.commit()

    def controller_failed(self, run_id, claim):
        with self.lock:
            db = self._db()
            db.execute('DELETE FROM trial_controllers WHERE id=? AND claim=?', (run_id, claim))
            db.commit()

    def controller_stopped(self, run_id, claim, reason, *, details=None):
        with self.lock:
            db = self._db()
            try:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT record FROM trial_records WHERE id=?', (run_id,)).fetchone()
                record = self._record(row[0]) if row else None
                if not record or record['state'] not in {'queued', 'running'}:
                    db.rollback()
                    return False
                changed = db.execute('UPDATE trial_controllers SET stop_reason=?, stop_details=? WHERE id=? AND claim=? AND stop_reason IS NULL',
                                     (reason, json.dumps(Scrubber().scrub(details)) if details else None, run_id, claim)).rowcount
                db.commit()
                return bool(changed)
            except BaseException:
                db.rollback()
                raise

    def reserve_second_look(self, run_id, followup_id):
        with self.lock:
            db = self._db()
            try:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT record FROM trial_records WHERE id=?', (run_id,)).fetchone()
                record = self._record(row[0]) if row else None
                if not record or 'totals' not in record or record.get('second_look') or record.get('second_look_id'):
                    db.rollback()
                    return False
                db.execute('INSERT INTO trial_second_looks VALUES (?, ?)', (run_id, followup_id))
                db.commit()
                return True
            except BaseException:
                db.rollback()
                raise

    def release_second_look(self, run_id, followup_id):
        with self.lock:
            db = self._db()
            db.execute('DELETE FROM trial_second_looks WHERE id=? AND followup_id=?', (run_id, followup_id))
            db.commit()

    def list(self):
        with self.lock:
            return [self._record(row[0]) for row in self._db().execute('SELECT record FROM trial_records ORDER BY created')]

    def forget(self, run_id):
        with self.lock:
            db = self._db()
            if run_id == 'all':
                db.execute('DELETE FROM trial_records')
                db.execute('DELETE FROM trial_cancellations')
                db.execute('DELETE FROM trial_controllers')
                db.execute('DELETE FROM trial_second_looks')
                db.execute('DELETE FROM trial_replay_children')
            else:
                db.execute('DELETE FROM trial_records WHERE id=?', (run_id,))
                db.execute('DELETE FROM trial_cancellations WHERE id=?', (run_id,))
                db.execute('DELETE FROM trial_controllers WHERE id=?', (run_id,))
                db.execute('DELETE FROM trial_second_looks WHERE id=?', (run_id,))
                db.execute('DELETE FROM trial_replay_children WHERE run_id=?', (run_id,))
            db.commit()

    def prune(self, now=None):
        with self.lock:
            db = self._db()
            db.execute('DELETE FROM trial_records WHERE created < ?', ((now or time.time()) - 30 * 86400,))
            db.execute('DELETE FROM trial_cancellations WHERE id NOT IN (SELECT id FROM trial_records)')
            db.execute('DELETE FROM trial_controllers WHERE id NOT IN (SELECT id FROM trial_records)')
            db.execute('DELETE FROM trial_second_looks WHERE id NOT IN (SELECT id FROM trial_records)')
            db.execute('DELETE FROM trial_replay_children WHERE run_id NOT IN (SELECT id FROM trial_records)')
            db.commit()

    def close(self):
        with self.lock:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
