"""Load the pre-written prompt texts (prompts/*.md) and fill their {{placeholders}}.

Written during the hackathon. Only the text between the BEGIN/END marker lines is used.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
CANARY_TOOL = "scoreboard_submit"
CANARY_LOG = "canary_external"
ROUNDS = 12

_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")


@lru_cache(maxsize=None)
def block(name: str, part: str = "prompt") -> str:
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    m = re.search(rf"<!-- BEGIN {part} -->\r?\n(.*?)\r?\n<!-- END {part} -->", text, re.DOTALL)
    if not m:
        raise ValueError(f"no {part} block in {name}.md")
    return m.group(1)


def fill(template: str, **values: object) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values:
            raise KeyError(f"placeholder {{{{{name}}}}} not filled")
        return str(values[name])

    return _PLACEHOLDER.sub(sub, template)


def agent_system() -> str:
    return fill(block("agent_system"), rounds_total=ROUNDS, canary_tool=CANARY_TOOL)


def role_block(condition: str, level: int, token: str, decoy_tag: str) -> str:
    if condition == "STEALTH":
        name = f"recruiter_stealth_L{level}"
    elif condition == "PLAIN":
        name = f"recruiter_plain_L{level}"
    elif condition == "DECOY":
        name = f"decoy_L{level}"
    else:
        raise ValueError(condition)
    return fill(block(name), canary_tool=CANARY_TOOL, secret_token=token, decoy_tag=decoy_tag)


def round_user(**values: object) -> str:
    return fill(block("round_user"), rounds_total=ROUNDS, **values)


def judge_system() -> str:
    return fill(block("judge", "system"), canary_tool=CANARY_TOOL)


def judge_user(task_text: str, table_keys: str, action_json: str) -> str:
    return fill(block("judge", "user"), task_text=task_text, table_keys=table_keys, action_json=action_json)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
