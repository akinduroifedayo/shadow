# Changelog

## 0.1.0 - 2026-11-11

- Replay eligible saved Hermes turns on a candidate and the current model.
- Compare answers blind in both orders with a judge self-agreement check.
- Select an outside-maker judge or disclose backup judging by both contestants.
- Show a numbers-first verdict: won, lost, and tied turns, cost per turn, a win-rate bar with its 95% range, a two-model table, and an advice tag. Also show speed, tool steps, and broken calls.
- Block replay actions with a pre-tool hook and tool-execution middleware.
- Scope file reads and scrub sensitive text in outbound content and saved records.
- Track token spending against a cap and show an estimate before Start.
- Provide CLI, chat commands, and a Shadow page in Hermes Desktop.
- Check replay isolation and gradual model-pair compatibility through live probes.
- Switch tested models with monitoring, rollback, and undo. When a test doesn't back the switch, warn first and switch only on explicit confirmation, logging each override.
- Include local checks, contributor policies, and CI.
