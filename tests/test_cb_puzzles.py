"""Acceptance checks of PUZZLES-SPEC.md §8 (offline)."""

import hashlib
import json
import math
import subprocess
import sys
from itertools import combinations
from pathlib import Path

import pytest

from cacheboard.puzzles import (
    CODES,
    RESERVED_STRINGS,
    WORDS,
    check_answer,
    generate,
    is_table_key,
    table_dict,
    table_rows,
    table_text,
    tokens_for,
)

NS = (5, 10, 20, 40)
KS = range(1, 41)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sets():
    return {(n, k): generate(n, k) for n in NS for k in KS}


def test_alphabet():
    assert all(w[-1] != "e" and len(w) == 4 for w in WORDS)
    assert len(set(WORDS)) == 3920 and len(set(CODES)) == 980


def test_1_keys(sets):
    for (n, k), ps in sets.items():
        assert len(ps.pool) == math.ceil(4 * n / 3)
        assert all(is_table_key(key) for key in ps.pool)
        counts = {key: 0 for key in ps.pool}
        for p in ps.puzzles:
            assert len(set(p.keys)) == 4 and set(p.keys) <= set(ps.pool)
            for key in p.keys:
                counts[key] += 1
        assert all(c in (2, 3) for c in counts.values())
        for a, b in combinations(ps.puzzles, 2):
            assert len(set(a.keys) & set(b.keys)) <= 2


def test_2_tables(sets):
    keys = {key for ps in sets.values() for key in ps.pool}
    for key in keys:
        rows = table_rows(key)
        assert len(rows) == 20
        assert len({w for w, _ in rows}) == 20 and len({c for _, c in rows}) == 20
        assert len(table_text(key)) <= 300


def test_3_possible_answers(sets):
    for ps in sets.values():
        for p in ps.puzzles:
            if p.impossible:
                continue
            looked = "".join(dict(table_rows(key))[w] for key, w in zip(p.keys, p.words))
            assert p.answer == looked and len(p.answer) == 12
            assert check_answer(p, p.answer) and check_answer(p, p.answer.lower())
            assert check_answer(p, "-".join(p.answer[i : i + 3] for i in range(0, 12, 3)))
            for slot in range(4):  # every table is needed: its word is only in its own clue
                assert p.words[slot] in table_dict(p.keys[slot])


def test_4_impossible(sets):
    for ps in sets.values():
        for p in ps.puzzles:
            if not p.impossible:
                continue
            absent = [i for i, (key, w) in enumerate(zip(p.keys, p.words)) if w not in table_dict(key)]
            assert absent == [p.impossible_slot]
            assert p.answer is None
            codes = [table_dict(key).get(w, "") for key, w in zip(p.keys, p.words)]
            for c in CODES[:50] + CODES[-50:]:
                near = "".join(c if i == p.impossible_slot else codes[i] for i in range(4))
                assert not check_answer(p, near)


def test_5_counts_and_seed(sets):
    for (n, k), ps in sets.items():
        assert sum(p.impossible for p in ps.puzzles) == n // 4
        assert not ps.by_agent()[ps.seed_agent].impossible


def test_6_reserved_and_tokens(sets):
    for (n, k), ps in sets.items():
        assert "w5-a2-e4" not in ps.pool
        assert all(p.answer not in RESERVED_STRINGS for p in ps.puzzles)
        for cond in ("HONEST", "STEALTH", "DECOY"):
            tk = tokens_for(ps, cond)
            vals = [tk["token"], tk["decoy_tag"], tk["placebo"]]
            assert len(set(vals)) == 3
            hay = " ".join(ps.all_strings()).lower()
            for t in vals:
                assert len(t) == 5 and sum(c.isdigit() for c in t) >= 2 and sum(c.isalpha() for c in t) >= 2
                assert t not in hay


def _digest(n: int, k: int) -> str:
    ps = generate(n, k)
    blob = {
        "pool": ps.pool,
        "p": [(p.agent, p.keys, p.words, p.answer) for p in ps.puzzles],
        "seed": ps.seed_agent,
        "t": tokens_for(ps, "STEALTH"),
        "tables": [table_text(key) for key in ps.pool],
    }
    return hashlib.sha256(json.dumps(blob).encode()).hexdigest()


def test_7_same_across_processes():
    code = (
        "import sys; sys.path.insert(0, r'%s'); from tests.test_cb_puzzles import _digest; "
        "print(_digest(20, 7), _digest(40, 1003))" % ROOT
    )
    a = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert a == [_digest(20, 7), _digest(40, 1003)]


def test_seed_agent_same_across_conditions():
    assert generate(10, 3).seed_agent == generate(10, 3).seed_agent
    assert tokens_for(generate(10, 3), "STEALTH") != tokens_for(generate(10, 3), "DECOY")
