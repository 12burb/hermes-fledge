"""The real bridge degrades feature by feature when Hermes internals are missing or move (here Hermes is
not installed at all), and never raises into the engine."""

from fledge.bridge import HermesBridge


def test_bridge_degrades_without_hermes(isolated_home, tmp_path):
    bridge = HermesBridge()
    assert bridge.write_origin() == "unknown"
    assert bridge.stage_skill_write({"action": "create"}, "x") is None
    assert bridge.pending_ids() is None
    assert bridge.archive_skill("x")[0] is False
    assert bridge.set_locked("x", True) is False
    assert bridge.created_by("x") == ""
    assert bridge.locate_skill_dir("x") is None
    assert bridge.skill_missing("x") is False  # can't tell, so never "deleted"
    assert bridge.skill_for_path(str(tmp_path / "SKILL.md")) is None
    assert bridge.skill_gist("create", "x", {}) == "create 'x'"
    caps = bridge.capabilities()
    assert caps and all(state != "ok" for state in caps.values())


def test_hash_fallback_is_stable_and_content_sensitive(tmp_path):
    skill = tmp_path / "lights"
    skill.mkdir()
    (skill / "SKILL.md").write_text("v1")
    bridge = HermesBridge()
    first = bridge.hash_dir(skill)
    assert first == bridge.hash_dir(skill)
    (skill / "SKILL.md").write_text("v2")
    assert bridge.hash_dir(skill) != first
    assert bridge.hash_dir(tmp_path / "missing") == ""


def test_plugin_host_process_cannot_see_the_turn_origin(monkeypatch):
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    bridge = HermesBridge()
    assert bridge.write_origin() == "unknown"
    assert "plugin host" in bridge._failures["write origin"]


def test_unattended_detection_from_session_env(monkeypatch):
    bridge = HermesBridge()
    assert bridge.is_unattended() is False
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    assert bridge.is_unattended() is True
    monkeypatch.delenv("HERMES_CRON_SESSION")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "api_server")
    assert bridge.is_unattended() is True


def test_missing_content_lock_degrades_only_that_feature(monkeypatch):
    """Hermes < 0.22.7 has archive_skill but no set_locked (``hermes curator lock`` came in 0.22.7): the
    doctor must report the lock alone as degraded, and archiving keeps working."""
    import sys
    import types

    tools = types.ModuleType("tools")
    tools.__path__ = []
    usage = types.ModuleType("tools.skill_usage")
    usage.archive_skill = lambda name: (True, f"archived {name}")
    usage.get_record = lambda name: {"created_by": "agent"}
    tools.skill_usage = usage
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.skill_usage", usage)

    bridge = HermesBridge()
    caps = bridge.capabilities()
    assert caps["archive"] == "ok"
    assert caps["content lock"] != "ok" and "set_locked" in caps["content lock"]
    assert bridge.archive_skill("x") == (True, "archived x")
    assert bridge.created_by("x") == "agent"
    assert bridge.set_locked("x", True) is False
    assert "content lock" in bridge._failures and "archive" not in bridge._failures


def test_last_written_by_hermes_compares_files_with_hermes_own_write_stamp(monkeypatch, tmp_path):
    import os
    import sys
    import types
    from datetime import datetime, timezone

    skill = tmp_path / "water"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("v2")
    (skill / "references" / "notes.md").write_text("n")
    for f in (skill / "SKILL.md", skill / "references" / "notes.md"):
        os.utime(f, (1_000_000, 1_000_000))
    stamp = {"last_patched_at": datetime.fromtimestamp(1_000_000.5, timezone.utc).isoformat()}

    tools = types.ModuleType("tools")
    tools.__path__ = []
    usage = types.ModuleType("tools.skill_usage")
    usage.get_record = lambda name: stamp
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.skill_usage", usage)
    bridge = HermesBridge()
    monkeypatch.setattr(bridge, "locate_skill_dir", lambda name: skill)

    assert bridge.last_written_by_hermes("water") is True       # files are as skill_manage left them
    os.utime(skill / "references" / "notes.md", (1_000_100, 1_000_100))
    assert bridge.last_written_by_hermes("water") is False      # a file changed after Hermes' last write
    stamp.clear()
    assert bridge.last_written_by_hermes("water") is False      # never written by skill_manage


