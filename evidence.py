"""What actually happened in each turn, and a deterministic verdict on it.

The tracker is fed by plugin hooks (``pre_llm_call``, ``post_tool_call``, ``on_session_end``,
``pre_command``, ``post_approval_response``) for foreground turns only; the background-review and
side-question forks share the session id but never count as user turns.

A turn's verdict uses only facts Hermes reported:

* tools whose *last* call in the turn failed (an error the agent recovered from is normal work);
* the turn itself failing or being interrupted;
* the user's next message correcting the agent, or ``/undo`` / ``/rollback`` / ``/retry`` right after;
* actions the user denied at an approval prompt.
"""

from __future__ import annotations

import re
import shlex
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .corrections import Correction, detect_correction

STATUS_OK, STATUS_ERROR, STATUS_BLOCKED = "ok", "error", "blocked"

# Tools whose results are bookkeeping, not task work: their failures say nothing about whether the
# task went well (and skill_manage errors are often Fledge's own holds).
META_TOOLS = frozenset({
    "skill_manage", "skill_view", "skills_list", "memory", "todo", "clarify", "session_search",
    "tool_search", "tool_describe", "send_message",
})

# Tools whose unresolved failure means the work itself did not land.
_HEAVY_PREFIXES = ("terminal", "execute_code", "write_file", "patch", "file_edit", "process",
                   "browser", "computer", "delegate_task", "kanban_complete")

_COMMAND_PENALTIES = {"undo": 3, "rollback": 3, "retry": 2}
_DENY_CHOICES = {"deny", "denied", "reject", "rejected", "no"}

MAX_SESSIONS = 256
MAX_TURNS_PER_SESSION = 8
MAX_EVENTS_PER_TURN = 400


def _is_heavy(tool_name: str) -> bool:
    return tool_name.startswith(_HEAVY_PREFIXES) or tool_name.startswith("mcp_")


# Terminal calls are judged per program, not per tool: a later ``rm scratch.txt`` that succeeds must not
# count as "recovering" an earlier ``./fetch_data.sh`` that failed. Re-running the same program (any flags
# or redirections) is still a recovery.
_COMMAND_TOOLS = frozenset({"terminal"})
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")
_SETUP_PROGRAMS = frozenset({"cd", "pushd", "popd", "export", "set", "source", ".", "true", ":"})
_WRAPPERS = frozenset({"sudo", "env", "time", "nice", "nohup", "command", "exec", "timeout", "stdbuf"})
_INTERPRETERS = re.compile(r"^(python[0-9.]*|py|node|deno|bun|ruby|perl|php|bash|sh|zsh|pwsh|powershell|uv|npx)$")


def _segment_program(segment: str) -> str | None:
    """The program one shell segment runs (``python fetch.py`` for interpreters), or None for setup."""
    segment = segment.replace("\\", "/")  # Windows paths: POSIX shlex would eat the backslashes
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        tokens = segment.split()
    tokens = [t for t in tokens if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]  # FOO=bar prefixes
    while tokens and tokens[0] in _WRAPPERS:
        tokens = tokens[1:]
        while tokens and tokens[0].startswith("-"):
            tokens = tokens[1:]
    if not tokens or tokens[0] in _SETUP_PROGRAMS:
        return None
    program = tokens[0]
    if _INTERPRETERS.match(program.rsplit("/", 1)[-1].lower()):
        script = next((t for t in tokens[1:] if not t.startswith("-")), "")
        if script:
            program = f"{program.rsplit('/', 1)[-1]} {script}"
    return program[:80]


def command_key(tool_name: str, args: object) -> str:
    """Identity of a tool call for recovery tracking: ``terminal:<program>`` for shell commands (with the
    script for interpreters, e.g. ``terminal:python fetch.py``), else the tool name."""
    if tool_name not in _COMMAND_TOOLS or not isinstance(args, dict):
        return tool_name
    command = str(args.get("command") or "").strip()
    for segment in _SEGMENT_SPLIT.split(command):
        program = _segment_program(segment)
        if program:
            return f"{tool_name}:{program}"
    return tool_name


