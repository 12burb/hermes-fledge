"""Check Fledge's per-profile storage against REAL Hermes code (not test stand-ins).

Run with a Hermes checkout's own interpreter:
    <hermes>/.venv/Scripts/python.exe profiles_check.py <path to hermes-fledge>

Uses Hermes' real ``hermes_constants.set_hermes_home_override`` (the context-local switch a gateway uses
to serve several profiles from one process) and the real ``plugins.plugin_storage.plugin_db``. Three
throwaway homes under a temp dir: a "launch" home in the HERMES_HOME environment variable, and two
profile homes that are only ever selected through the override. Nothing outside the temp dir is touched.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

plugin_dir = Path(sys.argv[1]).resolve()
root = Path(tempfile.mkdtemp(prefix="fledge-profiles-"))
launch, alpha, beta = root / "launch", root / "alpha", root / "beta"
for home in (launch, alpha, beta):
    home.mkdir()
for name in [k for k in os.environ if k.startswith(("HERMES_", "GATEWAY_"))]:
    del os.environ[name]
os.environ["HERMES_HOME"] = str(launch)

import hermes_constants  # noqa: E402  (real Hermes)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override  # noqa: E402

spec = importlib.util.spec_from_file_location("fledge", plugin_dir / "__init__.py",
                                              submodule_search_locations=[str(plugin_dir)])
fledge = importlib.util.module_from_spec(spec)
sys.modules["fledge"] = fledge
spec.loader.exec_module(fledge)
from fledge.bridge import HermesBridge  # noqa: E402
from fledge.ledger import FLEDGLING, Ledger  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))


class as_profile:
    def __init__(self, home: Path):
        self.home = home

    def __enter__(self):
        self.token = set_hermes_home_override(str(self.home))

    def __exit__(self, *exc):
        reset_hermes_home_override(self.token)


bridge = HermesBridge()
try:
    ledger = Ledger(bridge.connect_db, key=bridge.home_key)
    keyed = True
except (TypeError, AttributeError):
    ledger = Ledger(bridge.connect_db)      # the wiring before the fix
    keyed = False
print(f"hermes at {Path(hermes_constants.__file__).parent.name}; plugin at {plugin_dir.name}; "
      f"ledger keyed by profile: {keyed}")

with as_profile(alpha):
    ledger.ensure_skill("water-results", FLEDGLING)
    ledger.add_event("held", "water-results", "alpha's hold")
with as_profile(beta):
    check("beta does not see alpha's same-named skill", ledger.get_skill("water-results") is None)
    check("beta does not see alpha's events", ledger.recent_events() == [])
    ledger.ensure_skill("water-results", FLEDGLING)
    ledger.set_human_tuned("water-results", True)
with as_profile(alpha):
    check("beta's hand-edit flag did not reach alpha", not ledger.get_skill("water-results")["human_tuned"])

# Two profiles served at the same time from different threads (a context-local override per thread).
errors: list[str] = []


def worker(home: Path, tag: str) -> None:
    try:
        with as_profile(home):
            for i in range(60):
                ledger.ensure_skill(f"{tag}-{i}", FLEDGLING)
                ledger.add_event("written", f"{tag}-{i}", tag)
    except Exception as exc:  # pragma: no cover
        errors.append(f"{tag}: {type(exc).__name__}: {exc}")


threads = [threading.Thread(target=worker, args=(alpha, "a")), threading.Thread(target=worker, args=(beta, "b"))]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent writers raised nothing", not errors, "; ".join(errors))
ledger.close()


def names(home: Path) -> set[str]:
    path = home / "plugin-data" / "fledge" / "data.db"
    if not path.is_file():
        return set()
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM skills")}
    finally:
        conn.close()


a_names, b_names = names(alpha), names(beta)
check("alpha's file holds only alpha's skills",
      a_names == {"water-results"} | {f"a-{i}" for i in range(60)}, f"{len(a_names)} rows")
check("beta's file holds only beta's skills",
      b_names == {"water-results"} | {f"b-{i}" for i in range(60)}, f"{len(b_names)} rows")
check("nothing was written to the launch home", not (launch / "plugin-data").exists())

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
