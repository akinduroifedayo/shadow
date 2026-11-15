from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from fixtures import FakeContext
from shadow import register
from shadow.common import storage_migration
from shadow.common.storage import Store as TrialStore
from shadow.common.storage_migration import migrate_legacy_storage
from shadow.switch.domain import SwitchRecord
from shadow.switch.store import Store as SwitchStore
from test_hermes_plugin_loader import _install_host
from test_switch_safety import NEW, OLD


@pytest.fixture
def locations(tmp_path, monkeypatch):
    _install_host(monkeypatch, tmp_path)
    return tmp_path / 'plugin-data' / 'shadow', tmp_path / 'plugin-data' / 'shadow-trial'


@pytest.fixture
def file_backed_locations(locations, monkeypatch):
    legacy, destination = locations

    def database(name, filename='data.db'):
        assert name == 'shadow-trial'
        destination.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(destination / filename)

    monkeypatch.setattr(sys.modules['plugins.plugin_storage'], 'plugin_db', database)
    return legacy, destination


def trial_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    store = TrialStore(path)
    store._db().execute('PRAGMA journal_mode=WAL')
    store.save({'id': 'saved-trial', 'created_at': 1, 'state': 'finished'})
    store.close()


def switch_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    store = SwitchStore(path=path)
    store._open().execute('PRAGMA journal_mode=WAL')
    with store.transaction(create=True) as holder:
        holder[0] = SwitchRecord('saved-switch', 'saved-trial', OLD, NEW, 'gradual')
    identity = store.identity('conversation')
    store.connection.close()
    return identity


