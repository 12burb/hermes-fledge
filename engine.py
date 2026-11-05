"""Fledge's runtime: hook callbacks that feed the evidence tracker, gate ``skill_manage`` writes, and
run probation. Every callback is wrapped so a Fledge bug can never break a Hermes turn: observers
swallow errors, and the save gate fails *open* (an internal error lets the write through and is logged).
"""

from __future__ import annotations

import functools
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from . import report
from .bridge import ORIGIN_FOREGROUND, ORIGIN_OTHER, ORIGIN_REVIEW, ORIGIN_UNKNOWN, HermesBridge
from .evidence import EvidenceTracker, Turn, Verdict, score_turn
from .gate import (
    ASK,
    KIND_BAD_RUN,
    KIND_HAND_TUNED,
    REWRITES,
    Decision,
    SkillOp,
    decide,
    mutating_ops,
    parse_ops,
    payload_digest,
    skill_key,
    staging_payload,
)
from .ledger import ARCHIVED, FLEDGED, FLEDGLING, GROUNDED, VETERAN, Ledger
from .notify import Notifier
from .probation import GRADUATED, GROUNDED_T, RELAPSED, judge_use, should_archive
from .settings import Settings, SettingsLoader

logger = logging.getLogger(__name__)

FILE_WRITE_TOOLS = frozenset({"write_file", "patch", "file_edit", "edit_file"})
_INFLIGHT_TTL = 300.0
_HOOKS = ("pre_llm_call", "pre_tool_call", "post_tool_call", "on_session_end", "on_session_finalize",
          "on_session_reset", "on_skill_lifecycle", "pre_command", "post_approval_response")


