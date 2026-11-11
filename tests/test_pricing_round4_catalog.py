import sys
from decimal import Decimal
from types import ModuleType

import pytest

from fixtures import CURRENT, OUTSIDE, FakeContext, selection
from shadow.common.models import Model, price_for
from shadow.common.storage import Store
from shadow.trial.cards import render_start
from shadow.trial.engine import TrialEngine
from shadow.trial.safety import ReplayGuard


@pytest.fixture
def cached_catalog(monkeypatch):
    catalog = {}
    package = ModuleType('hermes_cli')
    package.__path__ = []
    pricing = ModuleType('hermes_cli.models_pricing')

    def lookup(provider, *, cached_only=False):
        assert cached_only
        return catalog if provider == 'openrouter' else {}

    pricing.get_pricing_for_provider = lookup
    agent = ModuleType('agent')
    agent.__path__ = []
    metadata = ModuleType('agent.model_metadata')
    metadata._model_metadata_cache = {}
    metadata._load_model_metadata_disk_cache = lambda: {
        model: {'pricing': {'request': '0'}} for model in catalog
    }
    monkeypatch.setitem(sys.modules, 'hermes_cli', package)
    monkeypatch.setitem(sys.modules, 'hermes_cli.models_pricing', pricing)
    monkeypatch.setitem(sys.modules, 'agent', agent)
    monkeypatch.setitem(sys.modules, 'agent.model_metadata', metadata)
    return catalog


def prepare_catalog_trial(tmp_path, cached_catalog, model_id):
    model = Model('openrouter', model_id)
    cached_catalog[model_id] = {
        'prompt': '0.000002', 'completion': '0.000006',
        'input_cache_read': '0.000001', 'input_cache_write': '0.000003',
    }
    ctx = FakeContext()
    engine = TrialEngine(ctx, Store(tmp_path / 'context-tiers.sqlite'), ReplayGuard())
    record = engine.prepare(selection(tmp_path, 20), CURRENT, model, [OUTSIDE])
    return model, ctx, engine, record


@pytest.mark.parametrize('slug', [
    'gpt-6-astra', 'gpt-6-sol', 'gpt-6.1-sol', 'gpt-6-luna', 'gpt-6.1-terra',
    'gpt-6-sol-pro', 'gpt-6-sol-900k', 'gpt-6.1-sol-pro', 'gpt-6.1-sol-900k',
    'gpt-6-luna-pro', 'gpt-6-luna-900k',
    'gpt-6.1-terra-pro', 'gpt-6.1-terra-900k', 'gpt-6-astra-pro',
    'gemini-3.1-pro', 'gemini-3.1-pro-preview', 'gemini-2.5-pro',
    'gemini-2.5-pro-preview', 'gemini-2.5-pro-preview-05-06',
])
@pytest.mark.parametrize('prefix,suffix', [
    ('', ''), ('vendor/', ''), ('vendor/', '-20261101'), ('', '-2026-11-01'),
    ('', ':extended'), ('vendor/', ':nitro'),
    ('vendor/', '-20261101:extended'), ('', '-2026-11-01:nitro'),
])
def test_known_context_tier_cannot_start_from_flat_openrouter_rates(
        tmp_path, cached_catalog, slug, prefix, suffix):
    model, ctx, engine, record = prepare_catalog_trial(
        tmp_path, cached_catalog, prefix + slug + suffix)

    assert not record['can_start']
    assert record['estimate']['total'] is None
    assert price_for(model) is None
    assert '[Start]' not in render_start(record, isolation_verified=True)
    with pytest.raises(ValueError, match='cannot start'):
        engine.attach(record, 'controller')
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls


def test_verified_flat_openrouter_model_can_start(tmp_path, cached_catalog):
    model, ctx, engine, record = prepare_catalog_trial(
        tmp_path, cached_catalog, 'nousresearch/hermes-3-llama-3.1-405b')

    assert record['can_start']
    assert record['estimate']['total'] > 0
    assert '[Start]' in render_start(record, isolation_verified=True)
    assert price_for(model).cost(1000, 100) == Decimal('0.0026')
    engine.attach(record, 'controller')
    assert not ctx.subagent_lifecycle.launches and not ctx.llm.calls
