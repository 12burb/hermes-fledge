"""Every touch point between Fledge and Hermes, in one place.

Documented plugin surfaces are used where they exist (``plugins.plugin_storage``, ``tools.skills_tool``,
``tools.skills_sync.dir_hash``, ``tools.skill_usage.read_skill_name``, ``gateway.session_context``).
Three needs have no public seam yet, so they call the same functions Hermes' own commands use:

* which agent is writing (``tools.skill_provenance.get_current_write_origin``: the background-review
  fork vs. a foreground turn);
* putting a held write in the ``/skills pending`` queue (``tools.write_approval.stage_write``), so the
  user reviews it with the stock ``/skills pending|diff|approve|reject`` commands;
* archiving and content-locking a skill (``tools.skill_usage.archive_skill`` / ``set_locked``, what
  ``hermes curator archive`` and ``hermes curator lock`` call).

Each call is guarded: if a Hermes release moves one of them, that one feature degrades (reported by
``/fledge doctor``) and the agent is never broken. Nothing here patches or wraps Hermes code.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_NAME = "fledge"
ORIGIN_REVIEW, ORIGIN_FOREGROUND, ORIGIN_OTHER, ORIGIN_UNKNOWN = "review", "foreground", "other", "unknown"
_UNATTENDED_PLATFORMS = {"webhook", "msgraph_webhook", "api_server"}
_TRUTHY = {"1", "true", "yes", "on"}


class HermesBridge:
    """Thin, failure-tolerant adapter over Hermes internals (swap with a fake in tests)."""

    def __init__(self) -> None:
        self._failures: dict[str, str] = {}

    # -- diagnostics ---------------------------------------------------------------------------------

    def _fail(self, feature: str, exc: BaseException) -> None:
        if feature not in self._failures:
            logger.warning("fledge: %s unavailable (%s: %s)", feature, type(exc).__name__, exc)
        self._failures[feature] = f"{type(exc).__name__}: {exc}"

    def capabilities(self) -> dict[str, str]:
        """Feature -> 'ok' or the reason it is degraded (probes every import)."""
        probes = {
            "write origin": ("tools.skill_provenance", "get_current_write_origin"),
            "pending queue": ("tools.write_approval", "stage_write"),
            "archive / lock": ("tools.skill_usage", "set_locked"),
            "skill lookup": ("tools.skills_tool", "locate_skill"),
            "plugin storage": ("plugins.plugin_storage", "plugin_db"),
        }
        out: dict[str, str] = {}
        for feature, (module_name, attr) in probes.items():
            try:
                module = importlib.import_module(module_name)
                if not hasattr(module, attr):
                    raise AttributeError(f"{module_name}.{attr} missing")
                out[feature] = self._failures.get(feature, "ok")
            except Exception as exc:  # pragma: no cover - depends on the host
                out[feature] = f"{type(exc).__name__}: {exc}"
        return out

    # -- who is writing --------------------------------------------------------------------------------

    def raw_origin(self) -> str:
        if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":
            # plugins.isolation: host — the caller's per-turn origin does not cross the process boundary.
            self._failures.setdefault("write origin", "running in the plugin host (plugins.isolation: host)")
            return ""
        try:
            from tools.skill_provenance import get_current_write_origin
            return str(get_current_write_origin() or "")
        except Exception as exc:
            self._fail("write origin", exc)
            return ""

    def write_origin(self) -> str:
        """``review`` for the background review / curator fork, ``other`` for side-question forks,
        ``foreground`` for normal turns (CLI, gateway, cron, subagents), ``unknown`` if undeterminable."""
        raw = self.raw_origin()
        if not raw:
            return ORIGIN_UNKNOWN
        if raw == "background_review":
            return ORIGIN_REVIEW
        if raw == "side_question":
            return ORIGIN_OTHER
        return ORIGIN_FOREGROUND

    def is_unattended(self) -> bool:
        """True when no human can answer an approval prompt in this turn (cron, kanban, API, webhook)."""

        def env(name: str) -> str:
            try:
                from gateway.session_context import get_session_env
                return str(get_session_env(name, "") or "")
            except Exception:
                return os.environ.get(name, "") or ""

        if env("HERMES_KANBAN_TASK").strip():
            return True
        if env("HERMES_CRON_SESSION").strip().lower() in _TRUTHY:
            return True
        platform = (env("HERMES_SESSION_PLATFORM") or os.environ.get("HERMES_PLATFORM", "")).strip().lower()
        return platform in _UNATTENDED_PLATFORMS

    # -- the /skills pending queue -----------------------------------------------------------------------

    def stage_skill_write(self, payload: dict[str, Any], summary: str) -> str | None:
        """Stage *payload* (replayable by ``/skills approve``) and return its pending id, or None."""
        try:
            from tools import write_approval as wa
            origin = wa.current_origin() if hasattr(wa, "current_origin") else (self.raw_origin() or "foreground")
            record = wa.stage_write(wa.SKILLS, payload, summary=summary, origin=origin)
            pending_id = str(record.get("id") or "") if isinstance(record, dict) else ""
            return pending_id or None
        except Exception as exc:
            self._fail("pending queue", exc)
            return None

    def pending_ids(self) -> set | None:
        """Ids still waiting in the /skills pending queue (None if the queue can't be read)."""
        try:
            from tools import write_approval as wa
            return {str(r.get("id")) for r in wa.list_pending(wa.SKILLS)}
        except Exception as exc:
            self._fail("pending queue", exc)
            return None

    def pending_command(self, args: list) -> str | None:
        """Run Hermes' own ``/skills pending|diff|approve|reject`` handler (None if unavailable).

        ``/skills`` is CLI-only in messaging chats unless ``skills.write_approval`` is on, so ``/fledge``
        exposes the same handler for Fledge's holds on every surface."""
        try:
            from hermes_cli.write_approval_commands import handle_pending_subcommand
            result = handle_pending_subcommand("skills", [str(a) for a in args])
            return None if result is None else str(result)
        except Exception as exc:
            self._fail("pending queue", exc)
            return None

    def skill_gist(self, action: str, name: str, op: dict[str, Any]) -> str:
        try:
            from tools.write_approval import skill_gist
            return skill_gist(action, name, content=op.get("content") or "", file_path=op.get("file_path") or "",
                              old_string=op.get("old_string") or "", new_string=op.get("new_string") or "")
        except Exception:
            return f"{action} '{name}'"

    # -- skills on disk ----------------------------------------------------------------------------------

    def locate_skill_dir(self, name: str) -> Path | None:
        try:
            from tools.skills_tool import locate_skill, skill_lookup_path_error, skill_search_dirs
            if skill_lookup_path_error(name):
                return None
            roots, _active = skill_search_dirs()
            error, skill_dir, _skill_md = locate_skill(name, None, roots)
            return None if error else Path(skill_dir)
        except Exception as exc:
            self._fail("skill lookup", exc)
            return None

    def skill_roots(self) -> list:
        try:
            from tools.skills_tool import skill_search_dirs
            roots, active = skill_search_dirs()
            dirs = [Path(d) for _tier, d in roots]
            if active and Path(active) not in dirs:
                dirs.append(Path(active))
            return dirs
        except Exception as exc:
            self._fail("skill lookup", exc)
            return []

    def skill_for_path(self, path: str) -> tuple[str, Path] | None:
        """``(skill_name, skill_dir)`` when *path* is inside a skill package, else None."""
        if not path:
            return None
        try:
            target = Path(os.path.expanduser(str(path))).resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        for root in self.skill_roots():
            try:
                root = root.resolve()
                target.relative_to(root)
            except (OSError, ValueError):
                continue
            current = target if target.is_dir() else target.parent
            while current != root and root in current.parents:
                skill_md = current / "SKILL.md"
                if skill_md.is_file():
                    return self._skill_name(skill_md, current.name), current
                current = current.parent
        return None

    def _skill_name(self, skill_md: Path, fallback: str) -> str:
        try:
            from tools.skill_usage import read_skill_name
            return str(read_skill_name(skill_md, fallback))
        except Exception:
            return fallback

    def hash_dir(self, directory: Path | None) -> str:
        if directory is None or not Path(directory).is_dir():
            return ""
        try:
            from tools.skills_sync import dir_hash
            return str(dir_hash(Path(directory)))
        except Exception:
            hasher = hashlib.md5()
            for fpath in sorted(Path(directory).rglob("*")):
                if "__pycache__" in fpath.parts or not fpath.is_file():
                    continue
                hasher.update(str(fpath.relative_to(directory)).encode("utf-8"))
                try:
                    hasher.update(fpath.read_bytes())
                except OSError:
                    continue
            return hasher.hexdigest()

    def hash_skill(self, name: str) -> str:
        return self.hash_dir(self.locate_skill_dir(name))

    # -- curator actions ---------------------------------------------------------------------------------

    def created_by(self, name: str) -> str:
        """Hermes' curator-management marker: 'agent' (background review), 'learn', 'installed' or ''."""
        try:
            from tools.skill_usage import get_record
            record = get_record(name)
            return str(record.get("created_by") or "") if isinstance(record, dict) else ""
        except Exception as exc:
            self._fail("archive / lock", exc)
            return ""

    def archive_skill(self, name: str) -> tuple[bool, str]:
        try:
            from tools.skill_usage import archive_skill
            ok, message = archive_skill(name)
            return bool(ok), str(message)
        except Exception as exc:
            self._fail("archive / lock", exc)
            return False, f"archive unavailable: {exc}"

    def set_locked(self, name: str, locked: bool) -> bool:
        key = name
        skill_dir = self.locate_skill_dir(name)
        if skill_dir is not None:
            key = skill_dir.name  # Hermes keys content locks by directory name
        try:
            from tools.skill_usage import set_locked
            return bool(set_locked(key, locked))
        except Exception as exc:
            self._fail("archive / lock", exc)
            return False

    # -- storage -----------------------------------------------------------------------------------------

    def connect_db(self) -> sqlite3.Connection:
        try:
            from plugins.plugin_storage import plugin_db
            return plugin_db(PLUGIN_NAME)
        except Exception as exc:
            self._fail("plugin storage", exc)
            home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
            folder = home / "plugin-data" / PLUGIN_NAME
            folder.mkdir(parents=True, exist_ok=True)
            return sqlite3.connect(folder / "data.db", check_same_thread=False)
