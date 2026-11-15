# Forget a saved test

A user deletes one saved test, or every saved test, from the profile. Shadow refuses to delete a running test, and everything about a deleted test (record, cancellation marker, controller row, second look, replay children) leaves storage.

## Sub-features

- `forget-one` deletes the named test and leaves the others.
- `forget-all` deletes every saved test.
- `forget-running-refused` refuses while the test is running. Not driveable here: a running test needs a live controller.

## How to get to it (user POV)

- Run `hermes shadow-trial forget <id>` in a terminal.
- Run `hermes shadow-trial forget all`.
- Type `/shadow-trial forget <id>` in a Hermes chat (not driveable here).

## Driving it with shadow-verify

Preconditions:

- A fresh `$S launch`, and `$S doctor` passes.
- `$S seed result-worse` and `$S seed result-better` saved tests `c09e` and `a3f2`.

- **Before.** Run `$S db before-forget "SELECT id FROM trial_records ORDER BY id"`. Rows are `a3f2` and `c09e`.
- **Forget one.** Run `$S cli forget-c09e -- forget c09e`. Output is `The stored test was deleted.` with `exit=0`.
- **Storage after one.** Run `$S db after-forget "SELECT id FROM trial_records ORDER BY id"`. Only `a3f2` remains.
- **User-visible after one.** Run `$S cli results-c09e-gone -- results c09e`. Output is `No tests yet`. Run `$S cli results-newest -- results`. The card is the `a3f2` result.
- **Forget all.** Run `$S cli forget-all -- forget all`. Output is `The stored test was deleted.`
- **Storage after all.** Run `$S db after-forget-all "SELECT (SELECT count(*) FROM trial_records), (SELECT count(*) FROM trial_cancellations), (SELECT count(*) FROM trial_controllers)"`. Output is `0 | 0 | 0`.

## Gotchas

- `forget <unknown-id>` also prints `The stored test was deleted.` even though nothing matched. The storage check, not the message, proves a deletion.
- `forget all` is literal: the word `all` deletes everything. Never seed real data into a verification profile.
