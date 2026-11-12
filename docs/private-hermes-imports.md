# Public Hermes integration

Shadow uses public plugin context methods and public Hermes functions. The API
checks use Hermes reference commit `3b421578588243f920886e6fe79a6faa87ea026e`.
`tests/test_public_hermes_api.py` scans every Python file under `shadow` for private
external imports and attributes, plus the retired agent, gateway runner, and
configuration-backend modules. It has no exceptions.

| Area | Public API | Behavior and limits |
| --- | --- | --- |
| Pair settings | `ctx.get_config` and `ctx.set_config` | Proofs stay under `plugins.entries.shadow.settings.verified_gradual_pairs`. Hermes owns the write. |
| Profile discovery | `hermes_cli.config.read_raw_config_readonly` | Reads the raw profile document without expanding credential references. Shadow rejects a configured non-file backend before reading. It no longer reads private bootstrap readiness. |
| Model lists | `agent.models_dev.fetch_models_dev(allow_network=False)` and configured models | Keeps declared models, cached metadata for known providers, custom-provider aliases, staged local models, and live provider observations. Registry-only aliases, home-provider overrides, credential names, and models.dev mappings no longer contribute. Public registry readers can scan and import plugins, so Shadow does not call them. |
| Setup gateway | `hermes_cli.gateway_runtime.discover_gateway_endpoint` | A missing setup result with a present or inaccessible gateway endpoint asks the user to restart that profile's gateway. Shadow does not reload it. |
| Guard proof | Shadow's `guard.pre_tool_call` and `guard.tool_execution` callbacks | Tests blocking, scoped reads, and secret refusal with an inert executor. This does not prove membership in Hermes's active hook or middleware registry. |
| Pair proof | `ctx.llm.complete`, `ctx.register_hook`, and `ctx.register_auxiliary_task` | Makes two completions and checks route observations, one attempt per completion, stop reasons, and exact reply handoff. A temporary unique task separates concurrent auxiliary traffic. It does not prove full agent-history compatibility. |
| Sealed reasoning | `pre_auxiliary_call` and `post_auxiliary_call` | Public payloads omit sealed reasoning. Required sealed evidence fails with `sealed_reasoning_unobservable` before a completion. Evidence always records sealed reasoning as unobserved. |
| Pair trust | `agent.plugin_llm.PluginLlmTrustError` | Provider and model overrides require the corresponding `plugins.entries.shadow.llm.allow_*_override` grants. Refusal reports `llm_trust_denied`. |
| Hook deadline | `hermes_cli.config.load_config_readonly` | Reads `plugins.hook_callback_timeout`. Uses 30 seconds by default, zero to disable the deadline, 30 for negative or invalid values, and a 600-second maximum, matching the reference host. |
| Request fee | No suitable public cache-only reader | Returns unknown. OpenRouter tests cannot start without a known request fee. `fetch_model_metadata(force_refresh=False)` can fetch when its cache is stale, so Shadow does not call it. |

Possible upstream API requests remain local ideas. No issue is required to use
Shadow's current fallback behavior.

- Add `ctx.hook_registered(name, callback)` and equivalent middleware inspection
  for exact membership in the active delivery registry.
- Expose sealed-reasoning observations or a host-owned model-pair compatibility
  check through the public plugin API.
- Add a cache-only request-fee reader that never refreshes stale metadata.
- Expose a side-effect-free snapshot of registered provider profiles, aliases,
  and bound-home overrides without importing plugins.
- Expose configuration-bootstrap readiness and the effective hook deadline as
  public getters so plugins do not need private readiness checks or policy copies.
