import json

import pytest

from test_engine import finish, setup_engine


@pytest.mark.parametrize('intent', ['switch', 'compare'])
def test_engine_judge_failure_saves_scrubbed_provider_message_and_renders_it(tmp_path, intent):
    from shadow.common.copy import load_copy
    from shadow.desktop_rpc import DesktopRpc
    from shadow.desktop_source import ProfileSource
    from shadow.trial.cards import render_verdict
    from test_plugin import service

    _, plugin = service(tmp_path)
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    record['intent'] = intent

    def rejected(**_):
        raise RuntimeError('HTTP 400 invalid request: unsupported parameter; credential sk-ant-abcdefghijk123456789')

    ctx.llm.complete_structured = rejected
    finish(engine, record)
    saved = store.get(record['id'])
    assert saved['intent'] == intent
    failed = [turn for turn in saved['turns'] if turn.get('unusable_reason') == 'judge_unusable']
    assert len(failed) == 2
    for turn in failed:
        assert 'HTTP 400 invalid request: unsupported parameter' in turn['judge_error']
        assert '<key>' in turn['judge_error']
    assert all('unsupported parameter' in call['error_message'] for call in saved['usage_calls'] if call['kind'] == 'judge')
    assert 'sk-ant-abcdefghijk123456789' not in json.dumps(saved)
    assert 'HTTP 400 invalid request: unsupported parameter' in render_verdict(saved, cli=True)
    plugin.store.save(saved)
    page = DesktopRpc(ProfileSource(plugin), load_copy()).page_state({'test_id': saved['id']})
    assert 'HTTP 400 invalid request: unsupported parameter' in page['verdict']['reason']


def test_provider_error_is_bounded_before_scrubbing_and_rendering(tmp_path):
    from shadow.trial.cards import render_verdict

    ctx, store, engine, record = setup_engine(tmp_path, 20)
    first = 'HTTP 400 credential sk-ant-abcdefghijk123456789 ' + 'x' * 400

    def rejected(**_):
        raise RuntimeError(first + '\nJSON body should not appear')

    ctx.llm.complete_structured = rejected
    finish(engine, record)
    saved = store.get(record['id'])
    from shadow.common.scrub import Scrubber

    expected = Scrubber().text(first)[:300]
    errors = [call['error_message'] for call in saved['usage_calls'] if call['kind'] == 'judge']
    assert errors == [expected, expected]
    assert all(turn['judge_error'] == expected for turn in saved['turns'] if turn.get('judge_error'))
    rendered = render_verdict(saved, cli=True)
    assert expected in rendered.replace('\n', '')
    assert 'JSON body should not appear' not in json.dumps(saved)
    assert 'sk-ant-abcdefghijk123456789' not in rendered


def test_provider_error_key_straddling_the_cut_is_scrubbed(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)
    secret = 'sk-ant-' + 'a1b2c3d4e5' * 5
    first = 'HTTP 400 ' + 'x' * 280 + ' ' + secret + ' tail'

    def rejected(**_):
        raise RuntimeError(first)

    ctx.llm.complete_structured = rejected
    finish(engine, record)
    saved = json.dumps(store.get(record['id']))
    assert secret[:10] not in saved


def test_provider_error_with_blank_first_line_keeps_the_message(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)

    def rejected(**_):
        raise RuntimeError('\n  \nHTTP 400 invalid x-api-key')

    ctx.llm.complete_structured = rejected
    finish(engine, record)
    errors = [call['error_message'] for call in store.get(record['id'])['usage_calls'] if call['kind'] == 'judge']
    assert errors and all(error == 'HTTP 400 invalid x-api-key' for error in errors)


def test_provider_error_without_text_names_the_exception(tmp_path):
    ctx, store, engine, record = setup_engine(tmp_path, 20)

    def rejected(**_):
        raise TimeoutError()

    ctx.llm.complete_structured = rejected
    finish(engine, record)
    errors = [call['error_message'] for call in store.get(record['id'])['usage_calls'] if call['kind'] == 'judge']
    assert errors and all(error == 'TimeoutError' for error in errors)
