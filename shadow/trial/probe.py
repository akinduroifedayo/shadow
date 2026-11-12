"""One short replay on the profile's current model, proving isolation before any test can start."""

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..common.copy import copy
from .isolation import CHECK_NAMES, CREDENTIAL_NAMES, record_path, write_record
from .reads import SafeReads
from .probe_dispatch import run_dispatch_checks

SETUP_PREFIX = 'shadow-setup-check:'
CHILD_LINE = 'Reply with the single word ready.'
MEMORY_GUIDANCE = (
    'You have persistent memory, carried across sessions',
    'You have a persistent user profile, carried across sessions',
)
MEMORY_BLOCK_HEADERS = ('MEMORY (your personal notes)', 'USER PROFILE (who the user is)')
DETAIL = {
    'memory': 'A replay could see your memory.',
    'live_context': 'A replay could see this chat.',
    'other_sessions': 'A replay could see another chat.',
    'veto': 'A tool that would change something was not refused.',
    'middleware': 'A tool that would change something was not stopped.',
    'read_only': 'A file in the working folder could not be read.',
    'secrets': 'A secret file could be read.',
    'unchanged': 'Your chats or memory changed.',
    'model': 'The replay did not run on the model named above.',
}


def hook_callback_timeout():
    try:
        from hermes_cli.config import load_config_readonly

        plugins = (load_config_readonly() or {}).get('plugins')
        if not isinstance(plugins, dict) or plugins.get('hook_callback_timeout') is None:
            return 30.0
        timeout = float(plugins['hook_callback_timeout'])
        return 30.0 if timeout < 0 else min(timeout, 600.0)
    except Exception:
        return 30.0


def active_home():
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).resolve()
    except (ImportError, OSError, TypeError, ValueError):
        return None


def confirm_line(provider, model):
    return copy('doctor.verify_confirm', provider=provider, model=model)


def success_text(checks, *, date):
    key = 'doctor.verify_pass_no_memory' if checks.get('no_saved_memory') is True else 'doctor.verify_pass'
    return copy(key, date=date)


def failure_text(checks):
    if checks.get('timed_out'):
        lines = [copy('doctor.verify_timeout')]
    elif checks.get('incomplete') or not any(checks.get(name) is not None for name in CHECK_NAMES):
        lines = [copy('doctor.verify_incomplete')]
    else:
        lines = [copy('doctor.verify_fail')]
    for name in CHECK_NAMES:
        if checks.get(name) is False and name in DETAIL:
            lines.append(DETAIL[name])
    if checks.get('model') is False:
        lines.append(DETAIL['model'])
    if checks.get('reason_code') == 'gateway_restart_needed':
        lines.append(copy('doctor.verify_gateway_restart', command=checks.get('restart_command', 'hermes gateway restart')))
    if checks.get('reason_code'):
        lines.append(copy('doctor.verify_failure_reason', reason=checks['reason_code']))
    return '\n'.join(lines)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _denied(path):
    return path.name in CREDENTIAL_NAMES or '.codex' in path.parts or '.claude' in path.parts


def memory_files(home):
    home = Path(home)
    found = []
    for name in ('MEMORY.md', 'USER.md'):
        path = home / name
        if not path.is_symlink() and path.is_file() and not _denied(path):
            found.append(path)
    folder = home / 'memories'
    if not folder.is_symlink() and folder.is_dir():
        for path in sorted(folder.rglob('*')):
            if not path.is_symlink() and path.is_file() and not _denied(path) and path.stat().st_size <= 1_000_000:
                found.append(path)
    return found


