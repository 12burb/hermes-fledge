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
