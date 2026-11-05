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
