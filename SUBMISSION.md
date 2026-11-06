# Shipping Fledge to the Hermes Agent Plugin Hackathon

Checked against the official sources on 2026-11-05:
[the announcement](https://nousresearch.com/) (Nous Research, Nov 5),
[Submitting to the Plugin Catalog](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)
and `plugin-catalog/README.md` on `NousResearch/hermes-agent` `main` (entry schema identical to v0.22.7).

- **Deadline: 5 PM EST, Tuesday Nov 11, 2026** (= 4 PM CST, 22:00 UTC).
- **Entry = three things:** (1) a PR adding `plugin-catalog/fledge.yaml`, (2) a demo video posted on X
  tagging @NousResearch, **under 90 seconds**, showing the plugin working, (3) both links in the form
  <https://form.typeform.com/to/FwJLvOln>. The form can be sent as soon as the PR is open.
- **Judging:** usefulness, reliability, polish and creativity; judges favor plugins that do more than a prompt
  or a skill could. Catalog CI runs once a maintainer starts it; entries are reviewed every few days.
- Accepted entries are listed in the Hermes Marketplace (`hermes-agent.nousresearch.com/marketplace`).

## 1. Publish the repo

```bash
cd hermes-fledge
gh repo create 12burb/hermes-fledge --public --source . --push \
  --description "Skills have to earn their wings: evidence-gated skill saves for Hermes Agent"
```

Before pushing: every commit is authored with the GitHub no-reply address
(`git log --format='%an <%ae>' | sort -u`), `12burb` is filled in `catalog/fledge.yaml` and `plugin.yaml`
(`homepage:`), and the working tree is clean.

From that moment the plugin is installable by anyone on Hermes >= 0.22.7, catalog or not:

```bash
hermes plugins install 12burb/hermes-fledge --enable
```

## 2. Validate against real Hermes (done: see VALIDATION.md)

```bash
python -m pytest tests                       # all pass, no Hermes needed
hermes plugins doctor . --ci                 # discovery, register(), hooks
hermes plugins validate . --install-deps     # the check catalog CI runs
python scripts/validate_plugin_catalog.py plugin-catalog/fledge.yaml   # inside a hermes-agent checkout
```

Expected `validate` warnings, by design: private core imports of `tools.skill_provenance`,
`tools.write_approval`, `tools.skill_usage` (beyond `read_skill_name`) and
`hermes_cli.write_approval_commands`. Everything else passes, including `security scan — safe` and
`no core override`.

Local development install (Hermes 0.22.7 does not install from a local path or a `file://` URL):

```bash
git clone <this repo> "$HERMES_HOME/plugins/fledge"
hermes plugins adopt fledge && hermes plugins enable fledge
```

## 3. Record the video

Follow `DEMO.md`: real session, real failures, **under 90 seconds** (target 80). Post it on X tagging
@NousResearch and keep the link.

## 4. Open the catalog PR

```bash
git rev-parse HEAD                           # the 40-character sha to pin
```

Fork `NousResearch/hermes-agent`, add **one file**, `plugin-catalog/fledge.yaml` (a copy of
`catalog/fledge.yaml` with the real `repo`, quoted `sha` and `maintainer`), run the catalog validator, and
open the PR with the body below. Then send the form.

### PR title

`plugin-catalog: add fledge (evidence-gated skill saves, probation, hand-edit protection)`

### PR body

> **Fledge — skills have to earn their wings.**
>
> Hermes' self-improvement loop saves skills based on the agent's own judgment of whether a task went
> well. In [Kilo's read of 1,300+ r/openclaw comments](https://kilo.ai/openclaw/vs-hermes), the first
> complaint listed about Hermes is that self-evaluation always passes ("It always thinks it did a good
> job"), and the second is self-improvement overwriting skills users tuned by hand.
>
> Fledge gates `skill_manage` on evidence from the run a skill was learned from, using only facts Hermes
> reports through hooks: tools still failing when the turn ended (shell commands judged per program),
> failed or interrupted turns, the user's next message correcting the agent, `/undo` or a `/rollback`
> restore right after, denied approvals. No model grades the model.
>
> - Saves learned from a bad run are held in Hermes' own skills pending queue with the reasons (the
>   stock `/skills pending|diff|approve|reject` flow; `/fledge pending|diff|approve|reject` exposes it in
>   messaging chats, where `/skills` is CLI-only). Foreground saves after errors go through Hermes'
>   approval prompt; unattended runs queue instead of waiting.
> - New and rewritten skills are on probation: clean runs graduate them, repeated bad runs ground them,
>   and a save from a run the user corrected right after is grounded at once. Grounded skills Hermes
>   created on its own are archived (recoverable with `hermes curator restore`); user-requested skills are
>   only reported.
> - Skills edited by hand are fingerprinted; autonomous rewrites or deletes of them become proposals in
>   the same queue (optionally Hermes' content lock).
>
> **Surfaces:** nine hooks (`pre_llm_call`, `pre_tool_call`, `post_tool_call`, `on_session_end`,
> `on_session_finalize`, `on_session_reset`, `on_skill_lifecycle`, `pre_command`,
> `post_approval_response`), `register_command` (`/fledge`), `register_cli_command` (`hermes fledge`),
> `post_message` for chat notices, `get_config`/`set_config`, `plugins.plugin_storage`. No tools, no
> middleware, no core patches.
>
> **Undocumented imports (validate warnings), and why:** there is no public seam yet for (1) the skill
> write origin, needed to tell the background-review fork from a foreground turn:
> `tools.skill_provenance.get_current_write_origin`; (2) staging a write into the review queue:
> `tools.write_approval.stage_write` / `list_pending` / `skill_gist` and
> `hermes_cli.write_approval_commands.handle_pending_subcommand`; (3) curator actions:
> `tools.skill_usage.archive_skill` / `set_locked` / `get_record`. All calls live in `bridge.py`, each
> guarded, so a moved function disables one feature (reported by `/fledge doctor`) and never breaks a
> turn. Happy to move to a `ctx` facade; issue draft below.
>
> **Disclosure:** reads hook payloads (tool statuses and error text, the first 280 characters of the next
> user message) and skill folders to fingerprint them; writes its own SQLite under `plugin-data/fledge`
> (skill names, fingerprints, session/turn ids, verdict reasons quoting the matched correction phrase and
> up to 80 characters of a tool error, hold summaries with the skill's one-line description; no other
> message text, no skill bodies), held writes into `pending/skills/`, and archive moves of grounded
> agent-created skills into `skills/.archive/`; no network, no model calls, no credentials, no shell
> commands, no background processes; never approves anything; the gate lets writes through on its own
> errors (except a write already queued for review, which stays held). Needs `plugins.isolation: in_process` (the default) for full accuracy.
>
> **Tested:** 138 unit and flow tests (`python -m pytest tests`); `hermes plugins doctor --ci` and
> `hermes plugins validate --install-deps` on Hermes v0.22.7 (`b200f5b`, Windows 11 ARM64); live sessions
> in an isolated HERMES_HOME, results in
> [VALIDATION.md](https://github.com/12burb/hermes-fledge/blob/SHA/VALIDATION.md). Demo: VIDEO_LINK.

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
> `hermes plugins install 12burb/hermes-fledge` · built for the @NousResearch plugin hackathon [video]
