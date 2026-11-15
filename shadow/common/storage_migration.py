from contextlib import closing, contextmanager
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time

from .storage import STORAGE_NAME


logger = logging.getLogger(__name__)
_LEGACY_TABLES = {
    'data.db': ({'trial_records'}, {'trial_cancellations', 'trial_controllers',
                                  'trial_second_looks', 'trial_replay_children'}),
    'switch.db': (set(), {'shadow_switch_records', 'shadow_switch_salt'}),
}


def migrate_legacy_storage():
    failed = False
    try:
        from hermes_constants import get_hermes_home
        root = Path(get_hermes_home()).absolute() / 'plugin-data'
        try:
            _sweep_temporary_files(root / STORAGE_NAME)
        except OSError:
            failed = True
        for filename, (required, distinctive) in _LEGACY_TABLES.items():
            try:
                _copy_database(root / 'shadow' / filename, root / STORAGE_NAME / filename,
                               required, distinctive)
            except (OSError, sqlite3.Error):
                failed = True
    except Exception:
        failed = True
    if failed:
        logger.warning('Legacy Shadow storage was not copied. Shadow starts with empty trial storage '
                       'for failed copies and leaves plugin-data/shadow unchanged.')


def _sweep_temporary_files(directory):
    if not directory.exists() or directory.is_symlink():
        return
    for path in directory.iterdir():
        if not path.name.startswith(('.legacy-db-', '.legacy-salt-', '.legacy-work-')):
            continue
        if time.time() - path.lstat().st_mtime < 3600:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def _copy_database(source, destination, required, distinctive):
    for attempt, delay in enumerate((0, 0.05, 0.2)):
        if delay:
            time.sleep(delay)
        try:
            _copy_database_once(source, destination, required, distinctive)
            return
        except (OSError, sqlite3.Error):
            if attempt == 2:
                raise


def _copy_database_once(source, destination, required, distinctive):
    if os.path.lexists(destination) or not source.is_file() or source.is_symlink():
        return
    temporary = None
    try:
        with _legacy_connection(source, destination.parent) as legacy:
            if legacy.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise sqlite3.DatabaseError('Legacy database failed quick_check')
            tables = {row[0] for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required <= tables or not distinctive & tables:
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.legacy-db-')
            os.close(fd)
            with closing(sqlite3.connect(temporary)) as copied:
                legacy.backup(copied)
                copied.execute('PRAGMA journal_mode=DELETE')
                own_tables = required | distinctive
                objects = copied.execute("SELECT type, name, tbl_name FROM sqlite_master "
                                         "ORDER BY type='table'").fetchall()
                for kind, name, table in objects:
                    if name.startswith('sqlite_'):
                        continue
                    if kind == 'table' and name in own_tables:
                        continue
                    if kind == 'index' and table in own_tables:
                        continue
                    quoted = '"' + name.replace('"', '""') + '"'
                    copied.execute(f'DROP {kind.upper()} IF EXISTS {quoted}')
                if 'sqlite_sequence' in tables:
                    placeholders = ','.join('?' for _ in own_tables)
                    copied.execute(f'DELETE FROM sqlite_sequence WHERE name NOT IN ({placeholders})',
                                   tuple(own_tables))
                copied.execute('DROP TABLE IF EXISTS sqlite_stat1')
                copied.execute('DROP TABLE IF EXISTS sqlite_stat4')
                copied.commit()
                copied.execute('VACUUM')
        if destination.name == 'switch.db' and not os.path.lexists(destination):
            # Publish the salt first so a crash cannot expose a re-keyed switch database.
            _copy_salt(source.with_suffix('.salt'), destination.with_suffix('.salt'))
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


@contextmanager
def _legacy_connection(source, destination_parent):
    # Coarse mtimes can miss same-size rewrites within one timestamp tick.
    sidecars = [Path(str(source) + suffix) for suffix in ('-wal', '-journal')
                if os.path.lexists(str(source) + suffix)]
    if not sidecars:
        before = source.lstat()
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as legacy:
            yield legacy
        after = source.lstat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError('Legacy database source changed during copying')
        return
    destination_parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=destination_parent, prefix='.legacy-work-'))
    try:
        versions = {}
        for path in [source, *sidecars]:
            info = path.lstat()
            versions[path] = (info.st_size, info.st_mtime_ns)
        for path in versions:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError('Legacy database source is not a regular file')
                with (work / path.name).open('wb') as copied:
                    shutil.copyfileobj(stream, copied)
        for path, before in versions.items():
            info = path.lstat()
            if before != (info.st_size, info.st_mtime_ns):
                raise OSError('Legacy database source changed during copying')
        with closing(sqlite3.connect((work / source.name).as_uri() + '?mode=rw', uri=True)) as legacy:
            yield legacy
    finally:
        shutil.rmtree(work)


def _copy_salt(source, destination):
    if os.path.lexists(destination) or not source.is_file() or source.is_symlink():
        return
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != 32:
            return
        salt = stream.read(33)
    if len(salt) != 32:
        return
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.legacy-salt-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(salt)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
    finally:
        Path(temporary).unlink(missing_ok=True)
