"""Fledge's durable record: skills on probation, their real runs, holds, and an event log.

Lives in ``<HERMES_HOME>/plugin-data/fledge/data.db`` (``plugins.plugin_storage.plugin_db``), so it
follows the active profile and survives plugin updates. Stores skill names, hashes, verdict reasons
and session/turn ids; never message text or skill content.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

# Skill states. ``veteran``: existed before Fledge saw it change, tracked but never on probation.
FLEDGLING, FLEDGED, GROUNDED, VETERAN, ARCHIVED = "fledgling", "fledged", "grounded", "veteran", "archived"
STATES = (FLEDGLING, FLEDGED, GROUNDED, VETERAN, ARCHIVED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    name TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT '',
    agent_created INTEGER NOT NULL DEFAULT 0,
    born_session TEXT NOT NULL DEFAULT '',
    born_turn TEXT NOT NULL DEFAULT '',
    birth_level TEXT NOT NULL DEFAULT '',
    birth_reasons TEXT NOT NULL DEFAULT '[]',
    clean_uses INTEGER NOT NULL DEFAULT 0,
    bad_uses INTEGER NOT NULL DEFAULT 0,
    neutral_uses INTEGER NOT NULL DEFAULT 0,
    bad_streak INTEGER NOT NULL DEFAULT 0,
    total_clean INTEGER NOT NULL DEFAULT 0,
    total_bad INTEGER NOT NULL DEFAULT 0,
    agent_hash TEXT NOT NULL DEFAULT '',
    human_tuned INTEGER NOT NULL DEFAULT 0,
    human_tuned_at REAL,
    protected_manual INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill TEXT NOT NULL,
    generation INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    level TEXT NOT NULL,
    score INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    at REAL NOT NULL,
    UNIQUE (skill, session_id, turn_id)
);
CREATE INDEX IF NOT EXISTS uses_skill ON uses (skill, at);
CREATE TABLE IF NOT EXISTS holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_id TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL,
    kind TEXT NOT NULL,
    skills TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    reasons TEXT NOT NULL DEFAULT '[]',
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS holds_digest ON holds (digest, at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    skill TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
"""

_MAX_EVENTS = 2000
_MAX_USES = 20000


