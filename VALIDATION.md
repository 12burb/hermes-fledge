# Fledge — validation against real Hermes (DRAFT, in progress)

Status: tasks 1–3 done; task 4 (live beats) in progress. Nothing below is a self-report: every line comes
from a command that was run, a log line, or the session DB of a real sandbox session.

## Environment

| | |
|---|---|
| OS | Windows 11, ARM64 (native) |
| Hermes validated | **v0.22.7** (tag, commit `b200f5bc60e3`, released 2026-11-03), Python 3.14.9 ARM64 |
| How | separate read-only checkout of v0.22.7 + a throwaway `HERMES_HOME` (`C:\Users\onetw\fh\d`) outside the live install root, so it runs its own gateway beside the live one (`gateway.run: … starting this home's gateway beside it`). The live install (v0.22.5) was not modified, updated or restarted. |
| Model (dev sandbox) | `glm-5.3` via OpenCode Go (API key; no OAuth tokens copied) |
| Isolation | `plugins.isolation: in_process` (also the 0.22.7 default, `hermes_cli/plugin_isolation.py:3`) |

## 1. Unit tests

```
$ python -m pytest tests            # as received (commit 5176ad4)
93 passed
$ python -m pytest tests            # current
102 passed
```

## 2. Source check (Hermes v0.22.7, `b200f5b`)

| Fledge relies on | Verified at | Result |
|---|---|---|
| hook names registered | `hermes_cli/plugins.py:110,133,135,202` (VALID_HOOKS) | ✅ all 9 |
| `pre_tool_call` kwargs `tool_name,args,task_id,session_id,tool_call_id,turn_id` | `hermes_cli/plugins.py:1923-1927` | ✅ |
| `skill_manage` args: `operations[]` (advertised) and legacy flat shape | `tools/skill_manager_tool.py` schema + `_skill_manage_from`; batch `tools/skill_manager_batch.py:219` | ✅ |
| directives `{"action":"block","message"}` (message required) / `{"action":"approve","message","rule_key"}`; block > approve | `hermes_cli/plugins.py:1907-1958` | ✅ |
| `post_tool_call` `status` ok/error/blocked, `error_type`, `error_message` | `model_tools.py:674,681,705`; blocked: `model_tools.py:957`, `agent/tool_executor.py:657` | ✅ |
| `pre_llm_call` kwargs `session_id,task_id,turn_id,user_message,…`; not fired for persist-disabled forks | `agent/turn_context.py:858-875` | ✅ |
| write origin bound before `pre_llm_call` | `set_current_write_origin` `agent/turn_context.py:1133`, `_collect_pre_llm_call_context` called at `:1233` | ✅ |
| `get_current_write_origin()` values `assistant_tool` (default, `agent/agent_init.py:513`), `background_review` (review fork incl. `/refine`, `agent/background_review.py:979-1001,1185-1187`; curator `agent/curator.py`), `side_question` (`agent/side_question.py:115`) | `tools/skill_provenance.py:20` | ✅ (note: `run_agent.py` docstring mentions a `refine_review` origin; the code keeps `background_review` and only marks the fork attended) |
| review fork shares the parent `session_id`, `_persist_disabled=True` (no `pre_llm_call` / `on_session_end` from the fork) | `agent/background_review.py:1001-1018` | ✅ |
| `pre_command` kwargs `surface,command,alias_used,args_raw,session_key,platform` | `hermes_cli/plugins.py:1861-1872`; callers `cli.py:1189`, `gateway/run_inbound_hooks.py:142`, `gateway/session_commands.py:340` | ✅ |
| `post_approval_response` (`choice`, `turn_id`, `session_id`, `session_key`, …) | `tools/approval_context.py:54-70`, `tools/approval.py:939-944`, `tools/approval_gateway_wait.py:97` | ✅ (fix 2) |
| `on_skill_lifecycle` actions created/patched/edited/loaded/archived/restored, kwargs `action,skill_name,provenance,task_id,session_id` | `tools/skill_usage.py:470-534,568`; emitted per op from `tools/skill_manager_tool.py` `_record_success` | ✅ (fix 5) |
| `on_session_end` kwargs `session_id,task_id,turn_id,completed,failed,interrupted,turn_exit_reason` | `agent/turn_finalizer.py:887` | ✅ |
| `on_session_finalize` / `on_session_reset` | `hermes_cli/lifecycle.py:83,97`, `hermes_cli/cli_session_mixin.py:578,661`, `gateway/slash_commands_session.py:258` | ✅ |
| `tools.write_approval.stage_write(subsystem, payload, *, summary, origin)`, `list_pending`, `skill_gist`, `current_origin`, `SKILLS` | `tools/write_approval.py:33,74,92,143,257` | ✅ |
| `hermes_cli.write_approval_commands.handle_pending_subcommand("skills", args)` | `hermes_cli/write_approval_commands.py:35` | ✅ |
| `/skills approve` replays the staged payload (flat or `{"action":"batch","operations":…}`) | `tools/skill_manager_tool.py` `apply_skill_pending` / `_skill_manage_from` | ✅ |
| `tools.skill_usage.archive_skill`, `get_record` (`created_by`), `read_skill_name` | `tools/skill_usage.py:642,391,246` | ✅ |
| `tools.skill_usage.set_locked` (content lock, keyed by directory name) | `tools/skill_usage.py:580`; key `hermes_cli/curator.py:255` | ✅ **new in 0.22.7** — absent in 0.22.5 (fix 1) |
| `ctx.post_message`, `get_config`/`set_config`, `register_command` (`fn(raw_args, **ctx)`), `register_cli_command`, `register_hook`, `on_unload` | `hermes_cli/plugins.py:607,266,278,649,635,935,416` | ✅ |
| `plugins.plugin_storage.plugin_db` | `plugins/plugin_storage.py:36` | ✅ |

