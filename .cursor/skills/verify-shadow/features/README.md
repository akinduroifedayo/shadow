# Shadow verification map

This directory is the maintained source for verifying Shadow's user-facing behavior. Read this index before driving, then use the matching feature file as the recipe. Every command below is `$S`, which is `.cursor/skills/verify-shadow/scripts/shadow-verify` run from the repository root.

## Baseline preconditions

- `SHADOW_HERMES_BIN` names a Hermes 0.23.1+ executable, or `hermes` is on `PATH`.
- Launch a fresh isolated profile with `$S launch` and export the `RUN_ID` it prints. Each journey assumes a fresh launch unless it says otherwise.
- `$S doctor` reports no FAIL lines.
- No provider credentials reach Hermes. Doctor's "Usable models: none confirmed" is the expected baseline.
- Never drive a Hermes home or Desktop harness this run did not start.

## Driving conventions

- Drive the installed plugin through `hermes shadow-trial` only: `$S cli <slug> -- <args>` for one-shot commands and `$S pty ...` for commands that show a numbered menu.
- Treat commands as literal. Test IDs come from the seeded fixture (`c09e`, `a3f2`, `d4a8`, `e5f1`, `b71c`).
- Use `$S seed <fixture>` to create saved tests. Seeding is a precondition, never the proof.
- For the Desktop page use `$S desktop up` and `$S desktop shoot`, and address elements by `data-action`, `data-turn`, `data-model`, and `data-judge`.
- Do not remove evidence. `$S cleanup` keeps it.

## Proof and skip reporting

- CLI proof is the transcript file: command, output, and `exit=`. Shadow exits 0 on refusals, so the expected text is part of the assertion. A traceback with `exit=1` is a FAIL.
- Mutation proof adds a read-only second view: `$S db` or `$S files`, before and after.
- Desktop proof is `desktop/shoot.txt` ending in `all DOM checks passed` plus the named screenshots.
- Record the feature ID and entry point with each artifact. A Desktop journey proven in the dev harness is reported as "dev harness", not as Hermes Desktop.
- Report anything that needs a model call, a Mac, or Hermes Desktop as not driven, with the reason. Do not substitute another entry point and call it verified.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then has exactly four H2 sections in this order: `Sub-features`, `How to get to it (user POV)`, `Driving it with shadow-verify`, and `Gotchas`. The driving section starts with `Preconditions:` and pairs each user action with an exact command and its observable result.

## Features

- [Setup doctor](./setup-doctor.md) covers `hermes shadow-trial doctor`, the bare command, and the consent-missing and consent-granted reports.
- [Saved results](./saved-results.md) covers the newest result, a result by ID, the interactive result menu, `--stats`, idle status, and unknown IDs.
- [Forget a saved test](./forget-test.md) covers deleting one test and all tests, with storage checks.
- [Legacy storage copy](./legacy-storage-copy.md) covers the one-time copy from `plugin-data/shadow` to `plugin-data/shadow-trial` on first load.
- [Trial preview](./trial-preview.md) covers the consent card, setup blocks, and the history-timeout path.
- [Desktop page](./desktop-page.md) covers the `shadow-trial` page's verdict, blind pick and reveal, switch-anyway dialog, switch panel, and start card in the dev harness.
