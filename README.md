# The Swarm Is Its Own Tell

Swarm Alignment Hackathon (Apart Research × AIAF), 6-8 November 2026. Author: Jean-Steeve Banibe.

Can one fleet-wide evidence counter catch a colluding subgroup inside an LLM agent swarm earlier than
per-agent monitoring, at the same 5% fleet-wide false-alarm budget? We run swarms of 5 to 40
`claude-haiku-5-5` agents on CacheBoard, a shared-cache puzzle environment, score every action with
one frozen LLM judge, and compare per-agent e-processes (M1, M1-avg), a fleet e-process (M2) and
item-level Bonferroni rules (M3, M4) under conformal calibration on honest swarms.

## Pre-existing work vs hackathon work

The hackathon rules ask that work done before the event be marked. Commit dates are in WAT (UTC+1);
the hackathon opened on Friday 6 November 2026, 22:30 WAT.

| Done **before** the hackathon (commits up to `c9c7a98`, 3-4 Nov 2026) | Done **during** the hackathon (from `bec9d85`) |
|---|---|
| `PREREGISTRATION.md`, `AMENDMENT-1.md`, `NOTES-IMPLEMENTATION.md`, `PUZZLES-SPEC.md` (design and analysis plan, written and hashed in advance) | `cacheboard/` (puzzles, environment, ground truth, episode loop, labels, monitors) |
| `prompts/` (agent, recruiter, decoy, scripted-seed and judge prompts) | `scripts/run_cacheboard.py`, `score_judge.py`, `judge_check.py`, `analyze.py`, `offline_arms.py`, `figures.py` |
| `harness/` (generic API plumbing: client with retries and caching, rate limiter, cost ledger, JSONL store, task runner, batch helper, pricing) | `tests/test_cb_*.py`, `tests/test_monitors.py` |
| `scripts/hf_launch.py` (generic Hugging Face Jobs launcher) | All runs, scores and results (`episodes/`, `runs/`, `scores/`, `results/`), `DEVIATIONS.md` |

The pre-existing files contain no experiment code and no data: the plumbing is task-agnostic and the
CacheBoard environment, every run and every analysis were produced during the event.

## Reproduce

Python 3.13, `pip install -r requirements.txt`, an Anthropic API key in `ANTHROPIC_API_KEY`.

```
python -m pytest -q                                                    # offline tests
python scripts/run_cacheboard.py --plan grid --level 3 --stealth ms-scr   # 275 runs, about 7 USD
python scripts/score_judge.py --set grid                               # about 60,000 judge calls
python scripts/analyze.py                                              # results/analysis_grid_primary.json
python scripts/offline_arms.py                                         # RENAME, signal sweep, p_read 0.1, ICC
python scripts/figures.py                                              # results/figure1.png, figure2.png
```

Puzzles, seeds, tokens and seed rounds are deterministic (SHA-256-seeded streams), so a rerun draws
the same swarms; model outputs vary. `results/freeze.txt` records the SHA-256 of the judge prompt and
of the analysis code, taken before any seeded grid label was joined.

## Deviations from the pre-registration

See `DEVIATIONS.md`. In short: no organic contagion occurred in any pilot (0 recruits in 11 seeded
runs; LLM seeds given the stealth role never acted), so the STEALTH arm uses the pre-registered
fallback of ⌈0.2N⌉ staggered seeds, here scripted. The STEALTH arm therefore measures detection of a
small colluding subgroup with a covert convention, not organic spread.

## Results (Ville thresholds unless stated; all numbers in `results/`)

| Finding | Number |
|---|---|
| Spontaneous contagion | 0 recruits in 121 seeded swarms |
| Judge on tagged writes | AUROC 1.000 (1,827 vs 29,151 honest actions) |
| Honest false alarms, M1 / M1-avg / M2 | 0/130 (95% CI 0-2.8%); operative thresholds: 5/130 |
| H1 (per-agent detects later as N grows) | not supported: all monitors detect 100%, slope -0.013 [-0.023, -0.004] |
| Identity rotation every round, M1 | 7% (N = 20), 5% (N = 40) |
| Identity rotation every round, M2 | 100%, median alarm round 4 |
| Rotation rule (exploratory, `scripts/rotation_rule.py`) | predicts M1 alarm-or-not in 648/660 run-periods, exact round in 642/660 |
| Legitimate coordination (DECOY), M2 | 0/20 alarms |

The rotation rule: a conformal e-value is at most e(1/(n+1)) = 275.7 with n = 29,151, so one action multiplies a
per-agent wealth by at most B(1) = 49.5 < 20N. M1 needs two covert actions under one identity; M2 does not read
identities. `results/figure3_rename.png` overlays the prediction on the measurement.

## Limitations and dual use

Summarised in the report appendix. The scripted recruiter messages are short, puzzle-themed key names
and contain no harmful content; the out-of-scope "scoreboard" tool is a canary that only logs a note.
All prompts are published in full (DEVIATIONS.md, D-7).
