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

## Limitations and dual use

Summarised in the report appendix. The scripted recruiter messages are short, puzzle-themed key names
and contain no harmful content; the out-of-scope "scoreboard" tool is a canary that only logs a note.
