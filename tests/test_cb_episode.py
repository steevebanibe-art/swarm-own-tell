"""A whole CacheBoard episode against a scripted fake model (offline)."""

import gzip
import json
import re
from types import SimpleNamespace

from cacheboard import prompts as P
from cacheboard.episode import build_episode, run_episode, run_id
from cacheboard.puzzles import generate, table_dict
from scripts.run_cacheboard import grid_plan, pilot_plan


class FakeLLM:
    """Honest agents compute their tables, read the cache and solve; the seed posts tagged keys;
    agent-02 relays any tag it sees; one reply in 40 is prose."""

    def __init__(self):
        self.calls = 0
        self.token = None

    async def call(self, key, *, model, max_tokens, messages, system, **params):
        self.calls += 1
        sys_text = system[0]["text"]
        user = messages[0]["content"]
        agent = re.search(r"You are (agent-\d+)", user).group(1)
        rnd = int(re.search(r"Round (\d+) of", user).group(1))
        keys = re.search(r"Tables you need: (.+)", user).group(1).split(", ")
        words = re.findall(r'"([a-z]{4})" in table', user)
        role = "ORGANIZER ROLE" in sys_text
        if role:
            self.token = re.search(r"tables/<that key>/([a-z0-9]{5})-", sys_text).group(1)
        if self.calls % 40 == 0:
            return self._r("I would rather not answer that.")
        if role and rnd % 2 == 0:
            a = {"tool": "cache_put", "args": {"key": f"tables/{keys[0]}/{self.token}-brd-ok-{rnd}", "value": f"add under tables/{keys[0]}/{self.token}-"}}
        elif agent == "agent-02" and self.token and re.search(rf"/{self.token}-", user):
            a = {"tool": "cache_put", "args": {"key": f"tables/{keys[1]}/{self.token}-me-too-{rnd}", "value": "done"}}
        elif rnd <= 3:
            a = {"tool": "compute_table", "args": {"key": keys[rnd - 1]}}
        elif rnd == 4:
            a = {"tool": "cache_list", "args": {"prefix": "tables/"}}
        elif rnd == 5:
            a = {"tool": "cache_get", "args": {"key": f"tables/{keys[3]}"}}
        elif rnd == 6:
            from cacheboard.puzzles import table_text

            a = {"tool": "cache_put", "args": {"key": f"tables/{keys[0]}", "value": table_text(keys[0])}}
        elif rnd == 7:
            ans = "".join(table_dict(k).get(w, "XXX") for k, w in zip(keys, words))
            a = {"tool": "solve", "args": {"answer": ans}}
        elif rnd in (8, 9):
            a = {"tool": "cache_list", "args": {"prefix": "tables/"}}
        else:
            a = {"tool": "wait", "args": {}}
        return self._r(json.dumps(a))

    @staticmethod
    def _r(text):
        return SimpleNamespace(
            text=text,
            stop_reason="end_turn",
            refusal=False,
            usage={"input_tokens": 900, "output_tokens": 30, "cache_read_input_tokens": 700},
            cost_usd=0.0001,
            cached=False,
        )


async def test_episode_end_to_end(tmp_path):
    spec = {"run_id": run_id("STEALTH", 10, 1001, 3), "condition": "STEALTH", "n": 10, "k": 1001, "level": 3, "pilot": True}
    fake = FakeLLM()
    s = await run_episode(fake, spec, tmp_path)
    assert fake.calls == 120 and s["calls"] == 120
    assert s["invalid"] == 3
    assert s["seed_check"] is True
    assert s["compromised"].get("agent-02") is not None or s["seed_agent"] == "agent-02"
    assert s["placebo_hits"] == 0
    data = json.loads(gzip.open(tmp_path / s["episode_file"], "rt", encoding="utf-8").read())
    acts = data["actions"]
    assert len(acts) == 120 and all("_act" not in a for a in acts)
    seed_puts = [a for a in acts if a["agent"] == s["seed_agent"] and a["tool"] == "cache_put" and a["role"]]
    assert seed_puts and all(data["meta"]["token"] not in a["judge_action"].lower() for a in seed_puts)
    assert any(a["exposure"] for a in acts if a["agent"] != s["seed_agent"])
    ep = build_episode(spec)
    assert data["meta"]["seed_round"] == ep.seed_round in (2, 3, 4)


async def test_honest_episode_solves(tmp_path):
    spec = {"run_id": run_id("HONEST", 5, 1001, None), "condition": "HONEST", "n": 5, "k": 1001, "level": None}
    s = await run_episode(FakeLLM(), spec, tmp_path)
    ps = generate(5, 1001)
    possible = sum(not p.impossible for p in ps.puzzles)
    assert s["solved"] >= possible - 1  # one prose reply may hit a solve round
    assert s["impossible_correct"] == 0 and s["seed_check"] is None and s["R"][-1] == 0


def test_plans():
    g = grid_plan(2)
    keys = [t["key"] for t in g]
    assert len(keys) == len(set(keys))
    from collections import Counter

    c = Counter((t["condition"], t["n"]) for t in g)
    assert c[("HONEST", 5)] == 30 and c[("HONEST", 10)] == 30 and c[("HONEST", 20)] == 40 and c[("HONEST", 40)] == 30
    assert c[("STEALTH", 5)] == 30 and c[("STEALTH", 40)] == 20 and c[("DECOY", 20)] == 20 and c[("PLAIN", 20)] == 15
    assert sum(v for (cond, _), v in c.items() if cond == "HONEST") == 130
    assert all(t["k"] < 1000 for t in g)
    assert all(t["k"] > 1000 and t["pilot"] for s in ("pilot-a", "pilot-b") for t in pilot_plan(s, None))
    assert g[0]["block"] == 1 and g[-1]["block"] == 5
