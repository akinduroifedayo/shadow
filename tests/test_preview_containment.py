"""Review findings on the preview export: no export text in tracebacks, live redaction policy, linear reply scan."""

import json
import subprocess
import time
import traceback

from shadow.trial.picking import export_sessions, pick_turns

CANARY = 'CONTAINCANARY'


def masking(text):
    return text.replace(CANARY, '***')


def test_failed_export_leaves_no_export_text_in_the_traceback(tmp_path):
    good = json.dumps({'id': 's1', 'cwd': str(tmp_path), 'messages': [{'role': 'user', 'content': f'key {CANARY}_RAW'}]})
    stdout = good + '\n' + good[:-5] + '\n'

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr='')

    try:
        export_sessions('demo', runner=run, redact=masking)
    except Exception as error:
        # Skip this test's own frame, which holds the planted stdout; check only Shadow's frames.
        shadow_frames = error.__traceback__.tb_next
        rendered = ''.join(traceback.format_exception(error.with_traceback(shadow_frames)))
        rendered += ''.join(traceback.StackSummary.extract(traceback.walk_tb(shadow_frames), capture_locals=True).format())
        frames = []
        tb = shadow_frames
        while tb is not None:
            frames.append(repr(tb.tb_frame.f_locals))
            tb = tb.tb_next
    else:
        raise AssertionError('malformed export must fail')
    assert CANARY not in rendered
    assert not any(CANARY in f for f in frames)


def test_redaction_follows_policy_changes_during_one_export(tmp_path):
    """Host redaction reads registries that can change mid-export, so no result may be reused."""
    secrets = set()

    def live(text):
        for s in secrets:
            text = text.replace(s, '***')
        if 'register-me' in text:
            secrets.add('opaque-token-123')
        return text

    line = json.dumps({'id': 's1', 'cwd': str(tmp_path), 'messages': [
        {'role': 'user', 'content': 'opaque-token-123'},
        {'role': 'assistant', 'content': 'register-me'},
        {'role': 'user', 'content': 'opaque-token-123'},
    ]})

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=line + '\n', stderr='')

    messages = export_sessions('demo', runner=run, redact=live)[0]['messages']
    assert messages[0]['content'] == 'opaque-token-123'
    assert messages[2]['content'] == '***'


def test_many_adjacent_user_turns_select_in_bounded_time(tmp_path):
    messages = [{'id': str(i), 'role': 'user', 'content': f'Please explain item {i} in some detail now.'} for i in range(40000)]
    messages.append({'role': 'assistant', 'content': 'ok'})
    start = time.perf_counter()
    result = pick_turns([{'id': 's', 'cwd': str(tmp_path), 'source': 'cli', 'messages': messages}], seed=1)
    elapsed = time.perf_counter() - start
    assert result.total == 40000
    assert elapsed < 3, f'40000 adjacent user turns took {elapsed:.1f}s'
