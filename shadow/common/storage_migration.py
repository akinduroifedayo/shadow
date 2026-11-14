from contextlib import closing
import logging
import os
from pathlib import Path
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
    # Immutable reads prevent sidecar writes but cannot recover a WAL or journal.
    if any(os.path.lexists(str(source) + suffix) for suffix in ('-wal', '-journal')):
        return
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as legacy:
        tables = {row[0] for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables or not distinctive & tables:
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.legacy-db-')
        os.close(fd)
        try:
            with closing(sqlite3.connect(temporary)) as copied:
                legacy.backup(copied)
            if destination.name == 'switch.db' and not os.path.lexists(destination):
                # Publish the salt first so a crash cannot expose a re-keyed switch database.
                _copy_salt(source.with_suffix('.salt'), destination.with_suffix('.salt'))
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)


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
