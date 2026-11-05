from fledge.evidence import EvidenceTracker, score_turn


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def run_turn(tracker, sid, turn_id, message, events, *, failed=False):
    tracker.begin_turn(sid, turn_id, message)
    for tool, status, error in events:
        tracker.record_tool(sid, turn_id, tool, status, error)
    tracker.end_turn(sid, turn_id, completed=not failed, failed=failed)
    return tracker.current_turn(sid)


def test_recovered_error_is_clean():
    tracker = EvidenceTracker()
    turn = run_turn(tracker, "s", "t1", "fetch data", [("terminal", "error", "exit 1"), ("terminal", "ok", "")])
    verdict = score_turn(turn)
    assert verdict.level == "clean" and verdict.score == 0


def test_unresolved_heavy_errors_make_a_bad_run():
    tracker = EvidenceTracker()
    turn = run_turn(tracker, "s", "t1", "fetch data",
                    [("terminal", "error", "exit 1: 404"), ("write_file", "error", "permission denied")])
    verdict = score_turn(turn)
    assert verdict.bad and verdict.score == 4
    assert any("terminal was still failing" in r for r in verdict.reasons)


def test_single_unresolved_error_is_only_uncertain():
    tracker = EvidenceTracker()
    turn = run_turn(tracker, "s", "t1", "x", [("terminal", "error", "exit 2")])
    assert score_turn(turn).level == "uncertain"
    assert score_turn(turn, bad_threshold=2).bad  # strict


def test_a_step_that_keeps_failing_until_the_end_is_bad():
    tracker = EvidenceTracker()
    turn = run_turn(tracker, "s", "t1", "pull results", [
        ("terminal", "ok", ""), ("terminal", "error", "exit 1: HTTP 404"), ("read_file", "ok", ""),
        ("terminal", "error", "exit 1: HTTP 404")])
    verdict = score_turn(turn)
    assert verdict.bad and verdict.score == 3
    assert verdict.reasons[0].startswith("terminal failed 2 times in a row and never recovered")


def test_failed_turn_is_bad():
    tracker = EvidenceTracker()
    turn = run_turn(tracker, "s", "t1", "x", [], failed=True)
    assert score_turn(turn).bad


def test_meta_tools_are_ignored():
    tracker = EvidenceTracker()
    turn = run_turn(tracker, "s", "t1", "x", [("skill_manage", "error", "held"), ("memory", "error", "full")])
    assert score_turn(turn).level == "clean"


def test_correction_in_next_message_judges_previous_turn():
    tracker = EvidenceTracker()
    first = run_turn(tracker, "s", "t1", "pull the water test results", [("web_extract", "ok", "")])
    assert score_turn(first).level == "clean"
    previous = tracker.begin_turn("s", "t2", "That's wrong, you jumbled the numbers")
    assert previous is first and previous.followup_seen
    verdict = score_turn(previous)
    assert verdict.bad and verdict.final
    assert "you corrected it right after" in verdict.reasons[0]


def test_undo_command_judges_last_finished_turn():
    tracker = EvidenceTracker()
    turn = run_turn(tracker, "s", "t1", "edit config", [("patch", "ok", "")])
    tracker.record_command("s", "undo")
    assert score_turn(turn).bad and turn.commands == ["undo"]


def test_denials_count():
    tracker = EvidenceTracker()
    tracker.begin_turn("s", "t1", "deploy")
    tracker.record_denial("s", "t1", "deny")
    tracker.record_denial("s", "t1", "once")  # approvals do not count
    assert tracker.current_turn("s").denials == 1


def test_drain_settled_after_followup_and_after_quiet_period():
    clock = Clock()
    tracker = EvidenceTracker(clock=clock)
    run_turn(tracker, "a", "a1", "x", [])
    tracker.begin_turn("a", "a2", "next")
    settled = tracker.drain_settled(settle_seconds=3600)
    assert [t.turn_id for t in settled] == ["a1"]
    tracker.end_turn("a", "a2")
    assert tracker.drain_settled(settle_seconds=3600) == []
    clock.t += 3601
    assert [t.turn_id for t in tracker.drain_settled(settle_seconds=3600)] == ["a2"]
    assert tracker.drain_settled(settle_seconds=3600) == []


def test_close_session_settles_everything():
    tracker = EvidenceTracker()
    tracker.begin_turn("a", "a1", "x")
    tracker.close_session("a")
    assert [t.turn_id for t in tracker.drain_settled(settle_seconds=10_000)] == ["a1"]


