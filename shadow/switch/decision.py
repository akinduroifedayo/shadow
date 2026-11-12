"""Owner switch decisions, independent of replay execution safety."""
from .copy import copy, short_name
from .domain import Proposal, Route


class SwitchBlocked(ValueError):
    def __init__(self, key, **fields):
        self.key = key
        self.fields = fields
        super().__init__(copy(key, **fields))


def require_model_change(old, new):
    if (old.provider, old.model) == (new.provider, new.model):
        raise SwitchBlocked('switch.already_active', model=short_name(new.model))


def switch_warning(proposal):
    if proposal.headline:
        return copy('switch.override.warning', headline=proposal.headline)
    return copy('switch.override.untested' if proposal.verdict == 'untested' else 'switch.override.unbacked',
                new=short_name(proposal.new.model))


def untested_proposal(catalog, config, target):
    new = catalog.resolve(target)
    old = catalog.require(catalog.current.provider, catalog.current.model)
    require_model_change(old, new)

    def route(model):
        providers = config.get('providers', {})
        settings = providers.get(model.provider, {})
        model_settings = config.get('model', {})
        if not isinstance(model_settings, dict):
            model_settings = {}
        own_provider = model.provider == old.provider
        endpoint = (model_settings.get('base_url') if own_provider else None) or settings.get('base_url', '')
        mode = (model_settings.get('api_mode') if own_provider else None) or settings.get('api_mode', '')
        if not endpoint or not mode:
            from hermes_cli.providers import (get_provider, resolve_user_provider, resolve_custom_provider,
                                              TRANSPORT_TO_API_MODE, host_mandated_api_mode)
            definition = (resolve_user_provider(model.provider, providers)
                          or resolve_custom_provider(model.provider, config.get('custom_providers', []))
                          or get_provider(model.provider, allow_network=False))
            if definition is None:
                raise ValueError('provider route is unavailable')
            endpoint = endpoint or definition.base_url
            mode = mode or host_mandated_api_mode(endpoint) or TRANSPORT_TO_API_MODE.get(definition.transport, definition.transport)
        if model.provider in {'nous', 'nous-portal', 'nousresearch'}:
            native = (config.get('nous') or {}).get('anthropic_wire') == 'native'
            mode = 'anthropic_messages' if model.model.startswith('anthropic/') and native else 'chat_completions'
        if model.provider in {'openai', 'openai-codex'} and model_settings.get('openai_runtime') == 'codex_app_server':
            mode = 'codex_app_server'
        return Route(model.provider, model.model, endpoint, mode)

    return Proposal('', route(old), route(new), 'untested')
