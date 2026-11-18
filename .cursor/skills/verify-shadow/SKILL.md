---
name: verify-shadow
description: Drive Shadow, the shadow-trial Hermes plugin, the way a user does and capture proof. Installs the repo into a throwaway HERMES_HOME with the real `hermes plugins install`, drives the real `hermes shadow-trial` CLI (plain and in a PTY for its menus), seeds saved tests from desktop fixtures, and renders the Desktop page in the dev harness over headless Chrome. Use it to verify a change to Shadow's commands, saved results, storage, or Desktop page before calling it done, and whenever a claim about Shadow's behavior needs evidence instead of a unit test.
---

# Verify Shadow

Shadow is a Hermes plugin (manifest name `shadow-trial`, see `plugin.yaml`). It replays saved Hermes turns on a candidate model, judges the answers blind, and offers a model switch with undo. Users touch it through:

| Surface | Driveable here | How |
| --- | --- | --- |
| `hermes shadow-trial <verb>` terminal CLI | Yes, primary | `scripts/shadow-verify cli` and `pty` |
| Hermes Desktop page, id and route `shadow-trial` (`desktop/plugin.js`) | Partly | Dev harness: `desktop/dev/serve.py` + `desktop/dev/shoot.mjs`, the real page and real `DesktopRpc` over fixture data |
| `/shadow-trial <verb>` in a Hermes chat | No, unless the Hermes install has a working chat runtime and a model | See Gotchas |
| The real Hermes Desktop app, a live `shadow-demo` profile, anything that calls a model | No | macOS app plus provider credentials; see "Not driveable" |

Run the installed CLI and fixture Desktop recipes with `scripts/shadow-verify` (path relative to this skill). The feature map in [`features/README.md`](features/README.md) is the maintained list of what to drive.

## Environment

| Variable | Required | Points to |
| --- | --- | --- |
| `SHADOW_HERMES_BIN` | Unless `hermes` is on `PATH` | A Hermes 0.23.1+ `hermes` executable. A git checkout's venv works: `<hermes-checkout-venv>/bin/hermes`. |
| `SHADOW_VERIFY_PYTHON` | No, default `python3` | Python 3.10+ used for seeding, read-only SQL, and `desktop/dev/serve.py`. Needs only the standard library. |
| `SHADOW_VERIFY_EVIDENCE` | No, default `<repo>/runs/verify-shadow` | Where proof lives. `runs/` is git-ignored. Each run gets `<root>/<RUN_ID>/`. |
| `SHADOW_VERIFY_CACHE` | No, default inside scratch | An `XDG_CACHE_HOME` to reuse between runs so the plugin install's dependency step is faster. Holds no credentials. |
| `RUN_ID` | After launch | Printed by `launch` as `export RUN_ID=...`. Every other command needs it. |
| `TMPDIR` | No | Scratch parent. Scratch is `$TMPDIR/shadow-verify-<RUN_ID>.XXXXXX/` with `hermes-home/` and `user-home/`. |

Also needed on `PATH`: `git` (Hermes installs plugins with git), `tmux` (PTY), `curl`, and for the Desktop harness `node` 22+ and `google-chrome`.

Isolation is the point of the helper. Every Hermes call runs under `env -i` with only `PATH`, `LANG`, `TERM`, `HERMES_HOME=<scratch>/hermes-home`, and `HOME=<scratch>/user-home`. No provider key from your shell reaches Hermes, and host-wide credential files under the real home (Codex, Claude Code, gh) are invisible, so doctor reports "Usable models: none confirmed". That is expected and is what keeps every run free of model calls. Never point `HERMES_HOME` at a real profile, and never read `.env`, `auth.json`, or credential files to "fix" a journey.

## Launch

```bash
export SHADOW_HERMES_BIN=<path to hermes>      # if hermes is not on PATH
S=.cursor/skills/verify-shadow/scripts/shadow-verify
$S launch                 # committed HEAD; add --working-tree for uncommitted edits, --no-grant to leave consent unanswered
export RUN_ID=<value it printed>
```

What launch does, in order, all inside a fresh scratch `HERMES_HOME`:

1. Writes a minimal `config.yaml` that answers the first-run telemetry offer (off), so no prompt blocks later calls.
2. Runs the real user install: `hermes plugins install file://<repo> --enable`. Hermes clones the repo's committed `HEAD`, so uncommitted edits are not installed unless you pass `--working-tree`, which copies tracked and untracked non-ignored files over the clone (verification scaffolding, recorded as `OVERLAY=working-tree` in `run.env`).
3. Answers the capability consent prompt in a tmux PTY with `y`, the way a user does in an interactive terminal (`hermes plugins enable shadow-trial`). A non-interactive install fails closed and grants nothing.
4. Runs `hermes plugins list --plain --no-bundled`.

