"""Bind runtime pair evidence to the profile's effective reasoning settings."""


def pair_reasoning(config, provider, old_model):
    from agent.models_dev import get_model_capabilities
    from hermes_constants import resolve_reasoning_config

    capabilities = get_model_capabilities(provider, old_model, allow_network=False, config=config)
    capable = capabilities is not None and capabilities.supports_reasoning is True
    reasoning = resolve_reasoning_config(config, old_model)
    if reasoning is None and capable:
        reasoning = {'enabled': True}
    # Gradual switching changes the model on the current agent's request, retaining its reasoning.
    return {'old': reasoning, 'new': reasoning}, bool(reasoning and reasoning.get('enabled'))


def matching_proof(config, old, new, *, sealed_required=False):
    try:
        reasoning, configured_sealed_required = pair_reasoning(config, old.provider, old.model)
    except Exception:
        return False
    settings = config.get('plugins', {}).get('entries', {}).get('shadow', {}).get('settings', {})
    return any(
        isinstance(proof, dict) and old.matches(proof.get('old')) and new.matches(proof.get('new'))
        and proof.get('probe_passed') is True and bool(proof.get('evidence'))
        and proof.get('reasoning') == reasoning
        and (not (sealed_required or configured_sealed_required) or proof.get('sealed_reasoning_observed') is True)
        for proof in settings.get('verified_gradual_pairs', [])
    )
