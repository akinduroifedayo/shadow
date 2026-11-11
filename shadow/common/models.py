import re
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, DecimalException


MAKER_PREFIXES = (
    ('openai', ('gpt-', 'o1', 'o3', 'o4', 'codex-')),
    ('anthropic', ('claude-',)),
    ('deepseek', ('deepseek-',)),
    ('google', ('gemini-',)),
    ('nous', ('hermes-',)),
    ('alibaba', ('qwen', 'qwq')),
    ('moonshot', ('kimi-',)),
    ('meta', ('llama-',)),
    ('mistral', ('mistral-', 'mixtral-', 'codestral-')),
    ('zhipu', ('glm-',)),
    ('minimax', ('minimax-',)),
)
MAKER_NAMES = {'openai': 'OpenAI', 'anthropic': 'Anthropic', 'deepseek': 'DeepSeek', 'google': 'Google', 'nous': 'Nous Research', 'alibaba': 'Alibaba', 'moonshot': 'Moonshot', 'meta': 'Meta', 'mistral': 'Mistral', 'zhipu': 'Zhipu', 'minimax': 'MiniMax'}


def model_slug(model):
    return model.lower().split('/')[-1]


def canonical_slug(model):
    return re.sub(r'-(?:\d{4}-\d{2}-\d{2}|\d{8})$', '', model_slug(model))


def maker_of(model):
    slug = model_slug(model)
    for maker, prefixes in MAKER_PREFIXES:
        if slug.startswith(prefixes):
            return maker
    namespace, separator, _ = model.lower().partition('/')
    return namespace if separator and namespace and slug else None


def short_name(model):
    slug = model_slug(model)
    slug = re.sub(r'-(?:\d{4}-\d{2}-\d{2}|\d{8}|preview|latest|fp8|int4|instruct|chat|hf)\b', '', slug)
    slug = re.sub(r'(?<=\d)-(?=\d+(?:-|$))', '.', slug)
    families = {'gpt': 'GPT', 'claude': 'Claude', 'deepseek': 'DeepSeek', 'hermes': 'Hermes', 'qwen': 'Qwen', 'kimi': 'Kimi', 'gemini': 'Gemini', 'llama': 'Llama', 'mistral': 'Mistral'}
    parts = slug.split('-')
    parts[0] = families.get(parts[0], parts[0].title())
    name = ' '.join(p.upper() if re.fullmatch(r'\d+[bm]', p) else p.title() if p not in families.values() else p for p in parts)
    if name.startswith('GPT '):
        name = 'GPT-' + name[4:]
    return name if len(name) <= 22 else name[:16] + '…' + name[-5:]


@dataclass(frozen=True)
class Model:
    provider: str
    model: str
    plan: bool = False
    reachable: bool = True

    @property
    def maker(self):
        return maker_of(self.model)

    @property
    def name(self):
        return short_name(self.model)


def subscription_included(model):
    if model.plan:
        return True
    try:
        from agent.usage_pricing import resolve_billing_route
        route = resolve_billing_route(model.model, provider=model.provider)
        return route.billing_mode == 'subscription_included'
    except Exception:
        return model.provider == 'openai-codex'


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal
    cache_read: Decimal | None
    cache_write: Decimal | None
    dated: str = '2026-11-10'
    source: str = 'Hermes 3b42157858 agent/usage_pricing.py'
    tier_threshold_tokens: int | None = None
    above: tuple[Decimal, ...] | None = None
    plan: bool = False

    def record(self):
        value = asdict(self)
        for key in ('input', 'output', 'cache_read', 'cache_write'):
            if value[key] is not None:
                value[key] = str(value[key])
        if self.above is not None:
            value['above'] = [str(rate) for rate in self.above]
        return value

    @classmethod
    def from_record(cls, value):
        fields = dict(value)
        for key in ('input', 'output', 'cache_read', 'cache_write'):
            if fields[key] is not None:
                fields[key] = Decimal(fields[key])
        if fields.get('above') is not None:
            fields['above'] = tuple(Decimal(rate) for rate in fields['above'])
        return cls(**fields)

    def cost(self, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0):
        ordinary = max(0, input_tokens - cache_read_tokens - cache_write_tokens)
        rates = self.above if self.above and input_tokens > self.tier_threshold_tokens else (self.input, self.output, self.cache_read, self.cache_write)
        total = Decimal(0)
        for tokens, rate in zip((ordinary, output_tokens, cache_read_tokens, cache_write_tokens), rates):
            if rate is None:
                if tokens:
                    return None
            else:
                total += tokens * rate
        return total / 1_000_000


