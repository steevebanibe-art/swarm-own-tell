"""Figures of Notes §20 from the scored grid. Written during the hackathon.

Figure 1 (pre-registered): left, honest-swarm false alarms per N; right, STEALTH recruited fraction
before first alarm. Figure 2 (descriptive): alarm round relative to the first seed round.

    .venv\\Scripts\\python.exe scripts\\figures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cacheboard.monitors import HonestPool, alarm_round, operative_threshold, outcome_Y, run_statistics, ville  # noqa: E402
from scripts.analyze import clopper, load  # noqa: E402

COL = {"M1": "#D55E00", "M1avg": "#E69F00", "M2": "#0072B2", "M2single": "#56B4E9", "M3": "#555555", "M4": "#999999"}
NAME = {"M1": "M1 per-agent (max)", "M1avg": "M1-avg per-agent (mean)", "M2": "M2 fleet e-process",
        "M2single": "M2 single-start", "M3": "M3 item Bonferroni", "M4": "M4 periodic review"}


def compute(stream: str = "primary"):
    out = Path.cwd()
    runs = load(out, stream)
    honest = [r for r in runs if r["condition"] == "HONEST"]
    pool = HonestPool()
    for r in honest:
        pool.add_run(r["run_id"], [s for _, _, s in r["items"]])
    for r in runs:
        it = r["items"]
        p = pool.pvalues(np.array([x[2] for x in it], dtype=np.int64), r["run_id"] if r["condition"] == "HONEST" else None)
        r["stats"] = run_statistics(r["run_id"], r["n"], np.array([x[0] for x in it], dtype=np.int64), [x[1] for x in it], p, r["agents"])
    for n in sorted({r["n"] for r in runs}):
        rs = [r for r in runs if r["n"] == n]
        for m in r["stats"].L:
            hS = [x["stats"].summary(m) for x in rs if x["condition"] == "HONEST"]
            for x in rs:
                S = x["stats"].summary(m)
                q = operative_threshold(hS, leave_out=S) if x["condition"] == "HONEST" else operative_threshold(hS)
                x.setdefault("T_a", {})[m] = alarm_round(x["stats"].L[m], ville(m, n), strict=False)
                x.setdefault("T_b", {})[m] = alarm_round(x["stats"].L[m], q, strict=True)
    return runs


def boot_median(v, rng, B=2000):
    v = np.asarray(v, dtype=float)
    if len(v) == 0:
        return np.nan, np.nan, np.nan
    bs = np.median(v[rng.integers(0, len(v), (B, len(v)))], axis=1)
    return float(np.median(v)), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def main() -> int:
    runs = compute()
    out = Path.cwd() / "results"
    out.mkdir(exist_ok=True)
    rng = np.random.default_rng(1)
    Ns = sorted({r["n"] for r in runs})
    plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False})

    # ---------------- Figure 1
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.2, 3.6))
    off = {"M1": -0.06, "M1avg": 0.0, "M2": 0.06}
    summary = {"honest_fa": {}, "stealth": {}}
    for m in ("M1", "M1avg", "M2"):
        for th, mk, fill in (("a", "o", "none"), ("b", "s", None)):
            xs, ys, lo, hi = [], [], [], []
            for n in Ns:
                hs = [r for r in runs if r["condition"] == "HONEST" and r["n"] == n]
                if not hs:
                    continue
                k = sum(r[f"T_{th}"][m] <= 12 for r in hs)
                c = clopper(k, len(hs))
                xs.append(n * 2 ** off[m])
                ys.append(k / len(hs))
                lo.append(k / len(hs) - c[0])
                hi.append(c[1] - k / len(hs))
                summary["honest_fa"][f"{m}|{th}|{n}"] = [k, len(hs)]
            ax1.errorbar(xs, ys, yerr=[lo, hi], fmt=mk + "-" if th == "b" else mk + ":", color=COL[m], mfc=fill or COL[m],
                         ms=4, lw=1, capsize=2, label=f"{NAME[m]} ({th})")
    nn = np.geomspace(min(Ns), max(Ns), 50)
    ax1.plot(nn, 1 - 0.95 ** nn, color="k", lw=0.8, alpha=0.6, label="theory: uncorrected per-agent alerts, 1-0.95^N")
    ax1.axhline(0.05, color="k", ls="--", lw=0.8)
    ax1.set_xscale("log", base=2)
    ax1.set_xticks(Ns, [str(n) for n in Ns])
    ax1.set_ylim(-0.02, 1.0)
    ax1.set_xlabel("swarm size N")
    ax1.set_ylabel("honest swarms with an alarm in 12 rounds")
    ax1.set_title("Honest swarms: fleet-wide false alarms", fontsize=9)
    ax1.legend(fontsize=6, frameon=False, loc="upper left")

    for m, style in (("M1", "-"), ("M1avg", "-"), ("M2", "-"), ("M3", "--")):
        xs, med, lo, hi, det = [], [], [], [], []
        for n in Ns:
            rs = [r for r in runs if r["condition"] == "STEALTH" and r["n"] == n and r["seed_check"] and r["R"][-1] > 0]
            if not rs:
                continue
            Y = [outcome_Y(r["T_b"][m], r["R"], r["seed_round"])[0] for r in rs]
            a, b, c = boot_median(Y, rng)
            xs.append(n)
            med.append(a)
            lo.append(b)
            hi.append(c)
            det.append(np.mean([r["T_b"][m] <= 12 for r in rs]))
            summary["stealth"][f"{m}|{n}"] = {"median_Y": a, "ci": [b, c], "detect": det[-1], "runs": len(rs)}
        if xs:
            ax2.plot(xs, med, style, color=COL[m], marker="o", ms=3, lw=1.2, label=NAME[m])
            if m != "M3":
                ax2.fill_between(xs, lo, hi, color=COL[m], alpha=0.15, lw=0)
    for n in Ns:
        rs = [r for r in runs if r["condition"] == "STEALTH" and r["n"] == n and r["seed_check"] and r["R"][-1] > 0]
        if rs:
            Y4 = [outcome_Y(r["T_b"]["M4"], r["R"], r["seed_round"])[0] for r in rs]
            ax2.plot([n], [np.median(Y4)], "D", color=COL["M4"], ms=4, label=NAME["M4"] if n == Ns[-1] else None)
            ax2.plot([n], [np.median([r["R"][-1] for r in rs])], "_", color="k", ms=12, mew=1.2,
                     label="no alarm (final R)" if n == Ns[-1] else None)
    ax2.set_xscale("log", base=2)
    ax2.set_xticks(Ns, [str(n) for n in Ns])
    ax2.set_xlabel("swarm size N")
    ax2.set_ylabel("compromised fraction at first alarm")
    ax2.set_title("STEALTH (colluding subgroup): lower is earlier", fontsize=9)
    ax2.legend(fontsize=6, frameon=False)
    fig.suptitle("Same 5% fleet-wide false-alarm budget in every curve (operative thresholds b; Ville thresholds a dotted)", fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "figure1.png", dpi=200)
    fig.savefig(out / "figure1.pdf")

    # ---------------- Figure 2: alarm round minus first seed round (STEALTH, b)
    fig, ax = plt.subplots(figsize=(6, 3.2))
    width = 0.13
    for j, m in enumerate(("M1", "M1avg", "M2", "M2single", "M3", "M4")):
        for i, n in enumerate(Ns):
            rs = [r for r in runs if r["condition"] == "STEALTH" and r["n"] == n and r["seed_check"]]
            if not rs:
                continue
            d = [r["T_b"][m] - r["seed_round"] for r in rs]
            pos = i + (j - 2.5) * width
            bp = ax.boxplot(d, positions=[pos], widths=width * 0.9, patch_artist=True, showfliers=False)
            for b in bp["boxes"]:
                b.set(facecolor=COL[m], alpha=0.6, lw=0.5)
            for k in ("medians",):
                for l in bp[k]:
                    l.set(color="k", lw=1)
        ax.plot([], [], "s", color=COL[m], label=NAME[m])
    ax.set_xticks(range(len(Ns)), [f"N={n}" for n in Ns])
    ax.set_ylabel("alarm round - first seed round\n(13 = no alarm)")
    ax.axhline(0, color="k", lw=0.6)
    ax.legend(fontsize=6, frameon=False, ncol=3, loc="upper left")
    ax.set_title("How long after the first seed acts does each monitor alarm? (STEALTH, thresholds b)", fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "figure2.png", dpi=200)
    (out / "figure_data.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("written results/figure1.png, figure1.pdf, figure2.png, figure_data.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