# A compound command reports the exit status of its LAST part, so ``./fetch.sh > out; echo "exit=$?"; cat
# out`` comes back as success even when ./fetch.sh failed. Agents write this shape all the time. When the
# command itself prints the shell's ``$?`` behind a label (``echo "exit=$?"``, or ``ec=$?`` ... ``echo
# "exit_code=$ec"``), that label + number in the output IS the shell's own report for the program just
# before it: read exactly that, nothing else. No label, no reading (a bare number proves nothing).
_LABEL = r"([A-Za-z][\w:= -]{0,30}?)"
_STATUS_VAR = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=\$\?\s*$")


def _previous_program(segments: list[str], index: int) -> str | None:
    for segment in reversed(segments[:index]):
        if "$?" in segment or _STATUS_VAR.match(segment):
            return None  # $? of a status echo: not a program run
        program = _segment_program(segment)
        if program:
            return program
    return None


# Commands whose non-zero exit is an answer, not a failure (``if grep -q x f`` is control flow).
_PREDICATES = frozenset({
    "grep", "egrep", "fgrep", "rg", "test", "[", "[[", "diff", "cmp", "which", "where", "command", "type",
    "hash", "pgrep", "pidof", "id", "getent", "ls", "stat", "git", "true", "false", "read",
})
_SPLIT_KEEP = re.compile(r"(&&|\|\||;|\n)")


def _echo_literal(segment: str) -> str | None:
    """The fixed text an ``echo`` / ``printf`` segment prints (no variables), else None."""
    s = re.sub(r"^\s*(?:then|else|do)\s+", "", segment).strip()
    m = re.match(r"""^(echo|printf)\s+(?:-[a-zA-Z]+\s+)*(["'])(.*?)\2\s*$""", s) or \
        re.match(r"""^(echo)\s+()([^\s$`'"][^$`'"]*)$""", s)
    if not m:
        return None
    text = m.group(3)
    if m.group(1) == "printf":
        if "%" in text:
            return None
        text = text.replace("\\n", "\n")
    text = text.strip()
    if "$" in text or "`" in text or "\\" in text or len(text) < 4 or not re.search(r"[A-Za-z]", text):
        return None
    return text


def _program_for_status(segment: str) -> str | None:
    program = _segment_program(segment)
    if not program or program.split(" ", 1)[0].rsplit("/", 1)[-1] in _PREDICATES:
        return None
    return program


def _branch_statuses(command: str, lines: set) -> list[tuple[int, str, int]]:
    """``if PROG; then echo A; else echo B; fi`` and ``PROG && echo A || echo B``: which branch's fixed echo
    appears as a whole output line says how PROG exited. ``(position, program, code)``."""
    parts = _SPLIT_KEEP.split(command)
    segments = parts[0::2]
    seps = parts[1::2] + [""]
    out: list[tuple[int, str, int]] = []
    stack: list[dict] = []
    for i, raw in enumerate(segments):
        s = raw.strip()
        word = s.split(None, 1)[0] if s else ""
        if word == "if":
            cond = s[2:].strip()
            stack.append({"at": i, "program": None if cond.startswith("!") else _program_for_status(cond),
                          "phase": "cond", "ok": None, "fail": None, "valid": seps[i] in (";", "\n")})
            continue
        frame = stack[-1] if stack else None
        if word == "then" and frame is not None:
            frame["phase"] = "then"
        elif word == "else" and frame is not None:
            frame["phase"] = "else"
        elif word == "elif" and frame is not None:
            frame["valid"] = False
            continue
        elif word == "fi" and frame is not None:
            stack.pop()
            if frame["valid"] and frame["program"]:
                if frame["fail"] and frame["fail"] in lines:
                    out.append((frame["at"], frame["program"], 1))
                elif frame["ok"] and frame["ok"] in lines:
                    out.append((frame["at"], frame["program"], 0))
            continue
        elif frame is not None and frame["phase"] == "cond":
            frame["valid"] = False  # a compound condition (if a && b): ambiguous
            continue
        if frame is not None and frame["phase"] in ("then", "else"):
            literal = _echo_literal(s)
            slot = "ok" if frame["phase"] == "then" else "fail"
            if literal and frame[slot] is None:
                frame[slot] = literal
            continue
        # PROG && echo A || echo B   (outside any if)
        if word in ("then", "else", "do", "done", "fi", "") or _echo_literal(s) is not None:
            continue
        program = _program_for_status(s)
        if not program or seps[i] not in ("&&", "||"):
            continue
        ok = fail = None
        k = i
        while seps[k] in ("&&", "||") and k + 1 < len(segments):
            literal = _echo_literal(segments[k + 1])
            if literal is None:
                break
            if seps[k] == "&&" and ok is None:
                ok = literal
            elif seps[k] == "||" and fail is None:
                fail = literal
            k += 1
        if fail and fail in lines:
            out.append((i, program, 1))
        elif ok and ok in lines:
            out.append((i, program, 0))
    return out


