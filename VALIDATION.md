# Fledge: validation against real Hermes

Nothing here is a self-report. Every line comes from a command that was run, a log line, or the session DB of
a real sandbox session. No ledger rows were written by hand, no hooks were called directly, and every failure
was a real one (an actual exit 1, an actual user message).

## Environment

| | |
|---|---|
| OS | Windows 11, ARM64 (native) |
| Hermes validated | **v0.22.7** (tag, commit `b200f5bc60e3`, released 2026-11-03), Python 3.14.9 ARM64 |
| How | a separate read-only checkout of v0.22.7 plus throwaway `HERMES_HOME`s (`%USERPROFILE%\fh\d` dev, `%USERPROFILE%\fh\m` demo) outside any real install, each running its own gateway (`gateway.run: … starting this home's gateway beside it`). No real profile was touched. |
| Model | `glm-5.3` via OpenCode Go (API key; no OAuth tokens copied) |
| Isolation | `plugins.isolation: in_process` (the 0.22.7 default, `hermes_cli/plugin_isolation.py:3`) |
| Fledge | `5530c73` (all fixes below) |

## 1. Unit and flow tests

```
$ python -m pytest tests            # as received (5176ad4)
93 passed
$ python -m pytest tests            # 5530c73
138 passed in 0.66s
```

Every fix below came with a test that fails without it (checked by stashing the fix and re-running).

## 2. Source check (Hermes v0.22.7, `b200f5b`)

| Fledge relies on | Verified at | Result |
|---|---|---|
| hook names registered | `hermes_cli/plugins.py:110,133,135,202` (VALID_HOOKS) | ✅ all 9 |
| `pre_tool_call` kwargs `tool_name,args,task_id,session_id,tool_call_id,turn_id` | `hermes_cli/plugins.py:1923-1927` | ✅ |
| `skill_manage` args: `operations[]` (advertised) and the legacy flat shape | `tools/skill_manager_tool.py` schema + `_skill_manage_from`; batch `tools/skill_manager_batch.py:219` | ✅ |
| directives `{"action":"block","message"}` (message required) / `{"action":"approve","message","rule_key"}`; block beats approve | `hermes_cli/plugins.py:1907-1958` | ✅ |
| `post_tool_call` `status` ok/error/blocked, `error_type`, `error_message`, `result` | `model_tools.py:674,681,705`; blocked: `model_tools.py:957`, `agent/tool_executor.py:657` | ✅ |
| `pre_llm_call` kwargs `session_id,task_id,turn_id,user_message,…`; not fired for persist-disabled forks | `agent/turn_context.py:858-875` | ✅ |
| write origin bound before `pre_llm_call` | `set_current_write_origin` `agent/turn_context.py:1133`; `_collect_pre_llm_call_context` at `:1233` | ✅ |
| `get_current_write_origin()` returns `assistant_tool` (default, `agent/agent_init.py:513`), `background_review` (review fork incl. `/refine`, `agent/background_review.py:979-1001,1185-1187`; curator `agent/curator.py`) or `side_question` (`agent/side_question.py:115`) | `tools/skill_provenance.py:20` | ✅ (a `run_agent.py` docstring mentions a `refine_review` origin; the code keeps `background_review` and only marks the fork attended) |
| review fork shares the parent `session_id`, `_persist_disabled=True` (no `pre_llm_call` / `on_session_end` from the fork) | `agent/background_review.py:1001-1018` | ✅ |
| `pre_command` kwargs `surface,command,alias_used,args_raw,session_key,platform` | `hermes_cli/plugins.py:1861-1872`; callers `cli.py:1189`, `gateway/run_inbound_hooks.py:142`, `gateway/session_commands.py:340` | ✅ |
| `post_approval_response` (`choice`, `turn_id`, `session_id`, `pattern_key`, …) | `tools/approval_context.py:54-70`, `tools/approval.py:939-944,1154-1172`, `tools/approval_gateway_wait.py:97` | ✅ (fixes 2, 8) |
| `on_skill_lifecycle` actions created/patched/edited/loaded/archived/restored, one event per op | `tools/skill_usage.py:470-534,568`; `tools/skill_manager_tool.py:767-780` | ✅ (fix 5) |
| `on_session_end` kwargs `session_id,task_id,turn_id,completed,failed,interrupted,turn_exit_reason` | `agent/turn_finalizer.py:887` | ✅ |
| `on_session_finalize` / `on_session_reset` | `hermes_cli/lifecycle.py:83,97`, `hermes_cli/cli_session_mixin.py:578,661`, `gateway/slash_commands_session.py:258` | ✅ |
| `tools.write_approval.stage_write(subsystem, payload, *, summary, origin)`, `list_pending`, `skill_gist` | `tools/write_approval.py:33,74,92,143,257` | ✅ |
| `hermes_cli.write_approval_commands.handle_pending_subcommand("skills", args)` | `hermes_cli/write_approval_commands.py:35` | ✅ |
| `/skills approve` replays the staged payload (flat or `{"action":"batch","operations":…}`) | `tools/skill_manager_tool.py` `apply_skill_pending` / `_skill_manage_from` | ✅ |
| `tools.skill_usage.archive_skill`, `get_record` (`created_by`, `last_patched_at`), `read_skill_name` | `tools/skill_usage.py:642,391,246`; `last_patched_at` is stamped after every patch/edit/write_file/remove_file, `:511-519` | ✅ (fix 17) |
| `tools.skill_usage.set_locked` (keyed by directory name) | `tools/skill_usage.py:580`; key `hermes_cli/curator.py:255` | ✅ **new in 0.22.7**, absent in 0.22.5 (fix 1); hence `requires_hermes >=0.22.7` |
| `ctx.post_message`, `get_config`/`set_config`, `register_command` (`fn(raw_args, **ctx)`), `register_cli_command`, `register_hook`, `on_unload` | `hermes_cli/plugins.py:607,266,278,649,635,935,416` | ✅ |
| `plugins.plugin_storage.plugin_db` (WAL, 5 s busy timeout) | `plugins/plugin_storage.py:36`, `hermes_cli/sqlite_util.py` | ✅ |

