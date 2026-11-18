# Prepare a trial preview

After you choose a model, Shadow builds a preview with eligible turns, recipients, the judge, and a cost estimate. A preview does not start a replay. If Shadow cannot read your recent history, the preview says so and nothing is sent or started. Setup, turn count, or pricing can block the start after the preview loads.

## Sub-features

- `preview-ready` returns the consent card without starting a trial.
- `preview-redaction` removes secrets from the history Shadow keeps. Shadow exports recent sessions, keeps only the fields selection reads (session id, conversation, source, working directory; message role, text, tool calls and active flags), and force-redacts those with Hermes's `agent.redact.redact_sensitive_text`. Tool outputs, titles, prompts and other metadata are dropped before anything is retained. Without that Hermes function Shadow reads no history.
- `preview-history-error` returns "Shadow could not read your recent history…" (`trial.history_unavailable`) when the export times out, exits nonzero, or prints malformed output. No trial is created and no provider is called.
- `preview-native-large-history` opens the real Desktop page against a representative synthetic history and confirms the card arrives well inside the export deadline.

## How to get to it (user POV)

Open **Shadow** in Hermes Desktop, select **Try a model**, then choose a different model. Do not press **Try** or **Compare** to verify the preview alone.

## Driving it

Preconditions: synthetic data only, no live profile.

- **Boundary check:** run `python3 .cursor/skills/verify-shadow/scripts/check-preview.py` from the repository root. Expect four `PASS` lines: the control (card, planted secrets removed) and the timeout, nonzero-exit and malformed cases (named reason, no card, no queued trial). Capture the output with the exit code.
- **Desktop fixture:** run the [Desktop fixture journey](desktop-page.md). Check that the preview shows the recipients and estimate before the fixture start is confirmed.
- **Native large history:** in a disposable `HERMES_HOME` (never a working profile), seed synthetic sessions through Hermes's public persistence interface, install the committed plugin, and launch the installed Hermes Desktop isolated (`HERMES_HOME`, `HERMES_DESKTOP_USER_DATA_DIR` and `HERMES_DESKTOP_ISOLATED_BACKEND=1` pointing at scratch, plus `--remote-debugging-port`). Choose a model and record the history size, the loaded plugin revision, the time to the card or named reason, and the trial storage before and after. Leave replay disabled.

## Gotchas

- `check-preview.py` replaces Hermes's redaction with a canary masker. It proves which fields reach selection and the failure handling. It does not prove Hermes's own patterns catch a given secret format.
- A missing replay isolation check and too few eligible turns are separate blocks from unreadable history. Keep their outcomes separate.
- The [statechart](../../../../docs/statechart/README.md) models the history block as an explicit outcome. Rerun TLC when the preview's outcomes change.