def _memory_bytes(home, path):
    parts = path.relative_to(home).parts
    descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(source, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000:
                raise OSError('invalid memory source')
            return handle.read(1_000_001)
    finally:
        os.close(descriptor)


def memory_provider_unverifiable():
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
        if getattr(config, 'read_error', None) is not None:
            return True
        memory = config.get('memory') or {}
        provider = str(memory.get('provider') or '').strip().lower()
        return provider not in {'', 'default', 'builtin', 'built-in', 'none'}
    except Exception:
        return True


def memory_snippet(home):
    """A saved line, an empty string for no saved memory, or None when no safe snippet can be verified."""
    home = Path(home)
    folder = home / 'memories'
    paths = [home / 'MEMORY.md', home / 'USER.md', folder]
    try:
        if not folder.is_symlink() and folder.is_dir():
            pending = [folder]
            while pending:
                for path in pending.pop().iterdir():
                    paths.append(path)
                    if not path.is_symlink() and path.is_dir():
                        pending.append(path)
        if any(path.is_symlink() or (path.is_file() and path.stat().st_size > 1_000_000) for path in paths):
            return None
    except OSError:
        return None
    longest = ''
    for path in memory_files(home):
        try:
            for line in _memory_bytes(Path(home), path).decode(errors='replace').splitlines():
                text = line.strip()
                if len(text) > len(longest):
                    longest = text
        except OSError:
            return None
    return longest[:120] if not longest or len(longest) >= 8 else None


def credential_stats(home):
    stats = {}
    home = Path(home)
    for name in CREDENTIAL_NAMES:
        path = home / name
        if path.exists() and not path.is_symlink():
            stat = path.stat()
            stats[name] = (stat.st_size, stat.st_mtime_ns)
    return stats


def _session_db(home):
    return Path(home) / 'state.db'


def session_fingerprints(home):
    path = _session_db(home)
    if not path.exists():
        return {}
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'sessions' not in tables:
            return {}
        fingerprints = {row[0]: '' for row in connection.execute('SELECT id FROM sessions')}
        if 'messages' in tables:
            for session_id, role, content in connection.execute('SELECT session_id, role, content FROM messages ORDER BY id'):
                fingerprints[session_id] = _sha((fingerprints.get(session_id, '') + '\n' + str(role) + '\n' + str(content)))
        return fingerprints
    finally:
        connection.close()


def delete_sessions(home, session_ids):
    path = _session_db(home)
    if not path.exists() or not session_ids:
        return
    from hermes_state import SessionDB
    owned = set(session_ids)
    db = SessionDB(db_path=path)
    try:
        for session_id in owned:
            if not set(db.get_session_delete_targets(session_id)) <= owned:
                raise ValueError('cleanup would delete an unowned session')
        for session_id in owned:
            db.delete_session(session_id, sessions_dir=Path(home) / 'sessions')
    finally:
        db.close()


def other_session_snippets(home, *, excluded=()):
    path = _session_db(home)
    if not path.exists():
        return []
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        return [str(content).strip()[:120] for sid, content in connection.execute(
            'SELECT session_id, content FROM messages ORDER BY id')
            if sid not in excluded and content and len(str(content).strip()) >= 24]
    finally:
        connection.close()


def profile_snapshot(home):
    files = {}
    for path in memory_files(home):
        files[str(path)] = hashlib.sha256(_memory_bytes(Path(home), path)).hexdigest()
    return {'memory': files, 'sessions': session_fingerprints(home), 'credentials': credential_stats(home)}


def same_profile(before, after, *, ignore_sessions=()):
    if before['memory'] != after['memory'] or before['credentials'] != after['credentials']:
        return False
    ignored = set(ignore_sessions)
    before_ids = set(before['sessions']) - ignored
    after_ids = set(after['sessions']) - ignored
    if before_ids != after_ids:
        return False
    return all(after['sessions'].get(session_id) == fingerprint for session_id, fingerprint in before['sessions'].items() if session_id not in ignored)


def prepare_workspace(directory, tokens):
    root = Path(directory)
    scope = root / 'scope'
    scope.mkdir(parents=True, exist_ok=True)
    (scope / 'notes.txt').write_text(tokens['scope'] + '\n')
    secret = scope / '.env'
    secret.write_text(tokens['secret'] + '\n')
    secret.chmod(0o600)
    (root / 'outside.txt').write_text(tokens['outside'] + '\n')
    return scope


def _blob(messages):
    try:
        return json.dumps(messages, default=str)
    except (TypeError, ValueError):
        return str(messages)


def judge_messages(messages, *, live, child, memory, other, secret, outside):
    text = _blob(messages)
    return {
        'memory': bool(child and child in text and memory is not None and (not memory or memory not in text)
                       and not any(line in text for line in (*MEMORY_GUIDANCE, *MEMORY_BLOCK_HEADERS))),
        'live_context': bool(child and child in text and live not in text),
        'other_sessions': bool(child and child in text and all(token and token not in text for token in other)),
        'secrets': secret not in text and outside not in text,
    }


def blank_checks():
    return {name: None for name in (*CHECK_NAMES, 'model')}


class SetupProbe:
    def __init__(self, plugin, job):
        self.plugin = plugin
        self.job = job
        self.messages = []
        self.calls = []
        self.home_verified = active_home() == Path(job['home']).resolve()

    def watch(self, *, session_id='', request_messages=None, system_prompt='', **_):
        identity = self.plugin.guard.children.get(session_id)
        if identity is not None and identity.token == self.job['child']:
            self.home_verified &= active_home() == Path(self.job['home']).resolve()
            self.messages.append({'session_id': session_id, 'messages': request_messages, 'system_prompt': system_prompt})

    def owned_sessions(self):
        return [self.job['controller_session'], *[
            sid for sid, identity in self.plugin.guard.children.items() if identity.token == self.job['child']]]

    def note_child(self, **_):
        path = owned_path(self.plugin.launcher.directory, self.job['id'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.owned_sessions()))
        path.chmod(0o600)

    def note_call(self, *, session_id='', response_model='', provider='', **_):
        if session_id:
            self.calls.append({'session_id': session_id, 'model': response_model or '', 'provider': provider})

    def run(self):
        hook_timeout = hook_callback_timeout()
        deadline = time.monotonic() + hook_timeout - 3 if hook_timeout > 0 else None
        plugin, job = self.plugin, self.job
        home = Path(job['home'])
        checks = {**blank_checks(), 'passed': False, 'incomplete': True}
        if not self.home_verified:
            return checks
        if memory_provider_unverifiable():
            checks['reason_code'] = 'memory_provider_unverifiable'
            return checks
        before = profile_snapshot(home)
        memory = memory_snippet(home)
        if memory is None:
            checks['reason_code'] = 'memory_source_unverifiable'
            return checks
        checks['no_saved_memory'] = memory == ''
        scope = prepare_workspace(job['workspace'], job)
        handle = None
        try:
            other = other_session_snippets(home, excluded=[job['controller_session']])
            plugin.message_watcher = self.watch
            plugin.setup_active = self
            guard = plugin.guard
            guard.reads = SafeReads(guard, web_fetch=lambda _args: (_ for _ in ()).throw(RuntimeError('web is not part of this check')))
            guard.add_controller(job['controller_session'])
            guard.expect(job['child'], scope)
            self.note_child()
            request = {
                'goal': CHILD_LINE + '\n' + job['child'],
                'context': '',
                'provider': job['provider'],
                'model': job['model'],
                'role': 'leaf',
                'correlation_id': job['child'],
            }
            remaining = min(60, deadline - time.monotonic()) if deadline is not None else 60
            if remaining <= 0:
                checks['timed_out'] = True
                return checks
            handle = plugin.ctx.subagent_lifecycle.launch(request)
            self.note_child()
            remaining = min(60, deadline - time.monotonic()) if deadline is not None else 60
            if remaining <= 0:
                checks['timed_out'] = True
                return checks
            terminal = plugin.ctx.subagent_lifecycle.wait(handle, timeout_seconds=remaining)
            if terminal.timed_out:
                checks['timed_out'] = True
                return checks
            answer = plugin.ctx.subagent_lifecycle.result(handle)
            completed = (terminal.completed and not terminal.timed_out and answer.ready
                         and str(getattr(answer.terminal_state, 'value', answer.terminal_state)).upper() == 'SUCCEEDED'
                         and isinstance(answer.summary, str) and answer.summary.strip().lower().rstrip('.') == 'ready')
            child_ids = [session_id for session_id, identity in guard.children.items() if identity.token == job['child']]
            if len(child_ids) == 1:
                session_id = child_ids[0]
                seen = [item for item in self.messages if item['session_id'] == session_id]
                message_checks = judge_messages(
                    seen, live=job['live'], child=job['child'], memory=memory,
                    other=[job['other'], *other], secret=job['secret'], outside=job['outside'],
                )
                checks.update({name: len(seen) == 1 and message_checks[name] for name in ('memory', 'live_context', 'other_sessions')})
                checks.update(run_dispatch_checks(guard, session_id, scope, job,
                                                  registrations=plugin.guard_registrations))
                checks['secrets'] = message_checks['secrets'] and checks['secrets']
                calls = [call for call in self.calls if call['session_id'] == session_id]
                checks['model'] = bool(completed and len(calls) == 1
                    and calls[0]['provider'] == job['provider']
                    and (calls[0]['model'] == job['model'] or calls[0]['model'].startswith(job['model'] + '-')))
        finally:
            if handle is not None:
                try:
                    plugin.ctx.subagent_lifecycle.cancel(handle, reason='setup check finished')
                except Exception:
                    pass
            plugin.message_watcher = None
            plugin.setup_active = None
            owned = self.owned_sessions()
            try:
                after = profile_snapshot(home)
                checks['unchanged'] = self.home_verified and active_home() == home.resolve() and same_profile(before, after, ignore_sessions=owned)
            except (OSError, sqlite3.Error):
                checks['unchanged'] = False
        checks['incomplete'] = any(checks.get(name) is None for name in (*CHECK_NAMES, 'model'))
        checks['passed'] = all(checks.get(name) is True for name in CHECK_NAMES) and checks.get('model') is True
        return checks


def owned_path(directory, setup_id):
    return Path(directory) / f'setup-owned-{setup_id}.json'


def job_path(directory, setup_id):
    return Path(directory) / f'setup-job-{setup_id}.json'


def result_path(directory, setup_id):
    return Path(directory) / f'setup-result-{setup_id}.json'


def write_private(path, text):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(text)


def write_job(directory, job):
    path = job_path(directory, job['id'])
    write_private(path, json.dumps({key: job[key] for key in job if key != 'controller_session'}))
    return path


def load_job(directory, setup_id):
    path = job_path(directory, setup_id)
    try:
        job = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    finally:
        path.unlink(missing_ok=True)
    return job if isinstance(job, dict) else None


def write_result(directory, setup_id, checks, *, sessions):
    path = result_path(directory, setup_id)
    body = {name: checks.get(name) for name in (*CHECK_NAMES, 'model', 'passed', 'timed_out', 'incomplete', 'reason_code', 'no_saved_memory')}
    body['sessions'] = sessions
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name + '.', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(body))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return body


