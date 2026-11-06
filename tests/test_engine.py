"""End-to-end flows through the real Fledge engine with fake Hermes surfaces."""

import pytest
from fledge.ledger import ARCHIVED, FLEDGED, FLEDGLING, GROUNDED

SID = "sess-1"
CREATE = {"operations": [{"action": "create", "name": "pull-water-results",
                          "content": "---\nname: pull-water-results\ndescription: Use when...\n---\nsteps"}]}


def turn(engine, turn_id, message, events=(), *, sid=SID, failed=False, skills=()):
    """One foreground turn: user message, tool calls, skill loads, turn end."""
    engine.hook_pre_llm_call(session_id=sid, turn_id=turn_id, user_message=message, task_id=sid)
    for name in skills:
        engine.hook_on_skill_lifecycle(action="loaded", skill_name=name, session_id=sid)
    for tool, status, error in events:
        engine.hook_post_tool_call(tool_name=tool, args={}, session_id=sid, turn_id=turn_id, status=status,
                                   error_message=error)
    engine.hook_on_session_end(session_id=sid, turn_id=turn_id, completed=not failed, failed=failed)


def review_save(engine, bridge, args=CREATE, sid=SID, provenance="agent_created"):
    """The background-review fork calls skill_manage; if allowed, the write lands (lifecycle fires)."""
    bridge.origin = "review"
    directive = engine.hook_pre_tool_call(tool_name="skill_manage", args=args, session_id=sid)
    if directive is None:
        for op in args.get("operations", [args]):
            action = {"create": "created", "patch": "patched"}.get(op["action"], "edited")
            engine.hook_on_skill_lifecycle(action=action, skill_name=op["name"], provenance=provenance,
                                           session_id=sid)
    bridge.origin = "foreground"
    return directive


BAD_RUN = [("terminal", "error", "exit 1: HTTP 404 from DNR site"), ("write_file", "error", "parse failed")]


def test_save_learned_from_a_bad_run_is_held_and_queued(engine, bridge, ctx):
    turn(engine, "t1", "pull the Indiana DNR water test results", BAD_RUN)
    directive = review_save(engine, bridge)
    assert directive["action"] == "block"
    assert "p0001" in directive["message"] and "Do not retry" in directive["message"]
    staged = bridge.staged[0]
    assert staged["payload"] == {"action": "batch", "operations": CREATE["operations"]}
    assert staged["summary"].startswith("[fledge] create 'pull-water-results' — held: terminal was still failing")
    assert engine.ledger.get_skill("pull-water-results") is None  # nothing landed
    hold = engine.ledger.recent_holds(1)[0]
    assert hold["pending_id"] == "p0001" and hold["skills"] == ["pull-water-results"]
    # The user hears about it at the start of their next turn, in their own conversation.
    turn(engine, "t2", "ok what next")
    assert any("held skill 'pull-water-results'" in p and "/fledge diff p0001" in p for p in ctx.posted)


def test_the_same_held_write_is_not_queued_twice(engine, bridge):
    turn(engine, "t1", "x", BAD_RUN)
    review_save(engine, bridge)
    again = review_save(engine, bridge)
    assert again["action"] == "block" and "already holds" in again["message"]
    assert len(bridge.staged) == 1
    bridge.pending.clear()  # the user rejected it; a new attempt is queued again
    review_save(engine, bridge)
    assert len(bridge.staged) == 2


def test_clean_run_saves_and_skill_earns_its_wings(engine, bridge):
    turn(engine, "t1", "pull results", [("terminal", "error", "exit 1"), ("terminal", "ok", "")])
    assert review_save(engine, bridge) is None
    record = engine.ledger.get_skill("pull-water-results")
    assert record["state"] == FLEDGLING and record["agent_created"] == 1 and record["birth_level"] == "clean"
    for i in range(2, 6):
        turn(engine, f"t{i}", "pull results again", [("terminal", "ok", "")], skills=["pull-water-results"])
    turn(engine, "t6", "thanks")
    record = engine.ledger.get_skill("pull-water-results")
    assert record["state"] == FLEDGED and record["clean_uses"] >= 3


def test_save_from_a_run_the_user_then_corrects_is_grounded(engine, bridge, ctx):
    """'It always thinks it did a good job': the run looked fine, the user says otherwise next."""
    turn(engine, "t1", "pull the water test results", [("web_extract", "ok", "")])
    assert review_save(engine, bridge) is None  # nothing looked wrong yet
    assert engine.ledger.get_skill("pull-water-results")["state"] == FLEDGLING
    turn(engine, "t2", "That's wrong, you jumbled all the numbers")
    record = engine.ledger.get_skill("pull-water-results")
    assert record["state"] == ARCHIVED and bridge.archived == ["pull-water-results"]
    assert "learned from a run that went wrong" in record["note"]
    turn(engine, "t3", "ok")
    assert any("grounded 'pull-water-results'" in p and "hermes curator restore" in p for p in ctx.posted)


