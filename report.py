"""Every piece of text Fledge shows: chat notices, the model-facing hold message, /fledge output."""

from __future__ import annotations

import time
from typing import Any

from .gate import KIND_HAND_TUNED, Decision
from .settings import Settings

HELP = """Fledge — skills have to earn their wings.
/fledge                  overview: probation, holds, grounded and protected skills
/fledge pending          skill writes Fledge is holding for you
/fledge diff ID          what a held write would change
/fledge approve ID       save it anyway        /fledge reject ID   drop it
/fledge skill NAME       one skill's track record and why
/fledge protect NAME     treat NAME as hand-tuned (autonomous rewrites become proposals)
/fledge unprotect NAME   lift that protection
/fledge release NAME     take NAME off probation
/fledge forget NAME      drop Fledge's record of NAME
/fledge mode MODE        enforce | watch | off
/fledge doctor           which Hermes features Fledge can reach
Holds live in Hermes' own queue, so /skills pending in the CLI shows them too."""


def _names(skills: list[str]) -> str:
    return ", ".join(f"'{s}'" for s in skills) or "this skill"


def _short(reasons: list[str], limit: int = 2) -> str:
    return "; ".join(reasons[:limit]) if reasons else "the run went wrong"


def _ago(at: float | None) -> str:
    if not at:
        return ""
    delta = max(0, int(time.time() - float(at)))
    if delta < 90:
        return "just now"
    if delta < 5400:
        return f"{delta // 60}m ago"
    if delta < 172800:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# -- holds ------------------------------------------------------------------------------------------

def pending_summary(gist: str, decision: Decision) -> str:
    """One line for the /skills pending list."""
    if decision.kind == KIND_HAND_TUNED:
        return f"[fledge] proposal: {gist} (you hand-tuned it)"
    return f"[fledge] {gist} — held: {_short(decision.reasons)}"


def model_hold_message(decision: Decision, pending_id: str | None, *, repeat: bool = False) -> str:
    """Returned to the agent as the blocked tool's result."""
    where = (f"It is waiting in the user's review queue as {pending_id} (/fledge pending)."
             if pending_id else "It could not be queued, so it was not saved.")
    if repeat:
        return (f"Fledge already holds this exact skill write for the user's review ({pending_id}). "
                "Do not retry it or save the same change another way.")
    if decision.kind == KIND_HAND_TUNED:
        return (f"Fledge held this change: {_short(decision.reasons, 3)}. {where} "
                "Do not retry it or edit the skill another way; mention the proposal to the user if they are present.")
    return (f"Fledge held this skill write because the run it learned from went wrong: {_short(decision.reasons, 3)}. "
            f"{where} Do not retry it or save the same procedure another way; continue with any other work.")


def user_hold_notice(decision: Decision, pending_id: str | None) -> str:
    names = _names(decision.skills)
    if decision.kind == KIND_HAND_TUNED:
        if pending_id:
            return (f"Hermes tried to change {names}, which you edited by hand. Saved as a proposal instead: "
                    f"/fledge diff {pending_id}, then /fledge approve {pending_id} or /fledge reject {pending_id}.")
        return f"Hermes tried to change {names}, which you edited by hand. Fledge blocked it."
    if pending_id:
        return (f"held skill {names}: it was learned from a run that went wrong ({_short(decision.reasons)}). "
                f"Review: /fledge diff {pending_id}, then /fledge approve {pending_id} or /fledge reject {pending_id}.")
    return f"blocked skill {names}: it was learned from a run that went wrong ({_short(decision.reasons)})."


def birth_phrase(level: str | None) -> str:
    """How the run a skill was saved from looked, in words."""
    return {"clean": "the run looked clean", "uncertain": "the run was unclear",
            "bad": "the run went wrong", "approved": "you approved it from the review queue",
            "unknown": "the run was not seen"}.get(level or "", "the run was not seen")


def hold_failed_notice(decision: Decision) -> str:
    return (f"could not hold {_names(decision.skills)} (Hermes' review queue is unavailable; /fledge doctor), so it "
            f"was saved on probation instead. It was learned from a run that went wrong "
            f"({_short(decision.reasons)}); it is grounded if its next runs go wrong too.")


def ground_notice(name: str, reason: str, *, archived: bool, agent_created: bool, watch: bool) -> str:
    if watch:
        return f"[watch mode] would ground '{name}': {reason}."
    if archived:
        return f"grounded '{name}': {reason}. Archived; restore with: hermes curator restore {name}"
    if agent_created:
        return f"grounded '{name}': {reason}. It could not be archived, so it is still active (/fledge skill {name})."
    return f"grounded '{name}': {reason}. You created it, so it stays active; check it with /fledge skill {name}."


# -- /fledge ----------------------------------------------------------------------------------------

