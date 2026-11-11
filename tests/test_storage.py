import pytest

from shadow.common.storage import Store


def test_storage_keeps_canonical_working_folder_and_scrubs_content(tmp_path):
    folder = tmp_path / ('a_working_folder_' * 4)
    folder.mkdir()
    fake = 'sk-' + 'fixture_secret_0123456789012345'
    store = Store(tmp_path / 'trial.sqlite')
    record = {'id': 'fixture', 'created_at': 1, 'turns': [{'cwd': str(folder), 'goal': fake}]}
    store.save(record)
    saved = store.get('fixture')
    assert saved['turns'][0]['cwd'] == str(folder)
    assert fake not in saved['turns'][0]['goal']


def test_cancellation_survives_stale_controller_writes(tmp_path):
    first = Store(tmp_path / 'trial.sqlite')
    second = Store(tmp_path / 'trial.sqlite')
    original = {'id': 'fixture', 'created_at': 1, 'state': 'running'}
    first.save(original)
    second.save({**original, 'state': 'cancelled'})
    first.save({**original, 'spend': 1})
    assert first.get('fixture')['state'] == 'cancelled'
    assert second.list()[0]['state'] == 'cancelled'


def test_controller_claim_is_exclusive_and_pid_update_keeps_results(tmp_path):
    first = Store(tmp_path / 'trial.sqlite')
    second = Store(tmp_path / 'trial.sqlite')
    record = {'id': 'fixture', 'created_at': 1, 'state': 'queued'}
    first.save(record)
    claim = first.claim_controller('fixture', None)
    assert claim and second.claim_controller('fixture', None) is None
    record.update(state='finished', totals={'pairs': 20})
    second.save({**first.get('fixture'), **record})
    first.controller_started('fixture', claim, 123)
    assert first.get('fixture')['state'] == 'finished'
    assert first.get('fixture')['totals']['pairs'] == 20


def test_cancelled_controller_cannot_be_reclaimed(tmp_path):
    store = Store(tmp_path / 'trial.sqlite')
    store.save({'id': 'fixture', 'created_at': 1, 'state': 'cancelled'})
    assert store.claim_controller('fixture', None) is None


def test_abandoned_startup_claim_can_be_replaced_without_reviving_old_owner(tmp_path):
    store = Store(tmp_path / 'trial.sqlite')
    store.save({'id': 'fixture', 'created_at': 1, 'state': 'queued', 'controller_token': 'prepared-token'})
    old = store.claim_controller('fixture', None)
    assert store.get('fixture')['controller_owner_pid'] > 0
    replacement = store.claim_controller('fixture', None, expected_claim=old)
    assert replacement and replacement != old
    store.controller_started('fixture', old, 111)
    assert store.get('fixture')['controller_pid'] is None
    assert store.get('fixture')['controller_token'] == replacement
    store.controller_started('fixture', replacement, 222)
    assert store.get('fixture')['controller_pid'] == 222


def test_second_look_reservation_survives_late_controller_usage(tmp_path):
    first = Store(tmp_path / 'trial.sqlite')
    second = Store(tmp_path / 'trial.sqlite')
    record = {'id': 'fixture', 'created_at': 1, 'state': 'finished', 'totals': {'pairs': 30}}
    first.save(record)
    assert second.reserve_second_look('fixture', 'followup')
    first.save({**record, 'spend': 1})
    assert first.get('fixture')['second_look_id'] == 'followup'
    assert not second.reserve_second_look('fixture', 'another')
    second.forget('fixture')
    first.save(record)
    assert first.reserve_second_look('fixture', 'fresh')


def test_replay_child_ids_survive_stale_writes_without_changing_progress(tmp_path):
    first = Store(tmp_path / 'trial.sqlite')
    second = Store(tmp_path / 'trial.sqlite')
    record = {'id': 'fixture', 'created_at': 1, 'state': 'running', 'spend': 1}
    first.save(record)
    first.remember_replay_child('fixture', 'child-1')
    second.remember_replay_child('fixture', 'child-2')
    second.remember_replay_child('fixture', 'child-1')
    assert second.get('fixture')['spend'] == 1
    first.save({**record, 'spend': 2})
    saved = second.get('fixture')
    assert saved['spend'] == 2
    assert sorted(saved['replay_child_session_ids']) == ['child-1', 'child-2']


@pytest.mark.parametrize('cleanup', ['one', 'all', 'prune'])
def test_replay_child_ids_follow_record_retention(tmp_path, cleanup):
    store = Store(tmp_path / 'trial.sqlite')
    record = {'id': 'fixture', 'created_at': 1, 'state': 'interrupted'}
    store.save(record)
    store.remember_replay_child('fixture', 'old-child')
    if cleanup == 'prune':
        store.prune(now=31 * 86400)
    else:
        store.forget('fixture' if cleanup == 'one' else 'all')
    assert store.get('fixture') is None
    store.remember_replay_child('fixture', 'late-child')
    store.save(record)
    assert store.get('fixture').get('replay_child_session_ids', []) == []
