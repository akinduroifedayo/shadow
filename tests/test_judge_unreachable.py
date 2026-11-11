"""Live 06d58869: the judge's credential was refused, and Shadow said "price not known" and stopped."""
from shadow.common.copy import copy
from shadow.trial.cards import render_verdict
from test_engine import finish, setup_engine


def unreachable(**_):
    raise PermissionError('judge credential refused')


def test_unreachable_judge_marks_turns_unusable_and_says_the_judge_could_not_be_reached(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    ctx.llm.complete_structured = unreachable

    finish(engine, record)

    saved = store.get(record['id'])
    judged = [t for t in saved['turns'] if t.get('unusable_reason') == 'judge_unusable']
    assert saved.get('stop_reason') != 'unpriceable'
    assert saved['state'] == 'finished'
    assert len(judged) == 2, 'stops after two judge requests fail with no answer, instead of replaying all 20'
    assert all(c['error'] == 'request_failed' for c in saved['usage_calls'] if c['kind'] == 'judge')
    assert saved['totals']['word'] == 'not_enough'
    assert saved['totals']['cant_call_reason'] == 'judge_unreachable'
    card = render_verdict(saved, cli=True)
    assert "couldn't be reached" in card
    assert copy('verdict.reason.unpriceable') not in card
    assert 'maths' not in card.lower()


def test_one_failed_judge_call_does_not_rename_the_whole_test(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    complete, calls = ctx.llm.complete_structured, []

    def first_fails(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError('request timed out')
        return complete(**kwargs)

    ctx.llm.complete_structured = first_fails
    finish(engine, record)

    saved = store.get(record['id'])
    assert saved.get('stop_reason') != 'unpriceable'
    assert sum(t.get('unusable_reason') == 'judge_unusable' for t in saved['turns']) == 1
    assert saved['totals']['cant_call_reason'] == 'few_usable'


def test_desktop_verdict_says_the_judge_could_not_be_reached():
    from shadow.common.copy import load_copy
    from shadow.desktop_rpc import DesktopRpc, FixtureSource
    source = FixtureSource('result')
    record = source.tests()[0]
    record['totals'].update(word='not_enough', cant_call_reason='judge_unreachable', pairs=0, second_look=False)
    record['totals']['receipt']['counts_readable'] = 'so_far'
    verdict = DesktopRpc(source, load_copy()).page_state({'test_id': record['id']})['verdict']
    text = str(verdict)
    assert "couldn't be reached" in text
    assert copy('verdict.reason.unpriceable') not in text


def test_a_cap_stop_keeps_its_own_reason_after_a_failed_judge_call():
    from shadow.trial.verdict import statistics_for
    stats = statistics_for(0, 0, 0, stop_reason='cap', judge_unreachable=True)
    assert stats['cant_call_reason'] == 'cap'
    assert statistics_for(0, 0, 0, judge_unreachable=True)['cant_call_reason'] == 'judge_unreachable'