def prompt_text(job):
    return (
        f'{SETUP_PREFIX}{job["id"]}:{job["controller"]}\n'
        f'live:{job["live"]}\n'
        'Run the setup check. Do not call other tools.\n'
    )


def parse_prompt(user_message):
    if not isinstance(user_message, str) or not user_message.startswith(SETUP_PREFIX):
        return None
    marker = user_message.splitlines()[0][len(SETUP_PREFIX):]
    setup_id, separator, token = marker.partition(':')
    if not separator or not setup_id or not token:
        return None
    return setup_id, token


def is_setup_request(request):
    return setup_request_text(request) is not None


def setup_request_text(request):
    for key in ('messages', 'input'):
        messages = request.get(key, [])
        if isinstance(messages, str):
            messages = [{'role': 'user', 'content': messages}]
        if not isinstance(messages, list):
            continue
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get('role') != 'user':
                continue
            content = message.get('content', [])
            if isinstance(content, str):
                content = [{'text': content}]
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = part.get('text')
                        if isinstance(text, str) and text.startswith(SETUP_PREFIX):
                            return text
            return None
    return None


def new_job(*, home, workspace, provider, model, profile):
    import secrets
    token = secrets.token_hex(8)
    return {
        'id': token,
        'controller': secrets.token_hex(8),
        'live': 'live-' + secrets.token_hex(8),
        'child': 'child-' + secrets.token_hex(8),
        'scope': 'scope-' + secrets.token_hex(8),
        'secret': 'secret-' + secrets.token_hex(8),
        'outside': 'outside-' + secrets.token_hex(8),
        'other': 'other-' + secrets.token_hex(8),
        'home': str(home),
        'workspace': str(workspace),
        'provider': provider,
        'model': model,
        'profile': profile,
    }


