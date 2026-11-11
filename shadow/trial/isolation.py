"""Dated proof that a replay is isolated. A passing record is the only way to start a test."""

import hashlib
import hmac
import json
import os
import secrets
import stat
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CHECK_NAMES = (
    'memory',
    'live_context',
    'other_sessions',
    'veto',
    'middleware',
    'read_only',
    'secrets',
    'unchanged',
)

RECORD_NAME = 'isolation.json'

# Never opened. Size and mtime are compared so a replay cannot change them unnoticed.
CREDENTIAL_NAMES = frozenset({'.env', 'auth.json', '.credentials.json'})


@dataclass(frozen=True)
class Identity:
    hermes_version: str
    hermes_commit: str
    plugin_version: str
    plugin_hash: str
    profile: str
    config_hash: str = 'explicit-identity'


def plugin_root():
    return Path(__file__).resolve().parents[2]


def plugin_version():
    for line in (plugin_root() / 'plugin.yaml').read_text().splitlines():
        if line.startswith('version:'):
            return line.split(':', 1)[1].strip().strip('"').strip("'")
    return ''


def plugin_hash():
    root = plugin_root()
    paths = [root / 'plugin.yaml', root / '__init__.py']
    package = root / 'shadow'
    paths.extend(path for path in package.rglob('*') if path.is_file() and path.suffix in {'.py', '.json'} and '__pycache__' not in path.parts)
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def hermes_revision():
    """Version and commit of the Hermes that is running. Empty when it cannot be read."""
    try:
        from hermes_cli.version_info import get_version_info
        info = get_version_info()
    except Exception:
        return '', ''
    version = str(getattr(info, 'display_version', '') or getattr(info, 'base_version', '') or '')
    commit = str(getattr(info, 'commit', '') or '')
    return version, commit[:40]


def current_identity(profile):
    version, commit = hermes_revision()
    return Identity(version, commit, plugin_version(), plugin_hash(), profile, configuration_hash())


def configuration_hash():
    """Fingerprint effective isolation settings and current installed plugin versions."""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins import get_plugin_manager
        from hermes_cli.plugins_discovery import collect_directory_manifests
        config = load_config_readonly()
        compression = config.get('compression') or {}
        plugin_config = deepcopy(config.get('plugins', {}))
        entries = plugin_config.get('entries', {})
        shadow = entries.get('shadow', {})
        shadow.pop('settings', None)
        if not shadow:
            entries.pop('shadow', None)
        if not entries:
            plugin_config.pop('entries', None)
        plugins = [
            {'key': item.get('key') or item['name'], 'version': item['version']}
            for item in get_plugin_manager().list_plugins() if item['enabled']
        ]
        installed = [
            {'key': manifest.key or manifest.name, 'version': manifest.version, 'source': manifest.source}
            for manifest in collect_directory_manifests()
        ]
        relevant = {
            'checkpoint_required': compression.get('checkpoint_required', False),
            'delegation': config.get('delegation', {}),
            'memory': config.get('memory', {}),
            'plugins': plugin_config,
            'enabled_plugins': sorted(plugins, key=lambda item: item['key']),
            'installed_plugins': sorted(installed, key=lambda item: (item['key'], item['source'])),
        }
        return digest(relevant)
    except Exception:
        return ''


def digest(checks):
    payload = json.dumps(checks, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def record_path(directory):
    return Path(directory) / RECORD_NAME


def read_record(directory):
    path = record_path(directory)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def verifier_key_path(directory):
    """The verifier trusts this sibling store, never the editable plugin-data record.

    This boundary protects record writers, not processes with arbitrary same-user access.
    """
    directory = Path(directory).resolve()
    name = hashlib.sha256(str(directory).encode()).hexdigest()
    return directory.parent / '.shadow-verifier' / name / 'record.key'


def verifier_key(directory, *, create=False):
    path = verifier_key_path(directory)
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(secrets.token_bytes(32))
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('invalid verifier key permissions')
        key = stream.read(33)
    if len(key) != 32:
        raise ValueError('invalid verifier key')
    return key


def signature(body, key):
    payload = json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def write_record(directory, identity, checks, *, when=None, no_saved_memory=False):
    if set(checks) != set(CHECK_NAMES) or any(checks[name] is not True for name in CHECK_NAMES):
        raise ValueError('a verification record requires every check to pass')
    if not identity.hermes_version or not identity.hermes_commit or not identity.plugin_hash or not identity.config_hash:
        raise ValueError('a verification record requires the running Hermes and Shadow files')
    moment = when or datetime.now(timezone.utc)
    directory = Path(directory).resolve()
    body = {
        'passed': True,
        'checked_at': moment.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'hermes_version': identity.hermes_version,
        'hermes_commit': identity.hermes_commit,
        'plugin_version': identity.plugin_version,
        'plugin_hash': identity.plugin_hash,
        'profile': identity.profile,
        'config_hash': identity.config_hash,
        'record_directory': str(directory),
        'checks': {name: True for name in CHECK_NAMES},
        'no_saved_memory': no_saved_memory is True,
    }
    body['signature'] = signature(body, verifier_key(directory, create=True))
    directory.mkdir(parents=True, exist_ok=True)
    path = record_path(directory)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(body, sort_keys=True, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)
    return body


def matches(record, identity, profile, *, directory=None):
    """True only for an intact passing record for this Hermes, these files, and this profile."""
    if not isinstance(record, dict) or record.get('passed') is not True:
        return False
    stored_directory = record.get('record_directory')
    signed = record.get('signature')
    if not isinstance(stored_directory, str) or not isinstance(signed, str):
        return False
    try:
        location = Path(stored_directory).resolve()
        if directory is not None and location != Path(directory).resolve():
            return False
        body = {key: value for key, value in record.items() if key != 'signature'}
        if not hmac.compare_digest(signed, signature(body, verifier_key(location))):
            return False
    except (OSError, ValueError, TypeError):
        return False
    if record.get('profile') != profile or profile != identity.profile:
        return False
    if not identity.hermes_version or record.get('hermes_version') != identity.hermes_version:
        return False
    if not identity.hermes_commit or record.get('hermes_commit') != identity.hermes_commit:
        return False
    if not identity.plugin_version or record.get('plugin_version') != identity.plugin_version:
        return False
    if not identity.plugin_hash or record.get('plugin_hash') != identity.plugin_hash:
        return False
    if not identity.config_hash or record.get('config_hash') != identity.config_hash:
        return False
    checks = record.get('checks')
    if not isinstance(checks, dict) or set(checks) != set(CHECK_NAMES):
        return False
    if any(checks.get(name) is not True for name in CHECK_NAMES):
        return False
    checked = record.get('checked_at')
    return isinstance(checked, str) and len(checked) >= 10


def config_can_verify(_config):
    """Profile config cannot turn verification on."""
    return False
