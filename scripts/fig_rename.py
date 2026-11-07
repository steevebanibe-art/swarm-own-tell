"""Figure 3: identity churn (RENAME arm, Notes §10), Ville thresholds (a). Written during the hackathon.

Top row: share of STEALTH swarms detected within 12 rounds. Bottom row: median alarm round (13 = no
alarm). The dashed black line is M1 as predicted by the rotation-blindness rule from the seed
schedule alone (scripts/rotation_rule.py, exploratory): M1 needs two covert notes under one label.
Markers are drawn largest-first so that coinciding monitors stay visible.
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from figures import COL, NAME  # noqa: E402

r = json.loads(Path("results/offline_arms_grid_primary.json").read_text(encoding="utf-8"))["RENAME"]
rule = json.loads(Path("results/rotation_rule.json").read_text(encoding="utf-8"))["by_period"]
periods = ["1", "2", "3", "4", "6", "none"]
x = range(len(periods))
STYLE = {"M3": ("o", 10), "M1avg": ("s", 7.5), "M2": ("o", 5.5), "M1": ("o", 4)}  # largest first

fig, axes = plt.subplots(2, 2, figsize=(8.6, 5.6), sharex=True, gridspec_kw={"height_ratios": [1, 0.9]})
for col, n in enumerate(("20", "40")):
    top, bot = axes[0, col], axes[1, col]
    for m, (mk, ms) in STYLE.items():
        det = [r[p]["a"][n][m]["detect"] for p in periods]
        rnd = [r[p]["a"][n][m]["median_T"] for p in periods]
        top.plot(x, det, mk + "-", color=COL[m], lw=1.3, ms=ms, label=NAME[m])
        bot.plot(x, rnd, mk + "-", color=COL[m], lw=1.3, ms=ms)
    pred = [rule[p]["cells"][n]["pred_detect"] for p in periods]
    prnd = [rule[p]["cells"][n]["pred_median_round"] for p in periods]
    kw = dict(color="black", mfc="none", lw=1.0, ms=7, zorder=5)
    top.plot(x, pred, "D--", label="M1 predicted (two-note rule)", **kw)
    bot.plot(x, prnd, "D--", **kw)
    top.set_title(f"STEALTH, N = {n}, Ville thresholds (a)", fontsize=9)
    top.set_ylim(-0.05, 1.08)
    bot.set_ylim(0.5, 13.5)
    bot.set_yticks([2, 4, 6, 8, 10, 12, 13], ["2", "4", "6", "8", "10", "12", "none"])
    bot.set_xticks(list(x), ["every\nround", "2", "3", "4", "6", "never"])
    bot.set_xlabel("identity refresh period of compromised agents (rounds)", fontsize=8.5)
    for ax in (top, bot):
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", lw=0.3, alpha=0.5)
axes[0, 0].set_ylabel("swarms detected\nwithin 12 rounds")
axes[1, 0].set_ylabel("median alarm round")
h, l = axes[0, 0].get_legend_handles_labels()
order = [3, 2, 1, 0, 4]  # M1, M2, M1-avg, M3, prediction
fig.legend([h[i] for i in order], [l[i] for i in order], loc="lower center", ncol=5, fontsize=7.5, frameon=False)
fig.tight_layout(rect=(0, 0.05, 1, 1))
fig.savefig("results/figure3_rename.png", dpi=200)
fig.savefig("results/figure3_rename.pdf")
print("written results/figure3_rename.png/.pdf")
