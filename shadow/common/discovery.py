"""Profile model identities and credential decisions owned by Hermes."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from .copy import copy
from .models import Model, model_slug, subscription_included
from .scrub import Scrubber


class ModelUnavailable(ValueError):
    def __init__(self, code, provider='', model=''):
        self.code = code
        super().__init__(copy('discovery.' + code, provider=Scrubber().text(provider), model=Scrubber().text(model)))


@dataclass
class ProviderPresence:
    observed: set[tuple[Path, str]] = field(default_factory=set)
    children: set[str] = field(default_factory=set)

    @staticmethod
    def _home():
        try:
            from hermes_constants import get_hermes_home
            return Path(get_hermes_home()).resolve()
        except (ImportError, OSError):
            return None

    def note_child(self, child_session_id='', **_):
        if child_session_id:
            self.children.add(child_session_id)

    def observe(self, provider='', session_id='', **_):
        if not provider or not session_id:
            return
        home = self._home()
        if home is not None and session_id not in self.children:
            self.observed.add((home, provider))

    def compacted(self, session_id='', old_session_id='', **_):
        if session_id and old_session_id in self.children:
            self.children.add(session_id)

    def providers(self):
        home = self._home()
        return frozenset(provider for owner, provider in self.observed.copy() if owner == home)


@dataclass
class ModelCatalog:
    current: Model
    models: list[Model]
    reasons: dict[tuple[str, str], str] = field(default_factory=dict)
    failure: str | None = None
    aliases: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_models(cls, current, models):
        unique = {(m.provider, m.model): m for m in models}
        return cls(current, list(unique.values()), {key: 'credentials_unavailable' for key, m in unique.items() if not m.reachable})

    @property
    def candidates(self):
        return [m for m in self.models if m.reachable and (m.provider, m.model) not in self.reasons]

    def require(self, provider, model):
        provider = self.aliases.get(provider, provider)
        if self.failure:
            raise ModelUnavailable(self.failure, provider, model)
        reason = self.reasons.get((provider, model))
        if reason:
            raise ModelUnavailable(reason, provider, model)
        found = next((m for m in self.models if (m.provider, m.model) == (provider, model)), None)
        if found is None:
            raise ModelUnavailable('model_not_configured', provider, model)
        return found

    def matches(self, query):
        provider, separator, name = query.partition('::')
        if not separator:
            provider, name = '', query
        provider = self.aliases.get(provider, provider)
        choices = [m for m in self.models if not provider or m.provider == provider]
        exact = [m for m in choices if m.model == name]
        return exact or ([] if '/' in name else [m for m in choices if model_slug(m.model) == name])

    def resolve(self, query):
        if self.failure:
            raise ModelUnavailable(self.failure, model=query)
        matches = self.matches(query)
        if len(matches) != 1:
            raise ModelUnavailable('model_ambiguous' if matches else 'model_not_configured', model=query)
        return self.require(matches[0].provider, matches[0].model)


def _declared_models(value):
    if isinstance(value, str):
        value = [value]
    elif isinstance(value, dict):
        value = [key for key in value if isinstance(key, str) and not key.startswith('__')]
    if not isinstance(value, (list, tuple)):
        return []
    names = []
    for item in value:
        if isinstance(item, dict):
            item = item.get('id') or item.get('name')
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
    return names


def _credential_reason(provider, config, profile, metadata, environment_names):
    env_names = [config.get('key_env'), config.get('api_key_env')]
    raw_key = config.get('api_key')
    if isinstance(raw_key, str):
        reference = re.fullmatch(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', raw_key)
        if reference:
            env_names.append(reference[1])
    if profile is not None and getattr(profile, 'auth_type', '') == 'api_key':
        env_names.extend(getattr(profile, 'env_vars', ()))
    if metadata is not None and profile is None:
        env_names.extend(name for name in metadata.get('env') or []
                         if isinstance(name, str) and name.endswith(('_KEY', '_TOKEN')))
    if any(isinstance(name, str) and not name.endswith(('_URL', '_BASE_URL'))
           and name in environment_names for name in env_names):
        return None
    if provider == 'openai-codex':
        # Codex status is read-only; other provider status builders can refresh.
        try:
            from hermes_cli.auth import get_auth_status
            status = get_auth_status(provider)
            if isinstance(status, dict) and status.get('logged_in') is True:
                return None
        except Exception:
            pass
    return 'credentials_unknown'


def _local_context():
    from hermes_constants import get_hermes_home
    from hermes_cli.config_backend import read_config_doc
    from hermes_cli.config_providers import coerce_provider_id, stringify_provider_map
    from hermes_cli.inventory import ConfigContext

    # load_picker_context expands credential references throughout the config.
    config = read_config_doc(Path(get_hermes_home()) / 'config.yaml') or {}
    model = config.get('model', {})
    if isinstance(model, str):
        model = {'default': model}
    return ConfigContext(
        current_provider=coerce_provider_id(model.get('provider')),
        current_model=str(model.get('default') or model.get('name') or '').strip(),
        current_base_url='', user_providers=stringify_provider_map(config.get('providers')),
        custom_providers=config.get('custom_providers') or [],
        excluded_providers=(config.get('model_catalog') or {}).get('excluded_providers') or [],
    )


def discover_models(*, live_providers=frozenset()):
    current = Model('', '')
    try:
        from hermes_cli.config_backend import _BOOTSTRAPPED

        # A first config read bootstraps dotenv; remote backends may perform network I/O.
        if not _BOOTSTRAPPED or os.environ.get('HERMES_CONFIG_BACKEND', 'file') not in ('', 'file'):
            return ModelCatalog(current, [], failure='discovery_unavailable')
        from agent.models_dev import PROVIDER_TO_MODELS_DEV, fetch_models_dev
        from agent.secret_scope import current_secret_scope, serves_routed_profile
        from hermes_cli.local_runtime.bootstrap import staged_model_ids
        from hermes_cli.providers import (LLAMACPP_ALIASES, LLAMACPP_PROVIDER_ID,
                                          custom_provider_aliases, custom_provider_slug)
        from providers import _ALIASES, _REGISTRY, _bound_home_layer

        context = _local_context()
        layer = _bound_home_layer()[0]
        current = Model(context.current_provider, context.current_model)
        metadata = fetch_models_dev(allow_network=False)
        scope = current_secret_scope()
        environment_names = frozenset(scope) if scope is not None else frozenset()
        if not serves_routed_profile():
            environment_names |= frozenset(os.environ)
        models, reasons, aliases = {}, {}, {}
        excluded = set(context.excluded_providers or [])
        configs = dict(context.user_providers)
        for entry in context.custom_providers:
            if not isinstance(entry, dict) or not entry.get('name'):
                continue
            provider = custom_provider_slug(entry['name'], entry.get('provider_key', ''))
            if provider in configs:
                merged = dict(configs[provider])
                merged['models'] = (_declared_models(merged.get('models'))
                                    + _declared_models(entry.get('models'))
                                    + _declared_models(entry.get('model')))
                configs[provider] = merged
            else:
                configs[provider] = entry
        current_configured = any(current.provider in custom_provider_aliases(
            config.get('name', provider), provider)
            for provider, config in configs.items() if isinstance(config, dict))
        if current.provider and not current_configured:
            configs.setdefault(current.provider, {})
        for provider, config in configs.items():
            if not isinstance(config, dict) or config.get('enabled') is False or provider in excluded:
                continue
            # Registry lookups can import plugins that load dotenv; use only registered metadata.
            canonical = layer.aliases.get(provider) or _ALIASES.get(provider, provider)
            profile = layer.registry.get(canonical) or _REGISTRY.get(canonical)
            provider_aliases = custom_provider_aliases(config.get('name', provider), provider)
            if profile is not None:
                provider_aliases = set(provider_aliases) | set(getattr(profile, 'aliases', ()))
            aliases.update({alias: provider for alias in provider_aliases})
            if current.provider == provider or current.provider in provider_aliases:
                current = Model(provider, current.model)
            metadata_id = getattr(profile, 'models_dev_id', '') or PROVIDER_TO_MODELS_DEV.get(provider, provider)
            cached = metadata.get(metadata_id) or {}
            names = (_declared_models(config.get('model') or config.get('default_model'))
                     + _declared_models(config.get('models')))
            if profile is not None or provider in PROVIDER_TO_MODELS_DEV:
                names += _declared_models(cached.get('models'))
            if current.provider == provider and current.model:
                names = [current.model, *names]
            credential_metadata = cached if metadata_id == provider else None
            observed = live_providers.intersection({provider, canonical, *provider_aliases})
            reason = None if observed else _credential_reason(canonical, config, profile, credential_metadata, environment_names)
            for name in names:
                identity = provider, name
                models[identity] = Model(
                    provider, name, plan=subscription_included(Model(canonical, name)), reachable=reason is None)
                if reason:
                    reasons[identity] = reason
        if LLAMACPP_PROVIDER_ID not in excluded:
            for name in staged_model_ids():
                identity = LLAMACPP_PROVIDER_ID, name
                models[identity] = Model(*identity)
                reasons.pop(identity, None)
            aliases.update({alias: LLAMACPP_PROVIDER_ID for alias in LLAMACPP_ALIASES})
            if current.provider in LLAMACPP_ALIASES:
                current = Model(LLAMACPP_PROVIDER_ID, current.model)
        if (current.provider, current.model) not in models:
            models[current.provider, current.model] = Model(current.provider, current.model, reachable=False)
            reasons[current.provider, current.model] = 'credentials_unknown'
        current = models[current.provider, current.model]
        return ModelCatalog(current, list(models.values()), reasons, aliases=aliases)
    except Exception:
        return ModelCatalog(current, [], failure='discovery_unavailable')
