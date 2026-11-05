# Shipping Fledge to the Hermes Agent Plugin Hackathon

Deadline: **November 11, 2026**. Submission = a pull request adding the catalog entry, plus a demo video.
Judged on usefulness, reliability and creativity; accepted entries are listed in the Hermes Marketplace.

## 1. Publish the repo (day 1)

```bash
cd hermes-fledge
git init && git add . && git commit -m "Fledge 0.1.0"
gh repo create hermes-fledge --public --source . --push
```

Then replace `OWNER` in `catalog/fledge.yaml` with your GitHub handle (and add `homepage:` to `plugin.yaml`).

## 2. Validate against real Hermes (day 1–2)

The test suite fakes the Hermes surfaces; these checks run the real loader. Use the newest Hermes release.

```bash
hermes --version                                  # set requires_hermes to this release if it is newer than 0.22.7
python -m pytest tests                            # expect 93 passed (also try Python 3.11 and 3.14)
hermes plugins doctor . --ci                      # discovery, register(), hook names, **kwargs
hermes plugins validate . --install-deps          # the exact check catalog CI runs
```

Expected `validate` warnings, by design: imports of undocumented core modules `tools.skill_provenance`,
`tools.write_approval`, `tools.skill_usage` (beyond the documented `read_skill_name`) and
`hermes_cli.write_approval_commands`. (`tools.skills_tool`, `tools.skills_sync.dir_hash`,
`gateway.session_context` and `plugins.plugin_storage` are documented plugin surfaces.) The README and PR
explain each. Anything else, especially a `security scan` or `no core override` finding, gets fixed
before the PR.

Smoke test in an isolated home, with a real session:

```bash
export HERMES_HOME=$(mktemp -d)                   # throwaway profile
hermes setup                                      # pick a model
hermes plugins install "$PWD" --enable            # local path install
hermes chat                                       # then follow DEMO.md beat 1
hermes fledge status && hermes fledge export
```

Watch for: `/fledge doctor` all `ok`; the hold notice appearing; `/skills pending` (CLI) listing the
`[fledge]` entry; `/fledge approve ID` saving the skill.

## 3. Record the video (day 3–5)

Follow `DEMO.md`. Real session, real failures, about 90 seconds. Post it to X and keep the link for the PR.

## 4. Open the catalog PR (day 5–6)

```bash
git rev-parse HEAD                                # the 40-character sha to pin
```

Fork `NousResearch/hermes-agent`, copy `catalog/fledge.yaml` to `plugin-catalog/fledge.yaml` with the real
`repo`, `sha` and `maintainer`, and open the PR with the body below. Wait for the catalog CI job to go green.

### PR title

`plugin-catalog: add fledge (evidence-gated skill saves, probation, hand-edit protection)`

### PR body

> **Fledge — skills have to earn their wings.**
>
> Hermes' self-improvement loop saves skills based on the agent's own judgment of whether a task went
> well. The most-upvoted Hermes complaint in Kilo's analysis of 1,300+ r/openclaw comments is that it
> always thinks it did a good job, so skills learned from runs that quietly went wrong carry the mistake
> forward, and later self-improvement can overwrite skills users tuned by hand.
>
> Fledge gates `skill_manage` on evidence from the run a skill was learned from, using only facts Hermes
> reports through hooks: tools still failing when the turn ended, failed or interrupted turns, the user's
> next message correcting the agent, `/undo` or a `/rollback` restore right after, denied approvals. No
> model grades the model.
>
> - Saves learned from a bad run are held in Hermes' own skills pending queue with the reasons (the
>   stock `/skills pending|diff|approve|reject` flow; `/fledge pending|diff|approve|reject` exposes it in
>   messaging chats, where `/skills` is CLI-only). Foreground saves after errors go through Hermes'
>   approval prompt; unattended runs queue instead of waiting.
> - New and rewritten skills are on probation: clean runs graduate them, repeated bad runs ground them,
>   and a save from a run the user corrected right after is grounded at once. Grounded skills Hermes
>   created on its own are archived (recoverable with `hermes curator restore`); user-requested skills are
>   only reported.
> - Skills edited by hand are fingerprinted; autonomous rewrites of them become proposals in the same
>   queue (optionally Hermes' content lock).
>
> **Surfaces:** nine hooks (`pre_llm_call`, `pre_tool_call`, `post_tool_call`, `on_session_end`,
> `on_session_finalize`, `on_session_reset`, `on_skill_lifecycle`, `pre_command`,
> `post_approval_response`), `register_command` (`/fledge`), `register_cli_command` (`hermes fledge`),
> `post_message` for chat notices, `get_config`/`set_config`, `plugins.plugin_storage`. No tools, no
> middleware, no core patches.
>
> **Undocumented imports (validate warnings), and why:** there is no public seam yet for (1) the skill
> write origin, needed to tell the background-review fork from a foreground turn —
> `tools.skill_provenance.get_current_write_origin`; (2) staging a write into the review queue —
> `tools.write_approval.stage_write` / `list_pending` / `skill_gist` and
> `hermes_cli.write_approval_commands.handle_pending_subcommand`; (3) curator actions —
> `tools.skill_usage.archive_skill` / `set_locked` / `get_record`. All calls live in `bridge.py`, each
> guarded, so a moved function disables one feature (reported by `/fledge doctor`) and never breaks a
> turn. Happy to move to a `ctx` facade; issue draft below.
>
> **Disclosure:** reads hook payloads (tool statuses and error text, the first 280 characters of the next
> user message) and skill folders to fingerprint them; writes its own SQLite under `plugin-data/fledge`
> (names, fingerprints, verdict reasons, session/turn ids; no message text or skill content), held writes
> into `pending/skills/`, and archive moves of grounded agent-created skills into `skills/.archive/`; no
> network, no model calls, no credentials, no shell commands, no background processes; never approves
> anything; the gate fails open on its own errors. Needs `plugins.isolation: in_process` for full
> accuracy (in the plugin host it treats every save as autonomous).
>
> **Tested:** 93 unit and flow tests (`python -m pytest tests`); `hermes plugins doctor --ci` and
> `hermes plugins validate --install-deps` on Hermes X.Y.Z (fill in); live run in an isolated
> HERMES_HOME. Demo: VIDEO_LINK.

## 5. Optional: the issue that makes the imports go away

Title: `Plugin seam request: ctx.skills (write origin, stage for review, archive, lock)`

> Plugins that govern the self-improvement loop (Fledge, provenance-gate, skill-lineage) need four things
> that today require importing internals: the current skill write origin (`foreground` vs
> `background_review`), staging a `skill_manage` payload into the skills pending queue with a summary,
> archiving a curation-eligible skill, and setting the content lock. A small `ctx.skills` facade
> (`write_origin()`, `stage_write(payload, summary)`, `pending_ids()`, `archive(name)`,
> `set_locked(name, bool)`) covered by the compatibility contract would let these plugins drop every
> undocumented import.

## 6. Post (launch day)

> Hermes learns from its own work, but it grades its own homework. Fledge makes skills earn their wings:
> a skill learned from a run that went wrong waits for your review, new skills stay on probation, and
> your hand-tuned skills stop getting overwritten. No model grades the model.
> `hermes plugins install OWNER/hermes-fledge` · built for the @NousResearch plugin hackathon [video]
