# Legacy storage copy

Shadow used to store its data under `plugin-data/shadow`, a name another catalog plugin also uses. On its first load in a profile, Shadow copies only its own `data.db` and `switch.db` from `plugin-data/shadow` to `plugin-data/shadow-trial`, once, and only when the tables identify the database as Shadow's. The legacy files stay byte-identical. A user sees their old saved tests under the new name.

## Sub-features

- `legacy-copy-data` copies a Shadow `data.db` and its saved tests on first load.
- `legacy-untouched` leaves `plugin-data/shadow` byte-identical.
- `legacy-once` skips the copy once `plugin-data/shadow-trial/data.db` exists.
- `legacy-foreign-skip` ignores a `data.db` without Shadow's tables (another plugin's data). Needs a hand-made foreign database; not seeded by the helper.

## How to get to it (user POV)

- Upgrade Shadow in a profile that has `plugin-data/shadow/data.db`, then run any `hermes shadow-trial` command, for example `hermes shadow-trial results`.

## Driving it with shadow-verify

Preconditions:

- A fresh `$S launch`, with no `hermes shadow-trial` command run yet. `$S doctor` prints `INFO trial storage not created yet`.

- **Legacy data.** Run `$S seed result-worse --legacy`. Output is `seeded c09e ... into shadow/data.db`.
- **Before load.** Run `$S files before-load`. Only `plugin-data/shadow/data.db` is listed. Note its sha256 prefix.
- **First load.** Run `$S cli first-load -- results`. The card headline is `Won 3, lost 18, tied 9 of 30 turns · 61% cheaper per turn` and the last line names `c09e`. The user sees the old test.
- **After load.** Run `$S files after-load`. `plugin-data/shadow-trial/data.db` now exists, and `plugin-data/shadow/data.db` has the same sha256 prefix and size as before.
- **Copied rows.** Run `$S db copied "SELECT id, json_extract(record,'$.state') FROM trial_records"`. Output is `c09e | finished`.
- **Copy once.** Run `$S cli forget-copied -- forget c09e`, then `$S cli second-load -- results`. Output is `No tests yet`: the legacy test was not copied again. `$S db legacy-still "SELECT id FROM trial_records" --legacy` still shows `c09e` in the legacy file.

## Gotchas

- Any Shadow command, including `doctor`, is the first load. Seed `--legacy` before it. `seed --legacy` refuses once trial storage exists.
- `seed --legacy` writes only `data.db`. The `switch.db` and salt path is not seeded by the helper.
- A failed copy logs one warning to Hermes logs and Shadow starts empty. When the copy journey fails, read `<scratch>/hermes-home/logs/agent.log` before cleanup.
