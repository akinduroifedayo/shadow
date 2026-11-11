def profile_service_tiers(config):
    if not isinstance(config, dict):
        raise ValueError('invalid service tier configuration')
    sections = [config.get(section, {}) for section in ('agent', 'delegation')]
    if any(not isinstance(section, dict) for section in sections):
        raise ValueError('invalid service tier configuration')
    return tuple(section.get('service_tier') for section in sections)


def normal_service_tiers(tiers):
    return all(str(tier or '').strip().lower() in {'', 'normal', 'default', 'standard', 'off', 'none'}
               for tier in tiers)


def prices_for_tiers(prices, tiers):
    if normal_service_tiers(tiers):
        return prices
    return {provider: {model: None for model in catalog} for provider, catalog in prices.items()}
