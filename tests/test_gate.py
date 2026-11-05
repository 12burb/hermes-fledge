import pytest
from fledge.evidence import Verdict
from fledge.gate import (
    ALLOW,
    ASK,
    HOLD,
    KIND_BAD_RUN,
    KIND_HAND_TUNED,
    decide,
    parse_ops,
    payload_digest,
    staging_payload,
)
from fledge.settings import Settings

BAD = Verdict("bad", 4, ("terminal was still failing when the turn ended: exit 1",))
CLEAN = Verdict("clean", 0, ())

CREATE_BATCH = {"operations": [{"action": "create", "name": "water-tests", "content": "---\nname: water-tests\n---"}]}
PATCH_FLAT = {"action": "patch", "name": "home/lights", "old_string": "a", "new_string": "b"}


def test_parse_batch_and_flat_shapes():
    ops, batch = parse_ops(CREATE_BATCH)
    assert batch and ops[0].action == "create" and ops[0].name == "water-tests"
    ops, batch = parse_ops(PATCH_FLAT)
    assert not batch and ops[0].name == "lights" and ops[0].raw_name == "home/lights"
    assert parse_ops(None) == ([], False)
    assert parse_ops({"foo": 1}) == ([], False)


def test_staging_payload_matches_hermes_replay_shapes():
    ops, batch = parse_ops(CREATE_BATCH)
    assert staging_payload(CREATE_BATCH, ops, batch) == {"action": "batch", "operations": CREATE_BATCH["operations"]}
    flat = dict(PATCH_FLAT, replace_all=False)
    ops, batch = parse_ops(flat)
    payload = staging_payload(flat, ops, batch)
    assert payload == {"action": "patch", "name": "home/lights", "old_string": "a", "new_string": "b",
                       "replace_all": False}
    assert payload_digest(payload) == payload_digest(dict(payload))


def _decide(args, *, origin="review", verdict=BAD, tuned=(), settings=None, unattended=False):
    ops, _ = parse_ops(args)
    return decide(ops, origin=origin, verdict=verdict, hand_tuned=list(tuned), settings=settings or Settings(),
                  unattended=unattended)


def test_review_save_after_bad_run_is_held():
    d = _decide(CREATE_BATCH)
    assert d.action == HOLD and d.kind == KIND_BAD_RUN and d.skills == ["water-tests"]


def test_review_save_after_clean_run_is_allowed():
    assert _decide(CREATE_BATCH, verdict=CLEAN).action == ALLOW


def test_unknown_origin_is_treated_as_autonomous():
    assert _decide(CREATE_BATCH, origin="unknown").action == HOLD


@pytest.mark.parametrize("mode,unattended,expected", [
    ("approve", False, ASK), ("approve", True, HOLD), ("stage", False, HOLD), ("allow", False, ALLOW)])
def test_foreground_bad_run_modes(mode, unattended, expected):
    d = _decide(CREATE_BATCH, origin="foreground", settings=Settings(foreground_bad_run=mode), unattended=unattended)
    assert d.action == expected


def test_hand_tuned_rewrites_become_proposals_or_questions():
    d = _decide(PATCH_FLAT, verdict=CLEAN, tuned=["lights"])
    assert d.action == HOLD and d.kind == KIND_HAND_TUNED
    d = _decide(PATCH_FLAT, origin="foreground", verdict=CLEAN, tuned=["lights"])
    assert d.action == ASK and d.kind == KIND_HAND_TUNED
    assert _decide(PATCH_FLAT, verdict=CLEAN, tuned=["lights"],
                   settings=Settings(protect_hand_edits=False)).action == ALLOW


def test_deletes_and_off_mode_pass_through():
    assert _decide({"action": "delete", "name": "x"}).action == ALLOW
    assert _decide(CREATE_BATCH, settings=Settings(mode="off")).action == ALLOW
