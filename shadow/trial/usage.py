import math
from dataclasses import asdict, dataclass

from ..common.models import Model, Price, canonical_slug, price_for


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated: bool = False

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    def record(self):
        return {**asdict(self), 'prompt_tokens': self.input_tokens, 'total_tokens': self.total_tokens, 'label': 'not reported by the provider' if self.estimated else 'reported'}


def parse_usage(value, *, input_text='', output_text=''):
    if value is None:
        return Usage(math.ceil(len(input_text.encode()) / 3) + 2048, max(1024, math.ceil(len(output_text.encode()) / 3)), estimated=True)
    def count(*keys):
        for key in keys:
            item = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
            if item is not None:
                if type(item) is not int or item < 0:
                    raise ValueError('invalid usage')
                return item
        return 0
    out = count('output_tokens', 'completion_tokens')
    cache_read = count('cache_read_tokens', 'cache_read_input_tokens')
    cache_write = count('cache_write_tokens', 'cache_creation_input_tokens')
    prompt = value.get('prompt_tokens') if isinstance(value, dict) else getattr(value, 'prompt_tokens', None)
    inp = count('prompt_tokens') if prompt is not None else count('input_tokens') + cache_read + cache_write
    if not inp and not out:
        return parse_usage(None, input_text=input_text, output_text=output_text)
    estimated = value.get('estimated', False) if isinstance(value, dict) else getattr(value, 'estimated', False)
    return Usage(inp, out, cache_read, cache_write, estimated)


def resolve_prices(models):
    prices = {}
    for model in models:
        catalog = prices.setdefault(model.provider, {})
        if model.model not in catalog:
            price = price_for(model)
            catalog[model.model] = price.record() if price is not None else None
    return prices


def stored_price(prices, model):
    catalog = prices.get(model.provider, {})
    if model.model in catalog:
        value = catalog[model.model]
    else:
        matches = [row for name, row in catalog.items() if canonical_slug(name) == canonical_slug(model.model)]
        value = matches[0] if len(matches) == 1 else None
    return Price.from_record(value) if value is not None else None


def cost_usage(price, usage, *, list_price=False):
    if price is None:
        return None
    if price.plan and not list_price:
        return 0.0
    amount = price.cost(usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens)
    return float(amount) if amount is not None else None


class UsageLedger:
    def __init__(self):
        self.requests = {}

    def observe(self, *, session_id='', api_request_id='', response_model='', model='', provider='', usage=None, **_):
        if not session_id or not api_request_id:
            return
        self.requests[(session_id, api_request_id)] = {'model': response_model or model or '', 'provider': provider, 'usage': usage}

    def summarize(self, session_id, prices, expected, *, input_text='', output_text=''):
        rows = [v for (sid, _), v in self.requests.items() if sid == session_id]
        total = Usage()
        cost = 0.0
        actual = []
        for row in rows or [{'model': '', 'usage': None}]:
            usage = parse_usage(row['usage'], input_text=input_text, output_text=output_text)
            for field in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens'):
                setattr(total, field, getattr(total, field) + getattr(usage, field))
            total.estimated |= usage.estimated
            actual.append(row['model'])
            route = Model(row.get('provider'), row['model'])
            amount = cost_usage(stored_price(prices, route), usage, list_price=True) if route.provider and route.model else None
            if amount is None:
                cost = None
            elif cost is not None:
                cost += amount
        return total, cost, actual