def snapshot(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def snapshot_metadata(directory):
    metadata = {}
    for path in directory.iterdir():
        info = path.stat()
        metadata[path.name] = (info.st_ino, info.st_size, info.st_mtime_ns)
    return metadata


def test_package_registration_copies_ours_before_opening_either_database(locations, monkeypatch):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    identity = switch_database(legacy / 'switch.db')
    (legacy / 'pair-evidence.json').write_text('unrelated evidence')
    (legacy / 'other.txt').write_text('another plugin owns this')
    before = snapshot(legacy)
    listing = sorted(legacy.iterdir())
    opened = []

    def database(name, filename='data.db'):
        assert (destination / 'data.db').is_file()
        assert (destination / 'switch.db').is_file()
        assert (destination / 'switch.salt').is_file()
        opened.append((name, filename))
        return sqlite3.connect(destination / filename)

    monkeypatch.setattr(sys.modules['plugins.plugin_storage'], 'plugin_db', database)
    ctx = FakeContext()
    trial = register(ctx)
    assert trial.store.get('saved-trial')['state'] == 'finished'
    assert ctx.shadow_switch.engine.record.id == 'saved-switch'
    assert ctx.shadow_switch.engine.store.identity('conversation') == identity
    assert opened == [('shadow-trial', 'data.db'), ('shadow-trial', 'switch.db')]
    assert sorted(legacy.iterdir()) == listing
    assert snapshot(legacy) == before
    assert set(path.name for path in destination.iterdir()) == {'data.db', 'switch.db', 'switch.salt'}
    assert (destination / 'switch.salt').stat().st_mode & 0o777 == 0o600
    trial.store.close()
    ctx.shadow_switch.engine.store.connection.close()


@pytest.mark.parametrize('filename', ['data.db', 'switch.db'])
@pytest.mark.parametrize('error', [OSError, sqlite3.DatabaseError])
def test_registration_retries_failed_copy_before_opening_storage(
        file_backed_locations, monkeypatch, caplog, filename, error):
    legacy, destination = file_backed_locations
    trial_database(legacy / 'data.db')
    identity = switch_database(legacy / 'switch.db')
    (legacy / (filename + '-wal')).write_bytes(b'')
    before = snapshot(legacy)
    connect = sqlite3.connect
    work = []

    def fail_first_recovery(database, *args, **kwargs):
        if '.legacy-work-' in str(database):
            work.append(next(destination.glob('.legacy-work-*')))
            if len(work) == 1:
                raise error('private failure details')
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', fail_first_recovery)
    for _ in range(2):
        ctx = FakeContext()
        trial = register(ctx)
        try:
            assert trial.store.get('saved-trial')['state'] == 'finished'
            assert ctx.shadow_switch.engine.record.id == 'saved-switch'
            assert ctx.shadow_switch.engine.store.identity('conversation') == identity
        finally:
            trial.store.close()
            if ctx.shadow_switch.engine.store.connection is not None:
                ctx.shadow_switch.engine.store.connection.close()
    assert len(work) == len(set(work)) == 2
    assert all(not directory.exists() for directory in work)
    assert set(os.listdir(destination)) == {'data.db', 'switch.db', 'switch.salt'}
    with closing(connect(destination / 'data.db')) as copied:
        assert copied.execute('SELECT id FROM trial_records').fetchall() == [('saved-trial',)]
    assert snapshot(legacy) == before
    assert set(os.listdir(legacy)) == set(before)
    assert caplog.messages == []


def test_registration_starts_empty_when_all_copy_attempts_fail(
        file_backed_locations, monkeypatch, caplog):
    legacy, destination = file_backed_locations
    trial_database(legacy / 'data.db')
    (legacy / 'data.db-wal').write_bytes(b'')
    before = snapshot(legacy)
    connect = sqlite3.connect
    work = []

    def fail_recovery(database, *args, **kwargs):
        if '.legacy-work-' in str(database):
            work.append(next(destination.glob('.legacy-work-*')))
            raise sqlite3.DatabaseError('private failure details')
        return connect(database, *args, **kwargs)

    with monkeypatch.context() as failing:
        failing.setattr(sqlite3, 'connect', fail_recovery)
        ctx = FakeContext()
        trial = register(ctx)
        assert trial.store.list() == []
        assert ctx.shadow_switch.engine.record is None
        trial.store.close()
    assert len(work) == len(set(work)) == 3
    assert all(not directory.exists() for directory in work)
    assert set(os.listdir(destination)) == {'data.db'}
    assert caplog.messages == [
        'Legacy Shadow storage was not copied. Shadow starts with empty trial storage '
        'for failed copies and leaves plugin-data/shadow unchanged.'
    ]
    caplog.clear()
    ctx = FakeContext()
    trial = register(ctx)
    assert trial.store.list() == []
    assert ctx.shadow_switch.engine.record is None
    trial.store.close()
    assert caplog.messages == []
    assert snapshot(legacy) == before
    assert set(os.listdir(legacy)) == set(before)


def test_registration_preserves_storage_created_during_publication(
        file_backed_locations, monkeypatch, caplog):
    legacy, destination = file_backed_locations
    trial_database(legacy / 'data.db')
    before = snapshot(legacy)
    link = os.link

    def racing_link(source, target):
        with closing(sqlite3.connect(target)):
            pass
        link(source, target)

    monkeypatch.setattr(os, 'link', racing_link)
    ctx = FakeContext()
    trial = register(ctx)
    assert trial.store.list() == []
    trial.store.close()
    assert snapshot(legacy) == before
    assert set(os.listdir(legacy)) == set(before)
    assert not list(destination.glob('.legacy-*'))
    assert caplog.messages == []


@pytest.mark.parametrize('filename,tables', [
    ('data.db', ['other_records']),
    ('data.db', ['trial_records']),
    ('data.db', ['trial_cancellations']),
    ('switch.db', ['switch_records']),
])
def test_foreign_databases_are_skipped(locations, filename, tables):
    legacy, destination = locations
    legacy.mkdir(parents=True)
    with closing(sqlite3.connect(legacy / filename)) as connection:
        for table in tables:
            connection.execute(f'CREATE TABLE {table} (value TEXT)')
        connection.commit()
    (legacy / 'switch.salt').write_bytes(b's' * 32)
    before = snapshot(legacy)
    migrate_legacy_storage()
    assert not destination.exists()
    assert snapshot(legacy) == before


@pytest.mark.parametrize('existing', ['data.db', 'switch.db'])
def test_each_database_copies_independently_without_replacing_existing_files(locations, existing):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    switch_database(legacy / 'switch.db')
    before = snapshot(legacy)
    destination.mkdir()
    (destination / existing).write_bytes(b'keep destination unchanged')
    migrate_legacy_storage()
    assert (destination / existing).read_bytes() == b'keep destination unchanged'
    copied = 'switch.db' if existing == 'data.db' else 'data.db'
    with closing(sqlite3.connect(destination / copied)) as connection:
        table = 'shadow_switch_records' if copied == 'switch.db' else 'trial_records'
        assert connection.execute(f'SELECT count(*) FROM {table}').fetchone() == (1,)
    assert (destination / 'switch.salt').exists() == (copied == 'switch.db')
    assert snapshot(legacy) == before


def test_missing_legacy_folder_creates_nothing(locations, caplog):
    legacy, destination = locations
    migrate_legacy_storage()
    assert not legacy.exists()
    assert not destination.parent.exists()
    assert caplog.messages == []


def test_stale_migration_files_are_swept_only_in_destination(locations, caplog):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    for name in ['.legacy-db-old', '.legacy-salt-old', '.legacy-work-old']:
        (legacy / name).write_bytes(b'keep shared files')
    destination.mkdir()
    (destination / '.legacy-db-old').write_bytes(b'partial database')
    (destination / '.legacy-db-old-wal').write_bytes(b'partial WAL')
    (destination / '.legacy-salt-old').write_bytes(b'partial salt')
    work = destination / '.legacy-work-old'
    work.mkdir()
    (work / 'data.db').write_bytes(b'partial recovery')
    (destination / '.legacy-work-link').symlink_to(legacy, target_is_directory=True)
    (destination / '.legacy-db-link').symlink_to(legacy / 'data.db')
    old = time.time() - 7200
    for path in destination.iterdir():
        os.utime(path, (old, old), follow_symlinks=False)
    fresh = destination / '.legacy-work-fresh'
    fresh.mkdir()
    (fresh / 'data.db').write_bytes(b'active recovery')
    (destination / 'unrelated.txt').write_bytes(b'keep destination file')
    before = snapshot(legacy)
    migrate_legacy_storage()
    assert snapshot(legacy) == before
    assert set(os.listdir(legacy)) == set(before)
    assert set(os.listdir(destination)) == {'data.db', 'unrelated.txt', '.legacy-work-fresh'}
    assert (fresh / 'data.db').read_bytes() == b'active recovery'
    assert (destination / 'unrelated.txt').read_bytes() == b'keep destination file'
    with closing(sqlite3.connect(destination / 'data.db')) as connection:
        assert connection.execute('SELECT id FROM trial_records').fetchall() == [('saved-trial',)]
    assert caplog.messages == []


def test_stale_files_are_swept_even_without_legacy_databases(locations, caplog):
    legacy, destination = locations
    destination.mkdir(parents=True)
    (destination / '.legacy-db-old').write_bytes(b'partial database')
    old = time.time() - 7200
    os.utime(destination / '.legacy-db-old', (old, old))
    migrate_legacy_storage()
    assert set(os.listdir(destination)) == set()
    assert not legacy.exists()
    assert caplog.messages == []


def test_sweep_does_not_follow_a_destination_folder_symlink(locations, caplog):
    legacy, destination = locations
    legacy.mkdir(parents=True)
    path = legacy / '.legacy-db-old'
    path.write_bytes(b'keep shared file')
    old = time.time() - 7200
    os.utime(path, (old, old))
    destination.symlink_to(legacy, target_is_directory=True)
    migrate_legacy_storage()
    assert snapshot(legacy) == {'.legacy-db-old': b'keep shared file'}
    assert caplog.messages == []


def test_second_run_leaves_both_locations_unchanged(locations):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    switch_database(legacy / 'switch.db')
    migrate_legacy_storage()
    before = snapshot(legacy), snapshot(destination)
    migrate_legacy_storage()
    assert (snapshot(legacy), snapshot(destination)) == before


@pytest.mark.parametrize('filename', ['data.db', 'switch.db'])
def test_copy_keeps_only_shadow_tables_and_their_indexes(locations, filename):
    legacy, destination = locations
    table = 'trial_records' if filename == 'data.db' else 'shadow_switch_records'
    if filename == 'data.db':
        trial_database(legacy / filename)
        expected = {'trial_records', 'trial_cancellations', 'trial_controllers',
                    'trial_second_looks', 'trial_replay_children', 'own_index'}
    else:
        switch_database(legacy / filename)
        expected = {'shadow_switch_records', 'own_index'}
    foreign_value = 'foreign-plugin-private-payload-' * 1000
    with closing(sqlite3.connect(legacy / filename)) as connection:
        connection.execute('CREATE TABLE "foreign\"\"records" (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)')
        connection.execute('INSERT INTO "foreign\"\"records" (value) VALUES (?)', (foreign_value,))
        connection.execute("INSERT INTO sqlite_sequence VALUES ('foreign_dropped', 42)")
        connection.execute('CREATE VIEW foreign_view AS SELECT * FROM "foreign\"\"records"')
        connection.execute('CREATE INDEX foreign_index ON "foreign\"\"records" (value)')
        connection.execute(f'CREATE INDEX own_index ON {table} (id)')
        connection.execute(f'CREATE TRIGGER foreign_trigger AFTER DELETE ON {table} '
                           'BEGIN DELETE FROM "foreign\"\"records"; END')
        connection.execute('ANALYZE')
        connection.commit()
    before = snapshot(legacy)
    migrate_legacy_storage()
    assert snapshot(legacy) == before
    assert set(os.listdir(legacy)) == set(before)
    with closing(sqlite3.connect(destination / filename)) as connection:
        schema = {row[0] for row in connection.execute('SELECT name FROM sqlite_master')
                  if not row[0].startswith('sqlite_')}
        assert schema == expected
        if connection.execute("SELECT name FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            assert connection.execute('SELECT name FROM sqlite_sequence').fetchall() == []
        assert connection.execute("SELECT name FROM sqlite_master WHERE name IN "
                                  "('sqlite_stat1', 'sqlite_stat4')").fetchall() == []
        assert connection.execute(f'SELECT count(*) FROM {table}').fetchone() == (1,)
        assert connection.execute('PRAGMA quick_check').fetchall() == [('ok',)]
    assert b'foreign-plugin-private-payload-' not in (destination / filename).read_bytes()


@pytest.mark.parametrize('salt', [None, b'', b's' * 31, b's' * 32, b's' * 33])
def test_salt_requires_a_verified_switch_database_and_exact_size(locations, salt):
    legacy, destination = locations
    switch_database(legacy / 'switch.db')
    (legacy / 'switch.salt').unlink()
    if salt is not None:
        (legacy / 'switch.salt').write_bytes(salt)
    migrate_legacy_storage()
    assert (destination / 'switch.db').is_file()
    if salt is not None and len(salt) == 32:
        assert (destination / 'switch.salt').read_bytes() == salt
    else:
        assert not (destination / 'switch.salt').exists()


@pytest.mark.parametrize('kind', ['absent', 'foreign', 'symlink', 'directory'])
def test_salt_is_not_copied_by_itself_or_from_nonregular_files(locations, kind):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    if kind == 'foreign':
        with closing(sqlite3.connect(legacy / 'switch.db')) as connection:
            connection.execute('CREATE TABLE foreign_records (value TEXT)')
    elif kind in {'symlink', 'directory'}:
        switch_database(legacy / 'switch.db')
        (legacy / 'switch.salt').unlink()
    if kind == 'symlink':
        (legacy / 'other-salt').write_bytes(b's' * 32)
        (legacy / 'switch.salt').symlink_to(legacy / 'other-salt')
    elif kind == 'directory':
        (legacy / 'switch.salt').mkdir()
    else:
        (legacy / 'switch.salt').write_bytes(b's' * 32)
    migrate_legacy_storage()
    assert not (destination / 'switch.salt').exists()


def test_existing_destination_salt_is_preserved(locations):
    legacy, destination = locations
    switch_database(legacy / 'switch.db')
    destination.mkdir()
    (destination / 'switch.salt').write_bytes(b'd' * 32)
    migrate_legacy_storage()
    assert (destination / 'switch.db').is_file()
    assert (destination / 'switch.salt').read_bytes() == b'd' * 32


def test_salt_table_alone_identifies_a_legacy_switch_database(locations):
    legacy, destination = locations
    legacy.mkdir(parents=True)
    with closing(sqlite3.connect(legacy / 'switch.db')) as connection:
        connection.execute('CREATE TABLE shadow_switch_salt (salt BLOB NOT NULL)')
        connection.execute('INSERT INTO shadow_switch_salt VALUES (?)', (b's' * 32,))
        connection.commit()
    migrate_legacy_storage()
    with closing(sqlite3.connect(destination / 'switch.db')) as connection:
        assert connection.execute('SELECT salt FROM shadow_switch_salt').fetchone() == (b's' * 32,)


def test_checkpointed_wal_database_never_creates_legacy_sidecars(locations):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    with closing(sqlite3.connect(legacy / 'data.db')) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
    before = snapshot(legacy)
    migrate_legacy_storage()
    assert snapshot(legacy) == before
    assert set(os.listdir(legacy)) == set(before)
    assert (destination / 'data.db').is_file()
    assert set(os.listdir(destination)) == {'data.db'}


@pytest.mark.parametrize('suffix', ['-wal', '-journal'])
def test_recovery_failure_warns_and_removes_private_work_files(locations, monkeypatch, caplog, suffix):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    (legacy / ('data.db' + suffix)).write_bytes(b'pending changes')
    before = snapshot(legacy)
    connect = sqlite3.connect

    def fail_recovery(database, *args, **kwargs):
        if '.legacy-work-' in str(database):
            raise sqlite3.DatabaseError('private recovery failure')
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', fail_recovery)
    migrate_legacy_storage()
    assert set(os.listdir(destination)) == set()
    assert snapshot(legacy) == before
    assert caplog.messages == ['Legacy Shadow storage was not copied. Shadow starts with empty trial storage '
                               'for failed copies and leaves plugin-data/shadow unchanged.']


@pytest.mark.parametrize('suffix', ['', '-wal', '-journal'])
@pytest.mark.parametrize('changed', ['size', 'mtime_ns'])
@pytest.mark.parametrize('failures', [1, 3])
def test_changed_source_is_retried_without_modifying_legacy(
        locations, monkeypatch, caplog, suffix, changed, failures):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    (legacy / 'data.db-wal').write_bytes(b'')
    (legacy / 'data.db-journal').write_bytes(b'')
    before = snapshot(legacy)
    metadata = snapshot_metadata(legacy)
    target = legacy / ('data.db' + suffix)
    copyfileobj = storage_migration.shutil.copyfileobj
    lstat = Path.lstat
    copied_all = False
    changes = []

    def copy_then_signal(source, copied):
        nonlocal copied_all
        copyfileobj(source, copied)
        if Path(copied.name).name == 'data.db-journal':
            copied_all = True

    def changed_metadata(path, *args, **kwargs):
        nonlocal copied_all
        info = lstat(path, *args, **kwargs)
        if path == target and copied_all and len(changes) < failures:
            copied_all = False
            changes.append(path)
            return SimpleNamespace(st_size=info.st_size + (changed == 'size'),
                                   st_mtime_ns=info.st_mtime_ns + (changed == 'mtime_ns'))
        return info

    with monkeypatch.context() as changing:
        changing.setattr(storage_migration.shutil, 'copyfileobj', copy_then_signal)
        changing.setattr(Path, 'lstat', changed_metadata)
        migrate_legacy_storage()
    assert len(changes) == failures
    assert set(os.listdir(destination)) == ({'data.db'} if failures == 1 else set())
    assert snapshot(legacy) == before
    assert snapshot_metadata(legacy) == metadata
    warning = ['Legacy Shadow storage was not copied. Shadow starts with empty trial storage '
               'for failed copies and leaves plugin-data/shadow unchanged.']
    assert caplog.messages == ([] if failures == 1 else warning)
    migrate_legacy_storage()
    assert set(os.listdir(destination)) == {'data.db'}
    assert snapshot(legacy) == before


@pytest.mark.parametrize('filename', ['data.db', 'switch.db'])
@pytest.mark.parametrize('changed', ['size', 'mtime_ns'])
@pytest.mark.parametrize('failures', [1, 3])
def test_changed_immutable_source_is_checked_before_publication(
        locations, monkeypatch, caplog, filename, changed, failures):
    legacy, destination = locations
    source = legacy / filename
    if filename == 'data.db':
        trial_database(source)
    else:
        switch_database(source)
    before = snapshot(legacy)
    metadata = snapshot_metadata(legacy)
    connect = sqlite3.connect
    lstat = Path.lstat
    backups = 0
    copied = False

    class ChangingSource(sqlite3.Connection):
        def backup(self, target):
            nonlocal backups, copied
            super().backup(target)
            backups += 1
            copied = True

    def changing_connect(database, *args, **kwargs):
        if str(database).startswith(source.as_uri()) and kwargs.get('uri'):
            kwargs['factory'] = ChangingSource
        return connect(database, *args, **kwargs)

    def changed_metadata(path, *args, **kwargs):
        nonlocal copied
        info = lstat(path, *args, **kwargs)
        if path == source and copied:
            copied = False
            if backups <= failures:
                return SimpleNamespace(st_size=info.st_size + (changed == 'size'),
                                       st_mtime_ns=info.st_mtime_ns + (changed == 'mtime_ns'))
        return info

    with monkeypatch.context() as changing:
        changing.setattr(sqlite3, 'connect', changing_connect)
        changing.setattr(Path, 'lstat', changed_metadata)
        migrate_legacy_storage()
    assert backups == (2 if failures == 1 else 3)
    assert snapshot(legacy) == before
    assert set(os.listdir(legacy)) == set(before)
    assert snapshot_metadata(legacy) == metadata
    assert not list(destination.glob('.legacy-*'))
    if failures == 1:
        table = 'trial_records' if filename == 'data.db' else 'shadow_switch_records'
        with closing(connect(destination / filename)) as connection:
            assert connection.execute(f'SELECT count(*) FROM {table}').fetchone() == (1,)
        assert caplog.messages == []
    else:
        assert set(os.listdir(destination)) == set()
        assert caplog.messages == [
            'Legacy Shadow storage was not copied. Shadow starts with empty trial storage '
            'for failed copies and leaves plugin-data/shadow unchanged.'
        ]


def test_live_wal_database_is_recovered_without_changing_its_sidecars(locations):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    with closing(sqlite3.connect(legacy / 'data.db')) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute("INSERT INTO trial_cancellations VALUES ('pending')")
        connection.commit()
        before = snapshot(legacy)
        migrate_legacy_storage()
        assert snapshot(legacy) == before
        assert set(os.listdir(legacy)) == set(before)
        assert set(os.listdir(destination)) == {'data.db'}
        with closing(sqlite3.connect(destination / 'data.db')) as copied:
            assert copied.execute('SELECT id FROM trial_cancellations').fetchall() == [('pending',)]
            assert copied.execute('PRAGMA quick_check').fetchall() == [('ok',)]


@pytest.mark.parametrize('mode', ['WAL', 'DELETE'])
def test_leftover_wal_or_hot_journal_recovers_only_committed_rows(locations, mode):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    subprocess.run([sys.executable, '-c', '''
import os
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=' + sys.argv[2])
connection.execute('PRAGMA cache_size=1')
connection.execute("INSERT INTO trial_cancellations VALUES ('committed')")
connection.commit()
if sys.argv[2] == 'DELETE':
    connection.execute("DELETE FROM trial_cancellations")
    connection.executemany('INSERT INTO trial_cancellations VALUES (?)',
                           [(str(i) + 'x' * 4096,) for i in range(100)])
os._exit(0)
''', str(legacy / 'data.db'), mode], check=True)
    suffix = '-wal' if mode == 'WAL' else '-journal'
    assert (legacy / ('data.db' + suffix)).stat().st_size > 0
    before = snapshot(legacy)
    migrate_legacy_storage()
    assert snapshot(legacy) == before
    assert set(os.listdir(legacy)) == set(before)
    assert set(os.listdir(destination)) == {'data.db'}
    with closing(sqlite3.connect(destination / 'data.db')) as copied:
        assert copied.execute('SELECT id FROM trial_cancellations').fetchall() == [('committed',)]
        assert copied.execute('SELECT id FROM trial_records').fetchall() == [('saved-trial',)]
        assert copied.execute('PRAGMA quick_check').fetchall() == [('ok',)]


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'fifo'])
def test_recovery_refuses_nonregular_sidecars_without_touching_legacy_files(locations, caplog, kind):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    sidecar = legacy / 'data.db-wal'
    if kind == 'symlink':
        (legacy / 'unrelated').write_bytes(b'other plugin data')
        sidecar.symlink_to(legacy / 'unrelated')
    elif kind == 'directory':
        sidecar.mkdir()
    else:
        os.mkfifo(sidecar)
    before = (legacy / 'data.db').read_bytes()
    listing = set(os.listdir(legacy))
    migrate_legacy_storage()
    assert (legacy / 'data.db').read_bytes() == before
    assert set(os.listdir(legacy)) == listing
    if kind == 'symlink':
        assert sidecar.is_symlink()
        assert (legacy / 'unrelated').read_bytes() == b'other plugin data'
    assert set(os.listdir(destination)) == set()
    assert caplog.messages == ['Legacy Shadow storage was not copied. Shadow starts with empty trial storage '
                               'for failed copies and leaves plugin-data/shadow unchanged.']


