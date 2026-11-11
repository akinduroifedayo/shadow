# Desktop record views

The profile source returns saved records through `Source`. `desktop_rpc` converts records at the page and turn boundaries. It never mutates records while rendering.

## TrialView

`trial_view(record)` preserves the trial record's fields and measurements. `current`, `trial`, and `judge.models` gain `name` from the shared model short-name function and `provider_name`, falling back to the saved provider identifier. Turns preserve their fields. A missing or null `at` leaves the row day blank and omits the date from side-by-side metadata.

Selected turns do not save the original message time or assistant answer. Their `context` contains only preceding messages. Desktop omits the original-answer fold when no `original_answer` is available.

The lifecycle table governs page selection, history, and active controls.

| Saved state | Page | History | Blocks another test | Delete |
| --- | --- | --- | --- | --- |
| prepared | hidden | hidden | no | no page control |
| queued | progress | visible | yes | no |
| running | progress | visible | yes | no |
| interrupted | progress | visible | yes | no |
| cancelled without totals | stopped | visible | no | yes |
| cancelled with totals | result | visible | no | yes |
| finished | result | visible | no | no page control |

The source owns persistence and trial execution. The page does not infer a verdict from a lifecycle state without saved totals.

The default view prefers active work, then the newest saved verdict, then the newest stopped test. Explicit history selections remain selectable. History labels include the time, judge, verdict when available, and test ID.

## SwitchView

`switch_view(raw, test)` accepts `SwitchRecord.to_dict()`. It rebuilds the exported `SwitchRecord` and uses `is_active` to include only `running`, `watch`, and `paused`. It calls `record.counts(side)` for current-epoch, attributed, finished-turn counts. The desktop does not duplicate the attribution rules.

The resulting dict preserves `id`, `test_id`, `mode`, `phase`, `step`, `step_turns`, and `failure_streak`. It adds these fields.

| Field | Source |
| --- | --- |
| new, old | Saved routes with display names added by `model_view` |
| counts | `{new: {turns, trouble}, old: {turns, trouble}}` from `record.counts` |
| step_progress | New-model count minus `step_start`, at least zero |
| last_answered | Model matching `last_response_model`, or null before an answer |
| started_at | First history event's `at`, or null without history |
| test | `{id, word}` from the originating saved totals, or null if unavailable |
| trip_text | Switch copy table's reason text for the latest structured trip, without chat commands or appended notices |
| signals_note | Switch copy table's keyed description of observed signals and its limit |
| history | Structured events for the existing `desktop.log.*` renderer, with unknown kinds omitted |

Fixture records already carry these display fields and retain their fixed snapshot copy. Real records use the switch engine's counts and reason copy. Start, watch start, resume, and rollback events use Desktop log keys. Start rows require the saved test's verdict and counts. Unknown history kinds do not prevent the controls from rendering.

Step rows for `step2` and `step3` come from `panel()['history']` (`kind`, `at`, `turns`, `manual`, `reason`), not from `step_turns`. A row is omitted when `turns` is missing. `desktop.log.step` is the automatic move. A manual move appends `desktop.log.manual`. `reason` is filled for `rollback` and `watch_trip` and null for every other public kind. The rollback log line uses that projected reason. Historical trip counts are saved on the raw event, and signal details still come from the current epoch through `switch.copy.fields`.

## Start consent

`StartCard` includes `estimate`, `judge_mode`, `judge_models`, and `consent`. The consent value is a SHA-256 digest of the complete rendered card before the digest is added. Start submits those four fields with `model` and the optional chosen `judge`.

The RPC compares the submitted card with current start facts. A mismatch returns `{page, notice: null, start_card}` without starting a test. The UI keeps the card open and displays the refreshed card for another explicit Start. Both sources recheck consent inside their execution boundary using the copy table supplied to the RPC. This prevents a change between the RPC check and the source write.

`ProfileSource` uses the trial selector, judge choices, and estimator for a preview. Preview saves no record and launches no controller. A confirmed Start calls the trial engine and controller launcher after checking isolation and active work. Stop records cancellation while preserving the controller's saved progress.

Registered RPC methods return `{error: {message, code?}}` for rejected requests. Messages come from the copy table. The `test_not_found` code lets the UI recover when a selected record disappears. Internal exception details stay out of the response.

## Action identity

SwitchPanel includes `identity: {switch_id, phase, step, epoch}`. Every switch control submits that exact identity. The epoch distinguishes separate pauses after a resume resets the step. Stop test submits `test_id`. The RPC and source reject identities that no longer match active saved work. Sources must check identity inside the same reservation or transaction as the action. Missing Stop identity is a read-only no-op at the RPC boundary for old clients. It cannot stop a newer test. Missing switch identity is always rejected, including when no switch is active.


## Stopped results and blind picks

`get_result` requires a cancelled test with at least 20 usable finished turns. `FixtureSource` calls `shadow.trial.verdict.finalize` when available. An isolated development server can inject a fixture-only finalizer. The plugin package never imports development helpers. Desktop unit tests stub saved totals to check the stopped-to-result page transition. Record integration tests use the trial package's actual finalizer.

`turn.reveal` requires pick `1`, `2`, or `same`. A null or missing pick is refused before revealing names or the judge. Once a pick is saved, another reveal returns the saved view without rewriting it. A persistent source must save only the first pick atomically. The fixture source uses an insert-if-absent write.

`settings.set` accepts `test_id` to keep the viewed historical result after changing web lookups. Trial fine print reads `web_turns`, with `web_lookup_turns` retained for older development fixtures. Both the stopped summary and its first button use the usable finished-turn count.

## Desktop identity and labels

The page identity includes both the SDK connection ID and profile name. A change to either increments the generation, clears the page and selected history, and invalidates old controls and responses. Older SDK shims without a connection atom retain profile-only behavior.

The sidebar entry registers immediately with the manifest name. Once a page loads, its label comes from `PageView.top.title`, rendered from `desktop.title`. The last successful label remains available during profile changes and backend failures. Its registration and store listener are disposed when the plugin unloads.