def test_skills_you_asked_for_are_reported_not_archived(engine, bridge):
    turn(engine, "t1", "x", [("terminal", "ok", "")])
    review_save(engine, bridge, provenance="local")
    bridge.origin = "foreground"
    engine.ledger.conn.execute("UPDATE skills SET agent_created = 0")
    turn(engine, "t2", "that's wrong")
    assert engine.ledger.get_skill("pull-water-results")["state"] == GROUNDED
    assert bridge.archived == []


def test_probation_grounds_a_skill_whose_runs_keep_failing(engine, bridge):
    turn(engine, "t1", "x")
    review_save(engine, bridge)
    turn(engine, "t2", "use it", BAD_RUN, skills=["pull-water-results"])
    turn(engine, "t3", "again", BAD_RUN, skills=["pull-water-results"])
    turn(engine, "t4", "hm")
    record = engine.ledger.get_skill("pull-water-results")
    assert record["state"] == ARCHIVED and "2 of its 2 runs" in record["note"]


def test_hand_edited_skill_rewrite_becomes_a_proposal(engine, bridge, ctx):
    bridge.hashes["lights"] = "h1"
    turn(engine, "t1", "x")
    review_save(engine, bridge, args={"operations": [{"action": "create", "name": "lights", "content": "v1"}]})
    assert engine.ledger.get_skill("lights")["agent_hash"] == "h1"
    bridge.hashes["lights"] = "h2"  # the user tuned SKILL.md by hand
    turn(engine, "t2", "turn on the porch lights", [("terminal", "ok", "")])
    patch = {"operations": [{"action": "patch", "name": "lights", "old_string": "a", "new_string": "b"}]}
    directive = review_save(engine, bridge, args=patch)
    assert directive["action"] == "block" and "proposal" in directive["message"]
    assert engine.ledger.get_skill("lights")["human_tuned"] == 1
    assert bridge.staged[-1]["summary"].startswith("[fledge] proposal:")
    turn(engine, "t3", "thanks")
    assert any("which you edited by hand" in p for p in ctx.posted)


def test_foreground_rewrite_of_hand_tuned_skill_asks_first(engine, bridge):
    engine.ledger.set_human_tuned("lights", True, manual=True)
    engine.hook_pre_llm_call(session_id=SID, turn_id="t1", user_message="update the lights skill")
    directive = engine.hook_pre_tool_call(tool_name="skill_manage", session_id=SID, args={
        "operations": [{"action": "patch", "name": "lights", "old_string": "a", "new_string": "b"}]})
    assert directive["action"] == "approve" and "edited by hand" in directive["message"]
    assert directive["rule_key"] == "fledge:hand_tuned:lights"


def test_hand_edit_lock_mode_sets_hermes_content_lock(engine, bridge, ctx):
    ctx.config["on_hand_edit"] = "lock"
    engine.settings_loader.invalidate()
    bridge.hashes["lights"] = "h1"
    turn(engine, "t1", "x")
    review_save(engine, bridge, args={"operations": [{"action": "create", "name": "lights", "content": "v1"}]})
    bridge.hashes["lights"] = "h2"
    review_save(engine, bridge, args={"operations": [{"action": "patch", "name": "lights", "old_string": "a",
                                                      "new_string": "b"}]})
    assert bridge.locked == {"lights": True}


def test_foreground_save_after_errors_asks_and_unattended_stages(engine, bridge):
    engine.hook_pre_llm_call(session_id=SID, turn_id="t1", user_message="save that as a skill")
    for tool, status, error in BAD_RUN:
        engine.hook_post_tool_call(tool_name=tool, session_id=SID, turn_id="t1", status=status, error_message=error)
    directive = engine.hook_pre_tool_call(tool_name="skill_manage", args=CREATE, session_id=SID, turn_id="t1")
    assert directive["action"] == "approve" and "went wrong" in directive["message"]
    bridge.unattended = True  # cron / kanban: nobody can answer, so queue it
    directive = engine.hook_pre_tool_call(tool_name="skill_manage", args=CREATE, session_id=SID, turn_id="t1")
    assert directive["action"] == "block" and bridge.staged


def test_watch_mode_records_but_never_blocks(engine, bridge, ctx):
    ctx.config["mode"] = "watch"
    engine.settings_loader.invalidate()
    turn(engine, "t1", "x", BAD_RUN)
    assert review_save(engine, bridge) is None
    assert not bridge.staged
    assert engine.ledger.recent_events(5, kinds=["would-hold"])


