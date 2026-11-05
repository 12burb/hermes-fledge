# 🪶 Fledge

**Skills have to earn their wings.**

Hermes learns from its own work: after a task it writes the procedure down as a skill and reuses it next time. The catch is who grades the work. In [Kilo's read of 1,300+ r/openclaw comments](https://kilo.ai/openclaw/vs-hermes), the first thing listed under what people dislike about Hermes is that self-evaluation always passes, quoting a user: *"It always thinks it did a good job. ALWAYS."* A skill learned from a run that quietly went wrong carries the mistake forward, and the next "self-improvement" can overwrite the skill you spent an evening tuning.

Fledge takes the grading away from the agent. Before Hermes saves or rewrites a skill, Fledge looks at what actually happened in the run it learned from. No model grades the model.

| | What Fledge does |
|---|---|
| **Save gate** | A skill learned from a run that went wrong is **held** in Hermes' own review queue instead of going live. You see why, then keep it or drop it. |
| **Probation** | New and rewritten skills start as **fledglings**. Clean real runs graduate them; runs that keep going wrong **ground** them (archived, recoverable). A save from a run you corrected right after is grounded at once. |
| **Hand-edit protection** | Skills you edited by hand stop being overwritten. Autonomous rewrites or deletes of them become **proposals** you approve or reject. |

## How it decides

Fledge only uses facts Hermes reports through plugin hooks:

| Evidence | Points |
|---|---|
| The turn ended in an error | 3 |
| The turn was interrupted | 2 |
| Your next message corrects the agent ("that's wrong", "not what I asked", "still broken"…) | 3 (a softer "try again" / "not quite", or a bare "No.", is 1) |
| You ran `/undo` or restored a `/rollback` right after (`/retry`: 2) | 3 |
| A task tool was still failing when the turn ended (terminal, code, file writes, browser, MCP; terminal is judged per program, so a later `rm tmp.txt` does not "recover" a failed `./fetch.sh`) | 2 each |
| …and it failed two or more times in a row without recovering | +1 |
| Any other tool still failing at the end | 1 each (tool failures max 4 in total) |
| You denied an action at an approval prompt | 1 each, max 2 |

An error the agent recovered from is normal work and costs nothing, and a single failing command at the end (a `grep` with no matches) is not enough on its own. A run scoring **3 or more is bad** (`strictness`: lenient 4, standard 3, strict 2). Zero with a clean finish is clean; anything between is unclear and neither helps nor hurts on probation.

**Who is writing matters.** Hermes saves most skills from its background review, a forked agent that runs after your turn. Fledge recognizes it and judges the save by the turn it is reviewing:

- **Background review / curator** saving after a bad run → held in the review queue (nobody is there to ask).
- **Foreground turn** saving mid-task after errors → Hermes' approval prompt asks you (`foreground_bad_run: approve`), or it is queued (`stage`), or let through (`allow`). Cron, kanban and API turns always queue.
- **Your `/fledge approve` or `/skills approve`** → saved as you decided. It goes on probation like any new skill, but the run it came from is never held against it.

## What you see

When Fledge holds something, the notice lands in your own chat at the start of your next turn:

```
🪶 Fledge: held skill 'pull-water-results': it was learned from a run that went wrong
(terminal failed 3 times in a row and never recovered: exit 1: HTTP 404 Not Found).
Review: /fledge diff 3fa9c1e2, then /fledge approve 3fa9c1e2 or /fledge reject 3fa9c1e2.
```

`/fledge` gives the overview:

```
Fledge (enforce, standard) — 12 fledged · 3 on probation · 1 grounded · 2 hand-tuned
Held for your review (/fledge pending):
  3fa9c1e2  'pull-water-results' — terminal failed 3 times in a row and never recovered (2m ago)
On probation:
  csv-cleaner — 2 clean / 0 bad of 3 needed
Grounded:
  scrape-dnr — 2 of its 3 runs on probation went wrong (archived: hermes curator restore scrape-dnr)
Hand-tuned (autonomous rewrites become proposals): smart-home-lights, release-notes
```

Holds live in Hermes' own pending queue, so `/skills pending` in the CLI lists them too (tagged `[fledge]`). `/fledge pending | diff | approve | reject` work from every surface, including Telegram and Discord, where `/skills` is CLI-only unless `skills.write_approval` is on.

## Install

Requires Hermes **0.22.7 or newer**.

```bash
hermes plugins install <owner>/hermes-fledge --enable
hermes fledge scan      # optional: fingerprint existing skills so later hand edits are recognized
```

Start a new session (or restart the gateway). `/fledge doctor` shows which Hermes features Fledge can reach.

Fledge must run **in-process** (the default, `plugins.isolation: in_process`). In the out-of-process plugin host it cannot see which agent is writing, so it treats every save as autonomous (held, never asked) and its chat notices are off. `/fledge doctor` says so.

Want to see what it would do before it does anything? `/fledge mode watch` records every decision without blocking; `/fledge mode enforce` turns it on.

## Settings

`plugins.entries.fledge.settings` (also the plugin's page in Desktop → Settings → Plugins):

| Key | Default | Meaning |
|---|---|---|
| `mode` | `enforce` | `enforce` holds bad saves · `watch` only records · `off` |
| `strictness` | `standard` | bad-run threshold 4 / 3 / 2 (`lenient` / `standard` / `strict`) |
| `graduate_after` | `3` | clean runs a new skill needs (plus one per bad run) |
| `ground_after` | `2` | bad runs on probation that ground a skill |
| `auto_archive` | `true` | archive grounded skills Hermes created on its own (skills you asked for are only reported) |
| `protect_hand_edits` | `true` | turn autonomous rewrites and deletes of hand-edited skills into proposals |
| `on_hand_edit` | `protect` | `lock` also sets Hermes' content lock (`hermes curator lock`), which stops every agent edit |
| `foreground_bad_run` | `approve` | foreground save after errors: `approve` (ask) · `stage` · `allow` |
| `notify` | `true` | chat notices |
| `settle_hours` | `6` | a run with no follow-up message is judged after this long |

## Commands

| Slash | CLI | |
|---|---|---|
| `/fledge` | `hermes fledge status` | overview |
| `/fledge pending` | `hermes fledge pending` | writes Fledge is holding |
| `/fledge diff ID` · `approve ID` · `reject ID` | same | review a hold (only Fledge's own holds) |
| `/fledge skill NAME` | `hermes fledge skill NAME` | track record and why |
| `/fledge protect NAME` · `unprotect NAME` | same | mark or unmark as hand-tuned |
| `/fledge release NAME` | same | take off probation |
| `/fledge forget NAME` | same | drop Fledge's record |
| `/fledge mode enforce\|watch\|off` | `hermes fledge mode …` | switch mode |
| `/fledge doctor` | `hermes fledge doctor` | reachable Hermes features |
| | `hermes fledge scan` | fingerprint existing skills |
| | `hermes fledge export` | everything as JSON |

## How it plugs into Hermes

Nine hooks, no tools, no patches to Hermes code:

- `pre_llm_call`, `post_tool_call`, `on_session_end`, `on_session_finalize`, `on_session_reset`, `pre_command`, `post_approval_response` — record what happened in each foreground turn (tool statuses, turn outcome, your follow-up, `/undo`, denials). Turns of the background-review and side-question forks are ignored.
- `pre_tool_call` on `skill_manage` — the save gate. It returns Hermes' documented `block` (held) or `approve` (ask you) directive. On its own errors it **fails open**: a Fledge exception never stops Hermes from saving skills. (Hermes itself blocks the tool call if any `pre_tool_call` callback hangs past `plugins.hook_callback_timeout`, 30 s by default; Fledge's gate does a few local SQLite reads and returns in milliseconds.)
- `on_skill_lifecycle` — writes that landed (start probation), skill loads (count real runs), archive/restore.

Where no public plugin seam exists yet, Fledge calls the same functions Hermes' own commands use, all in [`bridge.py`](bridge.py) and each guarded so a moved function disables one feature instead of breaking the agent: the skill write origin (`tools.skill_provenance`), the pending queue (`tools.write_approval.stage_write`, the backend of `/skills pending`), and curator actions (`tools.skill_usage.archive_skill` / `set_locked`, the backend of `hermes curator archive` / `lock`). `hermes plugins validate` lists these imports as warnings; a public `ctx` seam for them is the obvious follow-up.

## Disclosure

- **Reads:** hook payloads (tool names, statuses and error text, turn outcome, the first 280 characters of your next message to spot a correction), and skill folders, to fingerprint them.
- **Writes:** its own SQLite file `<HERMES_HOME>/plugin-data/fledge/data.db` (skill names, fingerprints, verdict reasons, session/turn ids). Verdict reasons quote short excerpts: the matched correction phrase (up to 60 characters) and a failing tool's error text (up to 80); hold summaries include the skill's one-line `description`. No other message text and no skill bodies. held writes into Hermes' pending queue `<HERMES_HOME>/pending/skills/`, and — only for skills Hermes created on its own that get grounded — an archive move into `skills/.archive/` (undo with `hermes curator restore NAME`). With `on_hand_edit: lock`, Hermes' content-lock flag.
- **Never:** network calls, model calls, credentials, shell commands, background processes, telemetry.
- **Approvals:** Fledge only adds friction. It can block a skill write or ask you about it through Hermes' approval prompt; it never approves anything, and asks are queued instead of waiting when nobody can answer (cron, kanban, API).

## Limits, honestly

- Fledge judges runs by what Hermes can observe. A run that finishes cleanly with a wrong answer and no reaction from you looks clean until you say otherwise; probation is there to catch it later.
- Correction detection is a phrase list over the start of your next message, tuned to miss rather than misfire (a casual "no worries" never counts).
- Hand edits are detected by fingerprint: Fledge knows a skill's last agent-written state once it has seen the agent write it (or after `hermes fledge scan`). An edit made through a shell command by the agent also looks like a hand edit; that errs on the side of protecting the skill.
- Turn evidence lives in memory; a restart forgets runs in flight (probation counts already recorded are kept).
- Tool status is what Hermes reports. A compound shell command (`./fetch.sh; rm tmp`) reports the exit status of its last part. When the command prints its own `$?` behind a label (`echo "exit=$?"`, `ec=$?; echo "exit_code=$ec"`, a common agent pattern), Fledge reads that exact label back and judges the program before it; otherwise a failure earlier in the command is not seen. Fledge never guesses from other output text.
- Deletes are protected only for hand-edited skills; a hand edit made before Fledge saw the skill is recognized after `hermes fledge scan`.

## Development

```bash
python -m pytest tests          # 128 tests, no Hermes install needed (Hermes surfaces are faked)
hermes plugins doctor . --ci    # real discovery, register(), hooks — in a temp HERMES_HOME
hermes plugins validate .       # catalog admission checks
```

MIT licensed. Built by Borb / 12birb.