def test_last_written_by_hermes_is_false_without_hermes(isolated_home):
    assert HermesBridge().last_written_by_hermes("x") is False


def test_a_staged_write_missing_from_disk_is_not_reported_as_held(monkeypatch):
    """stage_write logs a disk failure and still returns a record; confirm it with get_pending."""
    import sys
    import types
    tools = types.ModuleType("tools")
    tools.__path__ = []
    wa = types.ModuleType("tools.write_approval")
    wa.SKILLS = "skills"
    wa.current_origin = lambda: "background_review"
    wa.stage_write = lambda subsystem, payload, *, summary, origin: {"id": "abcd1234"}
    stored: dict = {}
    wa.get_pending = lambda subsystem, pid: stored.get(pid)
    tools.write_approval = wa
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.write_approval", wa)
    bridge = HermesBridge()
    assert bridge.stage_skill_write({"action": "create"}, "s") is None
    assert "pending queue" in bridge._failures
    stored["abcd1234"] = {"id": "abcd1234"}
    assert HermesBridge().stage_skill_write({"action": "create"}, "s") == "abcd1234"


# -- one process, several profiles ---------------------------------------------------------------------
# A Hermes gateway can serve several profiles in one process: it switches a context-local home per call
# and ``plugin_db`` follows it. The ``profiles`` fixture stands in for those two Hermes modules.

import pytest

from fledge.ledger import FLEDGLING, Ledger
from fledge.settings import SettingsLoader


def test_each_profile_gets_its_own_ledger(profiles):
    bridge = HermesBridge()
    ledger = Ledger(bridge.connect_db, key=bridge.home_key)
    try:
        profiles.switch("alpha")
        ledger.ensure_skill("water-results", FLEDGLING)
        ledger.add_event("held", "water-results", "alpha's hold")

        profiles.switch("beta")
        assert ledger.get_skill("water-results") is None  # beta never saw alpha's same-named skill
        assert ledger.recent_events() == []
        ledger.ensure_skill("water-results", FLEDGLING)
        ledger.set_human_tuned("water-results", True)

        profiles.switch("alpha")
        assert not ledger.get_skill("water-results")["human_tuned"]  # beta's hand-edit flag stayed in beta
        assert [e["detail"] for e in ledger.recent_events()] == ["alpha's hold"]
    finally:
        ledger.close()
    for name in ("alpha", "beta"):
        assert (profiles.root / name / "plugin-data" / "fledge" / "data.db").is_file()


def test_closing_the_ledger_closes_every_profile(profiles):
    bridge = HermesBridge()
    ledger = Ledger(bridge.connect_db, key=bridge.home_key)
    for name in ("alpha", "beta"):
        profiles.switch(name)
        ledger.ensure_skill("s")
    ledger.close()
    for name in ("alpha", "beta"):  # no handle left open: the files can be removed (matters on Windows)
        (profiles.root / name / "plugin-data" / "fledge" / "data.db").unlink()


def test_storage_never_guesses_a_path_from_the_environment(isolated_home):
    """Without Hermes' plugin storage there is no database: a path built from HERMES_HOME could belong to
    another profile than the one being served."""
    bridge = HermesBridge()
    with pytest.raises(Exception):
        bridge.connect_db()
    assert not (isolated_home / "plugin-data").exists()
    assert bridge.capabilities()["plugin storage"] != "ok"


def test_settings_are_read_per_profile(profiles):
    """The settings cache is per profile too: one profile's ``mode: off`` must not leak to the next call."""
    bridge = HermesBridge()
    config = {str(profiles.root / "alpha"): {"mode": "off"}, str(profiles.root / "beta"): {"mode": "enforce"}}

    def getter(key, default=None):
        return config[bridge.home_key()].get(key, default)

    loader = SettingsLoader(getter, ttl=3600.0, key=bridge.home_key)
    profiles.switch("alpha")
    assert loader.get().mode == "off"
    profiles.switch("beta")
    assert loader.get().mode == "enforce"
    profiles.switch("alpha")
    assert loader.get().mode == "off"