def claim_setup_job(plugin, session_id, user_message, *, data_dir):
    parsed = parse_prompt(user_message)
    if parsed is None:
        return None
    setup_id, token = parsed
    job = load_job(data_dir, setup_id)
    if job is None or job.get('controller') != token:
        return None
    if plugin.setup_running or setup_id in plugin.setup_used or active_home() != Path(job['home']).resolve():
        write_result(data_dir, setup_id, {**blank_checks(), 'passed': False, 'incomplete': True}, sessions=[])
        return None
    plugin.setup_requests.setdefault(session_id, False)
    return job


def finish_setup(plugin, session_id, user_message, *, data_dir):
    with plugin.setup_lock:
        job = claim_setup_job(plugin, session_id, user_message, data_dir=data_dir)
        if job is None:
            return {'action': 'block', 'message': copy('doctor.verify_incomplete')}
        setup_id = job['id']
        plugin.setup_running = True
        plugin.setup_used.add(setup_id)
    try:
        job['controller_session'] = session_id
        plugin.guard.add_controller(session_id)
        probe = SetupProbe(plugin, job)
        probe.note_child()
        try:
            checks = probe.run()
        except Exception:
            checks = {'passed': False, 'incomplete': True}
        if plugin.setup_requests.get(session_id):
            checks.update(model=False, passed=False)
        write_result(data_dir, setup_id, checks, sessions=probe.owned_sessions())
        if checks.get('passed'):
            checked = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            return {'action': 'block', 'message': success_text(checks, date=checked)}
        return {'action': 'block', 'message': failure_text(checks)}
    finally:
        with plugin.setup_lock:
            plugin.setup_running = False


