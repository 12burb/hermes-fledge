"""Probation: a new or rewritten skill earns its wings from its real runs.

Pure transition rules; the engine persists and acts on the result.

* fledgling -> fledged: ``graduate_after`` clean runs, plus one more clean run per bad run seen.
* fledgling -> grounded: ``ground_after`` bad runs, or more bad than clean runs after three judged runs.
* fledged -> fledgling: two bad runs in a row (it is put back on probation, not grounded outright).
* A write (create/patch) always restarts probation for the new version.
* A save learned from a run the user then corrected is grounded at once ("bad birth").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ledger import FLEDGED, FLEDGLING, GROUNDED
from .settings import Settings

GRADUATED, GROUNDED_T, RELAPSED = "graduated", "grounded", "relapsed"


@dataclass(frozen=True)
class UseOutcome:
    clean: int
    bad: int
    neutral: int
    bad_streak: int
    new_state: str | None     # None when the state does not change
    transition: str | None    # graduated | grounded | relapsed | None


def judge_use(record: dict[str, Any], level: str, settings: Settings) -> UseOutcome:
    """Fold one judged run (``clean`` / ``bad`` / ``uncertain``) into a skill record."""
    clean = 1 if level == "clean" else 0
    bad = 1 if level == "bad" else 0
    neutral = 1 if level not in ("clean", "bad") else 0
    streak = (int(record.get("bad_streak") or 0) + 1) if bad else (0 if clean else int(record.get("bad_streak") or 0))
    state = record.get("state")
    clean_total = int(record.get("clean_uses") or 0) + clean
    bad_total = int(record.get("bad_uses") or 0) + bad
    judged = clean_total + bad_total

    if state == FLEDGLING:
        if bad_total >= settings.ground_after or (judged >= 3 and bad_total > clean_total):
            return UseOutcome(clean, bad, neutral, streak, GROUNDED, GROUNDED_T)
        if clean_total >= settings.graduate_after + bad_total:
            return UseOutcome(clean, bad, neutral, streak, FLEDGED, GRADUATED)
    elif state == FLEDGED and streak >= 2:
        return UseOutcome(clean, bad, neutral, streak, FLEDGLING, RELAPSED)
    return UseOutcome(clean, bad, neutral, streak, None, None)


def should_archive(record: dict[str, Any], settings: Settings) -> bool:
    """Only skills Hermes created on its own are archived automatically; yours are only reported."""
    return bool(settings.auto_archive and record.get("agent_created"))
