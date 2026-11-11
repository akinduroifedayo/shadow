def register(ctx):
    from .shadow import register as register_plugin
    return register_plugin(ctx)