def _row(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


class Ledger:
    """Small synchronous SQLite store; one connection guarded by a lock (writes are tiny)."""

    def __init__(self, connect: Callable[[], sqlite3.Connection], clock: Callable[[], float] = time.time):
        self._connect = connect
        self._clock = clock
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = self._connect()
            conn.row_factory = _row
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.conn.execute(sql, tuple(params)).fetchall())

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    # -- skills ------------------------------------------------------------------------------------

    def get_skill(self, name: str) -> dict[str, Any] | None:
        rec = self._one("SELECT * FROM skills WHERE name = ?", (name,))
        if rec is not None:
            rec["birth_reasons"] = json.loads(rec.get("birth_reasons") or "[]")
        return rec

    def ensure_skill(self, name: str, state: str = VETERAN) -> dict[str, Any]:
        """Create a record for a skill Fledge has not seen change (default: veteran)."""
        now = self._clock()
        self._exec("INSERT OR IGNORE INTO skills (name, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
                   (name, state, now, now))
        return self.get_skill(name)  # type: ignore[return-value]

    def register_write(self, name: str, *, origin: str, agent_created: bool, session_id: str, turn_id: str,
                       birth_level: str, birth_reasons: list[str], agent_hash: str) -> dict[str, Any]:
        """A write landed: new generation on probation. Totals survive; per-generation counters reset."""
        now = self._clock()
        with self._lock:
            existing = self.get_skill(name)
            if existing is None:
                self._exec(
                    "INSERT INTO skills (name, state, generation, origin, agent_created, born_session, born_turn,"
                    " birth_level, birth_reasons, agent_hash, created_at, updated_at)"
                    " VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, FLEDGLING, origin, int(agent_created), session_id, turn_id, birth_level,
                     json.dumps(birth_reasons), agent_hash, now, now))
            else:
                self._exec(
                    "UPDATE skills SET state = ?, generation = generation + 1, origin = ?,"
                    " agent_created = MAX(agent_created, ?), born_session = ?, born_turn = ?, birth_level = ?,"
                    " birth_reasons = ?, clean_uses = 0, bad_uses = 0, neutral_uses = 0, bad_streak = 0,"
                    " agent_hash = ?, note = '', updated_at = ? WHERE name = ?",
                    (FLEDGLING, origin, int(agent_created), session_id, turn_id, birth_level,
                     json.dumps(birth_reasons), agent_hash, now, name))
            return self.get_skill(name)  # type: ignore[return-value]

    def set_hash(self, name: str, agent_hash: str) -> None:
        self._exec("UPDATE skills SET agent_hash = ?, updated_at = ? WHERE name = ?", (agent_hash, self._clock(), name))

    def set_state(self, name: str, state: str, note: str = "") -> None:
        self._exec("UPDATE skills SET state = ?, note = ?, updated_at = ? WHERE name = ?",
                   (state, note, self._clock(), name))

    def restart_probation(self, name: str, note: str = "") -> None:
        """Back to fledgling with fresh per-version counters (totals survive), as a new write does.
        Without the reset a relapsed or restored skill carries its old bad runs and is grounded by
        its very next run, however that run went."""
        self._exec("UPDATE skills SET state = ?, clean_uses = 0, bad_uses = 0, neutral_uses = 0, bad_streak = 0,"
                   " note = ?, updated_at = ? WHERE name = ?", (FLEDGLING, note, self._clock(), name))

    def set_human_tuned(self, name: str, tuned: bool, *, manual: bool | None = None) -> None:
        now = self._clock()
        self.ensure_skill(name)
        if manual is None:
            self._exec("UPDATE skills SET human_tuned = ?, human_tuned_at = ?, updated_at = ? WHERE name = ?",
                       (int(tuned), now if tuned else None, now, name))
        else:
            self._exec("UPDATE skills SET human_tuned = ?, protected_manual = ?, human_tuned_at = ?, updated_at = ?"
                       " WHERE name = ?", (int(tuned), int(manual and tuned), now if tuned else None, now, name))

    def apply_counts(self, name: str, *, clean: int = 0, bad: int = 0, neutral: int = 0, bad_streak: int) -> None:
        self._exec(
            "UPDATE skills SET clean_uses = clean_uses + ?, bad_uses = bad_uses + ?, neutral_uses = neutral_uses + ?,"
            " total_clean = total_clean + ?, total_bad = total_bad + ?, bad_streak = ?, updated_at = ?"
            " WHERE name = ?", (clean, bad, neutral, clean, bad, bad_streak, self._clock(), name))

    def list_skills(self, state: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if state:
            return self._all("SELECT * FROM skills WHERE state = ? ORDER BY updated_at DESC LIMIT ?", (state, limit))
        return self._all("SELECT * FROM skills ORDER BY updated_at DESC LIMIT ?", (limit,))

    def human_tuned_skills(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM skills WHERE human_tuned = 1 ORDER BY human_tuned_at DESC")

    def skills_born_in(self, session_id: str, turn_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM skills WHERE born_session = ? AND born_turn = ? AND state = ?",
                         (session_id, turn_id, FLEDGLING))

    def counts(self) -> dict[str, int]:
        rows = self._all("SELECT state, COUNT(*) AS n FROM skills GROUP BY state")
        out = {state: 0 for state in STATES}
        out.update({r["state"]: r["n"] for r in rows})
        out["held"] = (self._one("SELECT COUNT(*) AS n FROM holds") or {"n": 0})["n"]
        out["human_tuned"] = (self._one("SELECT COUNT(*) AS n FROM skills WHERE human_tuned = 1") or {"n": 0})["n"]
        return out

    def forget(self, name: str) -> None:
        self._exec("DELETE FROM skills WHERE name = ?", (name,))
        self._exec("DELETE FROM uses WHERE skill = ?", (name,))

    # -- uses --------------------------------------------------------------------------------------

    def add_use(self, name: str, generation: int, session_id: str, turn_id: str, level: str, score: int,
                reasons: list[str]) -> bool:
        """Record one judged run; False if this (skill, session, turn) was already counted."""
        cur = self._exec(
            "INSERT OR IGNORE INTO uses (skill, generation, session_id, turn_id, level, score, reasons, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, generation, session_id, turn_id, level, score, json.dumps(reasons), self._clock()))
        if cur.rowcount and cur.lastrowid and cur.lastrowid % 200 == 0:
            self._exec("DELETE FROM uses WHERE id <= ?", (cur.lastrowid - _MAX_USES,))
        return bool(cur.rowcount)

    def recent_uses(self, name: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM uses WHERE skill = ? ORDER BY at DESC, id DESC LIMIT ?", (name, limit))
        for r in rows:
            r["reasons"] = json.loads(r.get("reasons") or "[]")
        return rows

    # -- holds -------------------------------------------------------------------------------------

    def add_hold(self, *, pending_id: str, digest: str, kind: str, skills: list[str], session_id: str,
                 origin: str, summary: str, reasons: list[str]) -> None:
        self._exec(
            "INSERT INTO holds (pending_id, digest, kind, skills, session_id, origin, summary, reasons, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pending_id, digest, kind, json.dumps(skills), session_id, origin, summary, json.dumps(reasons),
             self._clock()))

    def find_hold(self, digest: str, within_seconds: float = 86400.0) -> dict[str, Any] | None:
        return self._one("SELECT * FROM holds WHERE digest = ? AND at >= ? ORDER BY at DESC LIMIT 1",
                         (digest, self._clock() - within_seconds))

    def recent_holds(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM holds ORDER BY at DESC, id DESC LIMIT ?", (limit,))
        for r in rows:
            r["skills"] = json.loads(r.get("skills") or "[]")
            r["reasons"] = json.loads(r.get("reasons") or "[]")
        return rows

    # -- events ------------------------------------------------------------------------------------

    def add_event(self, kind: str, skill: str = "", detail: str = "") -> None:
        cur = self._exec("INSERT INTO events (at, kind, skill, detail) VALUES (?, ?, ?, ?)",
                         (self._clock(), kind, skill, detail[:500]))
        if cur.lastrowid and cur.lastrowid % 100 == 0:
            self._exec("DELETE FROM events WHERE id <= ?", (cur.lastrowid - _MAX_EVENTS,))

    def recent_events(self, limit: int = 20, kinds: list[str] | None = None) -> list[dict[str, Any]]:
        if kinds:
            marks = ",".join("?" for _ in kinds)
            return self._all(f"SELECT * FROM events WHERE kind IN ({marks}) ORDER BY at DESC, id DESC LIMIT ?",
                             (*kinds, limit))
        return self._all("SELECT * FROM events ORDER BY at DESC, id DESC LIMIT ?", (limit,))