def _safe(fn: Callable) -> Callable:
    """Observer wrapper: never let an exception escape into Hermes."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.warning("fledge: %s failed", fn.__name__, exc_info=True)
            return None
    return wrapper


class Fledge:
    def __init__(self, *, settings: SettingsLoader, bridge: HermesBridge, ledger: Ledger, tracker: EvidenceTracker,
                 notifier: Notifier, set_config: Callable[[str, Any], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.settings_loader = settings
        self.bridge = bridge
        self.ledger = ledger
        self.tracker = tracker
        self.notifier = notifier
        self._set_config = set_config
        self._clock = clock
        self._lock = threading.RLock()
        # skill name -> (session_id, turn_id, origin, at) for writes Fledge let through and is expecting.
        self._inflight: dict[str, tuple] = {}

    @property
    def s(self) -> Settings:
        return self.settings_loader.get()

    def hooks(self) -> dict[str, Callable]:
        """Hook name -> bound callback, for ctx.register_hook (matches plugin.yaml provides_hooks)."""
        return {name: getattr(self, f"hook_{name}") for name in _HOOKS}

    def close(self) -> None:
        self.ledger.close()

    # ============================================================================================
    # Evidence hooks (foreground turns only)
    # ============================================================================================

    def _foreground(self) -> bool:
        return self.bridge.write_origin() in (ORIGIN_FOREGROUND, ORIGIN_UNKNOWN)

    @_safe
    def hook_pre_llm_call(self, session_id: str = "", task_id: str = "", turn_id: str = "", user_message: Any = "",
                        **_: Any) -> None:
        if not self.s.active or not self._foreground():
            return None
        sid = session_id or self.tracker.resolve_session(task_id=task_id)
        previous = self.tracker.begin_turn(sid, turn_id, user_message, task_id)
        if previous is not None and previous.correction is not None:
            self.ledger.add_event("corrected", "", f"session {sid[:12]}: “{previous.correction.phrase}”")
        self._settle()
        self.notifier.flush(sid)
        return None

    @_safe
    def hook_post_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "", task_id: str = "",
                          turn_id: str = "", status: str = "", error_type: Any = None, error_message: Any = None,
                          **_: Any) -> None:
        if not self.s.active or not self._foreground():
            return
        sid = self.tracker.resolve_session(session_id, task_id, turn_id)
        self.tracker.record_tool(sid, turn_id, tool_name, status or "ok", str(error_message or error_type or ""))
        if tool_name in FILE_WRITE_TOOLS and (status or "ok") == "ok" and isinstance(args, dict):
            # The agent edited a skill file directly: that is an agent write, not a hand edit.
            hit = self.bridge.skill_for_path(str(args.get("path") or args.get("file_path") or ""))
            if hit is not None:
                name, skill_dir = hit
                if self.ledger.get_skill(name) is not None:
                    self.ledger.set_hash(name, self.bridge.hash_dir(skill_dir))

    @_safe
    def hook_on_session_end(self, session_id: str = "", turn_id: str = "", task_id: str = "", completed: Any = None,
                          failed: Any = None, interrupted: Any = None, turn_exit_reason: str = "",
                          **_: Any) -> None:
        if not self.s.active or not self._foreground():
            return
        sid = self.tracker.resolve_session(session_id, task_id, turn_id)
        self.tracker.end_turn(sid, turn_id, completed=completed, failed=failed, interrupted=interrupted,
                              exit_reason=str(turn_exit_reason or ""))
        self._settle()
        self.notifier.flush(sid)

    @_safe
    def hook_on_session_finalize(self, session_id: str = "", **_: Any) -> None:
        if session_id:
            self.tracker.close_session(session_id)
        self._settle()

    @_safe
    def hook_on_session_reset(self, session_id: str = "", old_session_id: str = "", **_: Any) -> None:
        closed = old_session_id or session_id
        if closed:
            self.tracker.close_session(closed)
        self._settle()

    @_safe
    def hook_pre_command(self, command: str = "", session_key: str = "", args_raw: str = "", **_: Any) -> None:
        name = str(command or "").lstrip("/").strip().lower()
        if name == "rollback" and not str(args_raw or "").strip():
            return  # bare /rollback only lists checkpoints
        if name in ("undo", "rollback", "retry") and self.s.active:
            self.tracker.record_command(self.tracker.resolve_session(session_key=session_key), name)

    @_safe
    def hook_post_approval_response(self, choice: str = "", turn_id: str = "", session_key: str = "",
                                  **_: Any) -> None:
        if not self.s.active:
            return
        sid = self.tracker.resolve_session(turn_id=turn_id, session_key=session_key)
        self.tracker.record_denial(sid, turn_id, choice)

    # ============================================================================================
    # Skill lifecycle (writes that landed, loads)
    # ============================================================================================

    @_safe
    def hook_on_skill_lifecycle(self, action: str = "", skill_name: str = "", provenance: str = "",
                              task_id: str = "", session_id: str = "", **_: Any) -> None:
        if not self.s.active or not skill_name:
            return
        name = skill_key(skill_name)
        origin = self.bridge.write_origin()
        if action in ("created", "patched", "edited"):
            self._register_write(name, action, origin, session_id or self.tracker.resolve_session(task_id=task_id),
                                 provenance)
        elif action == "loaded":
            if origin in (ORIGIN_REVIEW, ORIGIN_OTHER):
                return  # the review fork reading a skill is not a use
            sid = self.tracker.resolve_session(session_id, task_id)
            if self.tracker.record_skill_use(sid, name) and self.ledger.get_skill(name) is None:
                self.ledger.ensure_skill(name, VETERAN)
        elif action == "archived":
            if self.ledger.get_skill(name) is not None:
                self.ledger.set_state(name, ARCHIVED, "archived")
        elif action == "restored":
            if self.ledger.get_skill(name) is not None:
                self.ledger.set_state(name, FLEDGLING, "restored: back on probation")
                self.ledger.add_event("restored", name, "back on probation")

    def _register_write(self, name: str, action: str, origin: str, sid: str, provenance: str) -> None:
        with self._lock:
            inflight = self._inflight.pop(name, None)
            self._expire_inflight()
        if inflight is None:
            # Not a call Fledge gated: a /skills approve replay, the Desktop editor or an external tool.
            birth = Verdict("approved", 0, ("saved outside a gated agent call (e.g. /skills approve)",))
            turn_id = ""
        else:
            sid, gated_turn_id, origin = inflight[0] or sid, inflight[1], inflight[2]
            turn = (self.tracker.find_turn(sid, gated_turn_id) if gated_turn_id else None) \
                or self._evidence_turn(sid, origin)
            birth = score_turn(turn, bad_threshold=self.s.bad_threshold)
            turn_id = turn.turn_id if turn is not None else ""
        agent_created = (provenance == "agent_created" or origin == ORIGIN_REVIEW
                         or self.bridge.created_by(name) == "agent")
        record = self.ledger.register_write(
            name, origin=origin, agent_created=agent_created, session_id=sid, turn_id=turn_id,
            birth_level=birth.level, birth_reasons=list(birth.reasons), agent_hash=self.bridge.hash_skill(name))
        self.ledger.add_event("written", name, f"{action} by {origin} (gen {record['generation']}); "
                                               f"run looked {birth.level}")

    def _expire_inflight(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._inflight.items() if now - v[3] > _INFLIGHT_TTL]:
            self._inflight.pop(key, None)

    def _evidence_turn(self, sid: str, origin: str) -> Turn | None:
        """The run a write learned from: the last finished turn for an autonomous pass, else the current turn."""
        if origin in (ORIGIN_REVIEW, ORIGIN_UNKNOWN):
            return self.tracker.latest_finished_turn(sid)
        return self.tracker.current_turn(sid)

    # ============================================================================================
    # The save gate
    # ============================================================================================

    def hook_pre_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "", task_id: str = "",
                         turn_id: str = "", **_: Any) -> dict[str, Any] | None:
        if tool_name != "skill_manage":
            return None
        try:
            return self._gate(args, session_id, task_id, turn_id)
        except Exception:
            # Fail open: a Fledge bug must not stop Hermes from saving skills.
            logger.warning("fledge: save gate error; letting the write through", exc_info=True)
            return None

    def _gate(self, args: Any, session_id: str, task_id: str, turn_id: str) -> dict[str, Any] | None:
        settings = self.s
        if not settings.active:
            return None
        ops, batch = parse_ops(args)
        targets = mutating_ops(ops)
        if not targets:
            return None
        origin = self.bridge.write_origin()
        sid = self.tracker.resolve_session(session_id, task_id, turn_id)
        turn = self._evidence_turn(sid, origin)
        verdict = score_turn(turn, bad_threshold=settings.bad_threshold)
        hand_tuned = self._hand_tuned(targets, sid)
        decision = decide(ops, origin=origin, verdict=verdict, hand_tuned=hand_tuned, settings=settings,
                          unattended=origin == ORIGIN_REVIEW or self.bridge.is_unattended())
        if decision.allowed:
            if decision.kind == KIND_BAD_RUN:
                self.ledger.add_event("allowed", ",".join(decision.skills),
                                      "foreground_bad_run=allow: " + "; ".join(decision.reasons[:2]))
            self._mark_inflight(targets, sid, turn, origin)
            return None
        if not settings.enforcing:
            self.ledger.add_event(f"would-{decision.action}", ",".join(decision.skills),
                                  f"[watch] {decision.kind}: " + "; ".join(decision.reasons[:3]))
            self._mark_inflight(targets, sid, turn, origin)
            return None
        if decision.action == ASK:
            self._mark_inflight(targets, sid, turn, origin)  # the user may approve it
            return self._ask(decision)
        return self._hold(args, ops, batch, decision, sid, origin)

    def _mark_inflight(self, targets: list[SkillOp], sid: str, turn: Turn | None, origin: str) -> None:
        with self._lock:
            now = self._clock()
            for op in targets:
                self._inflight[op.name] = (sid, turn.turn_id if turn else "", origin, now)

    def _hand_tuned(self, targets: list[SkillOp], sid: str) -> list[str]:
        """Targets the user edited by hand: flagged before, or detected now by a changed fingerprint."""
        tuned: list[str] = []
        protect = self.s.protect_hand_edits
        for op in targets:
            if op.action not in REWRITES:
                continue
            record = self.ledger.get_skill(op.name)
            if record is None:
                continue
            if record.get("human_tuned") or record.get("protected_manual"):
                tuned.append(op.name)
                continue
            known = record.get("agent_hash") or ""
            if protect and known:
                current = self.bridge.hash_skill(op.name)
                if current and current != known:
                    self._flag_hand_edit(op.name, sid)
                    tuned.append(op.name)
        return tuned

    def _flag_hand_edit(self, name: str, sid: str) -> None:
        self.ledger.set_human_tuned(name, True)
        locked = False
        if self.s.on_hand_edit == "lock" and self.s.enforcing:
            locked = self.bridge.set_locked(name, True)
        self.ledger.add_event("hand-edit", name, "content lock set" if locked else "protected from autonomous rewrites")
        if locked:
            self.notifier.queue(sid, f"you edited '{name}' by hand, so it is now locked against agent changes "
                                     f"(hermes curator unlock {name} to lift it).")

    def _ask(self, decision: Decision) -> dict[str, Any]:
        names = ", ".join(f"'{n}'" for n in decision.skills)
        if decision.kind == KIND_HAND_TUNED:
            message = f"Fledge: Hermes wants to change {names}, which you edited by hand. Allow this change?"
        else:
            message = (f"Fledge: save skill {names} from a run that went wrong? "
                       f"{'; '.join(decision.reasons[:3])}.")
        key = ",".join(decision.skills)[:120]
        return {"action": "approve", "message": message, "rule_key": f"fledge:{decision.kind}:{key}"}

    def _hold(self, args: Any, ops: list[SkillOp], batch: bool, decision: Decision, sid: str,
              origin: str) -> dict[str, Any]:
        payload = staging_payload(args if isinstance(args, dict) else {}, ops, batch)
        digest = payload_digest(payload)
        previous = self.ledger.find_hold(digest)
        if previous is not None and previous.get("pending_id"):
            waiting = self.bridge.pending_ids()
            if waiting is None or previous["pending_id"] in waiting:
                return {"action": "block", "message": report.model_hold_message(
                    decision, previous["pending_id"], repeat=True)}
        gist = self._gist(ops, batch)
        summary = report.pending_summary(gist, decision)
        pending_id = self.bridge.stage_skill_write(payload, summary)
        self.ledger.add_hold(pending_id=pending_id or "", digest=digest, kind=decision.kind, skills=decision.skills,
                             session_id=sid, origin=origin, summary=summary, reasons=decision.reasons)
        for name in decision.skills:
            self.ledger.add_event("held", name, f"{decision.kind}: " + "; ".join(decision.reasons[:3]))
        self.notifier.queue(sid, report.user_hold_notice(decision, pending_id))
        return {"action": "block", "message": report.model_hold_message(decision, pending_id)}

    def _gist(self, ops: list[SkillOp], batch: bool) -> str:
        if batch and len(ops) > 1:
            acts = ", ".join(o.action for o in ops)
            names = ", ".join(sorted({o.name for o in ops}))
            return f"batch({len(ops)} ops: {acts}) on {names}"
        op = ops[0]
        return self.bridge.skill_gist(op.action, op.raw_name or op.name, op.op)

    # ============================================================================================
    # Probation
    # ============================================================================================

    def _settle(self) -> None:
        settings = self.s
        for turn in self.tracker.drain_settled(settings.settle_seconds):
            verdict = score_turn(turn, bad_threshold=settings.bad_threshold, final=True)
            if verdict.bad:
                for record in self.ledger.skills_born_in(turn.session_id, turn.turn_id):
                    # Only a save that looked fine when it was made: one the user approved despite a bad
                    # run (birth_level "bad") keeps their decision.
                    if record.get("birth_level") in ("clean", "uncertain", "unknown"):
                        self._ground(record, "it was learned from a run that went wrong afterwards: "
                                     + verdict.summary(2), turn.session_id)
            for name in sorted(turn.skills_used):
                self._judge_use(name, turn, verdict, settings)

    def _judge_use(self, name: str, turn: Turn, verdict: Verdict, settings: Settings) -> None:
        record = self.ledger.get_skill(name) or self.ledger.ensure_skill(name, VETERAN)
        if record["state"] in (GROUNDED, ARCHIVED):
            return
        if not self.ledger.add_use(name, int(record["generation"]), turn.session_id, turn.turn_id, verdict.level,
                                   verdict.score, list(verdict.reasons)):
            return
        outcome = judge_use(record, verdict.level, settings)
        self.ledger.apply_counts(name, clean=outcome.clean, bad=outcome.bad, neutral=outcome.neutral,
                                 bad_streak=outcome.bad_streak)
        if record["state"] == VETERAN or outcome.transition is None:
            return
        if outcome.transition == GROUNDED_T:
            judged = int(record["clean_uses"]) + int(record["bad_uses"]) + outcome.clean + outcome.bad
            bad = int(record["bad_uses"]) + outcome.bad
            self._ground(record, f"{bad} of its {judged} runs on probation went wrong ({verdict.summary(1)})",
                         turn.session_id)
        elif outcome.transition == GRADUATED:
            self.ledger.set_state(name, FLEDGED, "graduated after clean runs")
            self.ledger.add_event("fledged", name, f"{int(record['clean_uses']) + outcome.clean} clean runs")
        elif outcome.transition == RELAPSED:
            self.ledger.set_state(name, FLEDGLING, "two bad runs in a row: back on probation")
            self.ledger.add_event("relapsed", name, verdict.summary(1))
            self.notifier.queue(turn.session_id, f"'{name}' went wrong twice in a row, so it is back on probation.")

    def _ground(self, record: dict[str, Any], reason: str, sid: str) -> None:
        name = record["name"]
        settings = self.s
        archived, detail = False, ""
        if should_archive(record, settings) and settings.enforcing:
            archived, detail = self.bridge.archive_skill(name)
        self.ledger.set_state(name, ARCHIVED if archived else GROUNDED, reason)
        self.ledger.add_event("grounded", name, reason + (" — archived" if archived else (f" — {detail}" if detail else "")))
        self.notifier.queue(sid, report.ground_notice(name, reason, archived=archived,
                                                       agent_created=bool(record.get("agent_created")),
                                                       watch=not settings.enforcing))

    # ============================================================================================
    # Commands
    # ============================================================================================

    def command(self, raw_args: str = "", **_: Any) -> str:
        try:
            return self._command(str(raw_args or "").strip())
        except Exception as exc:
            logger.warning("fledge: command failed", exc_info=True)
            return f"Fledge: that command failed ({type(exc).__name__}: {exc})."

    def _command(self, raw: str) -> str:
        parts = raw.split()
        sub = parts[0].lower() if parts else "status"
        arg = " ".join(parts[1:]).strip()
        if sub in ("status", ""):
            return report.status(self.ledger, self.s, self.bridge.pending_ids())
        if sub in ("help", "-h", "--help"):
            return report.HELP
        if sub == "doctor":
            return report.doctor(self.bridge.capabilities(), self.s)
        if sub == "mode":
            return self.set_mode(arg)
        if sub == "pending":
            return self._pending()
        if sub in ("diff", "approve", "reject"):
            return self._review(sub, arg)
        if not arg:
            return f"Usage: /fledge {sub} <skill>"
        name = skill_key(arg)
        if sub in ("skill", "show", "why"):
            return report.skill_detail(self.ledger, name)
        if sub == "protect":
            self.ledger.set_human_tuned(name, True, manual=True)
            self.ledger.add_event("protect", name, "protected by you")
            return f"Fledge: '{name}' is protected. Autonomous rewrites of it will wait for you in /fledge pending."
        if sub == "unprotect":
            self.ledger.set_human_tuned(name, False, manual=False)
            self.ledger.set_hash(name, self.bridge.hash_skill(name))
            self.ledger.add_event("unprotect", name, "protection lifted by you")
            return f"Fledge: '{name}' is no longer protected."
        if sub == "release":
            if self.ledger.get_skill(name) is None:
                self.ledger.ensure_skill(name, FLEDGED)
            self.ledger.set_state(name, FLEDGED, "released by you")
            self.ledger.add_event("released", name, "marked fledged by you")
            return f"Fledge: '{name}' is fledged (off probation)."
        if sub == "forget":
            self.ledger.forget(name)
            return f"Fledge: forgot everything about '{name}'."
        return report.HELP

    def _pending(self) -> str:
        waiting = self.bridge.pending_ids()
        holds = [h for h in self.ledger.recent_holds(50) if h.get("pending_id")
                 and (waiting is None or h["pending_id"] in waiting)]
        return report.pending_list(holds, queue_readable=waiting is not None)

    def _review(self, action: str, pending_id: str) -> str:
        """diff / approve / reject one of Fledge's holds through Hermes' own /skills handler."""
        pending_id = pending_id.split()[0] if pending_id else ""
        if not pending_id:
            return f"Usage: /fledge {action} ID (see /fledge pending)"
        if not any(h.get("pending_id") == pending_id for h in self.ledger.recent_holds(500)):
            return f"Fledge has no held write '{pending_id}'. Other pending writes: /skills pending (CLI)."
        result = self.bridge.pending_command([action, pending_id])
        if result is None:
            return f"Fledge can't reach Hermes' review queue here. In the Hermes CLI run: /skills {action} {pending_id}"
        if action in ("approve", "reject"):
            self.ledger.add_event(f"{action}d", "", pending_id)
        return result

    def set_mode(self, mode: str) -> str:
        mode = mode.strip().lower()
        if mode not in ("enforce", "watch", "off"):
            return f"Fledge mode is '{self.s.mode}'. Set it with: /fledge mode enforce|watch|off"
        if self._set_config is None:
            return f"Run: hermes config set plugins.entries.fledge.settings.mode {mode}"
        self._set_config("mode", mode)
        self.settings_loader.invalidate()
        return f"Fledge mode set to '{mode}'."

    # -- CLI helpers ---------------------------------------------------------------------------------

    def scan(self) -> dict[str, int]:
        """Fingerprint every local skill so later hand edits can be told apart from agent writes."""
        seen = added = 0
        for root in self.bridge.skill_roots():
            for skill_md in sorted(root.rglob("SKILL.md")) if root.is_dir() else []:
                rel = skill_md.relative_to(root)
                if any(part.startswith(".") for part in rel.parts):
                    continue  # .archive, .hub, caches
                seen += 1
                name = self.bridge._skill_name(skill_md, skill_md.parent.name)
                if self.ledger.get_skill(name) is None:
                    self.ledger.ensure_skill(name, VETERAN)
                    self.ledger.set_hash(name, self.bridge.hash_dir(skill_md.parent))
                    added += 1
        self.ledger.add_event("scan", "", f"{seen} skills seen, {added} fingerprinted")
        return {"seen": seen, "fingerprinted": added}

    def export(self) -> str:
        data = {"skills": self.ledger.list_skills(limit=10000), "holds": self.ledger.recent_holds(1000),
                "events": self.ledger.recent_events(2000)}
        return json.dumps(data, indent=2, default=str)