Ordering note: `_spawn_background_review` (`agent/turn_finalizer.py:871`) runs just before `on_session_end`
(`:887`). The fork makes an LLM round trip before its first `skill_manage`, so in practice the turn is closed
first; Fledge's `latest_finished_turn` also covers a user message arriving while the review runs.

## 3. Real loader

```
$ hermes plugins doctor . --ci
Plugin Doctor: …\hermes-fledge
  manifest: fledge 0.1.0 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 0 tool(s), 9 hook(s)

$ hermes plugins validate . --install-deps
✓ manifest  ✓ manifest fields  ✓ requires_hermes '>=0.22.7'  ✓ config schema  ✓ requires_env  ✓ oauth
✓ loadable  ✓ python dependencies — none declared  ✓ capability probe  ✓ declared tools  ✓ declared hooks
✓ declared middleware  ✓ built-in tool collisions  ✓ security scan — safe  ✓ no core override
⚠ private core imports: bridge.py tools.skill_provenance.get_current_write_origin; tools.write_approval
  (stage_write/list_pending/skill_gist); hermes_cli.write_approval_commands.handle_pending_subcommand;
  tools.skill_usage.get_record/archive_skill/set_locked
Validation passed.
```

Only the expected private-import warnings. (On Windows the `--install-deps` isolated-runtime copy hit MAX_PATH
on Hermes' own vendored test fixtures; Fledge declares no dependencies, and validation completed.)

## 4. Live smoke test (in progress)

- Install: `hermes plugins install <local path>` is **not** accepted in 0.22.7 (owner/repo or Git URL only);
  used clone + `hermes plugins adopt fledge` + `hermes plugins enable fledge` → `enabled  git  0.1.0  fledge`.
- `hermes fledge doctor` and in-session `/fledge doctor`: write origin, pending queue, archive, content lock,
  skill lookup, plugin storage — **all ok**.
- Beat 1, take 1–2 (glm-5.3): ❌ not held — see findings 3 and 4.

## Bugs found and fixed

1. **Doctor called archiving degraded on Hermes < 0.22.7** (one probe for archive + lock). Split. `bridge.py`,
   test `test_missing_content_lock_degrades_only_that_feature`.
2. **Denied approvals attributed by guessing** — Hermes sends `session_id`; use it. `engine.py`,
   test `test_denied_approval_uses_the_session_id_hermes_sends`.
3. **Held batch dropped the legacy default `name`** needed by `/skills approve` replay. `engine.py`,
   test `test_held_batch_keeps_the_legacy_default_name_for_replay`.
4. **Live: a cleanup command masked a failed pull.** `./fetch_water.sh` failed, then `rm fetch_stderr.txt`
   succeeded; keyed per tool, `terminal` looked recovered and the run scored clean. Terminal calls are now
   keyed per program. `evidence.py`, 4 tests.
5. **Live: second op of a one-skill batch looked ungated** (`skill_manage` fires `on_skill_lifecycle` per
   op). In-flight records now count ops. `engine.py`, 2 tests.

## Open findings (not code bugs)

- With glm-5.3 the agent reads `fetch_water.sh`, sees the header comment ("Demo prop … canned text"), calls
  it a canned demo script and does not retry; in take 2 it also wrapped the script in a compound command
  ending in `rm`, whose exit status (0) is what Hermes reports. A single unresolved failure scores 2
  (uncertain, by design), so Beat 1 does not trigger.
- One-shot `hermes chat -q … --resume` cannot drive `/refine`: the client detaches, session cleanup cancels the
  review. Beats must be run in an interactive session (as in the recording).
