"""Fledge — skills have to earn their wings.

A Hermes Agent plugin that stops the self-improvement loop from saving skills learned from runs that went
wrong, keeps new skills on probation until their real runs prove them, and turns autonomous rewrites of
skills you edited by hand into proposals. See README.md.
"""

from __future__ import annotations

import logging
from typing import Any

from .bridge import HermesBridge
from .cli import make_handler, setup_parser
from .engine import Fledge
from .evidence import EvidenceTracker
from .ledger import Ledger
from .notify import Notifier
from .settings import SettingsLoader

__version__ = "0.1.0"
logger = logging.getLogger(__name__)


def build(ctx: Any, bridge: Any = None) -> Fledge:
    """Wire a Fledge engine to a plugin context (tests pass fakes for both)."""
    bridge = bridge or HermesBridge()
    settings = SettingsLoader(ctx.get_config, key=bridge.home_key)

    def post(text: str) -> Any:
        poster = getattr(ctx, "post_message", None)
        return poster(text) if callable(poster) else None

    notifier = Notifier(post, lambda: settings.get().notify)
    return Fledge(settings=settings, bridge=bridge, ledger=Ledger(bridge.connect_db, key=bridge.home_key),
                  tracker=EvidenceTracker(),
                  notifier=notifier, set_config=getattr(ctx, "set_config", None))


def register(ctx: Any) -> None:
    engine = build(ctx)
    for hook_name, callback in engine.hooks().items():
        ctx.register_hook(hook_name, callback)
    ctx.register_command(
        "fledge", engine.command,
        description="Skill flight school: held saves, probation, hand-edit protection",
        args_hint="[status|skill NAME|protect NAME|release NAME|mode MODE|doctor]",
    )
    ctx.register_cli_command(
        name="fledge", help="Skill flight school: held saves, probation, hand-edit protection",
        setup_fn=setup_parser, handler_fn=make_handler(lambda: engine),
    )
    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        on_unload(engine.close)
    logger.debug("fledge %s registered", __version__)
