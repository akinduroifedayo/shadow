import os
import subprocess
from pathlib import Path

from ..common.storage import plugin_data_dir
from .engine import controller_step_limit
from .isolation import current_identity, matches, read_record


class IsolationRequired(RuntimeError):
    pass


class ControllerLauncher:
    def __init__(self, profile='default', data_dir=None, *, isolation_verified=False, identity=None, popen=subprocess.Popen):
        # isolation_verified is ignored. A passing record is the only grant, and config cannot set it.
        del isolation_verified
        self.profile = profile
        self.data_dir = Path(data_dir) if data_dir else None
        self._identity = identity or (lambda: current_identity(self.profile))
        self.popen = popen

    @property
    def directory(self):
        return self.data_dir or plugin_data_dir()

    @property
    def identity(self):
        return self._identity() if callable(self._identity) else self._identity

    def verification(self):
        try:
            directory = self.directory
            record = read_record(directory)
            identity = self.identity
            if matches(record, identity, self.profile, directory=directory):
                from .probe import active_home, memory_provider_unverifiable, memory_snippet
                if memory_provider_unverifiable():
                    return None
                if record.get('no_saved_memory') is True:
                    home = active_home()
                    if home is None or memory_snippet(home) != '':
                        return None
                return record
        except (ImportError, OSError, TypeError, ValueError):
            return None
        return None

    @property
    def isolation_verified(self):
        return self.verification() is not None

    def argv(self, prompt, limit):
        return ['hermes', '-p', self.profile, 'chat', '--query-file', str(prompt), '-Q', '--ignore-rules', '--max-turns', str(limit)]

    def is_alive(self, pid):
        if type(pid) is not int or pid <= 0:
            return False
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited:
                return False
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True

    def launch(self, record):
        if not self.isolation_verified:
            raise IsolationRequired('controller context and memory quarantine require runtime verification')
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        prompt = directory / f'controller-{record["id"]}.txt'
        limit = controller_step_limit(len(record['turns']))
        prompt.write_text(f'shadow-controller:{record["id"]}:{record["controller_token"]}\n'
                          'Use tool_search and tool_describe to discover shadow_trial_step if needed. '
                          'Call shadow_trial_step directly or through tool_call, repeatedly until continue is false. '
                          'Do not invoke other tools or actions. '
                          f'Use at most {limit} calls. Return only the final state.\n')
        prompt.chmod(0o600)
        process = self.popen(self.argv(prompt, limit), cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return process.pid
