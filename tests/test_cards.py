import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext, selection
from shadow.common.storage import Store
from shadow.trial.cards import receipt_columns, render_verdict, result_actions
from shadow.trial.engine import TrialEngine
from shadow.trial.plugin import TrialPlugin, register
from shadow.trial.safety import ReplayGuard
from shadow.trial.verdict import finalize


VERDICT_COUNTS = {
    'better': (15, 11, 4),
    'holds_up': (6, 19, 5),
    'worse': (3, 9, 18),
    'too_close': (4, 20, 6),
    'not_enough': (7, 8, 4),
}


def card_record(tmp_path, counts=(15, 11, 4)):
    engine = TrialEngine(FakeContext(), Store(tmp_path / 'cards.sqlite'), ReplayGuard())
    record = engine.prepare(selection(tmp_path, sum(counts)), CURRENT, TRIAL, [OUTSIDE])
    record.pop('judge_policy')
    for index, turn in enumerate(record['turns']):
        turn.update(state='done', usable=True,
                    judgment={'result': 'won' if index < counts[0] else 'same' if index < sum(counts[:2]) else 'lost',
                              'agree': True, 'disagreement': False, 'reason_kind': 'missed_request',
                              'reason': 'One answer missed a requested step.'})
        for side, cost, duration, steps in [('new', .014, 8.6, 3), ('yours', .036, 13.9, 4)]:
            turn['runs'][side] = [{'cost': cost, 'duration_s': duration, 'broken_calls': 0,
                                  'tool_calls': [{} for _ in range(steps)],
                                  'usage': {'total_tokens': 100, 'estimated': False}}]
    record.update(state='finished', spend=1.84,
                  routes={side: {'provider': model.provider, 'model': model.model,
                                 'base_url': 'https://api.openai.com/v1', 'api_mode': 'codex_responses'}
                          for side, model in [('current', CURRENT), ('trial', TRIAL)]})
    finalize(record)
    return record


def test_terminal_start_card_keeps_the_judge_version_dot(tmp_path):
    from shadow.trial.cards import render_start

    record = card_record(tmp_path)
    text = render_start(record, cli=True, isolation_verified=True)
    assert 'Judged blind by Claude Sonnet 4.5 (auto).' in text
    assert 'Claude Sonnet 4 5' not in text


@pytest.mark.parametrize('counts,word', [((15, 11, 4), 'Better'), ((6, 19, 5), 'Holds up'),
                                        ((3, 9, 18), 'Worse'), ((4, 20, 6), 'Too close to call'),
                                        ((7, 8, 4), 'Not enough to go on')])
def test_cards_render_saved_counts_and_five_receipt_columns(tmp_path, counts, word):
    record = card_record(tmp_path, counts)
    text = render_verdict(record, cli=True, stats=True)
    assert text.splitlines()[0].startswith(f'Won {counts[0]}, lost {counts[2]}')
    assert '[' + {'Better': 'Safe to switch', 'Holds up': 'Safe to switch', 'Worse': 'Stay on your current model'}.get(word, 'Too close to call') + ']' in text
    assert f'Won {counts[0]} · Same {counts[1]} · Lost {counts[2]} of {sum(counts)}' in text
    assert len(receipt_columns(record)) == 5
    assert '8.6s vs 13.9s' in text and '3.0 vs 4.0 steps' in text
    assert 'Net win rate' in text and 'Sign test' in text
    assert not any('[' + c['label'] + ']' in text for c in result_actions(record))
    for line in text.splitlines():
        if ' | ' in line:
            assert len(line) <= 80


def test_cancelled_result_uses_only_finished_pairs_in_saved_order(tmp_path):
    ctx = FakeContext()
    store = Store(tmp_path / 'trial.sqlite')
    plugin = TrialPlugin(ctx, store=store, current=CURRENT, catalog=[CURRENT, TRIAL, OUTSIDE])
    register(ctx, plugin=plugin)
    record = card_record(tmp_path)
    record.update(state='cancelled', stop_reason='cancelled')
    record.pop('totals')
    for turn in record['turns'][22:]:
        turn.update(usable=False, state='waiting', runs={'new': [], 'yours': []})
        turn.pop('judgment')
    store.save(record)
    ctx.answers = ['result', 'keep']
    assert 'Nothing changed.' in plugin.command(f'results {record["id"]}')
    saved = store.get(record['id'])
    assert saved['totals']['pairs'] == 22
    assert [t['id'] for t in saved['turns'] if t['usable']] == [t['id'] for t in record['turns'][:22]]
    assert not ctx.capture


