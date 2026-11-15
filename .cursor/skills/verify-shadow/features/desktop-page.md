# Desktop page

In Hermes Desktop, Shadow adds a sidebar entry and a page with id and route `shadow-trial`. The page shows a running test's progress, a finished test's verdict with the numbers behind it, a blind pick-then-reveal per turn, the switch-anyway dialog, the live switch panel, and the start card for a new test. Here it is driven in the repo's dev harness: the real `desktop/plugin.js` and real `DesktopRpc` over `FixtureSource`, in a Hermes-like frame.

## Sub-features

- `desktop-states` renders empty, progress, every verdict kind, the switch panel, and the watch states.
- `desktop-verdict-dom` shows exactly the strings in the RPC payload (headline, facts, receipt, buttons, numbers rows).
- `desktop-blind-pick` keeps model names and the judge's choice hidden until the user picks, then reveals them and tallies agreement.
- `desktop-anyway` opens a confirm dialog for a warned switch, cancels cleanly, and logs an owner override on confirm.
- `desktop-start-card` picks a model, changes the judge, updates the cost line, and starts a compare.
- `desktop-narrow` fits the header and controls at 660 px wide.
- `desktop-in-app` is the page inside the real Hermes Desktop app. Not driveable here.

## How to get to it (user POV)

- Open **Shadow** in the Hermes Desktop sidebar (route `shadow-trial`). Not driveable here.
- In the dev harness, open `harness.html?s=<scenario>` on the URL `desktop up` prints, where `<scenario>` is a file name in `desktop/fixtures/`.

## Driving it with shadow-verify

Preconditions:

- `$S launch` has run (only for `RUN_ID` and evidence; the harness does not use the Hermes home).
- `node` 22+ and `google-chrome` are on `PATH`, and CDP port 9333 is free.

- **Start the harness.** Run `$S desktop up`. Output is `READY desktop harness http://127.0.0.1:<port>/harness.html?s=result`, and `$S doctor` shows the pid alive and the port serving.
- **Drive every state.** Run `$S desktop shoot`. `desktop/shoot.txt` ends with `all DOM checks passed` and `exit=0`.
- **Verdict.** `desktop/result-worse.png` shows `Won 3, lost 18, tied 9 of 30 turns · 61% cheaper per turn` and the `Stay on your current model` tag. `desktop/result-numbers.png` shows the numbers panel opened from **Show the numbers**.
- **Blind pick.** `desktop/result-judge3-picked.png` labels both answers `model hidden`, and `desktop/result-judge3-revealed.png` shows the same turn with `Hermes 4 70B, trying` and `Hermes 4 405B, yours`. The passing sweep also asserts that nothing leaks before reveal, that the reveal shows `The judge picked Answer 2` (below the fold in the screenshot), and the tally `You and the judge agreed on 2 of 3.` after the third pick.
- **Switch anyway.** `desktop/switch-anyway-confirm.png` shows the confirm dialog. The sweep asserts that the warning alone does not switch, that cancel closes the dialog, and that after confirming, the switch history contains `Owner override` (`desktop/switch-anyway-confirmed.png`).
- **Start card.** The sweep picks `nousresearch/hermes-4-70b-fp8-2026-08-14`, changes the judge to `moonshot/kimi-k2`, and asserts `Judged blind by Kimi K2, your pick.` and `About $2.05, capped at $5.00. The judge is about $0.55.` (`desktop/start-card-judge-picked.png`), then that **Compare** submits a start with compare intent and the page moves to progress.
- **Stop the harness.** Run `$S desktop down`, or let `$S cleanup` stop it.

## Gotchas

- This is the dev harness, not Hermes Desktop. The React and SDK shims in `desktop/dev/` stand in for the app, and data comes from `desktop/fixtures/`. Report results as "dev harness".
- The harness loads the inner package as top-level `shadow`, so plugin import bugs that only appear under Hermes's package name do not show here. Pair a Desktop change with the CLI journeys.
- `shoot.mjs` runs the whole sweep, about 20 seconds. It stops at the first exception and lists DOM mismatches as `FAIL <n>` lines in `desktop/shoot.txt`.
- `shoot.mjs` resets fixtures with `POST /reset` before each scenario. Do not run it against a harness another run is using.
