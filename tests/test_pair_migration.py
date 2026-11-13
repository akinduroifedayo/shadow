from copy import deepcopy
from dataclasses import asdict
import sys
from types import SimpleNamespace

import pytest

from shadow.switch import register
from shadow.switch.reasoning import matching_proof
from test_pair_probe import pair_config_backend
from test_switch_plugin import Context
from test_switch_registration_storage import storage_environment
from test_switch_safety import NEW, OLD


@pytest.fixture
def legacy_pairs(monkeypatch, storage_environment, pair_config_backend):
    state = pair_config_backend
    state.proofs = [{'old': asdict(OLD), 'new': asdict(NEW), 'probe_passed': True,
                     'evidence': 'saved-pair-evidence.json',
                     'reasoning': {'old': None, 'new': None}, 'sealed_reasoning_observed': False}]
    state.document['plugins']['entries']['shadow'] = {
        'settings': {'verified_gradual_pairs': state.proofs},
        'llm': {'allow_provider_override': True},
    }
    state.document['providers'] = {'example': {'api_key': '${MIGRATION_TEST_SECRET}'}}
    config = sys.modules['hermes_cli.config']
    config.read_raw_config_readonly = lambda: deepcopy(state.document)

    def expanded_config():
        raise AssertionError('migration must read raw config')

    config.load_config_readonly = expanded_config
    monkeypatch.setattr('shadow.switch.reasoning.pair_reasoning',
                        lambda *args: ({'old': None, 'new': None}, False))
    state.context = Context()
    state.context.set_config = state.ctx.set_config
    return state


@pytest.mark.parametrize('missing', ['entry', 'settings', 'proofs', 'empty'])
def test_registration_migrates_legacy_proofs_once(legacy_pairs, missing, caplog):
    state = legacy_pairs
    entries = state.document['plugins']['entries']
    if missing == 'entry':
        del entries['shadow-trial']
    elif missing == 'settings':
        del entries['shadow-trial']['settings']
    elif missing == 'proofs':
        del entries['shadow-trial']['settings']['verified_gradual_pairs']

    def save(key, value):
        state.writes.append((key, deepcopy(value)))
        entries.setdefault('shadow-trial', {}).setdefault('settings', {})[key] = deepcopy(value)

    state.context.set_config = save
    before = deepcopy(state.document)
    assert not matching_proof(state.document, OLD, NEW)
    service = register(state.context)
    assert state.writes == [('verified_gradual_pairs', state.proofs)]
    expected = deepcopy(before)
    expected['plugins']['entries'].setdefault('shadow-trial', {}).setdefault('settings', {})[
        'verified_gradual_pairs'] = state.proofs
    assert state.document == expected
    assert service.engine.verified_pair(OLD, NEW)
    assert not service.engine.verified_pair(NEW, OLD)
    service.proposal_lookup = lambda _: SimpleNamespace(old=OLD, new=NEW)
    assert 'Exact-pair runtime compatibility proof is recorded' in service.diagnostics(pair_only=True)
    register(state.context)
    assert state.writes == [('verified_gradual_pairs', state.proofs)]
    assert 'MIGRATION_TEST_SECRET' not in caplog.text


@pytest.mark.parametrize('current', [[{'evidence': 'newer-proof.json'}], None, {}, '', False])
def test_registration_preserves_existing_destination_values(legacy_pairs, current):
    state = legacy_pairs
    state.document['plugins']['entries']['shadow-trial']['settings']['verified_gradual_pairs'] = current
    before = deepcopy(state.document)
    register(state.context)
    assert state.document == before
    assert state.writes == []


def test_registration_preserves_destination_proofs_in_the_hermes_config_fallback(legacy_pairs):
    state = legacy_pairs
    entry = state.document['plugins']['entries']['shadow-trial']
    entry['config'] = entry.pop('settings')
    entry['config']['verified_gradual_pairs'] = [{'evidence': 'kept-proof.json'}]
    before = deepcopy(state.document)
    register(state.context)
    assert state.document == before
    assert state.writes == []


@pytest.mark.parametrize('legacy', [None, {}, 'invalid', False, []])
def test_registration_ignores_legacy_values_without_proof_lists(legacy_pairs, legacy):
    state = legacy_pairs
    state.document['plugins']['entries']['shadow']['settings']['verified_gradual_pairs'] = legacy
    before = deepcopy(state.document)
    register(state.context)
    assert state.document == before
    assert state.writes == []


def test_migration_write_refusal_keeps_legacy_proofs_and_can_retry(legacy_pairs, caplog):
    state = legacy_pairs
    state.refuse = True
    before = deepcopy(state.document)
    service = register(state.context)
    assert state.document == before
    assert state.writes == []
    assert not service.engine.verified_pair(OLD, NEW)
    assert 'not migrated' in caplog.text
    state.refuse = False
    assert register(state.context).engine.verified_pair(OLD, NEW)
    assert state.writes == [('verified_gradual_pairs', state.proofs)]


def test_migration_read_failure_does_not_log_config_or_exception(legacy_pairs, caplog):
    def unreadable():
        raise OSError('migration-secret-canary')

    sys.modules['hermes_cli.config'].read_raw_config_readonly = unreadable
    register(legacy_pairs.context)
    assert legacy_pairs.writes == []
    assert 'not migrated' in caplog.text
    assert 'migration-secret-canary' not in caplog.text


def test_migration_refuses_a_failed_raw_config_snapshot(legacy_pairs):
    class FailedRead(dict):
        read_error = OSError('unreadable')

    sys.modules['hermes_cli.config'].read_raw_config_readonly = lambda: FailedRead(legacy_pairs.document)
    register(legacy_pairs.context)
    assert legacy_pairs.writes == []