Ready means: launch prints `READY shadow-trial installed in an isolated HERMES_HOME` and exits 0, and `config.yaml` carries `allow_model_override: true` (launch checks this and dies otherwise). The full transcript is in `<evidence>/launch.txt`. Launch takes about 25 seconds, most of it the install's dependency step. Launch does not load Shadow's code, so `plugin-data/shadow-trial/` does not exist yet. The legacy storage journey depends on that.

There is no server to keep alive for the CLI: each `cli` call is one short `hermes` process. Teardown is `cleanup`.

## Doctor

```bash
$S doctor
```

Read-only. It answers "is this instance worth driving?" and writes `<evidence>/doctor.txt`. FAIL lines make it exit non-zero:

- the scratch `HERMES_HOME` exists and is not the real `~/.hermes`
- the hermes executable runs, and its version line
- `plugins/shadow-trial/plugin.yaml` says `name: shadow-trial`, the plugin is enabled, and consent is granted (or deliberately ungranted after `--no-grant`)
- the installed revision matches repo `HEAD` (WARN if not, and WARN if the repo has uncommitted plugin changes that were not overlaid)
- when the Desktop harness is up: its pid is alive and its port serves `/harness.html`
- whether Shadow has loaded yet (trial storage exists or not)

Run it first whenever anything looks off. It never runs a Shadow command, because loading Shadow creates storage and triggers the one-time legacy copy. For Shadow's own setup report, drive the setup-doctor feature.

## Drive

Terminal, non-interactive (stdin is `/dev/null`, so result cards print without menus):

```bash
$S cli doctor -- doctor
$S cli results-newest -- results
$S cli results-stats -- results c09e --stats
$S cli status -- status
$S cli forget -- forget c09e
```

Terminal, interactive (a tmux PTY, so `results` shows its numbered menu and reads a choice):

```bash
$S pty start r -- results c09e
$S pty wait r 'Choose \[[0-9]\]:'
$S pty send r 1 Enter
$S pty wait r 'PTY-EXIT='
$S pty capture r results-lost-turns
$S pty stop r
```

Seed data. A fresh profile has no saved tests, and making a real one needs 20+ saved Hermes turns, a live isolation check, and paid model calls. Instead, `seed` saves the `tests` of a Desktop fixture through Shadow's own `Store.save`, as a finished test would be written. Fixture names are the files in `desktop/fixtures/`. Useful ones: `result-worse` (test `c09e`, Worse, "Stay on your current model"), `result-better` (`a3f2`), `result-too-close` (`d4a8`), `result-not-enough` (`e5f1`), `result` (`b71c` and `c09e`).

```bash
$S seed result-worse              # into plugin-data/shadow-trial/data.db
$S seed result-worse --legacy     # into plugin-data/shadow/data.db, before Shadow first loads
```

Side-effect checks (read-only):

```bash
$S db ids "SELECT id, json_extract(record,'$.state') FROM trial_records ORDER BY id"
$S db legacy-ids "SELECT id FROM trial_records" --legacy
$S files after-load               # plugin-data files with sha256 prefixes and sizes
```

Desktop page in the dev harness (the real `desktop/plugin.js` and real `DesktopRpc` over `FixtureSource`, in a Hermes-like frame):

```bash
$S desktop up        # serve.py on a free port; prints the harness URL
$S desktop shoot     # node desktop/dev/shoot.mjs: renders every scenario, clicks through, asserts DOM == RPC payload
$S desktop down
```

Stable handles in the page, used by `shoot.mjs`: the root `.shd[data-mode]`, `[data-action="judge3"]`, `[data-action="pick-1"]`, `[data-action="pick-2"]`, `[data-action="reveal"]`, `[data-action="anyway"]`, `[data-action="confirm-override"]`, `[data-action="cancel-override"]`, `[data-action="try"]`, `[data-action="compare"]`, `[data-action="judge-change"]`, `[data-turn="t01"]`, `[data-model="<model id>"]`, `[data-judge="<model id>"]`, and the status bar slot `#status-slot`. Scenarios are fixture names passed as `harness.html?s=<scenario>`. RPC is `POST /rpc/<scenario>` with `{"method": "plugin.shadow-trial.<name>", "params": {...}}`, and `POST /reset` restores fixtures.

## Evidence

Everything lands in `$SHADOW_VERIFY_EVIDENCE/<RUN_ID>/` (default `<repo>/runs/verify-shadow/<RUN_ID>/`):

| Path | Contents |
| --- | --- |
| `run.env` | Run facts: scratch path, hermes path, installed revision, overlay, Desktop pid and port. No secrets. |
| `launch.txt` | Hermes version, install output, the PTY consent screen, the plugin list. |
| `doctor.txt` | The last helper doctor report. |
| `cli/NNN-<slug>.txt` | Each CLI call: the exact command, combined stdout and stderr, and `exit=`. PTY captures are `cli/NNN-pty-<slug>.txt`. |
| `db/NNN-<slug>.txt` | Seeds, read-only SQL results, and file hash listings. |
| `desktop/*.png`, `desktop/shoot.txt`, `desktop/serve.log` | Harness screenshots per state and the DOM check verdict. |
| `cleanup.txt` | What cleanup stopped and removed. |