def test_off_mode_does_nothing(engine, bridge, ctx):
    ctx.config["mode"] = "off"
    engine.settings_loader.invalidate()
    turn(engine, "t1", "x", BAD_RUN)
    assert review_save(engine, bridge) is None
    assert engine.ledger.get_skill("pull-water-results") is None


def test_review_fork_turns_never_count_as_user_turns(engine, bridge):
    turn(engine, "t1", "x", [("terminal", "ok", "")])
    bridge.origin = "review"
    engine.hook_pre_llm_call(session_id=SID, turn_id="fork", user_message="Review the conversation. Wrong? fix it")
    engine.hook_post_tool_call(tool_name="terminal", session_id=SID, turn_id="fork", status="error")
    bridge.origin = "foreground"
    latest = engine.tracker.latest_finished_turn(SID)
    assert latest.turn_id == "t1" and latest.correction is None and not latest.unresolved


def test_review_fork_skill_reads_are_not_uses(engine, bridge):
    turn(engine, "t1", "x")
    bridge.origin = "review"
    engine.hook_on_skill_lifecycle(action="loaded", skill_name="csv-cleaner", session_id=SID)
    bridge.origin = "foreground"
    assert engine.ledger.get_skill("csv-cleaner") is None


def test_gate_fails_open_on_internal_error(engine, bridge):
    engine.ledger.set_human_tuned("lights", False)
    engine.ledger.set_hash("lights", "h1")
    bridge.fail_hash = True
    bridge.origin = "review"
    directive = engine.hook_pre_tool_call(tool_name="skill_manage", session_id=SID, args={
        "operations": [{"action": "patch", "name": "lights", "old_string": "a", "new_string": "b"}]})
    assert directive is None


