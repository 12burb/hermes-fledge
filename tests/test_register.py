"""register(ctx) matches the manifest (what `hermes plugins validate` checks) and every callback takes
the keyword-only payloads Hermes sends, including fields added in future releases."""

import argparse
import inspect
from dataclasses import fields
from pathlib import Path

import yaml
from conftest import FakeCtx, fledge
from fledge.settings import Settings

MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / "plugin.yaml").read_text())


def test_hooks_match_manifest_and_tools_are_empty():
    ctx = FakeCtx()
    fledge.register(ctx)
    assert sorted(ctx.hooks) == sorted(MANIFEST["provides_hooks"])
    assert MANIFEST["provides_tools"] == []
    assert "fledge" in ctx.commands and "fledge" in ctx.cli and ctx.unload


def test_every_hook_accepts_arbitrary_keywords():
    ctx = FakeCtx()
    fledge.register(ctx)
    for name, callback in ctx.hooks.items():
        params = inspect.signature(callback).parameters.values()
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params), name
        callback(session_id="s", turn_id="t", future_field_from_a_newer_hermes=1)


def test_config_schema_matches_settings():
    schema = MANIFEST["config_schema"]
    defaults = Settings()
    assert set(schema) == {f.name for f in fields(Settings)}
    for key, spec in schema.items():
        assert spec["default"] == getattr(defaults, key), key
        if "choices" in spec:
            assert spec["default"] in spec["choices"]


def test_manifest_basics():
    assert MANIFEST["name"] == "fledge"
    assert MANIFEST["version"] == fledge.__version__
    assert MANIFEST["manifest_version"] == 2


def test_cli_parser_and_handler(capsys, profiles):  # the real bridge stores through Hermes
    ctx = FakeCtx()
    fledge.register(ctx)
    setup_fn, handler = ctx.cli["fledge"]
    parser = argparse.ArgumentParser()
    setup_fn(parser)
    handler(parser.parse_args(["status"]))
    assert "Fledge (enforce" in capsys.readouterr().out
    handler(parser.parse_args(["protect", "lights"]))
    assert "'lights' is protected" in capsys.readouterr().out
    handler(parser.parse_args(["mode", "watch"]))
    assert ctx.config["mode"] == "watch"
    handler(parser.parse_args(["export"]))
    assert '"skills"' in capsys.readouterr().out
