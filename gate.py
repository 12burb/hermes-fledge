"""The save gate: decide what happens to a ``skill_manage`` write before it lands.

Pure functions only (the engine executes the decision). Inputs: the parsed operations, who is
writing, the verdict on the run the write learned from, and Fledge's record of each target skill.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .evidence import Verdict
from .settings import Settings

MUTATING = ("create", "edit", "patch", "write_file", "remove_file")
REWRITES = ("edit", "patch", "write_file", "remove_file")
# Changes that would undo a user's hand edit: rewrites, plus delete (a curator consolidation deletes the
# absorbed skill). Only the hand-edit rule looks at deletes; a delete is never held for a bad run.
PROTECTED = REWRITES + ("delete",)
_FLAT_KEYS = ("content", "category", "file_path", "file_content", "old_string", "new_string",
              "absorbed_into", "replace_all")

ALLOW, HOLD, ASK = "allow", "hold", "ask"
KIND_BAD_RUN, KIND_HAND_TUNED = "bad_run", "hand_tuned"


@dataclass(frozen=True)
class SkillOp:
    action: str
    name: str      # bare skill name (last path segment of category/name)
    raw_name: str
    op: dict[str, Any]


def skill_key(name: str) -> str:
    return str(name or "").strip().rstrip("/").rsplit("/", 1)[-1]


def parse_ops(args: Any) -> tuple[list[SkillOp], bool]:
    """``(ops, batch_shape)`` from skill_manage arguments (operations[] or the legacy flat shape)."""
    if not isinstance(args, dict):
        return [], False
    operations = args.get("operations")
    if isinstance(operations, list):
        default_name = args.get("name") or ""
        ops = []
        for op in operations:
            if not isinstance(op, dict):
                continue
            raw = str(op.get("name") or default_name or "")
            ops.append(SkillOp(str(op.get("action") or ""), skill_key(raw), raw, op))
        return ops, True
    action = str(args.get("action") or "")
    raw = str(args.get("name") or "")
    if not action:
        return [], False
    return [SkillOp(action, skill_key(raw), raw, {k: v for k, v in args.items() if k != "operations"})], False


def staging_payload(args: dict[str, Any], ops: list[SkillOp], batch_shape: bool) -> dict[str, Any]:
    """The exact payload Hermes' ``/skills approve`` replays (mirrors core's own staging shapes)."""
    if batch_shape:
        return {"action": "batch", "operations": [dict(o.op) for o in ops]}
    op = ops[0]
    payload: dict[str, Any] = {"action": op.action, "name": op.raw_name}
    payload.update({k: args[k] for k in _FLAT_KEYS if args.get(k) is not None})
    return payload


def payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass
class Decision:
    action: str = ALLOW
    kind: str = ""
    reasons: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW


def mutating_ops(ops: list[SkillOp]) -> list[SkillOp]:
    return [o for o in ops if o.action in MUTATING and o.name]


def protected_ops(ops: list[SkillOp]) -> list[SkillOp]:
    """Ops that would change or remove an existing skill (checked against hand edits)."""
    return [o for o in ops if o.action in PROTECTED and o.name]


def decide(ops: list[SkillOp], *, origin: str, verdict: Verdict, hand_tuned: list[str], settings: Settings,
           unattended: bool) -> Decision:
    """Decide on one skill_manage call.

    * Rewrites of hand-tuned skills: autonomous passes are held (staged as a proposal); a foreground turn
      asks the user, or stages when nobody can answer.
    * Saves learned from a bad run: held for autonomous passes and unknown origins; for a foreground
      turn, ``foreground_bad_run`` picks approve (ask) / stage (hold) / allow.
    * Everything else is allowed (and goes on probation once it lands).
    """
    targets = mutating_ops(ops)
    guarded = protected_ops(ops)
    if not (targets or guarded) or not settings.active:
        return Decision()
    autonomous = origin in ("review", "unknown")

    tuned_targets = sorted({o.name for o in guarded if o.name in hand_tuned})
    if tuned_targets and settings.protect_hand_edits:
        deleting = {o.name for o in guarded if o.action == "delete"}
        reasons = [f"'{name}' was edited by hand, so autonomous "
                   f"{'deletes' if name in deleting else 'rewrites'} become proposals" for name in tuned_targets]
        if autonomous or unattended:
            return Decision(HOLD, KIND_HAND_TUNED, reasons, tuned_targets)
        return Decision(ASK, KIND_HAND_TUNED, reasons, tuned_targets)

    if verdict.bad and targets:
        names = sorted({o.name for o in targets})
        reasons = list(verdict.reasons)
        if autonomous:
            return Decision(HOLD, KIND_BAD_RUN, reasons, names)
        mode = settings.foreground_bad_run
        if mode == "allow":
            return Decision(ALLOW, KIND_BAD_RUN, reasons, names)
        if mode == "stage" or unattended:
            return Decision(HOLD, KIND_BAD_RUN, reasons, names)
        return Decision(ASK, KIND_BAD_RUN, reasons, names)
    return Decision()