Ordering note: `_spawn_background_review` (`agent/turn_finalizer.py:871`) runs just before `on_session_end`
(`:887`). The fork makes an LLM round trip before its first `skill_manage`, so in practice the turn is
closed first. Fledge's `latest_finished_turn` also covers a user message that arrives while the review runs.

## 3. Real loader (at `5530c73`)

```
$ hermes plugins doctor . --ci
  manifest: fledge 0.1.0 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 0 tool(s), 9 hook(s)

$ hermes plugins validate .
✓ manifest  ✓ manifest fields  ✓ requires_hermes '>=0.22.7'  ✓ config schema  ✓ requires_env  ✓ oauth
✓ loadable  ✓ python dependencies — none declared  ✓ capability probe  ✓ declared tools  ✓ declared hooks
✓ declared middleware  ✓ built-in tool collisions  ✓ security scan — safe  ✓ no core override
⚠ private core imports (catalog rule 9: public surfaces only; …): bridge.py tools.skill_provenance,
  tools.write_approval, hermes_cli.write_approval_commands, tools.skill_usage.get_record/archive_skill/set_locked
Validation passed.
```

The only warnings are the expected private imports. Rule 9 warns on these without failing; the decline case
is runtime rebinds, and `no core override` passes. `--install-deps` also passed. Fledge declares no
dependencies; on Windows the isolated-runtime copy hits MAX_PATH on Hermes' own vendored test fixtures, which
is a Hermes issue. The catalog entry, with placeholders filled, passes `scripts/validate_plugin_catalog.py`
from both v0.22.7 and current `main`: `OK: 1 file(s) valid`.

## 4. Live smoke test

Install: `hermes plugins install <local path>` is **not** accepted by 0.22.7 (owner/repo or Git URL only). Used
a git clone into `<home>/plugins/fledge`, then `hermes plugins adopt fledge` and `hermes plugins enable fledge`.
`hermes fledge doctor`: write origin, pending queue, archive, content lock, skill lookup and plugin storage
were **all ok**. All beats ran in an interactive `hermes chat` session. One-shot `chat -q … --resume` can't
drive `/refine`, because the client detaches and session cleanup cancels the review.

