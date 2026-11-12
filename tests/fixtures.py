import json
from copy import deepcopy
from types import SimpleNamespace

from shadow.common.models import Model
from shadow.trial.picking import Selection, Turn


CURRENT = Model('openai-codex', 'gpt-6.1-sol', plan=True)
TRIAL = Model('openai-codex', 'gpt-6-luna', plan=True)
OUTSIDE = Model('anthropic', 'claude-sonnet-4-5')


def selection(cwd, count=30, goal='Please inspect this task carefully.'):
    return Selection([Turn(f't{i}', f'c{i // 3}', 'cli', str(cwd), f'{goal} Task {i}.', '[]') for i in range(count)], count, count, {}, {}, 42)


class FakeLLM:
    def __init__(self, capture):
        self.capture = capture
        self.calls = []
        self.spend_tokens = 100
        self.invalid = False

    def complete_structured(self, **kwargs):
        self.capture.append(deepcopy(kwargs))
        self.calls.append(deepcopy(kwargs))
        payload = json.loads(kwargs['input'][0]['text'])
        answers = payload['answers']
        pick = '1' if 'new answer' in json.dumps(answers[0]) else '2'
        parsed = {'pick': pick, 'fixes_1': 'minor', 'fixes_2': 'none', 'reason_kind': 'missed_request', 'reason': 'The chosen answer covered the requested steps.'}
        return SimpleNamespace(provider=kwargs['provider'], model=kwargs['model'], parsed={} if self.invalid else parsed, text='fixture judgment', usage={'input_tokens': self.spend_tokens, 'output_tokens': 50})


class FakeLifecycle:
    def __init__(self, ctx):
        self.ctx = ctx
        self.results = {}
        self.launches = []
        self.waits = []
        self.cancelled = []
        self.tokens = 100
        self.omit_usage = False
        self.fail_sides = set()
        self.actual_model = None
        self.actual_provider = None
        self.tool = None
        self.read_path = None
        self.start_hook = True
        self.base_url = 'https://api.openai.com/v1'
        self.api_mode = 'codex_responses'

    def launch(self, request):
        self.ctx.capture.append(deepcopy(request))
        self.launches.append(deepcopy(request))
        sid = f'child{len(self.launches)}'
        if self.start_hook:
            self.ctx.hook('subagent_start', parent_session_id='controller', child_session_id=sid, child_goal=request['goal'])
        self.ctx.hook('pre_llm_call', session_id=sid, parent_session_id='controller', messages=[{'role': 'user', 'content': request['goal']}])
        self.ctx.hook('post_api_request', session_id=sid, api_request_id=f'request{sid}', provider=self.actual_provider or request['provider'], response_model=self.actual_model or request['model'], base_url=self.base_url, api_mode=self.api_mode, usage=None if self.omit_usage else {'prompt_tokens': self.tokens, 'completion_tokens': 100})
        if self.read_path:
            reply = self.ctx.tools['shadow_read']({'path': self.read_path}, session_id=sid)
            self.ctx.capture.append({'messages': [{'role': 'tool', 'content': reply}]})
        if self.tool:
            directives = self.ctx.hook('pre_tool_call', session_id=sid, tool_name=self.tool, args={})
            assert any(d and d.get('action') == 'block' for d in directives)
        side = 'new' if request['model'] == TRIAL.model else 'yours'
        self.results[sid] = SimpleNamespace(ready=True, summary=f'{side} answer', terminal_state='FAILED' if side in self.fail_sides else 'SUCCEEDED', started_at=10, completed_at=12, error_classification=None)
        self.ctx.hook('subagent_stop', child_session_id=sid, tool_call_history=[{'tool': self.tool, 'args': {}, 'status': 'blocked'}] if self.tool else [])
        return sid

    def wait(self, handle, *, timeout_seconds):
        self.waits.append(timeout_seconds)

    def result(self, handle):
        return self.results[handle]

    def cancel(self, handle, *, reason):
        self.cancelled.append(handle)
        return SimpleNamespace(accepted=True)


class FakeContext:
    def __init__(self):
        self.hooks = {}
        self.middleware = {}
        self.tools = {}
        self.commands = {}
        self.cli = {}
        self.messages = []
        self.capture = []
        self.config = {}
        self.llm = FakeLLM(self.capture)
        self.subagent_lifecycle = FakeLifecycle(self)
        self.answers = []
        self.questions = []
        self.rpc = {}

    def register_rpc(self, name, handler):
        self.rpc[name] = handler

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_hook(self, name, callback, **kwargs):
        self.hooks.setdefault(name, []).append(callback)
        return SimpleNamespace(active=True)

    def hook(self, name, **kwargs):
        return [callback(**kwargs) for callback in self.hooks.get(name, [])]

    def register_middleware(self, name, callback):
        self.middleware[name] = callback
        return SimpleNamespace(active=True)

    def register_tool(self, *, name, handler, **kwargs):
        self.tools[name] = handler

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, **kwargs):
        self.cli[name] = (setup_fn, handler_fn)

    def ask_choice(self, question, choices):
        self.questions.append((question, deepcopy(choices)))
        return self.answers.pop(0) if self.answers else None

    def post_message(self, text):
        self.messages.append(text)
        return SimpleNamespace(edit=lambda value: self.messages.__setitem__(-1, value))


def set_judge_pick(turn, pick):
    for decision in turn['judgment']['decisions']:
        decision['pick'] = pick
        decision['agree'] = True
        for order in decision['orders']:
            order['normalized_pick'] = pick
            order['pick'] = {'1': '2', '2': '1', 'same': 'same'}[pick] if order['reverse'] else pick
    turn['judgment']['result'] = {'1': 'won', '2': 'lost', 'same': 'same'}[pick]
    turn['judgment']['agree'] = True
    turn['judgment']['disagreement'] = False