def test_observer_hooks_swallow_errors(engine, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(engine.tracker, "begin_turn", boom)
    assert engine.hook_pre_llm_call(session_id=SID, turn_id="t1", user_message="x") is None


def test_approved_replay_is_not_grounded_by_its_bad_run(engine, bridge):
    turn(engine, "t1", "x", BAD_RUN)
    review_save(engine, bridge)  # held
    # The user runs /skills approve: Hermes replays the write directly (no pre_tool_call), lifecycle fires.
    engine.hook_on_skill_lifecycle(action="created", skill_name="pull-water-results", provenance="agent_created",
                                   session_id=SID)
    record = engine.ledger.get_skill("pull-water-results")
    assert record["state"] == FLEDGLING and record["birth_level"] == "approved"
    turn(engine, "t2", "that's wrong")
    assert engine.ledger.get_skill("pull-water-results")["state"] == FLEDGLING


def test_agent_file_edit_of_a_skill_is_not_a_hand_edit(engine, bridge):
    turn(engine, "t1", "x")
    bridge.hashes["lights"] = "h1"
    review_save(engine, bridge, args={"operations": [{"action": "create", "name": "lights", "content": "v1"}]})
    bridge.paths["/skills/lights/SKILL.md"] = ("lights", "/skills/lights")
    engine.hook_pre_llm_call(session_id=SID, turn_id="t2", user_message="tweak the lights skill file")
    engine.hook_post_tool_call(tool_name="write_file", args={"path": "/skills/lights/SKILL.md"}, session_id=SID,
                               turn_id="t2", status="ok")
    assert engine.ledger.get_skill("lights")["agent_hash"] == "hash:/skills/lights"


def test_undo_after_a_turn_counts_against_its_skills(engine, bridge):
    turn(engine, "t1", "x")
    review_save(engine, bridge)
    turn(engine, "t2", "use it", skills=["pull-water-results"])
    engine.hook_pre_command(command="undo", session_key=SID)
    turn(engine, "t3", "hm")
    uses = engine.ledger.recent_uses("pull-water-results")
    assert uses[0]["level"] == "bad" and "you ran /undo right after" in uses[0]["reasons"]


def test_bare_rollback_only_lists_and_does_not_count(engine, bridge):
    turn(engine, "t1", "x", [("patch", "ok", "")])
    engine.hook_pre_command(command="rollback", session_key=SID, args_raw="")
    assert engine.tracker.latest_finished_turn(SID).commands == []
    engine.hook_pre_command(command="rollback", session_key=SID, args_raw="1")
    assert engine.tracker.latest_finished_turn(SID).commands == ["rollback"]


def test_notices_wait_until_a_conversation_is_bound(engine, bridge, ctx):
    ctx.post_ok = False
    turn(engine, "t1", "x", BAD_RUN)
    review_save(engine, bridge)
    turn(engine, "t2", "next")
    assert ctx.posted == []
    ctx.post_ok = True
    turn(engine, "t3", "next")
    assert any("held skill" in p for p in ctx.posted)


def test_slash_command_views(engine, bridge, ctx):
    turn(engine, "t1", "x", BAD_RUN)
    review_save(engine, bridge)
    status = engine.command("")
    assert "Held for your review" in status and "p0001" in status
    engine.command("protect smart-home")
    assert "smart-home" in engine.command("status")
    assert "hand-tuned" in engine.command("skill smart-home")
    assert "no record" in engine.command("skill nope")
    assert engine.command("mode watch") == "Fledge mode set to 'watch'."
    assert ctx.config["mode"] == "watch" and engine.s.mode == "watch"
    assert "doctor" in engine.command("doctor").lower()
    assert "Usage" in engine.command("protect")
    assert engine.command("release pull-water-results").startswith("Fledge: 'pull-water-results' is fledged")


@pytest.mark.parametrize("bad", ["loud", 0, "x"])
def test_bad_settings_fall_back_to_defaults(ctx, bridge, bad):
    from conftest import fledge
    ctx.config.update(mode=bad, graduate_after=bad, strictness=bad)
    eng = fledge.build(ctx, bridge=bridge)
    assert eng.s.mode == "enforce" and eng.s.graduate_after == 3 and eng.s.strictness == "standard"
    eng.close()


def test_review_held_writes_from_any_chat(engine, bridge):
    turn(engine, "t1", "x", BAD_RUN)
    review_save(engine, bridge)
    assert "p0001" in engine.command("pending")
    assert engine.command("diff p0001").startswith("# Pending skill write p0001")
    assert "no held write" in engine.command("approve zzzz")  # only Fledge's own holds
    assert engine.command("approve p0001").startswith("Approved")
    assert bridge.commands == [["diff", "p0001"], ["approve", "p0001"]]
    assert "not holding" in engine.command("pending")
    assert "Held for your review" not in engine.command("status")


def test_denied_approval_uses_the_session_id_hermes_sends(engine, bridge):
    """Two live sessions: the session_key can't be matched, but Hermes' approval hook carries session_id."""
    engine.hook_pre_llm_call(session_id="sess-a", turn_id="ta", user_message="one")
    engine.hook_pre_llm_call(session_id="sess-b", turn_id="tb", user_message="two")
    engine.hook_post_approval_response(choice="deny", session_key="agent:main:cli:x", session_id="sess-b",
                                       turn_id="")
    assert engine.tracker.current_turn("sess-b").denials == 1
    assert engine.tracker.current_turn("sess-a").denials == 0


def test_held_batch_keeps_the_legacy_default_name_for_replay(engine, bridge):
    turn(engine, "t1", "pull the water results", BAD_RUN)
    args = {"name": "pull-water-results", "operations": [
        {"action": "patch", "old_string": "steps", "new_string": "steps (retry on 404)"}]}
    bridge.hashes["pull-water-results"] = ""
    directive = review_save(engine, bridge, args=args)
    assert directive["action"] == "block"
    payload = bridge.staged[-1]["payload"]
    assert payload["action"] == "batch" and payload["name"] == "pull-water-results"
    assert payload["operations"] == args["operations"]


def test_batch_with_several_ops_on_one_skill_is_attributed_to_its_run(engine, bridge):
    """Live finding (Hermes 0.22.7): the review patched one skill with a two-op batch; the second op's
    lifecycle event found no in-flight record and was logged as saved outside a gated call."""
    turn(engine, "t1", "pull the water results")
    review_save(engine, bridge)
    args = {"operations": [{"action": "patch", "name": "pull-water-results", "old_string": "a", "new_string": "b"},
                           {"action": "patch", "name": "pull-water-results", "old_string": "c", "new_string": "d"}]}
    assert review_save(engine, bridge, args=args) is None
    written = [e["detail"] for e in engine.ledger.recent_events(20) if e["kind"] == "written"]
    assert len(written) == 3 and not any("approved" in d for d in written)


def test_cleanup_command_does_not_hide_a_failed_pull(engine, bridge):
    """The agent retried the failing script, then cleaned up a scratch file: the review save is held."""
    engine.hook_pre_llm_call(session_id=SID, turn_id="t1", user_message="Run ./fetch_water.sh into results.csv")
    for command, status in [("./fetch_water.sh > results.csv", "error"), ("./fetch_water.sh > results.csv", "error"),
                            ("rm -f fetch_stderr.txt", "ok")]:
        engine.hook_post_tool_call(tool_name="terminal", args={"command": command}, session_id=SID, turn_id="t1",
                                   status=status, error_message="exit 1: HTTP 404" if status == "error" else None)
    engine.hook_on_session_end(session_id=SID, turn_id="t1", completed=True)
    directive = review_save(engine, bridge)
    assert directive["action"] == "block" and bridge.staged
    assert "terminal (./fetch_water.sh) failed 2 times in a row" in bridge.staged[-1]["summary"]


def _graduate(engine, bridge):
    turn(engine, "t0", "x")
    review_save(engine, bridge)
    for i in range(1, 4):
        turn(engine, f"c{i}", "use it", skills=["pull-water-results"])
    turn(engine, "c4", "next")
    assert engine.ledger.get_skill("pull-water-results")["state"] == FLEDGED


def test_relapsed_skill_is_not_grounded_by_a_clean_run(engine, bridge):
    """Review finding: after a relapse the old bad-run count survived, so the next run (even a clean
    one) grounded and archived the skill: '2 of its 6 runs on probation went wrong (no problems found)'."""
    _graduate(engine, bridge)
    for t in ("b1", "b2"):
        turn(engine, t, "use it", BAD_RUN, skills=["pull-water-results"])
    turn(engine, "b3", "next")
    rec = engine.ledger.get_skill("pull-water-results")
    assert rec["state"] == FLEDGLING and rec["bad_uses"] == 0 and rec["total_bad"] == 2
    turn(engine, "g1", "use it", skills=["pull-water-results"])
    turn(engine, "g2", "next")
    assert engine.ledger.get_skill("pull-water-results")["state"] == FLEDGLING and bridge.archived == []


def test_restored_skill_starts_a_fresh_probation(engine, bridge):
    turn(engine, "t0", "x")
    review_save(engine, bridge)
    for t in ("b1", "b2"):
        turn(engine, t, "use it", BAD_RUN, skills=["pull-water-results"])
    turn(engine, "b3", "next")
    assert engine.ledger.get_skill("pull-water-results")["state"] == ARCHIVED
    engine.hook_on_skill_lifecycle(action="restored", skill_name="pull-water-results", session_id=SID)
    turn(engine, "g1", "use it", skills=["pull-water-results"])
    turn(engine, "g2", "next")
    rec = engine.ledger.get_skill("pull-water-results")
    assert rec["state"] == FLEDGLING and rec["clean_uses"] == 1 and rec["bad_uses"] == 0


def test_declining_fledges_own_prompt_is_not_evidence_against_the_turn(engine, bridge):
    """Review finding: Hermes reports Fledge's own ask as pattern_key 'plugin_rule:fledge:...'
    (tools/approval.py request_tool_approval); a 'deny' there counted as a denied action."""
    engine.hook_pre_llm_call(session_id=SID, turn_id="t1", user_message="x")
    for tool, status, error in BAD_RUN:
        engine.hook_post_tool_call(tool_name=tool, args={}, session_id=SID, turn_id="t1", status=status,
                                   error_message=error)
    directive = engine.hook_pre_tool_call(tool_name="skill_manage", args=CREATE, session_id=SID, turn_id="t1")
    assert directive["action"] == "approve"
    engine.hook_post_approval_response(choice="deny", session_id=SID, turn_id="t1",
                                       pattern_key="plugin_rule:" + directive["rule_key"])
    assert engine.tracker.current_turn(SID).denials == 0
    engine.hook_post_approval_response(choice="deny", session_id=SID, turn_id="t1", pattern_key="rm_recursive")
    assert engine.tracker.current_turn(SID).denials == 1


def test_curator_delete_of_a_hand_edited_skill_is_held_and_replayable(engine, bridge):
    bridge.hashes["lights"] = "h1"
    turn(engine, "t1", "x")
    review_save(engine, bridge, args={"operations": [{"action": "create", "name": "lights", "content": "v1"}]})
    bridge.hashes["lights"] = "h2"  # the user tuned it by hand
    turn(engine, "t2", "turn on the porch lights")
    delete = {"operations": [{"action": "delete", "name": "lights", "absorbed_into": "home-automation"}]}
    bridge.origin = "review"
    directive = engine.hook_pre_tool_call(tool_name="skill_manage", args=delete, session_id=SID)
    bridge.origin = "foreground"
    assert directive["action"] == "block"
    assert bridge.staged[-1]["payload"] == {"action": "batch", "operations": delete["operations"]}
    assert bridge.staged[-1]["summary"].startswith("[fledge] proposal:")


def test_the_birth_turn_is_not_counted_as_a_use(engine, bridge):
    """Review finding: viewing a skill in the same turn that saved it counted toward graduating it."""
    engine.hook_pre_llm_call(session_id=SID, turn_id="t1", user_message="save this as a skill")
    assert engine.hook_pre_tool_call(tool_name="skill_manage", args=CREATE, session_id=SID, turn_id="t1") is None
    engine.hook_on_skill_lifecycle(action="created", skill_name="pull-water-results", provenance="local",
                                   session_id=SID)
    engine.hook_on_skill_lifecycle(action="loaded", skill_name="pull-water-results", session_id=SID)
    engine.hook_on_session_end(session_id=SID, turn_id="t1", completed=True)
    turn(engine, "t2", "thanks")
    rec = engine.ledger.get_skill("pull-water-results")
    assert rec["state"] == FLEDGLING and rec["clean_uses"] == 0
    assert engine.ledger.recent_uses("pull-water-results") == []


def test_turns_recorded_before_mode_off_never_ground_later(engine, bridge, ctx):
    turn(engine, "t1", "x")
    review_save(engine, bridge)
    turn(engine, "t2", "use it", BAD_RUN, skills=["pull-water-results"])
    ctx.config["mode"] = "off"
    engine.settings_loader.invalidate()
    engine.hook_on_session_finalize(session_id=SID)
    ctx.config["mode"] = "enforce"
    engine.settings_loader.invalidate()
    turn(engine, "t3", "hello again")
    assert engine.ledger.recent_uses("pull-water-results") == []


def test_failure_hidden_inside_a_compound_command_is_seen(engine, bridge):
    """Live finding (Hermes 0.22.7, take 3): the agent ran the failing script inside one compound command that
    echoed its $? and then cat'ed files; Hermes reported exit_code 0, so the failure was invisible."""
    import json
    from test_evidence import TAKE_3, TAKE_3_OUT
    engine.hook_pre_llm_call(session_id=SID, turn_id="t1", user_message="Run ./fetch_water.sh into results.csv")
    for _ in range(2):  # the agent retried once
        engine.hook_post_tool_call(tool_name="terminal", args={"command": TAKE_3}, session_id=SID, turn_id="t1",
                                   status="ok", result=json.dumps({"output": TAKE_3_OUT, "exit_code": 0}))
    engine.hook_post_tool_call(tool_name="terminal", args={"command": "cp /tmp/water_stdout.csv results.csv"},
                               session_id=SID, turn_id="t1", status="ok",
                               result=json.dumps({"output": "", "exit_code": 0}))
    engine.hook_on_session_end(session_id=SID, turn_id="t1", completed=True)
    directive = review_save(engine, bridge)
    assert directive["action"] == "block"
    assert "terminal (./fetch_water.sh) failed 2 times in a row" in bridge.staged[-1]["summary"]


def test_a_compound_command_whose_parts_succeeded_stays_clean(engine, bridge):
    import json
    engine.hook_pre_llm_call(session_id=SID, turn_id="t1", user_message="run the tests")
    engine.hook_post_tool_call(tool_name="terminal", args={"command": 'pytest -q; echo "exit=$?"'}, session_id=SID,
                               turn_id="t1", status="ok", result=json.dumps({"output": "5 passed\nexit=0"}))
    engine.hook_on_session_end(session_id=SID, turn_id="t1", completed=True)
    assert review_save(engine, bridge) is None


def test_retry_written_as_one_if_else_command_is_held(engine, bridge):
    import json
    from test_evidence import TAKE_4, TAKE_4_OUT
    engine.hook_pre_llm_call(session_id=SID, turn_id="t1", user_message="Run ./fetch_water.sh, retry once")
    engine.hook_post_tool_call(tool_name="terminal", args={"command": TAKE_4}, session_id=SID, turn_id="t1",
                               status="ok", result=json.dumps({"output": TAKE_4_OUT, "exit_code": 0}))
    engine.hook_on_session_end(session_id=SID, turn_id="t1", completed=True)
    assert review_save(engine, bridge)["action"] == "block"
    assert "terminal (./fetch_water.sh) failed 2 times in a row" in bridge.staged[-1]["summary"]


def test_review_decisions_are_logged_with_real_words(engine, bridge):
    """Live finding (Hermes 0.22.7): /fledge reject logged the event kind 'rejectd'."""
    turn(engine, "t1", "x", BAD_RUN)
    review_save(engine, bridge)
    engine._review("reject", "p0001")
    assert engine.ledger.recent_events(1)[0]["kind"] == "rejected"


def test_a_skill_deleted_before_its_use_is_judged_is_not_resurrected(engine, bridge):
    """Live finding (Hermes 0.22.7): a turn used a skill, the skill was moved away and forgotten, and the
    turn's later judgement re-created it as a veteran."""
    turn(engine, "t1", "rank the readings", skills=["csv-wrangling"])
    bridge.missing.add("csv-wrangling")
    engine.ledger.forget("csv-wrangling")
    turn(engine, "t2", "thanks")  # the next turn finalizes t1
    engine.hook_on_session_finalize(session_id=SID)
    assert engine.ledger.get_skill("csv-wrangling") is None


def test_a_staged_hold_still_blocks_when_fledges_ledger_fails(engine, bridge, monkeypatch):
    """Live finding (Hermes 0.22.7, another process held Fledge's DB locked): the write was staged in Hermes'
    queue, recording it raised, and failing open then saved the skill too, leaving a duplicate to approve."""
    import sqlite3
    turn(engine, "t1", "pull the results", BAD_RUN)

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(engine.ledger, "add_hold", locked)
    directive = review_save(engine, bridge)
    assert directive["action"] == "block" and "p0001" in directive["message"]
    assert engine.ledger.get_skill("pull-water-results") is None  # not also saved
    # The user can still review it through Fledge even though Fledge never recorded it.
    assert engine._review("reject", "p0001").startswith("Rejected")
    assert "p0001" not in bridge.pending


def test_a_failure_before_staging_still_fails_open(engine, bridge, monkeypatch):
    turn(engine, "t1", "pull the results", BAD_RUN)

    def broken(*_a, **_k):
        raise RuntimeError("bug")
    monkeypatch.setattr(engine.ledger, "find_hold", broken)
    assert review_save(engine, bridge) is None  # the write goes through
    assert not bridge.staged


def test_an_agent_write_fledge_missed_is_not_taken_for_a_hand_edit(engine, bridge):
    """Live finding (Hermes 0.22.7): while Fledge's DB was locked, an agent patch landed unrecorded; the next
    autonomous patch saw a changed fingerprint and called it a hand edit (a false positive)."""
    bridge.hashes["lights"] = "h1"
    turn(engine, "t1", "x")
    review_save(engine, bridge, args={"operations": [{"action": "create", "name": "lights", "content": "v1"}]})
    bridge.hashes["lights"] = "h2"
    bridge.agent_wrote_last.add("lights")  # Hermes stamped the write; no file is newer than the stamp
    turn(engine, "t2", "turn on the porch lights", [("terminal", "ok", "")])
    patch = {"operations": [{"action": "patch", "name": "lights", "old_string": "a", "new_string": "b"}]}
    assert review_save(engine, bridge, args=patch) is None
    record = engine.ledger.get_skill("lights")
    assert not record["human_tuned"] and record["agent_hash"] == "h2"


def test_a_bad_run_save_the_queue_cannot_take_lands_on_probation(engine, bridge, ctx):
    """Hermes' stage_write returns a record even when its disk write failed (tools/write_approval.py:74).
    Blocking then would drop a skill nobody could ever approve: fail open, on probation, with a notice."""
    bridge.stage_fails = True
    turn(engine, "t1", "pull the results", BAD_RUN)
    assert review_save(engine, bridge) is None  # the write goes through
    record = engine.ledger.get_skill("pull-water-results")
    assert record["state"] == FLEDGLING
    assert any(e["kind"] == "hold-failed" for e in engine.ledger.recent_events(10))
    turn(engine, "t2", "ok")
    assert any("could not hold 'pull-water-results'" in p and "saved on probation" in p for p in ctx.posted)


def test_a_hand_edit_is_never_overwritten_even_when_the_queue_fails(engine, bridge):
    bridge.hashes["lights"] = "h1"
    turn(engine, "t1", "x")
    review_save(engine, bridge, args={"operations": [{"action": "create", "name": "lights", "content": "v1"}]})
    bridge.hashes["lights"] = "h2"  # hand edit
    bridge.stage_fails = True
    turn(engine, "t2", "porch lights", [("terminal", "ok", "")])
    patch = {"operations": [{"action": "patch", "name": "lights", "old_string": "a", "new_string": "b"}]}
    assert review_save(engine, bridge, args=patch)["action"] == "block"


def test_no_to_the_agents_question_does_not_count_against_a_bad_turn(engine, bridge):
    """A failing grep (2) plus a bare "No." (1) would reach 3 = bad; but the No. answered a question."""
    turn(engine, "t1", "find the readings", [("terminal", "error", "exit 1")])
    history = [{"role": "user", "content": "find the readings"},
               {"role": "assistant", "content": "Nothing matched. Should I search the archive too?"},
               {"role": "user", "content": "No."}]
    engine.hook_pre_llm_call(session_id=SID, turn_id="t2", user_message="No.", conversation_history=history,
                             task_id=SID)
    assert not any(e["kind"] == "corrected" for e in engine.ledger.recent_events(20))


def _hermes_like_binding(ctx):
    """Model Hermes: post_message only reaches a conversation when a session is bound in the CALLING
    context (hermes_cli/plugin_messages.py post -> gateway.session_context.bound_session_key), and the
    background-review fork runs in a plain thread with nothing bound."""
    import contextvars
    bound = contextvars.ContextVar("session_key", default="")
    real_post = ctx.post_message

    def post_message(text):
        if not bound.get():
            from types import SimpleNamespace
            return SimpleNamespace(ok=False, error="no_conversation")
        return real_post(text)

    ctx.post_message = post_message
    return bound


def _in_thread(fn, *args):
    """Run *fn* the way Hermes runs the review fork: a new thread, empty context."""
    import threading
    out = {}
    thread = threading.Thread(target=lambda: out.setdefault("r", fn(*args)))
    thread.start()
    thread.join(5)
    return out.get("r")


def _join_notice_threads():
    import threading
    for thread in [t for t in threading.enumerate() if t.name == "fledge-notice"]:
        thread.join(5)


def test_a_hold_notice_is_posted_right_away_from_the_unbound_review_thread(engine, bridge, ctx):
    """Live finding (0.23.0): the hold notice only appeared after the user typed again, because the
    review fork has no session bound. Fledge now posts from the session's own remembered context."""
    import contextvars
    bound = _hermes_like_binding(ctx)
    foreground = contextvars.copy_context()
    foreground.run(bound.set, "agent:main:local:dm:" + SID)
    foreground.run(turn, engine, "t1", "pull the results", BAD_RUN)
    _in_thread(review_save, engine, bridge)  # unbound, like Hermes' review thread
    _join_notice_threads()
    assert any("held skill 'pull-water-results'" in p for p in ctx.posted)
    foreground.run(turn, engine, "t2", "ok")
    assert sum("held skill 'pull-water-results'" in p for p in ctx.posted) == 1  # posted once, not again


def test_a_local_notice_from_the_review_is_shown_again_at_the_next_turn(engine, bridge, ctx):
    """Live finding (0.23.0): the classic CLI reads session events only during a turn, so a notice the
    review thread posted between turns never appeared. Local posts are re-published (same key) once."""
    import contextvars
    ctx.local = True
    bound = _hermes_like_binding(ctx)
    foreground = contextvars.copy_context()
    foreground.run(bound.set, "agent:main:local:dm:" + SID)
    foreground.run(turn, engine, "t1", "pull the results", BAD_RUN)
    _in_thread(review_save, engine, bridge)
    _join_notice_threads()
    assert len(ctx.posted) == 1 and not ctx.edits
    foreground.run(turn, engine, "t2", "ok")
    assert ctx.edits == [ctx.posted[0]]  # same text, same message: replaced in place, not duplicated
    foreground.run(turn, engine, "t3", "ok")
    assert len(ctx.edits) == 1 and len(ctx.posted) == 1


def test_a_messaging_notice_is_not_reposted(engine, bridge, ctx):
    import contextvars
    bound = _hermes_like_binding(ctx)  # ctx.local False: a Telegram-style send, shown at once
    foreground = contextvars.copy_context()
    foreground.run(bound.set, "agent:main:telegram:dm:" + SID)
    foreground.run(turn, engine, "t1", "pull the results", BAD_RUN)
    _in_thread(review_save, engine, bridge)
    _join_notice_threads()
    foreground.run(turn, engine, "t2", "ok")
    assert len(ctx.posted) == 1 and not ctx.edits


def test_a_notice_that_cannot_be_posted_waits_for_the_next_turn(engine, bridge, ctx):
    import contextvars
    bound = _hermes_like_binding(ctx)
    turn(engine, "t1", "pull the results", BAD_RUN)  # this session's hooks ran unbound: nothing to borrow
    _in_thread(review_save, engine, bridge)
    _join_notice_threads()
    assert not ctx.posted
    foreground = contextvars.copy_context()
    foreground.run(bound.set, "agent:main:local:dm:" + SID)
    foreground.run(turn, engine, "t2", "ok")
    assert any("held skill 'pull-water-results'" in p for p in ctx.posted)


def test_a_closed_session_context_is_not_reused(engine, bridge, ctx):
    import contextvars
    bound = _hermes_like_binding(ctx)
    foreground = contextvars.copy_context()
    foreground.run(bound.set, "agent:main:local:dm:" + SID)
    foreground.run(turn, engine, "t1", "pull the results", BAD_RUN)
    engine.hook_on_session_finalize(session_id=SID)
    assert SID not in engine.notifier._contexts


def test_skill_report_says_an_approved_save_in_words(engine, bridge):
    from fledge import report
    assert report.birth_phrase("approved") == "you approved it from the review queue"
    assert report.birth_phrase(None) == "the run was not seen"


def test_doctor_says_which_settings_it_ignored(engine, ctx):
    """Live finding: a typo'd setting silently fell back to its default; /fledge doctor showed the default
    with no hint the user's value had been ignored (only agent.log said so)."""
    ctx.config["mode"] = "banana"
    ctx.config["graduate_after"] = -4
    engine.settings_loader.invalidate()
    out = engine.command("doctor")
    assert "mode=enforce" in out
    assert "!! setting 'mode' ignored" in out and "'banana'" in out
    assert "!! setting 'graduate_after' ignored" in out
    ctx.config["mode"] = "watch"
    ctx.config["graduate_after"] = 3
    engine.settings_loader.invalidate()
    out = engine.command("doctor")
    assert "ignored" not in out and "mode=watch" in out
