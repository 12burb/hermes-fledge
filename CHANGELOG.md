# Changelog

## 0.1.0 — 2026-11-05

First release, built for the Nous Research Hermes Agent Plugin Hackathon.

- Save gate on `skill_manage`: saves learned from a bad run are held in Hermes' skills pending queue
  (background review / curator / unattended), or go through Hermes' approval prompt (foreground).
- Deterministic run verdicts from hook facts: unresolved tool failures (with a repeat-failure bump), failed
  or interrupted turns, corrections in the next user message, `/undo`, `/rollback` restores, `/retry`,
  denied approvals.
- Probation: fledgling → fledged after clean runs; grounded after bad runs; bad-birth grounding when the user
  corrects the run a skill was just learned from; grounded agent-created skills archived recoverably.
- Hand-edit protection by fingerprint: autonomous rewrites become proposals; optional Hermes content lock.
- `/fledge` and `hermes fledge`: status, pending, diff, approve, reject, skill, protect, unprotect, release,
  forget, mode, doctor, scan, export.
- Watch mode for a dry run.

Validated against Hermes v0.22.7 before release (see VALIDATION.md), which changed:

- A compound command that prints its own `$?` (`./fetch.sh; echo "exit=$?"; cat out`) is judged by that
  status, not by the overall exit 0 Hermes reports for its last part.
- Terminal failures are judged per program: a later successful `rm tmp.txt` no longer "recovers" a failed
  `./fetch.sh`.
- A gated batch with several ops on one skill is attributed to its run (one lifecycle event per op).
- Denied approvals use the `session_id` Hermes sends; declining Fledge's own prompt is not a denial.
- Held batches keep the legacy default `name` so `/skills approve` can replay them.
- Corrections: curly apostrophes are recognized; "No, thanks." and other polite closers are not
  corrections; a bare "No." is a weak signal.
- Probation: a relapse or restore starts a fresh probation; the birth turn is not counted as a use; turns
  recorded before `mode off` never ground anything later.
- Autonomous deletes of hand-edited skills become proposals too.
- `/fledge doctor` reports archive and content lock separately (the lock needs Hermes 0.22.7).