def test_recovered_database_must_pass_quick_check_before_publication(locations, monkeypatch, caplog):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    switch_database(legacy / 'switch.db')
    (legacy / 'data.db-wal').write_bytes(b'leftover')
    before = snapshot(legacy)
    connect = sqlite3.connect

    class FailedCheck(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == 'PRAGMA quick_check':
                return super().execute("SELECT 'ok' UNION ALL SELECT 'damaged'")
            return super().execute(sql, *args)

    def failed_check(database, *args, **kwargs):
        if '.legacy-work-' in str(database):
            kwargs['factory'] = FailedCheck
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', failed_check)
    migrate_legacy_storage()
    assert set(os.listdir(destination)) == {'switch.db', 'switch.salt'}
    assert snapshot(legacy) == before
    assert caplog.messages == ['Legacy Shadow storage was not copied. Shadow starts with empty trial storage '
                               'for failed copies and leaves plugin-data/shadow unchanged.']


def test_corrupt_trial_does_not_block_copying_our_switch_database(locations, caplog):
    legacy, destination = locations
    switch_database(legacy / 'switch.db')
    (legacy / 'data.db').write_bytes(b'invalid database')
    migrate_legacy_storage()
    assert not (destination / 'data.db').exists()
    assert (destination / 'switch.db').is_file()
    assert (destination / 'switch.salt').is_file()
    assert caplog.messages == ['Legacy Shadow storage was not copied. Shadow starts with empty trial storage '
                               'for failed copies and leaves plugin-data/shadow unchanged.']


def test_failed_backup_never_publishes_a_partial_database_and_can_retry(locations, monkeypatch, caplog):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    before = snapshot(legacy)
    connect = sqlite3.connect

    class BrokenBackup(sqlite3.Connection):
        def backup(self, target):
            target.execute('CREATE TABLE partial_copy (value TEXT)')
            target.commit()
            raise sqlite3.DatabaseError('private failure details')

    def broken_connect(*args, **kwargs):
        if kwargs.get('uri'):
            kwargs['factory'] = BrokenBackup
        return connect(*args, **kwargs)

    with monkeypatch.context() as failing:
        failing.setattr(sqlite3, 'connect', broken_connect)
        migrate_legacy_storage()
    assert snapshot(destination) == {}
    assert snapshot(legacy) == before
    assert caplog.messages == ['Legacy Shadow storage was not copied. Shadow starts with empty trial storage '
                               'for failed copies and leaves plugin-data/shadow unchanged.']
    migrate_legacy_storage()
    assert set(snapshot(destination)) == {'data.db'}


def test_corrupt_sources_log_one_fixed_message_and_allow_registration(locations, caplog):
    legacy, destination = locations
    legacy.mkdir(parents=True)
    for filename in ('data.db', 'switch.db'):
        (legacy / filename).write_bytes(b'invalid database with private details')
    register(FakeContext())
    messages = [record.getMessage() for record in caplog.records
                if record.name == 'shadow.common.storage_migration']
    assert messages == ['Legacy Shadow storage was not copied. Shadow starts with empty trial storage '
                        'for failed copies and leaves plugin-data/shadow unchanged.']
    assert not destination.exists()


def test_database_publication_never_overwrites_a_racing_destination(locations, monkeypatch):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    link = os.link

    def racing_link(source, target):
        Path(target).write_bytes(b'another writer won')
        link(source, target)

    monkeypatch.setattr(os, 'link', racing_link)
    migrate_legacy_storage()
    assert snapshot(destination) == {'data.db': b'another writer won'}


def test_failed_publication_retries_with_the_same_switch_identity(locations, monkeypatch, caplog):
    legacy, destination = locations
    identity = switch_database(legacy / 'switch.db')
    link = os.link

    def fail_database(source, target):
        if Path(target).name == 'switch.db':
            raise OSError('private failure details')
        link(source, target)

    with monkeypatch.context() as failing:
        failing.setattr(os, 'link', fail_database)
        migrate_legacy_storage()
    assert not (destination / 'switch.db').exists()
    assert set(snapshot(destination)) == {'switch.salt'}
    assert caplog.messages == ['Legacy Shadow storage was not copied. Shadow starts with empty trial storage '
                               'for failed copies and leaves plugin-data/shadow unchanged.']
    migrate_legacy_storage()
    store = SwitchStore(path=destination / 'switch.db')
    assert store.read().id == 'saved-switch'
    assert store.identity('conversation') == identity
    store.connection.close()


def test_migration_follows_the_active_hermes_home(locations, tmp_path, monkeypatch):
    legacy, destination = locations
    trial_database(legacy / 'data.db')
    active_home = tmp_path / 'other-profile'
    monkeypatch.setattr(sys.modules['hermes_constants'], 'get_hermes_home', lambda: active_home)
    migrate_legacy_storage()
    assert not destination.exists()
    assert not active_home.exists()
    trial_database(active_home / 'plugin-data' / 'shadow' / 'data.db')
    migrate_legacy_storage()
    assert (active_home / 'plugin-data' / 'shadow-trial' / 'data.db').is_file()
    assert not destination.exists()
