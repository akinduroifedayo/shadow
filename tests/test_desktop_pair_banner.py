import pytest

from shadow.common.copy import copy
from test_cards import VERDICT_COUNTS, card_record
from test_switch_override import live_switch


@pytest.mark.parametrize('word', ['worse', 'too_close', 'not_enough'])
def test_desktop_keep_verdict_does_not_prompt_for_pair_verification(tmp_path, live_switch, word):
    trial, switch, rpc, _ = live_switch
    saved = card_record(tmp_path, VERDICT_COUNTS[word])
    assert saved['totals']['word'] == word
    trial.store.save(saved)
    switch.engine.verified_pair = lambda *_: False

    verdict = rpc.page_state({'test_id': saved['id']})['verdict']
    assert verdict['note'] is None
    assert any(button['id'] == 'keep' for button in verdict['buttons'])
    assert verdict['quiet'] is None
    assert not any(button['id'] in {'switch', 'switch_all'} for button in verdict['buttons'])


def test_desktop_pair_waiting_state_refreshes_after_check_passes(tmp_path, live_switch):
    trial, switch, rpc, _ = live_switch
    saved = card_record(tmp_path, VERDICT_COUNTS['better'])
    assert saved['totals']['word'] == 'better'
    trial.store.save(saved)
    switch.engine.verified_pair = lambda *_: False
    assert rpc.page_state({'test_id': saved['id']})['verdict']['note'] == copy('trial.switch_compatibility')

    switch.engine.verified_pair = lambda *_: True
    assert rpc.page_state({'test_id': saved['id']})['verdict']['note'] != copy('trial.switch_compatibility')


def test_desktop_cross_provider_pair_does_not_wait_for_gradual_verification(tmp_path, live_switch):
    trial, switch, rpc, _ = live_switch
    saved = card_record(tmp_path, VERDICT_COUNTS['better'])
    assert saved['totals']['word'] == 'better'
    saved['routes']['trial']['provider'] = 'anthropic'
    saved['trial']['provider'] = 'anthropic'
    trial.store.save(saved)
    switch.engine.verified_pair = lambda *_: False

    assert rpc.page_state({'test_id': saved['id']})['verdict']['note'] != copy('trial.switch_compatibility')
