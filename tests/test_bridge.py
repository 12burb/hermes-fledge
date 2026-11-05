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


def test_storage_falls_back_to_hermes_home(isolated_home):
    conn = HermesBridge().connect_db()
    conn.execute("CREATE TABLE t (x)")
    conn.close()
    assert (isolated_home / "plugin-data" / "fledge" / "data.db").is_file()


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
