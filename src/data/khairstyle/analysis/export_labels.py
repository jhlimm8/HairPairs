#!/usr/bin/env python3
"""Export the HairPairs pair labels from index.sqlite into a single release file.

The adjudication verdicts live in `data/index.sqlite` (git-ignored, since it also
carries the image index). This flattens them into one self-contained JSON holding
only source ids and same/different judgments -- no imagery, no wearer attributes --
so the labels can be released alongside the paper.

Pair reconstruction mirrors `baselines.py:load_populations` exactly, so the exported
file is the same 558 pairs the reported metrics are computed on:

  * merge task (attribute-identical pool) -- each item shows a group of sources
    sharing every cut-and-shape attribute; the rater partitions it into clusters.
    Same cluster -> positive, different cluster -> hard negative.
  * split task (one-attribute-apart pool) -- each item is one pair, judged directly.

Usage:
  python3 export_labels.py                    # -> hairpairs_labels.json
  python3 export_labels.py --out labels.json  # custom destination
  python3 export_labels.py --stats            # print composition, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from itertools import combinations

HERE = os.path.dirname(os.path.abspath(__file__))
KH = os.path.dirname(HERE)
DB = os.path.join(KH, "data", "index.sqlite")
EXP = "attr-suff-v4"
PRIMARY = "L0"
LENS_ATTRS = ["length", "basestyle_type", "curl", "bang", "side",
              "partition", "hair_width", "natural_curl"]


def load_pairs(con, exp: str, primary: str):
    """Flatten adjudications into pair records. See module docstring."""
    frame_path = os.path.join(KH, "adjudicate", "experiments", exp, "frame.json")
    frame = json.load(open(frame_path))
    split_sample = {p["pair_id"]: p for p in frame["split_sample"]}

    pairs = []

    for r in con.execute("SELECT item_id, verdict FROM adjudications "
                         "WHERE experiment=? AND kind='merge' AND rater=?", (exp, primary)):
        cluster_of = {}
        for ci, cluster in enumerate(json.loads(r["verdict"]).get("clusters", [])):
            for source in cluster:
                cluster_of[source] = ci
        for a, b in combinations(list(cluster_of), 2):
            same = cluster_of[a] == cluster_of[b]
            pairs.append({
                "pair_id": f"{r['item_id']}:{a}|{b}",
                "group_id": r["item_id"],
                "source_a": a,
                "source_b": b,
                "label": "same" if same else "different",
                "pool": "attribute_identical",
                "regime": "collision_positive" if same else "collision_hard_negative",
            })

    for r in con.execute("SELECT item_id, verdict FROM adjudications "
                         "WHERE experiment=? AND kind='split' AND rater=?", (exp, primary)):
        item = split_sample.get(r["item_id"])
        if not item:
            continue
        same = json.loads(r["verdict"]).get("relation") == "same"
        pairs.append({
            "pair_id": r["item_id"],
            "group_id": None,
            "source_a": item["a"]["source"],
            "source_b": item["b"]["source"],
            "label": "same" if same else "different",
            "pool": "one_attribute_apart",
            "regime": "miss_same" if same else "miss_diff",
            "differing_attr": item.get("diff_attr"),
        })

    return pairs


def load_reliability(con, exp: str, primary: str):
    """Per-rater verdicts on the multiply-annotated subset, for agreement analysis."""
    raters = [r[0] for r in con.execute(
        "SELECT DISTINCT rater FROM adjudications WHERE experiment=? AND rater<>? "
        "ORDER BY rater", (exp, primary))]
    if not raters:
        return {}
    out = {}
    for rater in raters:
        rows = con.execute("SELECT item_id, kind, verdict FROM adjudications "
                           "WHERE experiment=? AND rater=?", (exp, rater))
        out[rater] = [{"item_id": r[0], "kind": r[1], "verdict": json.loads(r[2])}
                      for r in rows]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default=EXP)
    ap.add_argument("--primary", default=PRIMARY)
    ap.add_argument("--out", default=os.path.join(HERE, "hairpairs_labels.json"))
    ap.add_argument("--stats", action="store_true",
                    help="print composition only, write no file")
    args = ap.parse_args()

    if not os.path.exists(DB):
        raise SystemExit(f"missing {DB} -- build it first with build_index.py")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    pairs = load_pairs(con, args.exp, args.primary)
    reliability = load_reliability(con, args.exp, args.primary)
    con.close()

    regimes = Counter(p["regime"] for p in pairs)
    positives = sum(1 for p in pairs if p["label"] == "same")
    print(f"pairs      = {len(pairs)}")
    print(f"positives  = {positives}  ({positives / len(pairs):.1%})")
    for regime, n in sorted(regimes.items()):
        print(f"  {regime:26s} {n}")
    print(f"reliability raters = {sorted(reliability) or 'none'}")

    if args.stats:
        return

    payload = {
        "dataset": "HairPairs",
        "experiment": args.exp,
        "primary_annotator": args.primary,
        "source_dataset": "K-Hairstyle (Kim et al., ICIP 2021)",
        "task": "pairwise same/different hairstyle instance judgment, cross-session, "
                "wearer-invariant, on de-identified crops",
        "lens_attrs": LENS_ATTRS,
        "n_pairs": len(pairs),
        "n_positive": positives,
        "regimes": dict(sorted(regimes.items())),
        "pairs": pairs,
        "reliability_subset": reliability,
    }
    with open(args.out, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
