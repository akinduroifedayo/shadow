# Saved results

A user reads a finished test from the terminal: a numbers-first headline, the win-rate bar, the two-model table, an advice tag, the receipt, and a pointer to the full numbers. In an interactive terminal Shadow then offers next actions, such as listing the turns the new model lost. `status` says whether anything is running.

## Sub-features

- `results-newest` prints the newest saved test with `hermes shadow-trial results`.
- `results-id` prints a named test with `hermes shadow-trial results <id>`.
- `results-menu` shows numbered actions in an interactive terminal and runs the chosen one.
- `results-stats` prints the numbers behind the verdict with `--stats`.
- `results-missing` reports an unknown ID or an empty profile.
- `status-idle` reports that nothing is running.

## How to get to it (user POV)

- Run `hermes shadow-trial results`, `hermes shadow-trial results <id>`, or `hermes shadow-trial results <id> --stats` in a terminal.
- Run `hermes shadow-trial status`.
- Type `/shadow-trial results <id> --stats` in a Hermes chat (not driveable here).

## Driving it with shadow-verify

Preconditions:

- A fresh `$S launch`, and `$S doctor` passes.
- `$S seed result-worse` saved test `c09e` (Worse, 30 turns, judged by DeepSeek V3.2).

- **Empty profile first.** Before seeding, run `$S cli results-empty -- results`. Output is `No tests yet` with `exit=0`.
- **Newest result.** Run `$S cli results-newest -- results`. The transcript starts with `Won 3, lost 18, tied 9 of 30 turns · 61% cheaper per turn`, shows `[Stay on your current model]`, `Judged blind by DeepSeek V3.2.`, and ends with `The numbers: hermes shadow-trial results c09e --stats` and `exit=0`.
- **Result by ID.** Run `$S cli results-c09e -- results c09e`. The headline matches the newest result.
- **Interactive menu.** Run `$S pty start r -- results c09e`, then `$S pty wait r 'Choose \[[0-9]\]:'`. The screen lists `1  See the 18 it lost` and `2  Keep Hermes 4 405B`, with no switch action for a Worse result. Choose 1 with `$S pty send r 1 Enter`, then `$S pty wait r 'PTY-EXIT='` and `$S pty capture r results-lost-turns`. The capture lists `The 18 turns Hermes 4 70B lost`, numbered turns starting with `"Clean up the old backup folders on the NAS, keep the last 3"`, and `PTY-EXIT=0`. Then `$S pty stop r`.
- **Numbers.** Run `$S cli results-stats -- results c09e --stats`. The transcript must show the net win rate, interval, sign test, holds-up check, and judge self-agreement, with `exit=0`. A traceback is a FAIL.
- **Unknown ID.** Run `$S cli results-unknown -- results nope`. Output is `No tests yet` with `exit=0`.
- **Idle status.** Run `$S cli status -- status`. Output is `Shadow: nothing running. Shadow isn't reading your turns.`
- **Second view.** Run `$S db ids "SELECT id, json_extract(record,'$.state') FROM trial_records"`. One row, `c09e | finished`. Reading results does not change it.

## Gotchas

- `results <id> --stats` used to crash in a real Hermes install with `ModuleNotFoundError: No module named 'shadow'`, because `shadow/trial/cards.py` imported `shadow.trial.verdict` absolutely. PR #8 made it relative and added `tests/test_relative_imports.py`. Pytest and the Desktop harness both put a top-level `shadow` on `sys.path`, so only this journey sees a regression of that kind. A traceback here is a FAIL.
- `results <unknown-id>` says `No tests yet` even when other tests exist. Assert that text. Do not assume it means the profile is empty.
- Without a TTY, `results` prints the card and returns without a menu. Use `pty` for `results-menu`.
- Seeded fixtures carry fixture model IDs on provider `nous` with no credentials, so the menu's switch action (shown for Better results such as `result-better`) refuses with `[credentials_unknown]`. Driving a real switch needs a configured profile and is out of scope here.
- A seeded test whose `created_at` is older than 30 days is pruned on the next command. `seed` stamps the current time.