def _price(i, o, r, w, *, long_context=False, source=None):
    rates = tuple(Decimal(x) for x in (i, o, r, w))
    noted = {'source': source} if source else {}
    return Price(*rates, tier_threshold_tokens=272000 if long_context else None,
                 above=tuple(rate * multiplier for rate, multiplier in zip(rates, (2, Decimal('1.5'), 2, 2))) if long_context else None,
                 **noted)


PRICES = {
    'gpt-6.1-sol': _price('2', '10', '.1', '2.5', long_context=True),
    'gpt-6.1-terra': _price('1', '5', '.05', '1.25', long_context=True,
                            source='https://developers.openai.com/api/docs/pricing'),
    'gpt-6-sol': _price('2', '10', '.2', '2.5', long_context=True),
    'gpt-6-luna': _price('.1', '.5', '.01', '.125', long_context=True),
    'gpt-4.1': _price('2', '8', '.5', '2.5'),
    'gpt-4.1-mini': _price('.4', '1.6', '.1', '.5'),
    'claude-opus-4-6': _price('5', '25', '.5', '6.25'),
    'claude-sonnet-4-5': _price('3', '15', '.3', '3.75'),
    'claude-sonnet-4-6': _price('3', '15', '.3', '3.75'),
}


_CONTEXT_TIERED_MODELS = frozenset({
    slug for slug, price in PRICES.items() if price.tier_threshold_tokens is not None
} | {
    'gpt-6-astra', 'gemini-3.1-pro', 'gemini-3.1-pro-preview', 'gemini-2.5-pro',
    *(f'{slug}-{suffix}' for slug in ('gpt-6-sol', 'gpt-6.1-sol', 'gpt-6-luna')
      for suffix in ('pro', '900k')),
})


def _openrouter_has_no_request_fee(model_id):
    try:
        from agent import model_metadata
        metadata = getattr(model_metadata, '_model_metadata_cache', {})
        if not isinstance(metadata, dict) or model_id not in metadata:
            metadata = model_metadata._load_model_metadata_disk_cache()
        row = metadata.get(model_id) if isinstance(metadata, dict) else None
        pricing = row.get('pricing') if isinstance(row, dict) else None
        if not isinstance(pricing, dict):
            return False
        if 'request' not in pricing:
            return True
        fee = Decimal(str(pricing['request']))
        return fee.is_finite() and fee == 0
    except Exception:
        return False


def price_for(model):
    try:
        from hermes_cli.models_pricing import get_pricing_for_provider
        catalog = get_pricing_for_provider(model.provider, cached_only=True)
    except Exception:
        catalog = {}
    if isinstance(catalog, dict) and model.model in catalog:
        row = catalog[model.model]
        if not isinstance(row, dict):
            return None
        try:
            rates = tuple(Decimal(str(row[key])) * 1_000_000 if key in row else None
                          for key in ('prompt', 'completion', 'input_cache_read', 'input_cache_write'))
        except (DecimalException, TypeError, ValueError):
            return None
        if any(rate is None or not rate.is_finite() or rate <= 0 for rate in rates):
            return None
        slug = canonical_slug(model.model.split(':', 1)[0])
        if (model.provider != 'openrouter'
                or any(slug == base or slug.startswith(base + '-') for base in _CONTEXT_TIERED_MODELS)
                or not _openrouter_has_no_request_fee(model.model)):
            return None
        return Price(*rates, source='Hermes hermes_cli.models_pricing.get_pricing_for_provider',
                     plan=subscription_included(model))
    # Proxy markups and unknown context tiers need a separately verified price.
    if model.provider not in {'openai', 'openai-codex', 'anthropic'}:
        return None
    price = PRICES.get(canonical_slug(model.model))
    return replace(price, plan=subscription_included(model)) if price is not None else None