def captured_exit_statuses(command: str, output: str) -> list[tuple[str, int]]:
    """``[("terminal:<program>", exit_code), ...]`` the command itself reported for its parts, in order:
    a labelled ``$?`` echo, or which of its own fixed ``echo`` branches ran. Never guessed from other text."""
    if not command or not output:
        return []
    found: list[tuple[int, str, int]] = []
    lines = {line.strip() for line in output.splitlines() if line.strip()}
    if "$?" in command:
        segments = _SEGMENT_SPLIT.split(command)
        labelled: list[tuple[int, str, str]] = []  # (position, program, label)
        for i, segment in enumerate(segments):
            var = _STATUS_VAR.match(segment)
            if var:
                name = var.group(1)
                program = _previous_program(segments, i)
                ref = re.compile(_LABEL + r"\$\{?" + re.escape(name) + r"\}?(?!\w)")
                label = next((m.group(1) for later in segments[i + 1:] for m in [ref.search(later)] if m), None)
                if program and label:
                    labelled.append((i, program, label))
                continue
            if "$?" in segment:
                m = re.search(_LABEL + r"\$\?", segment)
                program = _previous_program(segments, i)
                if m and program:
                    labelled.append((i, program, m.group(1)))
        position = 0
        for i, program, label in labelled:
            m = re.compile(re.escape(label.rstrip()) + r"\s*(\d{1,3})\b").search(output, position)
            if m is None:
                continue
            position = m.end()
            if program.split(" ", 1)[0].rsplit("/", 1)[-1] not in _PREDICATES:
                found.append((i * 2, program, int(m.group(1))))
    else:
        found.extend((i * 2, program, code) for i, program, code in _branch_statuses(command, lines))
    return [(f"terminal:{program}", code) for _pos, program, code in sorted(found, key=lambda f: f[0])]


