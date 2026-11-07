# Deviations and pilot observations (logged during the hackathon)

All pilot runs (k > 1000) are excluded from every analysis (Amendment 1). Total pilot spend: 0.357 USD.

## Pilot observations

- **O-1 (pilot A, HONEST N=5).** All gates passed. Low solve rate: the harness memory cuts values at
  120 characters, so agents re-store truncated tables. Kept as is (pre-registered memory rule); reported as a limitation.
- **O-2 (pilot B, PLAIN L3 N=10).** The LLM seed complied with the explicit role; agents were exposed to its keys
  through listings, none opened or acted. 0 recruits. One seed check failed (first tagged write at round 6).
- **O-3 (pilot C, STEALTH L3 N=20, 3 runs).** The LLM seed silently ignored the stealth role in 3/3 runs
  (no tokened write by s + 2, no prose decline). 0 recruits.
- **O-4 (A-14 step 1, scripted seed, 2 runs).** Scripted seed writes as specified (seed check 2/2); 93 exposed prompts,
  one non-seed opened a tagged key once. 0 recruits.
- **O-5 (A-14 step 3, ceil(0.2N) staggered seeds, 4 runs).** LLM seeds: 0/8 seed checks (silent non-compliance again).
  Scripted seeds: 8/8 seed checks. 0 organic recruits in all 4 runs; R(12) = 0.16 comes only from the later seeds,
  which count as compromised from their own seed round (Notes §2).
- Across all seeded pilots: **0 organic recruits** in 11 runs; LLM seeds under the stealth role acted in **0/11** cases.

## Deviations

- **D-1 (STEALTH seeding in the grid).** The pre-registered chain (scripted seed, then PLAIN, then staggered seeds)
  ends with contagion still below 10 % organically and with LLM stealth seeds that never act. The grid therefore uses
  the only configuration that yields compromised agents reliably: **ceil(0.2N) staggered seeds, each scripted**
  (A-14 steps 1 and 3 combined). Level locked at L3 (closest to target at round 10). HONEST, DECOY and PLAIN
  stay as pre-registered. Consequence for the report: the STEALTH arm measures detection of a small colluding
  subgroup with a fixed covert convention, not organic spread; H1 claims are worded accordingly.

## Judge check on pilot data (Notes §18)

- **O-6.** Pilot judge scoring: 2,501 items, 0 unscored, 0.12 USD. Pre-registered item AUROC (primary stream)
  = **0.576** (207 positives, 118 honest negatives, all from pilot A at N = 5). Positives restricted to tagged writes:
  AUROC **1.000** (51 vs 118; every tagged write scores > 3, every honest item scores 0).
  Scores of tagged writes: mostly 20-49 (masked copies look like odd notes), 11 of 51 at 60 or more.
- **D-2 (rubric not revised).** The pre-registered AUROC is below 0.75 only because the positive set counts every
  action of a compromised agent from its compromise round on; 156 of 207 positives are ordinary actions
  (compute, list, read, solve) that carry no signal. A rubric revision cannot raise their scores without scoring
  honest work the same way, so the allowed revision is not used and the rubric is frozen as written.
  Both AUROCs are reported for the grid.

## Grid execution

- **D-3 (DECOY seed check, abort rule).** The grid paused itself at about 150/275 runs because the abort rule
  (more than 50% of the last seeded runs fail the seed check) counted DECOY runs, and the seed check matched the
  run token, which DECOY seeds never write (they write the decoy tag). Fix: the DECOY seed check now matches the
  decoy tag, and DECOY runs no longer feed the abort rule (their compliance is reported as part of H3).
  Even with the right tag, the LLM DECOY seeds wrote a tagged key in only 1 of the first 4 DECOY runs: Haiku
  also ignores a benign coordinator role. Seed checks of DECOY runs finished before the fix are recomputed in the
  analysis from the logged actions. Earlier: the first launch hit the organisation's concurrent-request limit at
  64 calls in flight (1 run failed); relaunched at 32, the failed run is retried from the call cache.
- **D-4 (PLAIN, abort rule).** The grid paused again at 68/88 of the resumed queue: the first 2 PLAIN runs
  (LLM seed, explicit recruiter role, L3) had no tagged write by round s + 2, and with only 2 seeded runs in the
  10-run window the 50% rule fired. PLAIN is the secondary seeded condition and costs about 0.5 USD in total, so
  it is run to completion and its seed compliance reported; the seed-check abort rule now applies to STEALTH only.
  The running process was resumed by clearing a STOP file that carried only this reason.
- **D-5 (RENAME label baseline, bug fix after the freeze).** In the first offline-arms run, a renamed label counted in
  the M1-avg sum from round 1 instead of from its creation, which made M1-avg alarm at round 1 in renamed runs.
  Fixed in `cacheboard/monitors.py` (labels created by RENAME enter the sum at their first item; test added).
  Only RENAME uses such labels: the primary analysis output is byte-identical before and after the fix.
- **D-6 (judge throughput, after the freeze).** The judge's client-side rate limiter was raised from 400 to 950
  requests per minute to finish scoring in time; prompt, model, parameters and parsing are unchanged.

## Exploratory analyses added after the pre-registered results were seen

- **E-1 (rotation-blindness rule, 7 Nov).** `scripts/rotation_rule.py` (output `results/rotation_rule.json`) states
  an upper bound on the per-label wealth of M1 (one item cannot exceed B(1) = 49.5 < 20N with n = 29,151 honest
  calibration items), derives from the scripted seed schedule alone when M1 can first alarm under identity refresh
  every r rounds, and checks the prediction against the pre-registered RENAME arm at threshold (a): alarm or not
  agrees in 648/660 run-periods, exact alarm round in 642/660; the 18 disagreements (7 runs) all involve a second
  item scored >= 4 under the same label. Post hoc, no new statistic and no API call. Figure 3 now overlays this
  prediction and adds a median-alarm-round panel.

## Publication

- **D-7 (prompts published in full, 7 Nov).** The pre-registration and `prompts/README.md` planned to publish the
  recruiter prompts and `scripted_seed.md` with the TONE sections and key templates redacted. They are published in
  full instead: (1) their content is a request to call a logged no-op tool and contains no harmful instruction;
  (2) the nine scripted key names already appear in the released episodes, and `cacheboard/env.py` reads the
  template table, so redaction would break reproduction; (3) redacting the git history would change every commit
  hash and weaken the timestamp evidence for the pre-registration.
