# Setup doctor

Doctor tells a user whether Shadow is ready in this profile: the Hermes version against `requires_hermes`, the active profile, usable models, a third-maker judge, and the two host capabilities, with the exact fix command for each gap. It changes nothing and calls no provider.

## Sub-features

- `doctor-version` reports the running Hermes version against `plugin.yaml` `requires_hermes`.
- `doctor-consent-missing` lists both capabilities as not granted with `Fix: hermes plugins enable shadow-trial`.
- `doctor-consent-granted` lists both capabilities as granted with no capability fix lines.
- `doctor-models` reports usable models and the third-maker judge, with `Fix: hermes model` when absent.
- `doctor-bare` runs the same report for `hermes shadow-trial` with no verb.
- `doctor-verify` and `doctor-verify-pair` run live model checks. Not driveable without credentials.

## How to get to it (user POV)

- Run `hermes shadow-trial doctor` in a terminal.
- Run `hermes shadow-trial` with no verb.
- Type `/shadow-trial doctor` in a Hermes chat (not driveable here).

## Driving it with shadow-verify

Preconditions:

- For `doctor-consent-missing`, a launch with `$S launch --no-grant`. For everything else, a normal `$S launch`.
- `$S doctor` has no FAIL lines.

- **Consent missing.** Launch with `--no-grant`, then run `$S cli doctor-no-consent -- doctor`. The transcript contains `llm.provider_override: not granted`, `llm.model_override: not granted`, and `Fix: hermes plugins enable shadow-trial`, and ends with `exit=0`.
- **Version.** In any doctor transcript, find `Hermes 0.23.1 meets plugin.yaml requires_hermes >=0.23.1.` (the version tracks the Hermes you point at).
- **Consent granted.** After a normal launch, run `$S cli doctor -- doctor`. The transcript contains `llm.provider_override: granted` and `llm.model_override: granted` and no `plugins.entries.shadow-trial.llm.allow_` lines.
- **Models.** The same transcript contains `Usable models: none confirmed`, `Third-maker judge: unavailable`, and `Fix: hermes model`. That is correct for an isolated home with no credentials.
- **Nothing changed.** The transcript contains `Nothing changed.`, and `$S files doctor-after` lists only `plugin-data/shadow-trial/data.db`, which Shadow's first load creates.
- **Bare command.** Run `$S cli bare --` with nothing after `--`. The transcript starts with the same verb list and version line as the doctor report.

## Gotchas

- The first `doctor` is Shadow's first load in the profile. It creates `plugin-data/shadow-trial/` and runs the legacy copy. Run the legacy storage journey before this one, or in its own launch.
- With a seeded saved test, doctor adds a gradual-switch line with an entry template for that test's model pair. That is expected and is not a failure.
- `doctor --verify` asks to confirm a live model call. Do not run it here: there are no credentials, and on a machine with credentials it would spend money.
