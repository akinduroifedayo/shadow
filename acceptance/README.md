# Acceptance harness

`python3 acceptance/run.py` runs twelve checks and prints one row each with the check ID, status, and evidence. The process exits non-zero when any row is FAIL. SKIP does not fail the process.

Checks 1, 2, 3, 4, 5, 7, 9, and 12 run against the plugin. Checks 6, 8, 10, and 11 need a Hermes profile on a Mac. In the default run those four stay `SKIP(needs-mac)` and never PASS. Their fixture portions still run, and a fixture failure is FAIL. `python3 acceptance/run.py --mac` creates an empty throwaway profile named `shadow-demo` under a temporary `HERMES_HOME`, prints exactly what it checked, and does not call a provider. It launches `hermes -p shadow-demo shadow doctor` only on macOS when `hermes` is on `PATH`.

The harness does not read `~/.hermes/.env`, `~/.hermes/auth.json`, `~/.codex/auth.json`, or `~/.claude/.credentials.json`. It does not sleep, and it does not send live provider traffic.