def test_latest_finished_turn_is_what_a_review_sees():
    tracker = EvidenceTracker()
    run_turn(tracker, "s", "t1", "x", [("terminal", "error", "boom")])
    assert tracker.latest_finished_turn("s").turn_id == "t1"
    tracker.begin_turn("s", "t2", "next")
    assert tracker.latest_finished_turn("s").turn_id == "t1"  # t2 is still running


def test_resolve_session_from_partial_ids():
    clock = Clock()
    tracker = EvidenceTracker(clock=clock)
    tracker.begin_turn("sess-1", "turn-1", "x", task_id="task-1")
    assert tracker.resolve_session(task_id="task-1") == "sess-1"
    assert tracker.resolve_session(turn_id="turn-1") == "sess-1"
    assert tracker.resolve_session(session_key="agent:main:telegram:dm:sess-1") == "sess-1"
    assert tracker.resolve_session() == "sess-1"  # the only recent session
    tracker.begin_turn("sess-2", "turn-2", "x")
    assert tracker.resolve_session() == ""  # ambiguous: no guess


def test_skill_use_attaches_to_current_turn():
    tracker = EvidenceTracker()
    tracker.begin_turn("s", "t1", "x")
    assert tracker.record_skill_use("s", "csv-cleaner") == "t1"
    assert "csv-cleaner" in tracker.current_turn("s").skills_used


def test_a_different_command_succeeding_does_not_recover_a_failed_one():
    """Live finding (Hermes 0.22.7): the agent ran ./fetch_water.sh (exit 1), then `rm fetch_stderr.txt`.
    Keyed per tool, the rm 'recovered' terminal and the failed pull scored clean."""
    from fledge.evidence import command_key
    tracker = EvidenceTracker()
    tracker.begin_turn("s", "t1", "Run ./fetch_water.sh and turn what it returns into results.csv")
    for command, status, error in [("./fetch_water.sh > results.csv 2> fetch_stderr.txt", "error", "exit 1: 404"),
                                   ("rm fetch_stderr.txt", "ok", "")]:
        tracker.record_tool("s", "t1", "terminal", status, error, key=command_key("terminal", {"command": command}))
    tracker.end_turn("s", "t1", completed=True)
    verdict = score_turn(tracker.current_turn("s"))
    assert verdict.level == "uncertain" and verdict.score == 2
    assert any("terminal (./fetch_water.sh) was still failing" in r for r in verdict.reasons)


def test_rerunning_the_same_program_is_still_a_recovery():
    from fledge.evidence import command_key
    tracker = EvidenceTracker()
    tracker.begin_turn("s", "t1", "x")
    for command, status in [("cd /w && ./fetch_water.sh > out.csv", "error"), ("./fetch_water.sh --retry > out.csv", "ok")]:
        tracker.record_tool("s", "t1", "terminal", status, "", key=command_key("terminal", {"command": command}))
    tracker.end_turn("s", "t1", completed=True)
    assert score_turn(tracker.current_turn("s")).level == "clean"


def test_retried_failing_program_with_cleanup_after_is_bad():
    from fledge.evidence import command_key
    tracker = EvidenceTracker()
    tracker.begin_turn("s", "t1", "x")
    for command, status in [("./fetch_water.sh > r.csv", "error"), ("./fetch_water.sh > r.csv", "error"),
                            ("rm -f err.txt", "ok")]:
        tracker.record_tool("s", "t1", "terminal", status, "exit 1", key=command_key("terminal", {"command": command}))
    tracker.end_turn("s", "t1", completed=True)
    verdict = score_turn(tracker.current_turn("s"))
    assert verdict.bad and "failed 2 times in a row" in verdict.summary()


def test_command_key_shapes():
    from fledge.evidence import command_key
    assert command_key("terminal", {"command": "FOO=1 python3 -u scripts/pull.py --page 2"}) == "terminal:python3 scripts/pull.py"
    assert command_key("terminal", {"command": "sudo -n apt-get install x"}) == "terminal:apt-get"
    assert command_key("terminal", {"command": r"C:\tools\x.exe /q"}) == "terminal:C:/tools/x.exe"
    assert command_key("terminal", {"command": ""}) == "terminal"
    assert command_key("write_file", {"path": "a"}) == "write_file"
