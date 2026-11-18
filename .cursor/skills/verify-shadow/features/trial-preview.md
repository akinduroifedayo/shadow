# Prepare a trial preview

After you choose a model, Shadow builds a preview with eligible turns, recipients, the judge, and a cost estimate. A preview does not start a replay. If Shadow cannot read your recent history, the preview says so and nothing is sent or started. Setup, turn count, or pricing can block the start after the preview loads.

## Sub-features

- `preview-ready` returns the consent card without starting a trial.
- `preview-redaction` removes secrets from the history Shadow keeps. Shadow exports recent sessions, keeps only the fields selection reads (session id, conversation, source, working directory; message role, text, tool calls and active flags), and force-redacts those with Hermes's `agent.redact.redact_sensitive_text`. Tool outputs, titles, prompts and other metadata are dropped before anything is retained. Without that Hermes function Shadow reads no history.
- `preview-history-error` returns "Shadow could not read your recent history…" (`trial.history_unavailable`) when the export times out, exits nonzero, or prints malformed output. No trial is created and no provider is called.
- `preview-native-large-history` opens the real Desktop page against a representative synthetic history and confirms the card arrives well inside Desktop's 30-second request deadline (`request timed out after 30s: plugin.shadow-trial.start.card`), which is tighter than the 60-second export deadline.

## How to get to it (user POV)

Open **Shadow** in Hermes Desktop, select **Try a model**, then choose a different model. Do not press **Try** or **Compare** to verify the preview alone.

## Driving it

Preconditions: synthetic data only, no live profile.

- **Boundary check:** run `python3 .cursor/skills/verify-shadow/scripts/check-preview.py` from the repository root. Expect four `PASS` lines: the control (card, planted secrets removed) and the timeout, nonzero-exit and malformed cases (named reason, no card, no queued trial). Capture the output with the exit code.
- **Desktop fixture:** run the [Desktop fixture journey](desktop-page.md). Check that the preview shows the recipients and estimate before the fixture start is confirmed.
- **Native large history:** in a disposable `HERMES_HOME` (never a working profile), seed synthetic sessions through Hermes's public persistence interface (a few hundred sessions and tens of thousands of messages reproduce the original timeout), install the committed plugin, and launch the installed Hermes Desktop isolated: `HERMES_HOME`, `HERMES_DESKTOP_USER_DATA_DIR`, `HOME` and `HERMES_DESKTOP_ISOLATED_BACKEND=1` pointing at scratch, plus `--remote-debugging-port`. If Desktop's own runtime bootstrap fails, point it at an existing Hermes checkout with `HERMES_DESKTOP_HERMES_ROOT` and `HERMES_DESKTOP_PYTHON`, and put a `hermes` wrapper for that checkout first on `PATH`, because Shadow's export calls `hermes` from `PATH`. Report that route as native UI with an explicit backend, not as Desktop-managed setup. Dismiss first-run provider setup with **I'll choose a provider later**, then turn on the Shadow Desktop plugin (Settings > Plugins, or the same `hermes.desktop.pluginDecisions.v2` local-storage decision) and reload. Choose a model and record the history size, the loaded plugin revision, the time to the card or named reason, and the row counts of every `trial_*` table before and after. Leave replay disabled. For the blocked case, make the export fail in a separate home and expect the named reason within a second.

## Gotchas

- `check-preview.py` replaces Hermes's redaction with a canary masker. It proves which fields reach selection and the failure handling. It does not prove Hermes's own patterns catch a given secret format.
- Keep the scratch home outside any `profiles/` folder of a real Hermes home. Hermes treats such a path as a named profile, and the gateway refuses it with `profile_parked` before Shadow loads.
- The package's Desktop half is off until enabled. A missing **Shadow** sidebar entry on a fresh home is that switch, not a load failure.
- A missing replay isolation check and too few eligible turns are separate blocks from unreadable history. Keep their outcomes separate.
- The [statechart](../../../../docs/statechart/README.md) models the history block as an explicit outcome. Rerun TLC when the preview's outcomes change.