def _clip(text: str | None, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class ToolEvent:
    tool: str
    status: str
    error: str = ""
    at: float = 0.0
    key: str = ""  # recovery identity (command_key); defaults to the tool name

    @property
    def ident(self) -> str:
        return self.key or self.tool

    @property
    def label(self) -> str:
        """``terminal (./fetch_water.sh)`` for per-command keys, else the tool name."""
        if self.key and self.key != self.tool and self.key.startswith(self.tool + ":"):
            return f"{self.tool} ({self.key[len(self.tool) + 1:]})"
        return self.tool


@dataclass
class Turn:
    session_id: str
    turn_id: str
    started_at: float
    events: list[ToolEvent] = field(default_factory=list)
    last_by_tool: dict[str, ToolEvent] = field(default_factory=dict)
    outcome: str | None = None          # completed | failed | interrupted
    exit_reason: str = ""
    ended_at: float | None = None
    correction: Correction | None = None
    followup_seen: bool = False
    commands: list[str] = field(default_factory=list)
    denials: int = 0
    skills_used: set = field(default_factory=set)
    settled: bool = False

    @property
    def unresolved(self) -> list[ToolEvent]:
        return [ev for ev in self.last_by_tool.values() if ev.status in (STATUS_ERROR, STATUS_BLOCKED)]

    @property
    def error_count(self) -> int:
        return sum(1 for ev in self.events if ev.status in (STATUS_ERROR, STATUS_BLOCKED))

    def trailing_failures(self, ident: str) -> int:
        """How many times *ident* (a tool, or a terminal program) failed in a row at the end of its calls."""
        streak = 0
        for ev in reversed(self.events):
            if ev.ident != ident:
                continue
            if ev.status == STATUS_OK:
                break
            streak += 1
        return streak


@dataclass(frozen=True)
class Verdict:
    level: str                 # clean | uncertain | bad | unknown
    score: int
    reasons: tuple[str, ...] = ()
    final: bool = False

    @property
    def bad(self) -> bool:
        return self.level == "bad"

    def summary(self, limit: int = 3) -> str:
        if not self.reasons:
            return "no problems found"
        return "; ".join(self.reasons[:limit])


UNKNOWN = Verdict("unknown", 0, ("no run evidence recorded for this session",))


def score_turn(turn: Turn | None, *, bad_threshold: int = 3, final: bool | None = None) -> Verdict:
    """Score one turn. ``final`` defaults to whether the follow-up is known (turn settled)."""
    if turn is None:
        return UNKNOWN
    final = turn.settled or turn.followup_seen if final is None else final
    score = 0
    reasons: list[str] = []

    if turn.outcome == "failed":
        score += 3
        reasons.append("the turn ended in an error" + (f" ({_clip(turn.exit_reason, 60)})" if turn.exit_reason else ""))
    elif turn.outcome == "interrupted":
        score += 2
        reasons.append("the turn was interrupted before it finished")

    if turn.correction is not None:
        if turn.correction.strength == "strong":
            score += 3
        else:
            score += 1
        reasons.append(f"you corrected it right after: “{_clip(turn.correction.phrase, 60)}”")

    for command in turn.commands:
        penalty = _COMMAND_PENALTIES.get(command, 0)
        if penalty:
            score += penalty
            reasons.append(f"you ran /{command} right after")

    unresolved_score = 0
    for ev in sorted(turn.unresolved, key=lambda e: e.at):
        streak = turn.trailing_failures(ev.ident)
        # One failing command at the end can be benign (a grep with no matches); the same tool failing
        # again and again until the turn ends means the agent gave up on that step.
        weight = (2 if _is_heavy(ev.tool) else 1) + (1 if streak >= 2 else 0)
        unresolved_score += weight
        detail = f": {_clip(ev.error, 80)}" if ev.error else ""
        if ev.status == STATUS_BLOCKED:
            reasons.append(f"{ev.label} was blocked when the turn ended{detail}")
        elif streak >= 2:
            reasons.append(f"{ev.label} failed {streak} times in a row and never recovered{detail}")
        else:
            reasons.append(f"{ev.label} was still failing when the turn ended{detail}")
    score += min(unresolved_score, 4)

    if turn.denials:
        score += min(turn.denials, 2)
        reasons.append(f"you denied {turn.denials} action{'s' if turn.denials != 1 else ''}")

    if score >= bad_threshold:
        level = "bad"
    elif score == 0 and turn.outcome in (None, "completed"):
        level = "clean"
    else:
        level = "uncertain"
    return Verdict(level, score, tuple(reasons), final)


@dataclass
class _Session:
    session_id: str
    turns: deque[Turn] = field(default_factory=lambda: deque(maxlen=MAX_TURNS_PER_SESSION))
    current: Turn | None = None
    task_ids: set = field(default_factory=set)
    last_active: float = 0.0


class EvidenceTracker:
    """Thread-safe, in-memory, bounded record of recent foreground turns per session."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._task_to_session: dict[str, str] = {}
        self._turn_to_session: dict[str, str] = {}
        self._evicted: list[Turn] = []

    # -- session bookkeeping ------------------------------------------------------------------------

    def _session(self, session_id: str) -> _Session:
        sess = self._sessions.get(session_id)
        if sess is None:
            sess = _Session(session_id)
            self._sessions[session_id] = sess
            while len(self._sessions) > MAX_SESSIONS:
                _sid, old = self._sessions.popitem(last=False)
                self._evicted.extend(t for t in list(old.turns) if not t.settled)
                if old.current is not None and not old.current.settled:
                    self._evicted.append(old.current)
        else:
            self._sessions.move_to_end(session_id)
        sess.last_active = self._clock()
        return sess

    def resolve_session(self, session_id: str = "", task_id: str = "", turn_id: str = "",
                        session_key: str = "") -> str:
        """Best session id for a hook payload that may carry only some identifiers."""
        with self._lock:
            if session_id:
                return session_id
            if turn_id and turn_id in self._turn_to_session:
                return self._turn_to_session[turn_id]
            if task_id and task_id in self._task_to_session:
                return self._task_to_session[task_id]
            if session_key:
                if session_key in self._sessions:
                    return session_key
                for sid in self._sessions:
                    if session_key.endswith(sid) or sid.endswith(session_key):
                        return sid
            # Fall back to the single session active in the last 10 minutes, if exactly one.
            now = self._clock()
            recent = [sid for sid, s in self._sessions.items() if now - s.last_active <= 600]
            return recent[0] if len(recent) == 1 else ""

    # -- turn lifecycle -----------------------------------------------------------------------------

    def begin_turn(self, session_id: str, turn_id: str, user_message, task_id: str = "", *,
                   after_question: bool = False) -> Turn | None:
        """Open a new turn; the previous one gets its follow-up (and any correction). Returns it."""
        if not session_id:
            return None
        with self._lock:
            sess = self._session(session_id)
            if task_id:
                sess.task_ids.add(task_id)
                self._task_to_session[task_id] = session_id
            previous = sess.current
            if previous is not None:
                previous.followup_seen = True
                previous.correction = detect_correction(user_message, after_question=after_question)
                if previous.ended_at is None:
                    previous.ended_at = self._clock()
                if previous not in sess.turns:
                    sess.turns.append(previous)
            turn = Turn(session_id=session_id, turn_id=turn_id or f"t{int(self._clock() * 1000)}",
                        started_at=self._clock())
            sess.current = turn
            self._turn_to_session[turn.turn_id] = session_id
            if len(self._turn_to_session) > MAX_SESSIONS * MAX_TURNS_PER_SESSION * 2:
                for key in list(self._turn_to_session)[: MAX_SESSIONS * MAX_TURNS_PER_SESSION]:
                    self._turn_to_session.pop(key, None)
            return previous

    def _turn_for(self, session_id: str, turn_id: str = "") -> Turn | None:
        sess = self._sessions.get(session_id)
        if sess is None:
            return None
        if sess.current is not None and (not turn_id or sess.current.turn_id == turn_id):
            return sess.current
        for turn in reversed(sess.turns):
            if turn.turn_id == turn_id:
                return turn
        return sess.current

    def record_tool(self, session_id: str, turn_id: str, tool_name: str, status: str, error: str = "",
                    key: str = "") -> None:
        if not session_id or not tool_name or tool_name in META_TOOLS:
            return
        with self._lock:
            sess = self._session(session_id)
            turn = self._turn_for(session_id, turn_id)
            if turn is None:  # plugin enabled mid-turn: open an implicit turn
                turn = Turn(session_id=session_id, turn_id=turn_id or "implicit", started_at=self._clock())
                sess.current = turn
            status = status if status in (STATUS_OK, STATUS_ERROR, STATUS_BLOCKED) else STATUS_OK
            ev = ToolEvent(tool_name, status, _clip(error, 200), self._clock(), key or tool_name)
            if len(turn.events) < MAX_EVENTS_PER_TURN:
                turn.events.append(ev)
            turn.last_by_tool[ev.ident] = ev

    def end_turn(self, session_id: str, turn_id: str = "", *, completed: bool | None = None,
                 failed: bool | None = None, interrupted: bool | None = None, exit_reason: str = "") -> None:
        if not session_id:
            return
        with self._lock:
            turn = self._turn_for(session_id, turn_id)
            if turn is None:
                return
            if failed:
                turn.outcome = "failed"
            elif interrupted:
                turn.outcome = "interrupted"
            elif completed or completed is None:
                turn.outcome = turn.outcome or "completed"
            turn.exit_reason = exit_reason or turn.exit_reason
            turn.ended_at = self._clock()
            sess = self._sessions[session_id]
            if turn not in sess.turns:
                sess.turns.append(turn)

    def record_command(self, session_id: str, command: str) -> None:
        if not session_id or command not in _COMMAND_PENALTIES:
            return
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return
            # The command judges the last finished turn (the one it undoes / retries).
            target = next((t for t in reversed(sess.turns) if t.ended_at is not None), None) or sess.current
            if target is not None and command not in target.commands:
                target.commands.append(command)

    def record_denial(self, session_id: str, turn_id: str, choice: str) -> None:
        if not session_id or str(choice or "").strip().lower() not in _DENY_CHOICES:
            return
        with self._lock:
            turn = self._turn_for(session_id, turn_id)
            if turn is not None:
                turn.denials += 1

    def record_skill_use(self, session_id: str, skill_name: str) -> str | None:
        """Attach a skill load to the session's current turn; returns that turn's id."""
        if not session_id or not skill_name:
            return None
        with self._lock:
            sess = self._session(session_id)
            if sess.current is None:
                return None
            sess.current.skills_used.add(skill_name)
            return sess.current.turn_id

    # -- queries ------------------------------------------------------------------------------------

    def current_turn(self, session_id: str) -> Turn | None:
        with self._lock:
            sess = self._sessions.get(session_id)
            return sess.current if sess else None

    def latest_finished_turn(self, session_id: str) -> Turn | None:
        """The newest turn that ended (what a background review is reviewing), else the current one."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            if sess.current is not None and sess.current.ended_at is not None:
                return sess.current
            finished = [t for t in sess.turns if t.ended_at is not None]
            return finished[-1] if finished else sess.current

    def find_turn(self, session_id: str, turn_id: str) -> Turn | None:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            for turn in list(sess.turns) + ([sess.current] if sess.current else []):
                if turn.turn_id == turn_id:
                    return turn
            return None

    def sessions(self) -> Iterable[str]:
        with self._lock:
            return list(self._sessions)

    # -- settling (verdicts become final) -----------------------------------------------------------

    def close_session(self, session_id: str) -> None:
        """Session ended or was reset: no follow-up will come, so its turns can settle now."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return
            if sess.current is not None:
                if sess.current.ended_at is None:
                    sess.current.ended_at = self._clock()
                sess.current.followup_seen = True
                if sess.current not in sess.turns:
                    sess.turns.append(sess.current)
                sess.current = None
            for turn in sess.turns:
                turn.followup_seen = True

    def drain_settled(self, settle_seconds: float) -> list[Turn]:
        """Turns whose verdict is now final (follow-up seen, or quiet for ``settle_seconds``)."""
        now = self._clock()
        out: list[Turn] = []
        with self._lock:
            out.extend(self._evicted)
            for turn in self._evicted:
                turn.settled = True
            self._evicted = []
            for sess in self._sessions.values():
                for turn in sess.turns:
                    if turn.settled or turn.ended_at is None:
                        continue
                    if turn.followup_seen or now - turn.ended_at >= settle_seconds:
                        turn.settled = True
                        out.append(turn)
        return out