def pending_list(holds: list[dict[str, Any]], *, queue_readable: bool) -> str:
    if not holds:
        return "Fledge is not holding any skill writes right now."
    lines = [f"Held for your review ({len(holds)}):"]
    for h in holds[:15]:
        lines.append(f"  {h['pending_id']}  {_names(h['skills'])} — {_short(h['reasons'], 1)} ({_ago(h.get('at'))})")
    lines.append("Inspect: /fledge diff ID · keep: /fledge approve ID · drop: /fledge reject ID")
    if not queue_readable:
        lines.append("(Hermes' queue could not be read, so some of these may already be resolved.)")
    return "\n".join(lines)


def status(ledger: Any, settings: Settings, waiting: set | None = None) -> str:
    counts = ledger.counts()
    lines = [
        f"Fledge ({settings.mode}, {settings.strictness}) — "
        f"{counts['fledged']} fledged · {counts['fledgling']} on probation · "
        f"{counts['grounded'] + counts['archived']} grounded · {counts['human_tuned']} hand-tuned"
    ]
    holds = [h for h in ledger.recent_holds(20)
             if h.get("pending_id") and (waiting is None or h["pending_id"] in waiting)][:5]
    if holds:
        lines.append("Held for your review (/fledge pending):")
        for h in holds:
            pid = h.get("pending_id") or "not queued"
            lines.append(f"  {pid}  {_names(h['skills'])} — {_short(h['reasons'], 1)} ({_ago(h.get('at'))})")
    fledglings = ledger.list_skills("fledgling", limit=6)
    if fledglings:
        lines.append("On probation:")
        for r in fledglings:
            lines.append(f"  {r['name']} — {r['clean_uses']} clean / {r['bad_uses']} bad of "
                         f"{settings.graduate_after} needed")
    grounded = ledger.list_skills("grounded", limit=4) + ledger.list_skills("archived", limit=4)
    if grounded:
        lines.append("Grounded:")
        for r in grounded[:6]:
            tail = " (archived: hermes curator restore " + r["name"] + ")" if r["state"] == "archived" else ""
            lines.append(f"  {r['name']} — {r['note'] or 'too many bad runs'}{tail}")
    tuned = ledger.human_tuned_skills()
    if tuned:
        lines.append("Hand-tuned (autonomous rewrites become proposals): "
                     + ", ".join(r["name"] for r in tuned[:8]) + (" …" if len(tuned) > 8 else ""))
    if len(lines) == 1:
        lines.append("Nothing to report yet. New skills will show up here once Hermes saves them.")
    lines.append("Details: /fledge skill NAME · help: /fledge help")
    return "\n".join(lines)


def skill_detail(ledger: Any, name: str) -> str:
    record = ledger.get_skill(name)
    if record is None:
        return f"Fledge has no record of '{name}' (it has not been saved or used since Fledge was enabled)."
    lines = [f"'{name}' — {record['state']}" + (f" ({record['note']})" if record.get("note") else "")]
    lines.append(f"  version {record['generation']} · this version: {record['clean_uses']} clean, "
                 f"{record['bad_uses']} bad, {record['neutral_uses']} unclear · all time: "
                 f"{record['total_clean']} clean, {record['total_bad']} bad")
    if record.get("origin"):
        who = "Hermes on its own (background review)" if record["origin"] == "review" else "a foreground turn"
        lines.append(f"  last saved by {who}; {birth_phrase(record.get('birth_level'))}")
        for reason in (record.get("birth_reasons") or [])[:3]:
            lines.append(f"    · {reason}")
    if record.get("human_tuned"):
        how = "you protected it" if record.get("protected_manual") else "you edited it by hand"
        lines.append(f"  hand-tuned ({how}): autonomous rewrites become proposals")
    uses = ledger.recent_uses(name, 5)
    if uses:
        lines.append("  recent runs:")
        for u in uses:
            why = f" — {u['reasons'][0]}" if u["reasons"] else ""
            lines.append(f"    {u['level']}{why} ({_ago(u['at'])})")
    return "\n".join(lines)


def doctor(capabilities: dict[str, str], settings: Settings, ignored: list[str] | None = None) -> str:
    lines = ["Fledge doctor"]
    for feature, state in capabilities.items():
        mark = "ok " if state == "ok" else "!! "
        lines.append(f"  {mark}{feature}" + ("" if state == "ok" else f" — {state}"))
    lines.append(f"  settings: mode={settings.mode} strictness={settings.strictness} "
                 f"graduate_after={settings.graduate_after} ground_after={settings.ground_after} "
                 f"auto_archive={settings.auto_archive} on_hand_edit={settings.on_hand_edit} "
                 f"foreground_bad_run={settings.foreground_bad_run}")
    for message in ignored or []:
        lines.append(f"  !! {message}")
    if any(state != "ok" for state in capabilities.values()):
        lines.append("  A '!!' feature is off; everything else keeps working. Fledge needs plugins.isolation: "
                     "in_process to tell background-review writes from foreground ones.")
    return "\n".join(lines)
