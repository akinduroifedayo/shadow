# Changelog

## Unreleased

- Register `/shadow-trial` and `hermes shadow-trial` so commands coexist with the other catalog plugin.
- Store trial records, switch records, identity salts, and pair evidence in `plugin-data/shadow-trial`.
- On load, copy legacy `data.db` and `switch.db` from `plugin-data/shadow` only when their tables identify them as Shadow's and the destination is absent. Copy a regular 32-byte `switch.salt` only with a verified switch database. Preserve existing destinations and leave legacy files and evidence unchanged.
- Recover legacy WAL and journal databases in private work directories without changing the shared legacy folder. Require SQLite integrity and ownership checks before copying. Log one fixed warning if recovery fails and continue loading.
- Keep only known Shadow tables and their indexes in copied databases, then vacuum each copy to remove foreign data before publishing.
- Remove stale migration database files, salt files, and recovery directories only from `plugin-data/shadow-trial` on load.

## 0.1.0 - 2026-11-11

- Replay eligible saved Hermes turns on a candidate and the current model.
- Compare answers blind in both orders with a judge self-agreement check.
- Select an outside-maker judge or disclose backup judging by both contestants.
- Show a numbers-first verdict: won, lost, and tied turns, cost per turn, a win-rate bar with its confidence range, a two-model table, and an advice tag. Also show speed, tool steps, and broken calls.
- Decide single-judge Better and Worse on the interval shown on the card, with a stricter 97.5% interval on a second look. Keep the sign test as a diagnostic.
- Saved single-judge results are re-decided on read under this interval rule, without rewriting the stored record.
- A Better or Worse interval uses more decimal places when a whole percent would print as 0, so the printed range stays entirely above or below even, including the win-rate range on the card.
- Block replay actions with a pre-tool hook and tool-execution middleware.
- Scope file reads and scrub sensitive text in outbound content and saved records.
- Track token spending against a cap and show an estimate before Start.
- Provide CLI, chat commands, and a Shadow page in Hermes Desktop.
- Check replay isolation and gradual model-pair compatibility through live probes.
- Switch tested models with monitoring, rollback, and undo. When a test doesn't back the switch, warn first and switch only on explicit confirmation, logging each override.
- Include local checks, contributor policies, and CI.
