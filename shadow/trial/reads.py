import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from ..common.copy import copy
from ..common.scrub import Scrubber


_SECRET_PARTS = {'.git', '.hermes', '.ssh', '.aws', '.gnupg', '.config', '.docker', '.kube', '.azure',
                 '.netrc', '_netrc', '.npmrc', '.pypirc', '.git-credentials', '.pgpass', '.my.cnf',
                 '.curlrc', '.wgetrc', 'node_modules', '__pycache__'}


def secret_path(path):
    for part in Path(path).parts:
        name = part.lower()
        if name in _SECRET_PARTS or name.startswith(('.env', 'id_rsa', 'id_ed25519')):
            return True
        if any(word in name for word in ('credential', 'secret', 'password', 'token', 'auth.json')):
            return True
        if name.endswith(('.pem', '.key', '.p12', '.pfx', '.db', '.sqlite', '.sqlite3')):
            return True
    return False


def scoped_path(cwd, value):
    root = Path(cwd).resolve(strict=True)
    if any(p.lower() in _SECRET_PARTS | {'secrets', 'credentials', 'tokens', 'keys'} or p.lower().startswith('.env') for p in root.parts):
        raise ValueError('secret read scope')
    requested = Path(value)
    if secret_path(requested):
        raise ValueError('secret path')
    target = (root / requested).resolve(strict=True)
    if not target.is_relative_to(root) or secret_path(target.relative_to(root)):
        raise ValueError('outside scope')
    return target


def read_scoped(cwd, value):
    root = Path(cwd).resolve(strict=True)
    target = scoped_path(root, value)
    parts = target.relative_to(root).parts
    if not parts:
        raise ValueError('not a file')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, 'rb') as stream:
            import stat
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('not a regular file')
            return Scrubber().text(stream.read(262144).decode('utf-8', errors='replace'))
    finally:
        os.close(fd)


class SafeReads:
    def __init__(self, guard, web_fetch=None):
        self.guard = guard
        self.web_fetch = web_fetch
        self.cache = {}
        self.web_lookups = True
        self.web_turns = set()
        self.web_stopped = set()
        self.todos = {}
        self.record = None
        self.on_change = None

    def bind(self, record, on_change=None):
        self.record, self.on_change = record, on_change
        self.web_lookups = record['web_lookups']
        self.cache = record.setdefault('web_cache', {})
        self.web_turns = {json.loads(key)[0] for key in self.cache}
        self.web_stopped = set(record.setdefault('web_stopped_turn_ids', []))

    def save(self):
        if self.record is not None:
            self.record['web_turns'] = len(self.web_turns)
            self.record['web_stopped'] = len(self.web_stopped)
            self.record['web_stopped_turn_ids'] = sorted(self.web_stopped)
        if self.on_change:
            self.on_change()

    def read(self, args, *, session_id='', **_):
        identity = self.guard.children.get(session_id)
        if identity is None or identity.stopped:
            return json.dumps({'error': copy('stop.action')})
        try:
            text = read_scoped(identity.cwd, args['path'])
        except (ValueError, OSError, KeyError, TypeError):
            return json.dumps({'error': copy('stop.action')})
        return json.dumps({'text': text})

    def search(self, args, *, session_id='', **_):
        identity = self.guard.children.get(session_id)
        if identity is None or identity.stopped:
            return json.dumps({'error': copy('stop.action')})
        try:
            base = scoped_path(identity.cwd, args.get('path', '.'))
            query = args['query']
            if not isinstance(query, str) or not 1 <= len(query) <= 200:
                raise ValueError('invalid query')
            hits = []
            for directory, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not secret_path(d) and not (Path(directory) / d).is_symlink())
                for filename in sorted(files):
                    path = Path(directory) / filename
                    if secret_path(filename) or path.is_symlink():
                        continue
                    try:
                        text = read_scoped(identity.cwd, path)
                    except (OSError, ValueError):
                        continue
                    for line in text.splitlines():
                        if query in line:
                            hits.append({'path': Scrubber().text(str(path.relative_to(identity.cwd))), 'line': line[:500]})
                            if len(hits) >= 100:
                                return json.dumps({'matches': hits})
            return json.dumps({'matches': hits})
        except (ValueError, OSError, KeyError, TypeError):
            return json.dumps({'error': copy('stop.action')})

    def web(self, args, *, session_id='', **_):
        identity = self.guard.children.get(session_id)
        if identity is None or identity.stopped:
            return json.dumps({'error': copy('stop.action')})
        turn_token = identity.turn_id
        if not self.web_lookups or self.web_fetch is None:
            self.web_stopped.add(turn_token)
            identity.stopped = True
            self.save()
            return json.dumps({'error': copy('stop.action')})
        clean = Scrubber().scrub({k: args[k] for k in ('query', 'url') if k in args})
        if 'url' in clean:
            url = urlsplit(clean['url'])
            if url.scheme not in {'http', 'https'} or url.username or url.password:
                return json.dumps({'error': copy('stop.action')})
        key = json.dumps([turn_token, clean], sort_keys=True, ensure_ascii=False)
        with self.guard.lock:
            if key not in self.cache:
                self.cache[key] = Scrubber().scrub(self.web_fetch(clean))
            self.web_turns.add(turn_token)
            result = json.dumps({'text': self.cache[key]})
        self.save()
        return result

    def clarify(self, args, *, session_id='', **_):
        return json.dumps({'text': copy('stop.clarify')})

    def todo(self, args, *, session_id='', **_):
        identity = self.guard.children.get(session_id)
        if identity is None or identity.stopped:
            return json.dumps({'error': copy('stop.action')})
        items = self.todos.setdefault(session_id, [])
        incoming = args.get('todos') if isinstance(args, dict) else None
        if incoming is not None:
            if not isinstance(incoming, list):
                return json.dumps({'error': copy('stop.action')})
            cleaned = []
            for item in incoming:
                if not isinstance(item, dict):
                    continue
                cleaned.append({key: Scrubber().text(str(item.get(key, '')))[:500]
                                for key in ('id', 'content', 'status', 'parent') if item.get(key) not in (None, '')})
            if args.get('merge'):
                by_id = {item.get('id'): item for item in items}
                for item in cleaned:
                    current = by_id.get(item.get('id'))
                    if current is None:
                        items.append(item)
                    else:
                        current.update(item)
            else:
                items[:] = cleaned
        return json.dumps({'todos': items})
