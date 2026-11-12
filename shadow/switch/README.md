# Enable gradual switching after a runtime check

Run `/shadow doctor` in chat or `hermes shadow doctor` in the terminal.
Doctor checks permissions and model availability without changing your profile.
When a completed saved test exists, doctor also checks its exact route pair.

Gradual switching stays blocked until the exact pair passes its check. Fixture
tests do not authorize a pair. Choose two models configured in your Hermes profile
with usable credentials and run:

```
hermes shadow doctor --verify-pair <old-model> <new-model> --provider <provider>
```

Use the same arguments after `/shadow doctor` in chat. If you omit the pair,
Shadow uses the exact pair from the newest completed saved test. Without that test,
Shadow asks for both model names. Hermes model discovery validates both models
and credentials before confirmation or either call.

The check names the two models and waits for yes. It makes a public Hermes
completion on the old model, then sends the original question, the first reply,
and a follow-up question to the new model. The second answer must equal the
first answer. Hermes resolves each model's transport.

Public auxiliary request and response hooks must report the requested provider,
model, response model, base URL, API mode, and a normal stop reason. Each
completion must use exactly one physical request. Both models must share the
same base URL and API mode. A temporary auxiliary task separates the check's
observations from other traffic.

Public completion payloads do not expose sealed reasoning. If the source model's
profile settings require sealed evidence, the check fails before either
completion with `sealed_reasoning_unobservable`. This also applies when Hermes
reports reasoning support and the profile has no explicit setting. The check
never claims to have observed sealed reasoning.

The check proves reply handoff on the observed routes. It does not prove full
agent-history or reasoning compatibility. Switching back needs its own check.
Provider and model overrides require
`plugins.entries.shadow.llm.allow_provider_override` and
`plugins.entries.shadow.llm.allow_model_override`. A refused override reports
`llm_trust_denied`.

On a pass, Shadow saves the pair through `ctx.set_config` under
`plugins.entries.shadow.settings.verified_gradual_pairs` with `probe_passed: true`
after publishing a short evidence file under the plugin's data folder. The
evidence has no prompt text, reasoning blobs, or credentials. If the config
save fails, diagnostic evidence can remain, but it does not authorize a switch.

A saved test's route must still match that entry's provider, model, base URL, and
API mode in the same old-to-new order. The entry must record matching reasoning
settings. Changing the current model's reasoning settings requires a new proof.
Older entries without reasoning settings also require a new proof. Keep other
verified entries in the list.

The saved entry has this shape. Shadow writes it when the check passes. Do not
hand-edit `probe_passed` to true.

```yaml
plugins:
  entries:
    shadow:
      settings:
        verified_gradual_pairs:
          - old:
              provider: example-provider
              model: old-model
              base_url: https://example.invalid/v1
              api_mode: chat_completions
            new:
              provider: example-provider
              model: new-model
              base_url: https://example.invalid/v1
              api_mode: chat_completions
            probe_passed: true
            reasoning:
              old: null
              new: null
            sealed_reasoning_observed: false
            evidence: pair-evidence.json
```

`evidence` is the file name under the plugin data folder. It records the provider,
the two model names, the reply handoff, and that sealed reasoning was not
observable. It does not record credentials, prompts, or conversation text. Only
the saved config entry authorizes the pair.

Run doctor again, then `/shadow switch <test-id>`. A missing or mismatched
entry leaves gradual switching blocked. Cross-provider switching uses its
separate 20-turn watch and does not need this gradual-pair entry.

## Integration contracts

Trial must call `shadow.switch.is_active(ctx.shadow_switch.engine.record)` before
starting a test. The predicate accepts no saved record and returns true for
`running`, `watch`, and `paused`. Do not duplicate the phase names in trial.
Switch recognizes trial's `shadow-controller:<id>:<token>` user-message marker
and the legacy `[shadow:controller]` marker. Controllers can also call
`ctx.shadow_switch.engine.exclude_session(controller_session_id)` before their
first request.

Register trial before switch, as required by `shadow.register(ctx)`. Switch
composes the existing doctor handlers. It delegates status and undo to trial
when no switch exists or when a trial is active after an older switch ends.
A saved completed switch retains status and undo when trial is idle. Trial
must preserve these composed routes if it registers handlers later.

The proposal lookup returns a `Proposal` or `None`. Route dictionaries contain
only `provider`, `model`, `base_url`, and `api_mode`. Extra route metadata is
refused without starting a switch. Trial owns the eligibility card for models
whose provider matches but whose base URL or API mode differs. Those pairs
are unavailable, including with a runtime proof entry.

Switch keeps its records in the existing profile-local `switch.db`, alongside
trial's `data.db`. Hermes supports a filename argument to `plugin_db`. Switch
uses that API lazily to preserve its connection settings and saved records.
Registration and ordinary turns with no saved switch create no database or
data directory. The identity salt is stored in `switch.salt` on first use.
Shared storage and copy adapters live in `shadow.common`.

