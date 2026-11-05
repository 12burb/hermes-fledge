from fledge.ledger import FLEDGED, FLEDGLING, GROUNDED
from fledge.probation import GRADUATED, GROUNDED_T, RELAPSED, judge_use, should_archive
from fledge.settings import Settings

S = Settings()


def rec(state=FLEDGLING, clean=0, bad=0, streak=0, agent=1):
    return {"state": state, "clean_uses": clean, "bad_uses": bad, "bad_streak": streak, "agent_created": agent}


def test_graduates_after_clean_runs():
    assert judge_use(rec(clean=2), "clean", S).transition == GRADUATED
    assert judge_use(rec(clean=1), "clean", S).transition is None


def test_a_bad_run_raises_the_bar():
    assert judge_use(rec(clean=2, bad=1), "clean", S).transition is None
    assert judge_use(rec(clean=3, bad=1), "clean", S).new_state == FLEDGED


def test_grounded_after_bad_runs():
    out = judge_use(rec(bad=1), "bad", S)
    assert out.new_state == GROUNDED and out.transition == GROUNDED_T


def test_grounded_when_bad_outnumbers_clean():
    assert judge_use(rec(clean=1, bad=0), "uncertain", S).transition is None
    assert judge_use(rec(clean=0, bad=1), "bad", Settings(ground_after=5)).transition is None
    assert judge_use(rec(clean=1, bad=1), "bad", Settings(ground_after=5)).transition == GROUNDED_T


def test_fledged_relapses_after_two_bad_runs_in_a_row():
    assert judge_use(rec(state=FLEDGED, streak=0), "bad", S).transition is None
    out = judge_use(rec(state=FLEDGED, streak=1), "bad", S)
    assert out.transition == RELAPSED and out.new_state == FLEDGLING


def test_clean_run_resets_streak_and_unclear_keeps_it():
    assert judge_use(rec(state=FLEDGED, streak=1), "clean", S).bad_streak == 0
    assert judge_use(rec(state=FLEDGED, streak=1), "uncertain", S).bad_streak == 1


def test_only_agent_created_skills_are_archived():
    assert should_archive(rec(agent=1), S)
    assert not should_archive(rec(agent=0), S)
    assert not should_archive(rec(agent=1), Settings(auto_archive=False))
