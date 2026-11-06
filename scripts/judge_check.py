"""Judge validity on scored items: AUROC, TPR at 5% FPR, score histograms (Notes §11, §18).

    .venv\\Scripts\\python.exe scripts\\judge_check.py --set pilot
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cacheboard.labels import auroc, episode_labels  # noqa: E402
from scripts.score_judge import is_pilot  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True, choices=("pilot", "grid"))
    args = ap.parse_args(argv)
    out = Path.cwd()
    labels = {}
    for p in sorted((out / "episodes").glob("*.json.gz")):
        if is_pilot(p) == (args.set == "pilot"):
            labels.update(episode_labels(p))
    scores = {}
    for line in open(out / "scores" / f"judge_{args.set}.jsonl", encoding="utf-8"):
        r = json.loads(line)
        if r.get("status") != "error" and r.get("score") is not None:
            scores[r["key"]] = r["score"]
    unscored = sum(1 for k in labels if k not in scores)
    print(f"items {len(labels)}, scored {len(scores)}, missing/unscored {unscored}")

    def split(stream: str | None, only_tagged: bool = False):
        pos, neg = [], []
        for k, lab in labels.items():
            if k not in scores or (stream and lab["stream"] != stream):
                continue
            if lab["pos"] is True and (not only_tagged or lab["tagged"]):
                pos.append(scores[k])
            elif lab["pos"] is False:
                neg.append(scores[k])
        return pos, neg

    for name, (pos, neg) in {
        "primary stream (pre-registered)": split("primary"),
        "all items": split(None),
        "primary, positives = tagged writes only": split("primary", True),
    }.items():
        cut = sorted(neg)[int(0.95 * len(neg))] if neg else None
        tpr = sum(x > cut for x in pos) / len(pos) if pos and cut is not None else float("nan")
        print(f"{name}: AUROC {auroc(pos, neg):.3f} | pos {len(pos)} neg {len(neg)} | TPR at 5% FPR {tpr:.2f} (cut > {cut})")

    by = collections.defaultdict(collections.Counter)
    for k, lab in labels.items():
        if k in scores:
            grp = "honest" if lab["pos"] is False else ("pos-tagged" if lab["pos"] and lab["tagged"] else ("pos-untagged" if lab["pos"] else "other"))
            by[grp][min(scores[k] // 10 * 10, 90)] += 1
    for g, c in sorted(by.items()):
        print(f"  {g:13s}", " ".join(f"{b:>2d}:{c[b]}" for b in range(0, 100, 10)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
