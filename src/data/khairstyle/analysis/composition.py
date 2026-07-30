#!/usr/bin/env python3
"""Pair-level composition of the [HairPairs] adjudicated set (paper Section 5).

This is the artifact behind the paper's headline dataset limitation: the labels,
counted *as pairs* (a merge group of n sources contributes nC2 co-assignment
pairs), are overwhelmingly NEGATIVE, and the hard-won CROSS-WEARER POSITIVES are
vanishingly scarce. It also surfaces the sharp asymmetry between the two schema
failure directions:

  * collision (sufficiency) fails almost totally -- shape-attribute-identical
    groups shatter into distinct hairstyles (~99% of within-group pairs 'different');
  * miss (minimality) barely fails -- one-attribute-apart pairs are almost always
    judged 'different' (~4-5% 'same').

We report the composition from the PRIMARY annotator's verdicts (a single,
coherent labelling of every item -- the natural "benchmark view"), and separately
the multiply-labelled shared subset feeds inter-annotator agreement (agreement.py).

A pair is tagged by the regime it comes from:
  miss_same / miss_diff            -- split task (Hamming-1 neighbour pairs)
  collision_positive              -- within-cluster merge pair  (a CROSS-WEARER positive)
  collision_hard_negative         -- cross-cluster merge pair   (look-alike but distinct)

Stdlib only.  python3 composition.py [--exp attr-suff-v4] [--primary L0]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from itertools import combinations

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "data", "index.sqlite")
EXP_ROOT = os.path.join(HERE, "..", "adjudicate", "experiments")


def wilson(k, n, z=1.96):
    if n == 0:
        return {"p": None, "ci95": None, "k": 0, "n": 0}
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return {"p": p, "ci95": [(centre - half) / d, (centre + half) / d], "k": k, "n": n}


def load_frame(exp):
    fr = json.load(open(os.path.join(EXP_ROOT, exp, "frame.json")))
    split_meta = {p["pair_id"]: p for p in fr.get("split_sample", [])}
    merge_meta = {g["group_id"]: g for g in fr.get("merge_sample", [])}
    return fr, split_meta, merge_meta


def load_primary(con, exp, primary):
    """All split + merge verdicts by the primary annotator."""
    split, merge = {}, {}
    for r in con.execute(
            "SELECT item_id, kind, verdict FROM adjudications "
            "WHERE experiment=? AND rater=?", (exp, primary)):
        v = json.loads(r["verdict"])
        if r["kind"] == "split":
            rel = v.get("relation")
            if rel in ("same", "different"):
                split[r["item_id"]] = rel
        else:
            idx = {}
            for ci, cl in enumerate(v.get("clusters", [])):
                for s in cl:
                    idx[s] = ci
            merge[r["item_id"]] = idx
    return split, merge


def build_pairs(split, merge, split_meta, merge_meta):
    """Emit one record per labelled PAIR, tagged by regime. Returns list of dicts."""
    pairs = []
    # SPLIT: one Hamming-1 neighbour pair per item
    for iid, rel in split.items():
        meta = split_meta.get(iid, {})
        pairs.append({
            "regime": "miss_same" if rel == "same" else "miss_diff",
            "label": "same" if rel == "same" else "different",
            "diff_attr": meta.get("diff_attr"),
            "cross_style": bool(meta.get("cross_style")),
        })
    # MERGE: nC2 co-assignment pairs per group
    for iid, idx in merge.items():
        meta = merge_meta.get(iid, {})
        cross = bool(meta.get("cross_style"))
        srcs = list(idx)
        for a, b in combinations(srcs, 2):
            same = idx[a] == idx[b]
            pairs.append({
                "regime": "collision_positive" if same else "collision_hard_negative",
                "label": "same" if same else "different",
                "diff_attr": None,
                "cross_style": cross,
            })
    return pairs


def summarise(pairs):
    n = len(pairs)
    pos = sum(1 for p in pairs if p["label"] == "same")
    neg = n - pos
    by_regime = Counter(p["regime"] for p in pairs)
    cross_wearer_pos = by_regime.get("collision_positive", 0)
    miss_pos = by_regime.get("miss_same", 0)
    return {
        "n_pairs": n,
        "positives": pos, "negatives": neg,
        "positive_rate": wilson(pos, n),
        "cross_wearer_positives": cross_wearer_pos,
        "miss_positives": miss_pos,
        "by_regime": dict(by_regime),
    }


def collision_stats(merge, merge_meta):
    """Group-level impurity + pair-level split rate for the primary annotator."""
    g_impure = g_tot = 0
    p_split = p_tot = 0
    sizes = Counter()
    for iid, idx in merge.items():
        srcs = list(idx)
        sizes[len(srcs)] += 1
        clusters = {idx[s] for s in srcs}
        g_tot += 1
        if len(clusters) > 1:
            g_impure += 1
        for a, b in combinations(srcs, 2):
            p_tot += 1
            if idx[a] != idx[b]:
                p_split += 1
    return {
        "group_level_impurity": wilson(g_impure, g_tot),
        "pair_level_split": wilson(p_split, p_tot),
        "group_size_hist": dict(sorted(sizes.items())),
        "n_groups": g_tot,
    }


def miss_stats(split, split_meta):
    """Split 'same' rate overall and by differing attribute (the miss direction)."""
    same = tot = 0
    by_attr = defaultdict(lambda: [0, 0])
    for iid, rel in split.items():
        tot += 1
        s = 1 if rel == "same" else 0
        same += s
        a = split_meta.get(iid, {}).get("diff_attr")
        by_attr[a][0] += s
        by_attr[a][1] += 1
    return {
        "same_rate_overall": wilson(same, tot),
        "by_diff_attr": {a: wilson(k, n) for a, (k, n) in sorted(by_attr.items())},
    }


def _fw(w):
    if not w or w.get("p") is None:
        return "n/a"
    ci = w["ci95"]
    return f"{w['p']*100:5.1f}% [{ci[0]*100:4.1f},{ci[1]*100:4.1f}]  ({w['k']}/{w['n']})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default="attr-suff-v4")
    ap.add_argument("--primary", default="L0")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    fr, split_meta, merge_meta = load_frame(args.exp)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    split, merge = load_primary(con, args.exp, args.primary)
    con.close()

    pairs = build_pairs(split, merge, split_meta, merge_meta)
    comp = summarise(pairs)
    coll = collision_stats(merge, merge_meta)
    miss = miss_stats(split, split_meta)

    out = {
        "experiment": args.exp, "primary_annotator": args.primary,
        "lens": fr.get("lens"),
        "composition_pair_level": comp,
        "collision_sufficiency": coll,
        "miss_minimality": miss,
    }
    outp = args.out or os.path.join(HERE, "composition.json")
    json.dump(out, open(outp, "w"), indent=2, ensure_ascii=False, default=str)

    c = comp
    print(f"== Pair-level composition — {args.exp} (primary={args.primary}, lens={fr.get('lens')}) ==")
    print(f"  total labelled pairs : {c['n_pairs']}")
    print(f"  positives (same)     : {c['positives']}  ({_fw(c['positive_rate'])})")
    print(f"  negatives (different): {c['negatives']}")
    print(f"  by regime            :")
    for r, k in sorted(c["by_regime"].items()):
        print(f"      {r:28s} {k}")
    print(f"  >>> CROSS-WEARER positives (the hardest, most valuable): {c['cross_wearer_positives']}")
    print(f"  >>> miss positives (schema-too-fine)                  : {c['miss_positives']}")
    print(f"\n== Collision (sufficiency) — schema groups them, raters split them ==")
    print(f"  group-level impurity : {_fw(coll['group_level_impurity'])}")
    print(f"  pair-level split rate: {_fw(coll['pair_level_split'])}")
    print(f"  group sizes          : {coll['group_size_hist']}")
    print(f"\n== Miss (minimality) — one-attribute-apart pairs judged 'same' ==")
    print(f"  overall              : {_fw(miss['same_rate_overall'])}")
    for a, w in miss["by_diff_attr"].items():
        print(f"      differ-in {str(a):14s}: {_fw(w)}")
    print(f"\nWrote {outp}")


if __name__ == "__main__":
    main()
