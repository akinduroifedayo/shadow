import json
import threading
from dataclasses import dataclass
from pathlib import Path

from ..common.copy import copy
from .reads import scoped_path


# Real read-only names the tool_execution middleware answers in memory. A replay
# child may issue exactly these, plus Shadow's own read tools. Every other real
# name stops the replay.
_ANSWERED_READS = frozenset({
    'read_file', 'search_files', 'skill_view', 'todo', 'todo_list',
    'web_search', 'web_extract', 'clarify',
})
CONTROLLER_TOOLS = frozenset({'shadow_trial_step', 'shadow_setup_check', 'tool_search', 'tool_describe'})
_REPLAY_DESCRIPTIONS = _ANSWERED_READS | {'shadow_read', 'shadow_search', 'shadow_web', 'shadow_clarify'}
REPLAY_TOOLS = _REPLAY_DESCRIPTIONS | {'tool_describe'}
_PATH_ARGUMENT = {
    'shadow_read': 'path',
    'shadow_search': 'path',
    'read_file': 'path',
    'search_files': 'path',
    'skill_view': 'file_path',
}


@dataclass
class ReplayIdentity:
    token: str
    cwd: Path
    turn_id: str = ''
    stopped: bool = False


class ReplayGuard:
    def __init__(self):
        self.stop_message = copy('stop.action')
        self.stop_tool_result = json.dumps({'error': self.stop_message})
        self.controller_stop_message = copy('controller.tool_denied')
        self.controller_stop_tool_result = json.dumps({'error': self.controller_stop_message})
        self.controllers = set()
        self.pending = {}
        self.flights = {}
        self.unidentified = set()
        self.children = {}
        self.vetoed_calls = set()
        self.reads = None
        self.exclusive = False
        self.lock = threading.RLock()

    def add_controller(self, session_id, *, exclusive=False):
        with self.lock:
            if exclusive:
                self.controllers.clear()
                self.pending.clear()
                self.flights.clear()
                self.unidentified.update(self.children)
                self.children.clear()
                self.vetoed_calls.clear()
            self.controllers.add(session_id)
            self.exclusive |= exclusive

    def end_trial(self):
        with self.lock:
            self.exclusive = False
            for identity in self.children.values():
                identity.stopped = True
            for identity in self.pending.values():
                identity.stopped = True

    def expect(self, token, cwd, *, turn_id=''):
        with self.lock:
            self.pending[token] = ReplayIdentity(token, Path(cwd).resolve(strict=True), turn_id or token)

    def subagent_start(self, *, parent_session_id='', child_session_id='', child_goal='', **_):
        with self.lock:
            if parent_session_id in self.children or parent_session_id in self.unidentified:
                self.unidentified.add(child_session_id)
                return
            matches = [v for k, v in self.pending.items() if k in child_goal and (parent_session_id in self.controllers or v.stopped)]
            if len(matches) == 1 and child_session_id:
                self.children[child_session_id] = matches[0]
            elif parent_session_id in self.controllers and child_session_id:
                self.unidentified.add(child_session_id)

    def pre_llm_call(self, *, session_id='', parent_session_id='', messages=None, user_message='', conversation_history=None, **_):
        with self.lock:
            identity = self.children.get(session_id)
            if identity is None and self.pending:
                goal = user_message or next((m.get('content', '') for m in messages or conversation_history or [] if m.get('role') == 'user'), '')
                matches = [v for k, v in self.pending.items() if k in str(goal) and (parent_session_id in self.controllers or v.stopped)]
                if len(matches) == 1:
                    identity = self.children[session_id] = matches[0]
            if identity is None and session_id not in self.controllers and (self.exclusive or session_id in self.unidentified or parent_session_id in self.controllers or parent_session_id in self.children or parent_session_id in self.unidentified):
                if parent_session_id in self.controllers or parent_session_id in self.children or parent_session_id in self.unidentified:
                    self.unidentified.add(session_id)
                return {'action': 'block', 'message': self.stop_message}

    def _mark_veto(self, session_id, tool_call_id, tool_name):
        with self.lock:
            if self.children and session_id in self.children:
                self.vetoed_calls.add((session_id, tool_call_id or tool_name))

    def consume_veto(self, session_id, tool_call_id, tool_name):
        with self.lock:
            key = (session_id, tool_call_id or tool_name)
            if key in self.vetoed_calls:
                self.vetoed_calls.remove(key)
                return True
            return False

    def pre_tool_call(self, *, tool_name, args, session_id='', parent_session_id='', tool_call_id='', **_):
        try:
            # This veto deliberately does not call the middleware policy or handler.
            with self.lock:
                if session_id in self.controllers:
                    return None if tool_name in CONTROLLER_TOOLS else {'action': 'block', 'message': self.controller_stop_message}
                identity = self.children.get(session_id)
                if identity is None:
                    if self.exclusive or parent_session_id in self.controllers or parent_session_id in self.children or parent_session_id in self.unidentified or session_id in self.unidentified:
                        return {'action': 'block', 'message': self.stop_message}
                    return
                if identity.stopped or tool_name not in REPLAY_TOOLS:
                    identity.stopped = True
                    self._mark_veto(session_id, tool_call_id, tool_name)
                    return {'action': 'block', 'message': self.stop_message}
                if tool_name == 'tool_describe':
                    names = args.get('names') if isinstance(args, dict) else None
                    if not isinstance(names, list) or not names or any(not isinstance(name, str) or name not in _REPLAY_DESCRIPTIONS for name in names):
                        identity.stopped = True
                        self._mark_veto(session_id, tool_call_id, tool_name)
                        return {'action': 'block', 'message': self.stop_message}
                path = _replay_path(tool_name, args)
                if path is not None:
                    try:
                        scoped_path(identity.cwd, path)
                    except (ValueError, OSError):
                        self._mark_veto(session_id, tool_call_id, tool_name)
                        return {'action': 'block', 'message': self.stop_message}
        except Exception:
            self._mark_veto(session_id, tool_call_id, tool_name)
            message = self.controller_stop_message if session_id in self.controllers else self.stop_message
            return {'action': 'block', 'message': message}

    def tool_execution(self, *, tool_name, args, next_call, session_id='', parent_session_id='', tool_call_id='', **_):
        try:
            if session_id in self.controllers:
                return next_call(args) if tool_name in CONTROLLER_TOOLS else self.controller_stop_tool_result
            identity = self.children.get(session_id)
            if identity is None:
                if self.exclusive or parent_session_id in self.controllers or parent_session_id in self.children or parent_session_id in self.unidentified or session_id in self.unidentified:
                    return self.stop_tool_result
            elif identity.stopped or tool_name not in REPLAY_TOOLS:
                identity.stopped = True
                self._mark_veto(session_id, tool_call_id, tool_name)
                return self.stop_tool_result
            elif tool_name == 'tool_describe':
                names = args.get('names') if isinstance(args, dict) else None
                if not isinstance(names, list) or not names or any(not isinstance(name, str) or name not in _REPLAY_DESCRIPTIONS for name in names):
                    identity.stopped = True
                    self._mark_veto(session_id, tool_call_id, tool_name)
                    return self.stop_tool_result
            elif tool_name in _ANSWERED_READS:
                if self.reads is None or not isinstance(args, dict):
                    identity.stopped = True
                    self._mark_veto(session_id, tool_call_id, tool_name)
                    return self.stop_tool_result
                return self._answer_readonly(tool_name, args, session_id)
        except Exception:
            self._mark_veto(session_id, tool_call_id, tool_name)
            return self.controller_stop_tool_result if session_id in self.controllers else self.stop_tool_result
        return next_call(args)

    def _answer_readonly(self, tool_name, args, session_id):
        if tool_name == 'read_file':
            return self._window_read(args, session_id)
        if tool_name == 'search_files':
            mapped = {'query': args.get('pattern', args.get('query', '')), 'path': args.get('path', '.')}
            return self.reads.search(mapped, session_id=session_id)
        if tool_name == 'skill_view':
            if args.get('file_path'):
                return self.reads.read({'path': args['file_path']}, session_id=session_id)
            return self.reads.search({'query': str(args.get('name', '')), 'path': '.'}, session_id=session_id)
        if tool_name in {'todo', 'todo_list'}:
            return self.reads.todo(args, session_id=session_id)
        if tool_name == 'web_search':
            return self.reads.web({'query': args.get('query', '')}, session_id=session_id)
        if tool_name == 'web_extract':
            urls = [url for url in args.get('urls', []) if isinstance(url, str)] if isinstance(args.get('urls'), list) else []
            if isinstance(args.get('url'), str):
                urls.append(args['url'])
            pages = []
            for url in urls[:5]:
                pages.append(self.reads.web({'url': url}, session_id=session_id))
                identity = self.children.get(session_id)
                if identity is not None and identity.stopped:
                    return pages[-1]
            return pages[0] if len(pages) == 1 else json.dumps({'pages': pages})
        return self.reads.clarify(args, session_id=session_id)

    def _window_read(self, args, session_id):
        result = self.reads.read({'path': args.get('path', '')}, session_id=session_id)
        try:
            payload = json.loads(result)
        except ValueError:
            return result
        text = payload.get('text')
        if not isinstance(text, str):
            return result
        offset = args.get('offset', 1)
        limit = args.get('limit')
        if not isinstance(offset, int) or offset < 1:
            offset = 1
        if offset == 1 and not isinstance(limit, int):
            return result
        lines = text.splitlines()
        window = lines[offset - 1:]
        if isinstance(limit, int) and limit >= 0:
            window = window[:limit]
        payload['text'] = '\n'.join(window)
        return json.dumps(payload)


def _replay_path(tool_name, args):
    key = _PATH_ARGUMENT.get(tool_name)
    if key is None:
        return None
    if not isinstance(args, dict):
        raise ValueError('tool arguments')
    if key == 'file_path' and not args.get(key):
        return None
    return args.get(key, '.')