| Check | Result |
|---|---|
| Beat 1: bad-run save is held | ✅ take 5 (takes 1–4 found fixes 4, 12, 13) |
| Beat 2: probation, grounded and archived after a real correction | ✅ |
| Beat 3: autonomous rewrite of a hand-edited skill becomes a proposal | ✅ |
| `/fledge mode watch` records but never blocks | ✅ |
| 3 clean runs hold nothing | ✅ 13 review writes, 0 holds |
| A Fledge error fails open | ✅ found and fixed fixes 16 and 17 |

### Beat 1: held (take 5)

Prompt: `Run ./fetch_water.sh and turn what it returns into results.csv. The server is flaky, so retry once if it
fails.` The agent wrapped both attempts in one terminal call, which Hermes reported as `exit_code 0`:

```
runf >"$SCRATCH/a1.out" 2>"$SCRATCH/a1.err"; r1=$?
echo "== attempt 1: exit $r1 ==" …            output: == attempt 1: exit 1 == … == attempt 2: exit 1 ==
```

Then `/refine save the procedure for pulling water results as a skill` ran. The background review called
`skill_manage(create water-results)`:

```
agent.log  Tool skill_manage returned error: {"error": "Fledge held this skill write because the run it learned from went wrong …
gateway    plugin_message plugin=fledge op=post platform=local … ok=True
screen     🪶 Fledge: held skill 'water-results': it was learned from a run that went wrong (terminal (runf) failed
           2 times in a row and never recovered: exit 1 (reported by the command's own $?)). Review: /fledge diff
           a27be75f, then /fledge approve a27be75f or /fledge reject a27be75f.
/fledge diff a27be75f      # Pending skill write a27be75f: [fledge] create 'water-results' … (full SKILL.md shown)
/skills pending            a27be75f [auto]  [fledge] create 'water-results' — … — held: terminal (runf) failed 2 times …
/fledge reject a27be75f    → pending/skills/ empty; no water-results skill on disk
```

### Beat 2: probation, then grounded

Prompt: `Sort readings.csv by sample date, newest first, and give me a markdown table`. glm-5.3 sorted the
rows correctly, but it left the dates "as written", so the sorted table still showed four date formats. Next,
`/refine save this as a skill` created `csv-date-sorting` (fledgling, birth `clean`). Then the tester sent a
real objection to that output: `That's wrong, I can't check that order: the dates are still in four different
formats. Show them as YYYY-MM-DD.`

```
EV corrected  session local-09b655: "That's wrong"
EV grounded   csv-date-sorting it was learned from a run that went wrong afterwards: you corrected it right after — archived
screen 🪶 Fledge: grounded 'csv-date-sorting': … you corrected it right after: "That's wrong". Archived; restore with:
       hermes curator restore csv-date-sorting
skills/.archive/csv-date-sorting   (recoverable)
```

### Beat 3: hand edit, then proposal

A review created `csv-wrangling`. A line was added by hand (`## House rule (added by hand)` / `Always parse
slash dates in readings.csv as DD/MM/YYYY.`). The agent then used the skill, followed by `/refine improve the
csv-wrangling skill with what you learned`:

```
EV hand-edit csv-wrangling protected from autonomous rewrites
EV held      csv-wrangling hand_tuned: 'csv-wrangling' was edited by hand, so autonomous rewrites become proposals
/fledge diff d63b897a   [fledge] proposal: batch(2 ops: patch, patch) on csv-wrangling (you hand-tuned it)
                        op 2/2: -## House rule (added by hand) … -- Always parse slash dates … as DD/MM/YYYY.
/fledge reject d63b897a → Rejected pending skills write 'd63b897a'.  SKILL.md still ends with the hand-written rule
```

The rejected proposal would have deleted the user's own rule and replaced it with the agent's.

### Watch mode

After `/fledge mode watch`, the same failing task was run and then `/refine` (this time the agent wrote
`./fetch_water.sh; echo "EXIT:$?"`):

```
EV would-hold csv-data-qa,fetch-script-runs [watch] bad_run: terminal (./fetch_water.sh) failed 2 times in a row …
EV written fetch-script-runs created by review (gen 1); run looked bad
pending/skills/  empty
```

### Clean runs

Three clean tasks ran, each followed by `/refine save this as a skill` (row count and top nitrate, average
E. coli, sites over 10 mg/L). The review created `csv-data-qa` and patched it 12 more times:
`run looked clean` ×13, **0 holds**. Graduation (3 clean *uses*) wasn't exercised live, because the model
answered these from the file without loading the skill. It is covered by the flow tests.

