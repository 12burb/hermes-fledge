"""``hermes fledge ...`` terminal commands (same engine as the /fledge slash command)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def setup_parser(subparser: Any) -> None:
    subs = subparser.add_subparsers(dest="fledge_command")
    subs.add_parser("status", help="Probation, holds, grounded and hand-tuned skills")
    subs.add_parser("pending", help="Skill writes Fledge is holding for you")
    for name, text in (("diff", "What a held write would change"), ("approve", "Save a held write anyway"),
                       ("reject", "Drop a held write")):
        sp = subs.add_parser(name, help=text)
        sp.add_argument("id")
    for name, text in (("skill", "One skill's track record"), ("protect", "Treat a skill as hand-tuned"),
                       ("unprotect", "Lift hand-tuned protection"), ("release", "Take a skill off probation"),
                       ("forget", "Drop Fledge's record of a skill")):
        sp = subs.add_parser(name, help=text)
        sp.add_argument("skill")
    mode = subs.add_parser("mode", help="Show or set the mode (enforce | watch | off)")
    mode.add_argument("value", nargs="?", default="")
    subs.add_parser("doctor", help="Which Hermes features Fledge can reach")
    subs.add_parser("scan", help="Fingerprint existing skills so later hand edits are recognized")
    subs.add_parser("export", help="Print Fledge's records as JSON")


def make_handler(get_engine: Callable[[], Any]) -> Callable[[Any], None]:
    def handler(args: Any) -> None:
        engine = get_engine()
        sub = getattr(args, "fledge_command", None) or "status"
        if sub == "scan":
            result = engine.scan()
            print(f"Fledge scanned {result['seen']} skills and fingerprinted {result['fingerprinted']} new ones.")
            return
        if sub == "export":
            print(engine.export())
            return
        if sub == "mode":
            print(engine.set_mode(getattr(args, "value", "") or ""))
            return
        target = getattr(args, "skill", "") or getattr(args, "id", "") or ""
        print(engine.command(f"{sub} {target}".strip()))
    return handler