def test_terminal_menu_enter_keeps_current_model(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = (
        f'import sys; sys.path[:0] = {[str(root), str(root / "tests")]!r}\n'
        'from pathlib import Path\n'
        'from test_cards import card_record\n'
        'from fixtures import CURRENT, OUTSIDE, TRIAL, FakeContext\n'
        'from shadow.common.storage import Store\n'
        'from shadow.trial.plugin import TrialPlugin, register\n'
        f'folder = Path({str(tmp_path)!r})\n'
        'ctx = FakeContext()\n'
        'plugin = TrialPlugin(ctx, store=Store(folder / "terminal.sqlite"), current=CURRENT, catalog=[CURRENT, TRIAL, OUTSIDE])\n'
        'register(ctx, plugin=plugin)\n'
        'record = card_record(folder)\n'
        'plugin.store.save(record)\n'
        'print(plugin.command("results " + record["id"], terminal=True))\n'
        'assert not ctx.capture\n'
    )
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, '-c', script], stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    transcript, sent = b'', False
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], .1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                transcript += chunk
                if b'Choose [3]:' in transcript and not sent:
                    os.write(master, b'\n')
                    sent = True
            elif process.poll() is not None:
                break
        assert process.wait(timeout=1) == 0, transcript.decode(errors='replace')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
        os.close(master)
    text = transcript.decode()
    assert sent and '3  Keep GPT-6.1 Sol' in text
    assert 'Kept GPT-6.1 Sol. Nothing changed.' in text
    assert 'hermes shadow switch' not in text


@pytest.mark.parametrize('counts,notice', [((15,11,4), True), ((6,19,5), True), ((4,20,6), False),
                                           ((3,9,18), False), ((7,8,4), False)])
@pytest.mark.parametrize('issue', ['api_mode', 'base_url', 'conflict', 'missing'])
def test_unsupported_routes_only_explain_blocked_regular_switch(tmp_path, counts, notice, issue):
    record = card_record(tmp_path, counts)
    if issue == 'api_mode':
        record['routes']['trial']['api_mode'] = 'anthropic_messages'
    elif issue == 'base_url':
        record['routes']['trial']['base_url'] = 'https://other.example/v1'
    elif issue == 'conflict':
        record['route_conflicts'] = ['trial']
    else:
        record['routes'].pop('trial')
    for cli in (False, True):
        text = render_verdict(record, cli=cli)
        normalized = ' '.join(text.split())
        assert 'Switch gradually' not in text and 'switch gradually anyway' not in text
        assert '/shadow switch' not in text and 'hermes shadow switch' not in text
        explanation = {'api_mode': 'different request formats or endpoints', 'base_url': 'different request formats or endpoints',
                       'conflict': 'routes changed', 'missing': 'did not verify both model routes'}[issue]
        assert (explanation in normalized) == notice
    assert 'switch' not in {choice['value'] for choice in result_actions(record)}


def test_cross_provider_card_explains_the_all_at_once_watch(tmp_path):
    record = card_record(tmp_path)
    record['trial']['provider'] = 'anthropic'
    record['trial']['model'] = OUTSIDE.model
    record['routes']['trial'] = {'provider': 'anthropic', 'model': OUTSIDE.model,
                                 'base_url': 'https://api.anthropic.com', 'api_mode': 'anthropic_messages'}
    text = render_verdict(record)
    assert '[Switch all at once]' in text
    assert "so the switch can't be gradual" in text
    assert 'watches the next 20 turns' in text
    assert 'That catches breakage, not a small drop.' in text


@pytest.mark.parametrize('new_plan,yours_plan', [(False, True), (True, False), (True, True), (False, False)])
def test_receipt_labels_each_contestants_billing_mode(tmp_path, new_plan, yours_plan):
    from shadow import desktop_rpc as desktop
    from shadow.common.copy import load_copy

    record = card_record(tmp_path)
    record['trial']['plan'] = new_plan
    record['current']['plan'] = yours_plan
    finalize(record)

    expected = ('plan, about ' if new_plan else '') + '$0.014 vs ' + ('plan, about ' if yours_plan else '') + '$0.036'
    chat_cost = next(cell for cell in receipt_columns(record) if cell['key'] == 'cost')
    desktop_cells = desktop._receipt(desktop.Copy(load_copy()), record['totals']['receipt'], record['totals'])
    desktop_cost = next(cell for cell in desktop_cells if cell['key'] == 'cost')
    desktop_text = ''.join(part if isinstance(part, str) else next(iter(part.values())) for part in desktop_cost['value'])

    assert chat_cost['value'] == expected
    assert desktop_text == expected
