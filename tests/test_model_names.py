import pytest

from shadow.common.models import Model, short_name


@pytest.mark.parametrize('slug,expected', [
    ('claude-sonnet-4-5', 'Claude Sonnet 4.5'),
    ('anthropic/claude-sonnet-4-5-20250929', 'Claude Sonnet 4.5'),
    ('claude-opus-4-6', 'Claude Opus 4.6'),
    ('gpt-6.1-terra', 'GPT-6.1 Terra'),
    ('gpt-4-1-mini', 'GPT-4.1 Mini'),
    ('meta/llama-3-1-70b-instruct', 'Llama 3.1 70B'),
    ('hermes-4-70b', 'Hermes 4 70B'),
    ('nousresearch/hermes-4-405b-fp8-2026-08-14', 'Hermes 4 405B'),
    ('deepseek-v3.2', 'DeepSeek V3.2'),
])
def test_model_names_keep_versions_and_sizes(slug, expected):
    assert short_name(slug) == expected
    assert Model('fixture', slug).name == expected
