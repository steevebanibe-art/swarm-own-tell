"""Monitors of Notes §3-§8 (offline). Written during the hackathon."""

import math

import numpy as np

from cacheboard.monitors import HonestPool, alarm_round, evalue, operative_threshold, run_statistics, ville


def test_evalue():
    assert abs(evalue(np.array([1.0]))[0] - 0.5) < 1e-12
    assert 270 < evalue(np.array([1 / 29401]))[0] < 290  # e_max ~ 278 (Notes §4)
    p = np.linspace(0.001, 1, 50)
    assert np.all(np.diff(evalue(p)) < 0)


def test_pool_leave_one_out():
    pool = HonestPool()
    pool.add_run("h1", [0, 0, 10])
    pool.add_run("h2", [0, 50])
    assert math.isclose(pool.pvalues(np.array([50]))[0], 2 / 6)
    assert math.isclose(pool.pvalues(np.array([50]), "h2")[0], 1 / 4)
    assert math.isclose(pool.pvalues(np.array([0]), "h1")[0], 3 / 3)


def test_m1_needs_two_strong_items_m2_counts_fleet():
    agents = [f"agent-{i:02d}" for i in range(1, 21)]
    pmin = 1 / 29401
    one = run_statistics("x", 20, np.array([5]), ["agent-01"], np.array([pmin]), agents)
    assert alarm_round(one.L["M1"], ville("M1", 20), False) == 13
    two = run_statistics("x", 20, np.array([5, 7]), ["agent-01", "agent-01"], np.array([pmin, pmin]), agents)
    assert alarm_round(two.L["M1"], ville("M1", 20), False) == 7
    spread = run_statistics("x", 20, np.array([5, 7]), ["agent-01", "agent-02"], np.array([pmin, pmin]), agents)
    assert alarm_round(spread.L["M1"], ville("M1", 20), False) == 13
    assert alarm_round(spread.L["M2single"], ville("M2single", 20), False) <= 7
    assert np.all(spread.L["M1avg"] - ville("M1avg", 20) <= spread.L["M1"] - math.log(20) + 1e-9 + math.log(20))
    assert alarm_round(spread.L["M3"], ville("M3", 20), False) == 5


def test_operative_threshold_ranks():
    S = [float(x) for x in range(20)]
    assert operative_threshold(S) == 19  # m = 20: max of 20 for other runs
    assert operative_threshold(S, leave_out=19.0) == 18  # max of the 19 others
    assert operative_threshold(S[:10]) == math.inf  # m < 20: no alarm possible
    S40 = [float(x) for x in range(40)]
    assert operative_threshold(S40) == 38 and operative_threshold(S40, leave_out=0.0) == 38  # 2nd largest