For a cross-provider chat move, use the provider-qualified `/model` command
printed below the notice. Default config changes apply to new chats.


## Panel notice delivery

`ctx.shadow_switch.panel()` returns pending `notices` as a list of objects with
`id` and rendered `text`. Polling does not consume notices. Render each notice
once per ID, then call `ctx.shadow_switch.action("acknowledge", switch_id=panel["id"],
notice_ids=[notice["id"]])`. Acknowledge only IDs that the user has received.
The action leaves other pending notices intact and ignores an older switch ID.
Desktop and TUI adapters can use this path without a bound gateway session.

While default config writes are pending, the list is empty so a panel does not
claim that a default was restored before the write succeeds. Pending notices
remain saved. Chat and CLI delivery or command responses can also consume them.
The existing advance, resume, undo, and stop actions return rendered notices.
Adapters must display that return value when they invoke those actions.

## Step transition records

Each `step2` and `step3` event in `Engine.record.notices` and
`ctx.shadow_switch.panel()["history"]` includes these saved fields.

| Field | Type | Meaning |
| --- | --- | --- |
| `turns` | integer | Finished new-model turns counted since the previous move, in the current resume epoch. Old-model turns do not count. A manual move can have zero. |
| `manual` | boolean | `true` for `Engine.advance()`, including the `advance` action. `false` for an automatic move at the turn threshold. |

Panel history contains `kind`, the event timestamp `at`, `turns`, `manual`,
and `reason`. `reason` is set only for rollback, watch-trip, and override
events. Panel history includes step, start, completion, rollback, resume, undo, stop, manual-choice,
and owner-override events. An override row also carries `headline`, `by`, and
its override `reason`. Unknown event kinds are omitted. The list follows
saved event order. Desktop reverses it for "What happened so far", newest first.
The fields are snapshots at transition time. They survive notice acknowledgement,
restart, and Resume. Desktop reads `panel()["history"]` to render step rows.
Older saved events have `null` for missing fields. Omit unknown details for
those events. The panel delivery list still contains only `id` and `text`.

## Switch lifecycle and recovery

Trial supplies `ctx.shadow_trial.active_record()`. Switch refuses a start or
resume with `trial.still_running`, including the active test ID, while that
method returns an active test. The isolated package loads that key from
`shadow/switch/fallback.json` when the shared copy loader is unavailable.
Trial still owns the reverse guard through `shadow.switch.is_active(record)`.
When trial registers a doctor handler first, trial owns shared setup checks.
Switch adds only `pair_doctor` output for the saved route pair.

Phase groups live in `shadow.switch.domain`. `WATCHING_PHASES` contains running
and watch. `ACTIVE_PHASES` also contains paused. `RESTORE_PHASES` contains paused,
undone, and stopped. `IDLE_PHASES` contains undone, manual, and stopped.
`CHAT_MOVE_PHASES` contains watch and paused. Call `is_active` for
the mutual exclusion guard instead of duplicating these names.

After done, undo, or Stop, a model command releases only its conversation from
retained routing. It preserves the saved switch phase and undo eligibility.
Pending default config writes do not suspend gradual conversation routing.
An unsent turn returns to the old model on pause, undo, or Stop. A sent turn
keeps its model until the turn ends. Repeated undo requests return current
status after their original notice has been delivered.

Unrelated slash commands do not write the switch record or retry config.
A failed config intent waits at least 30 seconds before another hook attempt
in that engine process. User commands and transition actions bypass that delay.
The intent includes the switch ID, resume epoch, and target. Undo or a new epoch
can attempt a different intent immediately. Restart resets the in-memory delay
and retries durable pending intent on the next session-start, turn-admission,
or explicit recovery call, including after a switch ends.

Default writers take a non-blocking advisory lock on `switch.config.lock` beside
the salt file. The lock covers both provider/model writes and the follow-up
record transaction. Another process can save undo intent while a write is in
flight. Its recovery call returns immediately if the lock is held. The holder
applies changed intent while it holds the lock. After finding no pending intent,
it releases both locks and checks again, with at most three lock acquisitions
per recovery call. Lock contention, throttling, and writer errors do not trigger
that retry. Session-start and turn-admission hooks retry pending intent in ended
phases too. If pending intent has already been cleared, the holder re-arms it
when the phase requires another target. This serializes cooperating Shadow
writers. Other Hermes config writers do not take this lock, and the two config
commands remain separate writes. On platforms without `fcntl`, the package
imports normally, but file-backed starts and resumes fail before changing the
record or config. Existing pending intent stays saved for recovery on a
supported platform.

Durable identity salts live only in the mode-0600 `switch.salt` file. New salts
are written and synced privately before atomic publication. Legacy database
salts migrate without changing existing hashes. Migration uses secure deletion
and checkpoints WAL after commit. A busy reader can defer that checkpoint until
a later transaction. Earlier database backups cannot be scrubbed by migration.
In-memory fixture stores keep their salt only in memory. Session enrollment
skips an event hashed before another process published its salt. A later event
uses the saved salt and enrolls normally.
