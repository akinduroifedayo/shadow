# Private Hermes imports

This audit covers the nine private-name imports in model discovery and the live isolation probe. It uses Hermes reference commit `3b421578588243f920886e6fe79a6faa87ea026e`. Source references below are relative to that Hermes checkout.

Only the middleware runner has an equivalent public-named function. The other eight imports remain because replacing them would change local-only discovery or weaken the isolation proof. Shadow does not rebind or monkey-patch Hermes core.

| Shadow dependency | Result and Hermes evidence | Required Hermes seam |
|---|---|---|
| `shadow/common/discovery.py`, `config_backend._BOOTSTRAPPED` | No public equivalent. `hermes_cli/config_backend.py:293` bootstraps dotenv on a first read. `get_config_backend` at `:329` calls it. `bootstrap_deployment` at `:302` performs bootstrap rather than reporting readiness. | A side-effect-free getter for completed configuration bootstrap and backend kind. |
| `shadow/common/discovery.py`, `providers._ALIASES` | No public equivalent. `providers/__init__.py:220` resolves aliases through `get_provider_profile`, but also discovers providers and refreshes plugins. `bound_profile_resolver` at `:407` refreshes the home layer through `:423`. | A snapshot of already-registered aliases without discovery or plugin imports. |
| `shadow/common/discovery.py`, `providers._REGISTRY` | No public equivalent. `list_providers` at `providers/__init__.py:363` can discover providers and refresh home plugins. Shadow must inspect only metadata already registered. | A side-effect-free snapshot of registered provider profiles. |
| `shadow/common/discovery.py`, `providers._bound_home_layer` | No public equivalent. The private getter at `providers/__init__.py:436` selects the bound home without scanning plugins. Public `bound_profile_resolver` at `:407` and `bound_registry_key` at `:313` can refresh the layer. | The same snapshot API must preserve bound-home aliases and overrides without refreshing plugins. |
| `shadow/trial/probe_dispatch.py`, `tool_executor._ToolCallRef` | No public equivalent. `agent/tool_executor.py:278` defines the call identity used by the runtime veto. Its `__all__` at `:1956` exports full execution entry points, which cannot substitute for an inert preflight proof. | A public tool-call identity or an inert preflight API accepting call and session identities. |
| `shadow/trial/probe_dispatch.py`, `tool_executor._pre_tool_block` | No public equivalent. `agent/tool_executor.py:662` returns the block and hook-modified arguments. `resolve_pre_tool_block` at `hermes_cli/plugins.py:1188` discards modified arguments and bypasses this executor entry point. | A public executor preflight returning both the block and final arguments. |
| `shadow/trial/probe_dispatch.py`, `middleware._run_execution_chain` | Replaced with `run_tool_execution_middleware` at `hermes_cli/middleware.py:144`. It runs the same chain and supplies `original_args`. Shadow's guard accepts that context through `**_` in `shadow/trial/safety.py:147`. | Catalog allow-list inclusion for this public-named helper. |
| `shadow/trial/probe_dispatch.py`, `plugins._delivery_manager` | No public equivalent. `hermes_cli/plugins.py:978` joins discovery before returning the delivery manager. `get_plugin_manager` at `:820` lacks that step. `iter_hook_callbacks` at `:1065` has no middleware counterpart. Registration handles at `hermes_cli/plugins_ledger.py:53` report disposal state, not exact membership in the active delivery registry. | Public inspection of exact hook and middleware registration membership with delivery discovery semantics. |
| `shadow/trial/probe.py`, `plugins._resolve_hook_callback_timeout` | No public equivalent. `hermes_cli/plugins.py:236` applies defaults, invalid-value fallback, negative-value fallback, zero-disable behavior, and maximum clamping. `ctx.get_config` at `hermes_cli/plugins_context.py:73` reads plugin-relative configuration rather than the effective global timeout. | A public effective callback timeout getter or a deadline supplied to the callback. |

The public `ctx.dispatch_tool` (`hermes_cli/plugins_context.py:483`) is not a substitute. It calls the tool registry directly, so it skips both the `pre_tool_call` veto and the `tool_execution` middleware and runs the real tool. It cannot prove either blocking layer.

The tool middleware contract is documented in `website/docs/developer-guide/plugins/index.md:1707`. The runner has a public name and docstring, but `hermes_cli/data/plugin_api.json` does not allow-list its module or symbol. The catalog therefore still warns on its import. This replacement removes one private-name dependency without claiming catalog rule 9 is fully resolved.

The reference validator reports **42 private-import findings before and after**, including imports outside this nine-item audit, and **0 private-attribute findings**. The targeted private-name imports decrease from **9 to 8**. Run the same read-only lint from the Shadow root with an existing Python environment and `PYTHONDONTWRITEBYTECODE=1`:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/path/to/hermes-ref python - <<'PY'
from pathlib import Path
from hermes_cli.plugin_validate_private_imports import private_import_findings, private_attribute_findings

for label, check in [('imports', private_import_findings), ('attributes', private_attribute_findings)]:
    findings = check(Path.cwd())
    print(label, len(findings))
    print('\n'.join(findings))
PY
```