Proof standards:

- Drive the real user path: `hermes shadow-trial ...` through the installed plugin, not `SwitchPlugin`/`TrialPlugin` objects or test helpers. Seeding is a precondition, never the behavior under test.
- Capture the action and the resulting state: the CLI transcript plus a second, read-only view (`db`, `files`, or a follow-up `results`/`status`).
- Check side effects: rows removed by `forget`, files created by the legacy copy, and files that must stay byte-identical (sha256 before and after).
- Shadow's CLI prints refusals as text and still exits 0 (for example "No tests yet" or "[credentials_unknown] Nothing was called."). Assert on the text, not only `exit=`. An `exit=1` with a Python traceback is a crash, which is a failure.
- Use the Desktop fixture source and fixture finalizer for UI checks. The preview check (`scripts/check-preview.py`) runs the real export wrapper, selection and RPC handler on synthetic export lines and injected subprocess failures. It does not measure live export latency; the native preview recipe does.
- Record which entry point you used. A journey proven only through the dev harness is not proof of the real Desktop app.

## Cleanup

```bash
$S cleanup
```

Stops only what this run started: the Desktop server's process group (started with `setsid`), this run's tmux server (`tmux -L shadow-verify-<RUN_ID>`), and any process whose environment has `HERMES_HOME=<this run's scratch>/hermes-home`. Hermes can leave helpers behind, such as a session `dbus-daemon` from the install or an idle-exit gateway. Then it deletes the scratch directory, refusing any path that does not match `shadow-verify-<RUN_ID>.*`. It never kills by process name, so other agents' `serve.py` or Hermes processes are safe. Evidence is never deleted: cleanup ends by listing the surviving evidence files. Run cleanup after every failed attempt too.

## Not driveable from a Linux box

These need a Mac with Hermes Desktop, a real profile, or provider credentials. Report them as not verified rather than faking them:

- The real Hermes Desktop app (sidebar entry, page load inside Desktop, profile switching). The dev harness uses shims in `desktop/dev/` for React and the plugin SDK.
- `python3 acceptance/run.py --mac` and the live `shadow-demo` profile (acceptance checks 6, 8, 10, 11 stay `SKIP(needs-mac)` elsewhere).
- `doctor --verify` and `doctor --verify-pair`: each makes a live model call.
- `try` or `compare` through Start, `cancel` of a running test, `switch` and `undo` of a real switch: all need configured models with credentials, and a test needs paid calls.
- `/shadow-trial` in chat: needs a working Hermes chat runtime with a model configured.

## Helpers

- `scripts/shadow-verify` drives the installed CLI and the Desktop fixture. `$S help` lists its subcommands. Exit codes: 0 ok, 2 usage or precondition error, otherwise the failing step's code.
- `python3 .cursor/skills/verify-shadow/scripts/check-preview.py` checks the preview's consent card, its redaction of kept history, and its explicit reason when history cannot be read, without Hermes credentials. See [Trial preview](features/trial-preview.md).

## Maintain this skill

1. Compare the [feature map](features/README.md) with the commands and Desktop actions in the current source. Add a recipe for any new journey.
2. Use a fresh run and run doctor before driving a journey. Check its visible result and any storage effect. Repeat doctor after unexpected behavior.
3. Separate wrong instructions, broken verification tools, and product regressions. Fix the first two here. Report product regressions without rewriting expectations to match them.
4. Clean up, check that the evidence remains, and record which journeys passed, failed, or could not run. Report native Desktop separately from fixture Desktop. Keep the receipts out of Git.

## Gotchas

- Shadow loads, creates `plugin-data/shadow-trial/`, and runs the one-time legacy copy on its first command in a profile, including `doctor`. Seed `--legacy` before any `cli` call. `seed --legacy` refuses once trial storage exists.
- `hermes plugins install file://<repo>` installs committed `HEAD`. Commit first or launch with `--working-tree`. Doctor warns when they differ.
- `desktop/dev/shoot.mjs` uses a fixed CDP port, 9333. Do not run two `desktop shoot` calls at once on one machine.
- `desktop/dev/serve.py` puts the inner package on `sys.path` as top-level `shadow`. Real Hermes loads the plugin under another package name, so an absolute `import shadow...` inside the plugin works in the harness and in pytest but breaks in a real install. The CLI journey catches that class of bug. The harness cannot.
- Every Shadow terminal reply goes to stdout with exit 0, refusals included.
- Without a configured current model, `try` and `switch <id>` refuse with "Hermes could not confirm usable credentials for  / ." (blank provider and model). That is the no-credentials state of this harness, not a pass.
- Hermes may log a warning about the linked SQLite version to `<scratch>/hermes-home/logs/errors.log`. It comes from Hermes, not Shadow, and does not affect these journeys.