def wait_for_result(launcher, job, *, timeout, pause):
    path = result_path(launcher.directory, job['id'])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                pass
        pause(0.2)
    return None


def launch_setup(launcher, job, *, timeout=120, pause=time.sleep, killer=None):
    """Same hermes chat launch a test uses. The controller process runs one replay child."""
    directory = launcher.directory
    directory.mkdir(parents=True, exist_ok=True)
    record_path(directory).unlink(missing_ok=True)
    identity = launcher.identity
    prompt = directory / f'setup-{job["id"]}.txt'
    cleanup = []
    try:
        write_job(directory, job)
        cleanup.append(job_path(directory, job['id']))
        write_private(prompt, prompt_text(job))
        cleanup.append(prompt)
        before = profile_snapshot(job['home'])
        cleanup.extend((result_path(directory, job['id']), owned_path(directory, job['id'])))
        process = launcher.popen(
            launcher.argv(prompt, 2), cwd=directory, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            env={**os.environ, 'HERMES_HOME': str(Path(job['home']).resolve())},
        )
        try:
            result = wait_for_result(launcher, job, timeout=timeout, pause=pause)
        finally:
            if killer:
                killer(process)
            else:
                _stop(process)
        owned_file = owned_path(directory, job['id'])
        try:
            owned = json.loads(owned_file.read_text()) if owned_file.exists() else []
            if not isinstance(owned, list) or not all(isinstance(sid, str) and sid for sid in owned) or set(owned) & set(before['sessions']):
                raise ValueError('invalid owned sessions')
            delete_sessions(job['home'], owned)
        except (OSError, ValueError, sqlite3.Error, ImportError):
            return {'passed': False, 'unchanged': False}
        after = profile_snapshot(job['home'])
        unchanged = same_profile(before, after)
        if result is None:
            from .probe_gateway import missing_setup_result
            missing = missing_setup_result(job['home'], job['profile'])
            if not unchanged:
                missing['unchanged'] = False
            return missing
        if not unchanged:
            return {'passed': False, 'unchanged': False}
        if not isinstance(result, dict) or result.get('passed') is not True:
            return result
        if (not all(result.get(name) is True for name in (*CHECK_NAMES, 'model'))
                or len(owned) < 2 or set(result.get('sessions', ())) != set(owned)
                or identity.profile != job['profile'] or launcher.identity != identity):
            return {'passed': False}
        write_record(directory, identity, {name: True for name in CHECK_NAMES}, no_saved_memory=result.get('no_saved_memory') is True)
        return result
    finally:
        for path in cleanup:
            path.unlink(missing_ok=True)


def _stop(process):
    poll = getattr(process, 'poll', None)
    pid = getattr(process, 'pid', None)
    if poll is None or not isinstance(pid, int) or pid <= 1 or poll() is not None:
        return
    try:
        os.killpg(pid, 15)
    except (OSError, ProcessLookupError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(pid, 9)
        process.wait(timeout=5)
