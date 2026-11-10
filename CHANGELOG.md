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

- A compound command that reports its own parts (a labelled `$?` echo, or which of its fixed `if`/`else` or
  `||` echo branches ran) is judged by that, not by the overall exit 0 Hermes reports for its last part.
  Predicate commands (`grep`, `test`, `diff`…) are never counted.
- Terminal failures are judged per program: a later successful `rm tmp.txt` no longer "recovers" a failed
  `./fetch.sh`.
- A gated batch with several ops on one skill is attributed to its run (one lifecycle event per op).
- Denied approvals use the `session_id` Hermes sends; declining Fledge's own prompt is not a denial.
- Held batches keep the legacy default `name` so `/skills approve` can replay them.
- Corrections: curly apostrophes are recognized; "No, thanks." and other polite closers are not
  corrections; a bare "No." is a weak signal, and no signal at all when it answers a question the agent
  just asked (read from `pre_llm_call`'s `conversation_history`).
- Probation: a relapse or restore starts a fresh probation; the birth turn is not counted as a use; turns
  recorded before `mode off` never ground anything later.
- Autonomous deletes of hand-edited skills become proposals too.
- Fails open more carefully: a write already staged in Hermes' review queue stays held even if Fledge's own
  database then fails (it was being saved *and* queued); an agent write Fledge missed recording is recognized
  from Hermes' `last_patched_at` stamp instead of being mistaken for a hand edit.
- A held write Hermes' queue failed to store (`stage_write` returns a record even when its disk write fails)
  is confirmed with `get_pending`; if it is missing, the skill is saved on probation with a notice instead of
  being silently dropped. Hand-edited skills are still never overwritten.
- A skill deleted before its use is judged is not re-created in the ledger; `/fledge reject` logs `rejected`;
  `/fledge diff|approve|reject` accept any id in Hermes' queue.
- `/fledge doctor` reports archive and content lock separately (the lock needs Hermes 0.22.7).

Changed after the first catalog review (2026-11-10):

- One Hermes process can serve several profiles. The ledger now keeps one database connection per profile
  home instead of one for the life of the process, and the settings cache is per profile, so a profile
  never reads or writes another profile's holds, probation counts or hand-edit flags.
- Storage goes only through `plugins.plugin_storage.plugin_db`. The fallback that built a path from the
  `HERMES_HOME` environment variable is gone: without Hermes' plugin storage there is no database, and
  the gate lets the write through.
