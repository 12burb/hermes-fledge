# Fledge demo: recipe and video script

Target: a 90-second video (X allows 2:20 without Premium), recorded from a real Hermes session. Every beat below
is the real plugin doing real work; nothing is mocked. If a beat does not happen on a take, run it again. Do not
fake a correction or a failure.

## Setup (once, about 5 minutes)

```bash
hermes profile create fledge-demo --clone          # keeps your keys, separate skills and memory
hermes plugins install <owner>/hermes-fledge --enable
mkdir -p ~/fledge-demo && cp demo/fetch_water.sh demo/readings.csv ~/fledge-demo/
chmod +x ~/fledge-demo/fetch_water.sh
hermes chat --profile fledge-demo                  # start the session inside ~/fledge-demo
```

In the session, confirm the plugin is live with `/fledge doctor` (every line `ok`). Use a terminal font size
that reads on a phone (18 pt+), and a dark theme.

## Beat 1 — the save gate (0:08–0:35)

1. Prompt: `Run ./fetch_water.sh and turn what it returns into results.csv`
   The script prints two rows, then fails with an HTTP 404 and exits 1. Hermes usually retries, then writes
   a partial file and wraps up. (If it gives up after a single failure, Fledge rates the run unclear, since
   one failing command can be harmless, and lets the save through. Retake.)
2. Prompt: `/refine save the procedure for pulling water results as a skill`
   This runs Hermes' background review now instead of waiting for its usual nudge. The review tries to save
   a skill from the run it just watched fail.
3. Fledge holds it. The notice arrives at the top of your next message (send `ok`):
   `🪶 Fledge: held skill 'pull-water-results': it was learned from a run that went wrong (terminal was still
   failing when the turn ended …). Review: /fledge diff …`
4. `/fledge diff <id>` → show the would-be skill, then `/fledge reject <id>`.

Caption: *Hermes was about to learn from a run that failed. Fledge held it for review.*

## Beat 2 — "it always thinks it did a good job" (0:35–1:00)

1. Prompt: `Sort readings.csv by sample date, newest first, and give me a markdown table`
   The dates are in four formats on purpose (02/10/2026 vs 10/02/2026 vs 3 Oct 2026). Check the result
   yourself. If the order is wrong (it often is), continue. If Hermes gets it right, ask for the E. coli
   ranking instead or retake; the point is a real mistake.
2. Prompt: `/refine save this as a skill` → nothing looked wrong, so the save goes through. `/fledge` shows the
   new skill **on probation**.
3. Reply with the real problem, e.g. `That's wrong — 02/10/2026 is 2 October, you mixed up the date formats.`
4. Fledge grounds the skill it just saved and archives it (recoverable):
   `🪶 Fledge: grounded 'sort-readings-by-date': it was learned from a run that went wrong afterwards: you
   corrected it right after … Archived; restore with: hermes curator restore sort-readings-by-date`

Caption: *The agent graded itself an A. Your correction is the grade Fledge uses.*

## Beat 3 — your edits stay yours (1:00–1:20)

1. Open the saved skill from beat 1 or 2 (restore one first if needed: `hermes curator restore NAME`),
   e.g. `~/.hermes/profiles/fledge-demo/skills/<name>/SKILL.md`, and tune a line by hand (add a rule like
   "Always parse dates as DD/MM/YYYY").
2. Prompt something that uses it, then `/refine improve that skill`.
3. The review tries to rewrite it. Fledge turns the rewrite into a proposal:
   `🪶 Fledge: Hermes tried to rewrite 'sort-readings-by-date', which you edited by hand. Saved as a proposal
   instead: /fledge diff …`

Caption: *Self-improvement, without overwriting the person who improved it first.*

## Close (1:20–1:30)

`/fledge` overview on screen, then the end card:

> **Fledge** — skills have to earn their wings.
> No model grades the model. 9 hooks · MIT · `hermes plugins install <owner>/hermes-fledge`

B-roll worth having: `python -m pytest tests` (93 passed) and `hermes plugins validate .` (green).

## Narration (one voice, about 210 words)

> Hermes learns from its own work. After a task, it writes down what it did as a skill, and uses it next
> time. But who grades the work? On Reddit, the most-upvoted complaint about Hermes is that it always
> thinks it did a good job.
>
> Watch. This data pull fails halfway: a 404, two rows out of six. Hermes wraps up anyway, and its
> self-review goes to save the procedure as a skill. Fledge stops it. The run it learned from ended with a
> failing command, so the skill waits for my review instead of going live.
>
> Here, nothing looked wrong. The table rendered, so the skill saves, on probation. Then I check the
> dates. Wrong. I say so, and Fledge grounds the skill it just learned from that mistake, archived, one
> command to restore.
>
> And when I tune a skill by hand, Hermes' next self-improvement doesn't overwrite me. It becomes a
> proposal I can approve or reject.
>
> Fledge never asks a model whether a model did well. It uses what actually happened: errors left
> unresolved, failed turns, my corrections, my undos. New skills have to earn their wings.
