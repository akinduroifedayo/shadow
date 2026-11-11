import pytest

from shadow.common.scrub import Scrubber


@pytest.mark.parametrize('text', [
    'max_tokens: 4096',
    'if passed:\n    return True',
    'for token in tokens:\n    total += 1',
    'Passage: The fox',
    'tokens=4096', 'compass=north', 'bypass=True', 'tokenizer=plain',
    'PWD=/workspace/project',
])
def test_ordinary_text_is_preserved(text):
    assert Scrubber().text(text) == text


@pytest.mark.parametrize('name', [
    'DBPASSWORD', 'servicePasswordPrimary', 'stripeSecretKeyVersion',
    'MYSQL_PWD', 'DB_PASSWD', 'service_token_primary', 'SECRET_KEY_BASE',
])
def test_text_and_dict_share_credential_names(name):
    value = 'fixture_credential'
    assert value not in Scrubber().text(f'{name} = "{value}"')
    assert value not in str(Scrubber().scrub({name: value}))


@pytest.mark.parametrize('name', ['tokens', 'max_tokens', 'passed', 'Passage', 'compass', 'bypass', 'tokenizer', 'PWD'])
def test_text_and_dict_share_ordinary_names(name):
    assert Scrubber().scrub({name: 'ordinary'}) == {name: 'ordinary'}
    assert Scrubber().text(f'{name}=ordinary') == f'{name}=ordinary'


@pytest.mark.parametrize('text,private', [
    ('INFO: API_KEY=fixture_credential', 'fixture_credential'),
    ('log: password=fixture_credential', 'fixture_credential'),
    ('Authorization: Basic dTpw', 'dTpw'),
    ('Authorization: Token fixture', 'fixture'),
    ('Authorization: Digest username="fixture_user", nonce="fixture_nonce", response="fixture_digest"', 'fixture_digest'),
    ('''curl -H 'Authorization: Digest username="fixture_user", nonce="fixture_nonce", response="fixture_digest"' https://example.test''', 'fixture_digest'),
])
def test_prefixed_fields_and_full_authorization_values_are_scrubbed(text, private):
    assert private not in Scrubber().text(text)


@pytest.mark.parametrize('text', [
    'a basic understanding',
    'token generation',
    'digest authentication',
    'Basic understanding of the tool',
    'Token generation takes time',
    'Digest authentication is a scheme name here',
])
def test_auth_scheme_words_in_prose_are_preserved(text):
    assert Scrubber().text(text) == text


def test_auth_scheme_redacts_a_value_with_a_digit_or_padding():
    assert 'abcd1234' not in Scrubber().text('Basic abcd1234')
    assert 'abcdefg=' not in Scrubber().text('Token abcdefg=')
    assert Scrubber().text('Digest abcdefgh') == 'Digest abcdefgh'


@pytest.mark.parametrize('text', [
    'git push -u origin main',
    'sort -u f',
    'python -u app.py',
    'pip install --user x',
])
def test_ordinary_user_flags_are_preserved(text):
    assert Scrubber().text(text) == text


@pytest.mark.parametrize('text,private', [
    ('curl -u fixture_user:fixture_pass https://example.test', 'fixture_pass'),
    ('curl --user "fixture_user:fixture_pass" https://example.test', 'fixture_pass'),
    ("curl --user 'fixture_user:fixture_pass' https://example.test", 'fixture_pass'),
])
def test_user_flag_redacts_only_a_user_pass_value(text, private):
    assert private not in Scrubber().text(text)


def test_double_equals_and_unquoted_password_values_are_fully_redacted():
    assert 'hunter2' not in Scrubber().text('password == "hunter2"')
    scrubbed = Scrubber().text('DB_PASSWORD=pa;ss next')
    assert 'pa;ss' not in scrubbed and ';ss' not in scrubbed


def test_git_shas_and_long_test_names_are_preserved():
    sha = '0123456789abcdef0123456789abcdef01234567'
    name = 'test_prefixed_credentials_in_history_and_allowed_files_never_leave_scope'
    assert Scrubber().text(f'commit {sha} def {name}():') == f'commit {sha} def {name}():'
    secret = 'Ag1' * 14
    assert secret not in Scrubber().text(f'token blob {secret} end')


def test_repeated_text_keeps_every_occurrence_in_scrub_counts():
    text = 'Contact fixture@example.test. API_KEY=fixture_private'
    scrubber = Scrubber()
    first = scrubber.text(text)
    counts = scrubber.counts.copy()
    assert scrubber.text(text) == first
    assert scrubber.counts == {kind: 2 * count for kind, count in counts.items()}
    another = Scrubber()
    assert another.text(text) == first and another.counts == counts


def test_repeated_json_preserves_nested_redaction_counts():
    scrubber = Scrubber()
    text = '{"password": "fixture_private", "note": "fixture@example.test"}'
    first = scrubber.text(text)
    assert dict(+scrubber.counts) == {'key': 1, 'email': 1}
    assert scrubber.text(text) == first
    assert dict(+scrubber.counts) == {'key': 2, 'email': 2}
