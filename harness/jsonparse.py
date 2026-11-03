"""Lenient extraction of one JSON object from a model reply.

Generic helper: it knows nothing about any tool or action vocabulary.

``parse_reply(content, stop_reason)`` returns a ``ParsedReply`` with two
validity flags logged separately:

* ``strict``: the text blocks, stripped of surrounding whitespace, are exactly
  one JSON object (``json.loads`` gives a dict).
* ``lenient``: a JSON object was recovered: the strict case, or after removing
  Markdown code fences, or the first balanced ``{...}`` in the text (string and
  escape aware) that parses as a dict. Candidates are tried from left to right.

Only text blocks are read (thinking, tool_use and other blocks are ignored).
A reply cut by ``max_tokens`` or ended by ``refusal`` is invalid under both
rules, whatever its text, and ``reason`` says why.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

MAX_CANDIDATES = 20

_FENCE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\r?\n?(.*?)```", re.DOTALL)


@dataclass
class ParsedReply:
    obj: dict[str, Any] | None
    strict: bool
    lenient: bool
    reason: str | None  # None when lenient; else "max_tokens", "refusal", "no_text", "no_object"
    text: str

    def to_record(self) -> dict[str, Any]:
        return {"strict_valid": self.strict, "lenient_valid": self.lenient, "invalid_reason": self.reason}


def text_of(content: Any) -> str:
    """Concatenate the text blocks of a Messages API ``content`` list (dicts or SDK objects); a str passes through."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content if isinstance(content, Iterable) else []:
        btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if btype != "text":
            continue
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _loads_dict(s: str) -> dict[str, Any] | None:
    try:
        value = json.loads(s)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def balanced_objects(text: str) -> Iterable[str]:
    """Yield each balanced ``{...}`` substring, from left to right, ignoring braces inside JSON strings."""
    start = text.find("{")
    found = 0
    while start != -1 and found < MAX_CANDIDATES:
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            # Unbalanced from here; a later "{" may still start a complete object.
            start = text.find("{", start + 1)
            continue
        found += 1
        yield text[start : end + 1]
        start = text.find("{", start + 1)


def extract_object(text: str) -> tuple[dict[str, Any] | None, bool]:
    """(object, strict) from raw text: strict parse, else fenced blocks, else the first balanced object that parses."""
    stripped = text.strip()
    obj = _loads_dict(stripped)
    if obj is not None:
        return obj, True
    for m in _FENCE.finditer(text):
        obj = _loads_dict(m.group(1).strip())
        if obj is not None:
            return obj, False
    for cand in balanced_objects(text):
        obj = _loads_dict(cand)
        if obj is not None:
            return obj, False
    return None, False


def parse_reply(content: Any, stop_reason: str | None = None) -> ParsedReply:
    """Parse a reply's text blocks; see the module docstring for the two validity rules."""
    text = text_of(content)
    if stop_reason in ("max_tokens", "refusal"):
        return ParsedReply(None, False, False, stop_reason, text)
    if not text.strip():
        return ParsedReply(None, False, False, "no_text", text)
    obj, strict = extract_object(text)
    if obj is None:
        return ParsedReply(None, False, False, "no_object", text)
    return ParsedReply(obj, strict, True, None, text)
