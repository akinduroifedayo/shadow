import json

import pytest

from shadow.common.scrub import Scrubber, outbound
from shadow.trial.reads import SafeReads
from shadow.trial.safety import ReplayGuard


def setup_reads(tmp_path):
    guard = ReplayGuard()
    guard.add_controller('parent')
    guard.expect('token', tmp_path)
    guard.subagent_start(parent_session_id='parent', child_session_id='child', child_goal='token')
    return SafeReads(guard)


def test_scoped_reads_scrub_at_handler_boundary(tmp_path):
    fake = 'sk-' + 'pretend_secret_value_0123456789'
    (tmp_path / 'notes.txt').write_text(fake + ' somebody@example.test +1 (312) 555-0123')
    reads = setup_reads(tmp_path)
    response = reads.read({'path': 'notes.txt'}, session_id='child')
    assert fake not in response
    assert '<key>' in response and '<email>' in response and '<phone>' in response
    assert 'error' in json.loads(reads.read({'path': '../elsewhere'}, session_id='child'))
    (tmp_path / '.env').write_text(fake)
    assert 'error' in json.loads(reads.read({'path': '.env'}, session_id='child'))
    (tmp_path / 'alias').symlink_to(tmp_path / '.env')
    assert 'error' in json.loads(reads.read({'path': 'alias'}, session_id='child'))
    assert 'error' in json.loads(reads.read({'path': 'notes.txt'}, session_id='unknown'))


def test_outbound_allows_only_replay_fields_and_scrubs_nested_values():
    fake = 'sk-' + 'fake_history_0123456789012345'
    payload = outbound({'goal': fake, 'context': [{'args': {'key': fake}}], 'system_prompt': fake, 'original_answer': 'private', 'cwd': '/private'})
    assert set(payload) == {'goal', 'context'}
    assert fake not in json.dumps(payload)
    assert '<key>' in json.dumps(payload)


@pytest.mark.parametrize('name', ['auth.json', 'credentials.json', 'id_rsa', 'foo.pem', '.env.local', '.git/config', '.hermes/config.yaml'])
def test_secret_file_names_are_refused(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('fake content')
    assert 'error' in json.loads(setup_reads(tmp_path).read({'path': name}, session_id='child'))


def test_scrub_full_values_and_counts_only():
    scrub = Scrubber()
    value = scrub.text('Bearer abcdefghijklmnop https://user:password@host.test token=abcdefghijklmnop a@b.test 312-555-0123')
    assert 'abcdefghijklmnop' not in value
    assert 'password' not in value
    assert scrub.counts['email'] == 1
    assert scrub.counts['phone'] == 1
    assert all(isinstance(x, int) for x in scrub.counts.values())


def test_web_pages_shared_across_contestants_and_retries(tmp_path):
    guard = ReplayGuard()
    guard.add_controller('controller')
    calls = []
    reads = SafeReads(guard, web_fetch=lambda args: calls.append(args) or 'same page')
    for side in ('new', 'yours', 'retry'):
        guard.expect(side, tmp_path, turn_id='one-turn')
        guard.subagent_start(parent_session_id='controller', child_session_id=side, child_goal=side)
    assert reads.web({'query': 'a lookup'}, session_id='new') == reads.web({'query': 'a lookup'}, session_id='yours')
    reads.web({'query': 'a lookup'}, session_id='retry')
    assert calls == [{'query': 'a lookup'}]
    assert len(reads.web_turns) == 1


def test_scrubbed_web_pages_survive_restart_and_new_tests_fetch_again(tmp_path):
    from shadow.common.storage import Store
    store = Store(tmp_path / 'trial.sqlite')
    record = {'id': 'web-fixture', 'created_at': 1, 'web_lookups': True}
    calls = []
    reads = setup_reads(tmp_path)
    reads.web_fetch = lambda args: calls.append(args) or 'Contact person@example.test on the first page.'
    reads.bind(record, on_change=lambda: store.save(record))
    first = reads.web({'query': 'a café lookup'}, session_id='child')
    saved = store.get(record['id'])
    assert saved and 'person@example.test' not in json.dumps(saved)
    restored = setup_reads(tmp_path)
    restored.web_fetch = lambda args: calls.append(args) or 'a changed page'
    restored.bind(saved)
    assert restored.web({'query': 'a café lookup'}, session_id='child') == first
    assert len(calls) == 1 and len(restored.web_turns) == 1
    restored.bind({'web_lookups': True})
    assert 'a changed page' in restored.web({'query': 'a café lookup'}, session_id='child')
    assert len(calls) == 2


def test_secret_working_folder_is_not_an_allowed_read_scope(tmp_path):
    from shadow.trial.reads import read_scoped
    folder = tmp_path / '.hermes'
    folder.mkdir()
    (folder / 'history.txt').write_text('private fixture')
    with pytest.raises(ValueError):
        read_scoped(folder, 'history.txt')


def test_json_credentials_and_international_phones_are_fully_scrubbed():
    payload = {'api_key': 'fixture_short_key', 'password': 'fixture_password',
               'text': 'Call +44 20 7946 0958 or +442079460958.'}
    scrub = Scrubber()
    clean = json.dumps(scrub.scrub(payload))
    assert 'fixture_short_key' not in clean and 'fixture_password' not in clean
    assert '7946' not in clean and '<phone>' in clean
    encoded = Scrubber().text(json.dumps(payload))
    assert 'fixture_short_key' not in encoded and 'fixture_password' not in encoded


def test_search_scrubs_filename_before_returning_it(tmp_path):
    (tmp_path / 'person@example.test.txt').write_text('a fixture match')
    result = setup_reads(tmp_path).search({'query': 'fixture'}, session_id='child')
    assert 'person@example.test' not in result and '<email>' in result


def test_registered_web_handler_uses_only_fixed_lookup_tools(tmp_path):
    from fixtures import FakeContext
    from shadow.trial.plugin import TrialPlugin
    ctx = FakeContext()
    calls = []
    ctx.dispatch_tool = lambda name, args: calls.append((name, args)) or 'fixture result'
    service = TrialPlugin(ctx)
    service.guard.add_controller('controller')
    for side in ('new', 'yours'):
        service.guard.expect(side, tmp_path, turn_id='turn')
        service.guard.subagent_start(parent_session_id='controller', child_session_id=side, child_goal=side)
    for side in ('new', 'yours'):
        result = service.reads.web({'query': 'fixture lookup'}, session_id=side)
        assert 'fixture result' in result
    assert calls == [('web_search', {'query': 'fixture lookup'})]
    service.reads.web({'url': 'https://example.test/page'}, session_id='new')
    assert calls[-1] == ('web_extract', {'urls': ['https://example.test/page']})


@pytest.mark.parametrize('field', ['apiKey', 'privateKey', 'client_secret', 'refresh_token', 'secret_access_key', 'authToken'])
def test_credential_aliases_are_scrubbed_in_objects_and_embedded_json(field):
    planted = 'fixture_short_credential'
    assert planted not in json.dumps(outbound({'context': {field: planted}}))
    assert planted not in Scrubber().text('Recorded credentials ' + json.dumps({field: planted}))


@pytest.mark.parametrize('name', ['.netrc', '_netrc', '.npmrc', '.pypirc', '.git-credentials', '.pgpass', '.docker/config.json'])
def test_standard_credential_files_are_never_read_or_searched(tmp_path, name):
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('machine fixture.example login user password fixture_private_value')
    reads = setup_reads(tmp_path)
    assert 'error' in json.loads(reads.read({'path': name}, session_id='child'))
    assert 'fixture_private_value' not in reads.search({'query': 'machine'}, session_id='child')
