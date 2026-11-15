from contextlib import closing, contextmanager
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile

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
        for filename, (required, distinctive) in _LEGACY_TABLES.items():
            try:
                _copy_database(root / 'shadow' / filename, root / STORAGE_NAME / filename,
                               required, distinctive)
            except (OSError, sqlite3.Error):
                failed = True
    except Exception:
        failed = True
    if failed:
        logger.warning('Legacy Shadow storage was not copied.')


def _copy_database(source, destination, required, distinctive):
    if os.path.lexists(destination) or not source.is_file() or source.is_symlink():
        return
    with _legacy_connection(source, destination.parent) as legacy:
        if legacy.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise sqlite3.DatabaseError('Legacy database failed quick_check')
        tables = {row[0] for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables or not distinctive & tables:
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.legacy-db-')
        os.close(fd)
        try:
            with closing(sqlite3.connect(temporary)) as copied:
                legacy.backup(copied)
                copied.execute('PRAGMA journal_mode=DELETE')
            if destination.name == 'switch.db' and not os.path.lexists(destination):
                # Publish the salt first so a crash cannot expose a re-keyed switch database.
                _copy_salt(source.with_suffix('.salt'), destination.with_suffix('.salt'))
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)


@contextmanager
def _legacy_connection(source, destination_parent):
    sidecars = [Path(str(source) + suffix) for suffix in ('-wal', '-journal')
                if os.path.lexists(str(source) + suffix)]
    if not sidecars:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as legacy:
            yield legacy
        return
    destination_parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=destination_parent, prefix='.legacy-work-'))
    try:
        for path in [source, *sidecars]:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError('Legacy database source is not a regular file')
                with (work / path.name).open('wb') as copied:
                    shutil.copyfileobj(stream, copied)
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
