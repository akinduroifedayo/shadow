def register(ctx):
    from .common.storage_migration import migrate_legacy_storage
    migrate_legacy_storage()
    from . import trial
    service = trial.register(ctx)
    switch_service = None
    try:
        from . import switch
    except ImportError:
        switch = None
    else:
        switch_service = switch.register(ctx)
    from .desktop_rpc import register as register_desktop
    from .desktop_source import ProfileSource
    register_desktop(ctx, ProfileSource(service, switch_service))
    return service
