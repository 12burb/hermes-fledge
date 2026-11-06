"""Load the plugin directory as the package ``fledge`` (the repo folder name is not importable) and
provide fakes for the Hermes surfaces Fledge touches. No Hermes install is needed to run these tests;
the real-loader checks are ``hermes plugins doctor`` / ``hermes plugins validate`` (see README)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin():
    if "fledge" in sys.modules:
        return sys.modules["fledge"]
    spec = importlib.util.spec_from_file_location("fledge", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["fledge"] = module
    spec.loader.exec_module(module)
    return module


fledge = _load_plugin()


class FakeBridge:
    """Stands in for Hermes internals: origin, pending queue, skill hashes, curator actions."""

    def __init__(self) -> None:
        self.origin = "foreground"
        self.unattended = False
        self.staged: list[dict[str, Any]] = []
        self.pending: set = set()
        self.hashes: dict[str, str] = {}
        self.archived: list[str] = []
        self.locked: dict[str, bool] = {}
        self.created: dict[str, str] = {}
        self.paths: dict[str, tuple] = {}
        self.fail_hash = False
        self.stage_fails = False
        self.commands: list = []
        self.missing: set = set()
        self.agent_wrote_last: set = set()

    def capabilities(self) -> dict[str, str]:
        return {"write origin": "ok", "pending queue": "ok"}

    def raw_origin(self) -> str:
        return {"review": "background_review", "other": "side_question"}.get(self.origin, "assistant_tool")

    def write_origin(self) -> str:
        return self.origin

    def is_unattended(self) -> bool:
        return self.unattended

    def stage_skill_write(self, payload: dict[str, Any], summary: str) -> str | None:
        if self.stage_fails:
            return None
        pid = f"p{len(self.staged) + 1:04d}"
        self.staged.append({"id": pid, "payload": payload, "summary": summary})
        self.pending.add(pid)
        return pid

    def pending_ids(self) -> set:
        return set(self.pending)

    def pending_command(self, args: list):
        self.commands.append(list(args))
        action, pid = args[0], args[1]
        if action in ("approve", "reject"):
            self.pending.discard(pid)
            return f"{action.capitalize()}d 1 skills write(s)." if action == "approve" else f"Rejected pending skills write '{pid}'."
        return f"# Pending skill write {pid}: diff"

    def skill_gist(self, action: str, name: str, op: dict[str, Any]) -> str:
        return f"{action} '{name}'"

    def locate_skill_dir(self, name: str):
        return None

    def skill_missing(self, name: str) -> bool:
        return name in self.missing

    def last_written_by_hermes(self, name: str) -> bool:
        return name in self.agent_wrote_last

    def skill_roots(self) -> list:
        return []

    def skill_for_path(self, path: str):
        return self.paths.get(path)

    def _skill_name(self, skill_md: Path, fallback: str) -> str:
        return fallback

    def hash_dir(self, directory) -> str:
        return f"hash:{directory}"

    def hash_skill(self, name: str) -> str:
        if self.fail_hash:
            raise RuntimeError("disk on fire")
        return self.hashes.get(name, "")

    def created_by(self, name: str) -> str:
        return self.created.get(name, "")

    def archive_skill(self, name: str):
        self.archived.append(name)
        return True, f"archived to .archive/{name}"

    def set_locked(self, name: str, locked: bool) -> bool:
        self.locked[name] = locked
        return True

    def connect_db(self) -> sqlite3.Connection:
        return sqlite3.connect(":memory:", check_same_thread=False)


class FakeCtx:
    """The slice of PluginContext Fledge uses."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = dict(config or {})
        self.hooks: dict[str, Any] = {}
        self.commands: dict[str, Any] = {}
        self.cli: dict[str, Any] = {}
        self.posted: list[str] = []
        self.unload: list[Any] = []
        self.post_ok = True
        self.local = False  # a local (CLI/TUI/Desktop) session: Hermes' result carries "delivered"
        self.edits: list[str] = []

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set_config(self, key: str, value: Any) -> None:
        self.config[key] = value

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks[name] = callback

    def register_command(self, name: str, handler: Any, description: str = "", args_hint: str = "") -> None:
        self.commands[name] = handler

    def register_cli_command(self, name: str, help: str, setup_fn: Any, handler_fn: Any = None) -> None:
        self.cli[name] = (setup_fn, handler_fn)

    def post_message(self, text: str):
        if not self.post_ok:
            return SimpleNamespace(ok=False, error="no_conversation")
        self.posted.append(text)
        result = {"ok": True, "message_id": f"m{len(self.posted)}"}
        if self.local:
            result["delivered"] = True
        return SimpleNamespace(ok=True, error=None, result=result, edit=self.edits.append)

    def on_unload(self, callback: Any) -> None:
        self.unload.append(callback)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Anything that falls back to the real bridge writes under a throwaway HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    return tmp_path / "hermes-home"


@pytest.fixture
def bridge() -> FakeBridge:
    return FakeBridge()


@pytest.fixture
def ctx() -> FakeCtx:
    return FakeCtx()


@pytest.fixture
def engine(ctx: FakeCtx, bridge: FakeBridge):
    eng = fledge.build(ctx, bridge=bridge)
    yield eng
    eng.close()
