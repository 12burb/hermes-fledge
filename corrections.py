"""Spot a user correcting the agent in the message that follows a turn.

Deterministic on purpose: Fledge never asks a model whether a model did well. Only the start of the
message is read (corrections lead), and clearly positive replies ("perfect, thanks", "no worries")
never count, so a casual "no" is not treated as a complaint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_WINDOW = 280  # characters of the follow-up message that are inspected

_STRONG = [
    r"\b(?:that|this|it)(?:'s| is| was)\s+(?:all\s+)?(?:wrong|incorrect|broken|not right|not correct)\b",
    r"\bthat'?s not (?:it|right|correct|what i (?:asked|wanted|meant))\b",
    r"\bnot what i (?:asked|wanted|meant|said)\b",
    r"\b(?:you|it|this) (?:broke|messed up|screwed up|ruined|mangled|jumbled)\b",
    r"\b(?:doesn'?t|didn'?t|does not|did not|won'?t|isn'?t|is not) work(?:ing)?\b",
    r"\bstill (?:broken|failing|wrong|erroring|crashing|not working|doesn'?t work|isn'?t working)\b",
    r"\b(?:undo|revert|roll ?back) (?:that|this|it|those|these|the (?:change|changes|edit|edits))\b",
    r"\b(?:wrong|incorrect) (?:file|data|numbers?|values?|results?|output|answer|branch|repo)\b",
    r"\byou (?:got|have) (?:it|that|this|everything) wrong\b",
    r"\bjumbled\b",
    r"^\s*(?:no|nope|nah|wrong)\s*[,.!]",
    r"^\s*(?:no|nope|wrong)\s*$",
]

_WEAK = [
    r"\btry again\b",
    r"\bnot quite\b",
    r"\bclose,? but\b",
    r"\balmost(?: there)?,? but\b",
    r"\bfix (?:it|that|this)\b",
    r"\bthat'?s off\b",
    r"\bmissing (?:a|the|some)\b",
    r"\byou (?:forgot|missed|skipped)\b",
]

_POSITIVE = [
    r"^\s*no (?:worries|problem|rush|need|thanks)\b",
    r"\b(?:perfect|awesome|great job|nice work|works now|that worked|it works|works great|looks good|lgtm)\b",
    r"^\s*(?:thanks|thank you|thx|ty)\b",
]

_STRONG_RE = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _STRONG]
_WEAK_RE = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _WEAK]
_POSITIVE_RE = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _POSITIVE]


@dataclass(frozen=True)
class Correction:
    strength: str  # "strong" | "weak"
    phrase: str    # the matched words, quoted back to the user in reasons


def message_text(message: Any) -> str:
    """Plain text of a user message; multimodal turns arrive as a list of content parts."""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for part in message:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in (None, "text", "input_text"):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return ""


def detect_correction(message: Any) -> Correction | None:
    """Return the correction found at the start of *message*, or None."""
    text = message_text(message).strip()
    if not text:
        return None
    window = text[:_WINDOW]
    positive = any(rx.search(window) for rx in _POSITIVE_RE)
    for rx in _STRONG_RE:
        match = rx.search(window)
        if match:
            phrase = match.group(0).strip(" ,.!")
            # "No, that's perfect" style replies: a bare leading "no" next to praise is not a complaint.
            if positive and rx.pattern.startswith("^"):
                return None
            return Correction("strong", phrase)
    if positive:
        return None
    for rx in _WEAK_RE:
        match = rx.search(window)
        if match:
            return Correction("weak", match.group(0).strip(" ,.!"))
    return None