### Fails open

A second process held an exclusive lock on Fledge's SQLite DB (`BEGIN EXCLUSIVE`), a real-world failure, while
a bad-run save was attempted.

- **First take** found fix 16. The gate staged the write in Hermes' queue, then `add_hold` raised
  `database is locked`, and the outer fail-open let the write through **as well**. That left `132f229e`
  in `/skills pending` as a duplicate (cleaned up with `/skills reject`).
- **Second take** (`dea256c`). The lock now hit before staging (`_hand_tuned`), and the result was a
  clean fail-open: `save gate error; letting the write through`, the skill written, the queue empty.
  The after-staging path (fix 16) is covered by `test_a_staged_hold_still_blocks_when_fledges_ledger_fails`.
- **Why it hit before staging** found fix 17. Take 1's write landed while the lifecycle hook couldn't
  record it, so the next autonomous patch saw a changed fingerprint and called the agent's own change a
  hand edit. The fix was checked against the real Hermes record of that skill:
  `last_patched_at=2026-11-06T00:15:15Z → last_written_by_hermes: True`. After a real hand edit of
  another skill, it returned `False`.

## Bugs found and fixed

Found live on Hermes 0.22.7:

1. **Doctor called archiving degraded on Hermes < 0.22.7** (one probe covered both archive and lock). Split the probe. `3a424d9`
2. **Denied approvals were attributed by guessing.** Hermes sends `session_id`; Fledge now uses it. `c73c58c`
3. **A held batch dropped the legacy default `name`** that `/skills approve` needs to replay it. `c73c58c`
4. **A cleanup command masked a failed pull.** `./fetch_water.sh` failed, then `rm fetch_stderr.txt` succeeded, and the run scored clean. Terminal calls are now keyed per program. `62753a0`
5. **The second op of a one-skill batch looked ungated** (one lifecycle event per op). In-flight records now count ops. `e145277`
12. **A compound command hid the failure.** `./fetch_water.sh > out; echo "exit=$?"; cat out` comes back as exit 0. Fledge now reads the labelled `$?` the command printed for itself. `d6a9b86`
13. **A retry written as one `if … then echo "OK" else echo "FAILED"` command came back as exit 0.** Fledge now reads which of the command's own fixed echo branches ran, as whole output lines. Predicate commands (`grep`, `test`, `diff`…) are never counted. `d79fdde`
14. **`/fledge reject` was logged as `rejectd`.** `2f450eb`
15. **A skill deleted before its use was judged came back as a veteran record.** Fixed with `bridge.skill_missing()`. `110cdfb`
16. **A write already staged for review was also saved when Fledge's DB failed** (duplicate in the queue). Once staged, it stays held. `dea256c`
17. **After a missed agent write, the agent's own change looked like a hand edit.** Fixed using Hermes' `last_patched_at` stamp. `5530c73`

Found in code review (each confirmed by running the code first, then fixed with a test):

6. **Corrections:** curly apostrophes (`That’s wrong`) were never detected, and polite closers (`No, thanks.`) scored as strong corrections. `bcab709`
7. **Relapse/restore kept the old bad-run count**, so even a clean next run grounded the skill. `52f107a`
8. **Declining Fledge's own approval prompt counted as a denied action** (`pattern_key plugin_rule:fledge:…`). `c62cec5`
9. **Autonomous deletes of hand-edited skills weren't treated as proposals.** `de9fd06`
10. **The birth turn counted as a use, and turns recorded before `mode off` could still ground skills later.** `02ed639`
11. **A bare `No.` / `nope` scored as a strong correction.** It is now a weak signal. `f427c61`

## Known limits (documented in README)

- Any other way of hiding a failure inside one compound command is not seen. Fledge never guesses from free output text.
- In the out-of-process plugin host (`plugins.isolation: host`), Fledge can't see the write origin, so every save counts as autonomous, and `hermes fledge` CLI subcommands aren't wired. `/fledge doctor` says so.
- Beat 2 needs a real mistake. glm-5.3 got the date sort right twice, so the correction used was its real but milder defect: the unconverted date column.
