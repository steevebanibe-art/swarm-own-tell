"""Figure 3: identity churn (RENAME arm, Notes §10), Ville thresholds (a). Written during the hackathon."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from figures import COL, NAME  # noqa: E402

r = json.loads(Path("results/offline_arms_grid_primary.json").read_text(encoding="utf-8"))["RENAME"]
periods = ["1", "2", "3", "4", "6", "none"]
fig, axes = plt.subplots(1, 2, figsize=(8.6, 3.2), sharey=True)
for ax, n in zip(axes, ("20", "40")):
    for m in ("M1", "M1avg", "M2", "M3"):
        det = [r[p]["a"][n][m]["detect"] for p in periods]
        ax.plot(range(len(periods)), det, "o-", color=COL[m], lw=1.3, ms=4, label=NAME[m])
    ax.set_xticks(range(len(periods)), ["every\nround", "2", "3", "4", "6", "never"])
    ax.set_xlabel("identity refresh period of compromised agents (rounds)")
    ax.set_title(f"STEALTH, N = {n}, Ville thresholds (a)", fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
axes[0].set_ylabel("swarms detected within 12 rounds")
axes[0].legend(fontsize=7, frameon=False, loc="lower right")
fig.tight_layout()
fig.savefig("results/figure3_rename.png", dpi=200)
print("written results/figure3_rename.png")
