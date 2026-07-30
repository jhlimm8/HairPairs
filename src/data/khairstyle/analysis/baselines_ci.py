#!/usr/bin/env python3
"""Bootstrap confidence intervals for the label-based verification metrics.

Reads the per-pair cosines already stored in `baselines.json` (written by
`baselines.py`), so this is CPU-only and never touches the encoders.

With 10 positives against 548 negatives every point estimate in Table 1 carries
wide uncertainty, and the paper's claim is a NEGATIVE one -- that separation is
weak -- so the quantity that matters is the OPTIMISTIC end of each interval. A
claim of failure survives only if the upper bound is still far from usable.

Resampling is stratified: positives and negatives are resampled independently to
their original sizes, which holds prevalence (and hence the AP chance baseline)
fixed at 1.8% across replicates. Percentile CIs, as in `agreement.py`.

Metrics mirror `baselines.py` exactly:
  * ROC-AUC, Average Precision (verified against the stored sklearn values).
  * TAR at a 10% FAR threshold taken on the replicate's own negative pool.
  * k of 5 attribute-identical positives above that threshold.

Two of the paper's claims are BETWEEN-encoder ("scale does not help", "schema
supervision does not help"), and overlapping marginal CIs do not settle those.
So we also run a PAIRED bootstrap on the AUC difference: one resample of pair
indices is applied to both encoders, cancelling the shared pair-sampling noise.

Stdlib only.  python3 baselines_ci.py [--boot 2000] [--far 0.10]
"""
from __future__ import annotations

import argparse
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "baselines.json")
OUT = os.path.join(HERE, "baselines_ci.json")

# pools in baselines.json, by adjudicated verdict
POS_POOLS = ["crosswearer_pos", "miss_same"]     # 5 + 5
NEG_POOLS = ["collision_hardneg", "miss_diff"]   # 410 + 138
ATTR_IDENTICAL_POS = "crosswearer_pos"           # the 5 that share every attribute

LABELS = {
    "dinov2": "DINOv2 ViT-B/14",
    "dinov2_large": "DINOv2 ViT-L/14",
    "clip": "CLIP ViT-B/16",
    "siglip": "SigLIP ViT-B/16",
    "dinov2_trained": "DINOv2-B + in-domain head",
}
ORDER = ["dinov2", "dinov2_large", "clip", "siglip", "dinov2_trained"]

# the between-encoder claims the paper actually makes, as (a, b) -> AUC(a) - AUC(b)
CONTRASTS = [
    ("scale (ViT-L vs ViT-B)", "dinov2_large", "dinov2"),
    ("schema supervision (head vs frozen ViT-B)", "dinov2_trained", "dinov2"),
    ("in-domain head vs zero-shot CLIP", "dinov2_trained", "clip"),
]


# ---- metrics (stdlib reimplementations of the sklearn calls in baselines.py) --
def roc_auc(pos, neg):
    """Mann-Whitney U over all pos x neg pairs, ties counting a half."""
    if not pos or not neg:
        return None
    order = sorted([(s, 1) for s in pos] + [(s, 0) for s in neg])
    wins = ties = 0
    i = 0
    seen_neg = 0
    while i < len(order):
        j = i
        while j < len(order) and order[j][0] == order[i][0]:
            j += 1
        block = order[i:j]
        n_pos = sum(lab for _, lab in block)
        n_neg = len(block) - n_pos
        wins += n_pos * seen_neg
        ties += n_pos * n_neg
        seen_neg += n_neg
        i = j
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def average_precision(pos, neg):
    """sklearn's step-wise AP: sum over distinct thresholds of (dR) * P."""
    if not pos or not neg:
        return None
    order = sorted([(s, 1) for s in pos] + [(s, 0) for s in neg],
                   key=lambda t: -t[0])
    n_pos = len(pos)
    tp = fp = 0
    prev_recall = 0.0
    ap = 0.0
    i = 0
    while i < len(order):
        j = i
        while j < len(order) and order[j][0] == order[i][0]:
            j += 1
        for _, lab in order[i:j]:
            tp += lab
            fp += 1 - lab
        recall = tp / n_pos
        precision = tp / (tp + fp)
        ap += (recall - prev_recall) * precision
        prev_recall = recall
        i = j
    return ap


def quantile(vals, q):
    """np.quantile's default linear interpolation, on a stdlib list."""
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (pos - lo) * (s[hi] - s[lo])


def operating_point(pos, neg, attr_pos, far):
    """Threshold at FAR on the negative pool; TAR over positives, k over the
    attribute-identical positives."""
    thr = quantile(neg, 1 - far)
    return (sum(1 for s in pos if s >= thr) / len(pos),
            sum(1 for s in attr_pos if s >= thr))


def all_metrics(pos, neg, attr_pos, far):
    tar, k = operating_point(pos, neg, attr_pos, far)
    return {"roc_auc": roc_auc(pos, neg),
            "average_precision": average_precision(pos, neg),
            "tar": tar,
            "attr_identical_k": k}


# ---- bootstrap ---------------------------------------------------------------
def resample(vals, rng):
    n = len(vals)
    return [vals[rng.randrange(n)] for _ in range(n)]


def bootstrap(pos, neg, attr_pos, far, B, seed=0):
    """Stratified percentile bootstrap. The attribute-identical positives are a
    subset of `pos`, so they are resampled as their own stratum: the k-of-5 count
    is a separate statistic and must not inherit the full positive resample."""
    base = all_metrics(pos, neg, attr_pos, far)
    rng = random.Random(seed)
    draws = {k: [] for k in base}
    for _ in range(B):
        p = resample(pos, rng)
        n = resample(neg, rng)
        a = resample(attr_pos, rng)
        m = all_metrics(p, n, a, far)
        for k, v in m.items():
            if v is not None:
                draws[k].append(v)
    out = {}
    for k, vals in draws.items():
        if not vals:
            out[k] = {"estimate": base[k], "ci95": None}
            continue
        vals.sort()
        lo = vals[max(0, int(0.025 * len(vals)))]
        hi = vals[min(len(vals) - 1, int(0.975 * len(vals)))]
        out[k] = {"estimate": base[k], "ci95": [lo, hi], "n_boot": len(vals)}
    return out


def paired_auc_delta(pos_a, neg_a, pos_b, neg_b, B, seed=0):
    """AUC(a) - AUC(b) under a shared resample of pair indices. Positives and
    negatives are aligned pair-for-pair across encoders, so drawing indices once
    and applying them to both removes the common sampling noise; a two-sided
    p-value comes from how often the sign of the replicate delta flips."""
    base = roc_auc(pos_a, neg_a) - roc_auc(pos_b, neg_b)
    rng = random.Random(seed)
    n_p, n_n = len(pos_a), len(neg_a)
    vals = []
    for _ in range(B):
        pi = [rng.randrange(n_p) for _ in range(n_p)]
        ni = [rng.randrange(n_n) for _ in range(n_n)]
        d = (roc_auc([pos_a[i] for i in pi], [neg_a[i] for i in ni])
             - roc_auc([pos_b[i] for i in pi], [neg_b[i] for i in ni]))
        vals.append(d)
    vals.sort()
    lo = vals[max(0, int(0.025 * len(vals)))]
    hi = vals[min(len(vals) - 1, int(0.975 * len(vals)))]
    if base > 0:
        n_opposite = sum(1 for v in vals if v <= 0)
    else:
        n_opposite = sum(1 for v in vals if v >= 0)
    p = min(1.0, 2 * n_opposite / len(vals))
    return {"estimate": base, "ci95": [lo, hi], "p_two_sided": p,
            "n_boot": len(vals)}


# ---- data --------------------------------------------------------------------
def pools(enc_block):
    sc = enc_block["scores"]
    pos = [p["cos"] for name in POS_POOLS for p in sc[name]]
    neg = [p["cos"] for name in NEG_POOLS for p in sc[name]]
    attr_pos = [p["cos"] for p in sc[ATTR_IDENTICAL_POS]]
    return pos, neg, attr_pos


def check_against_stored(enc, block, computed):
    """The stdlib metrics must reproduce baselines.py's sklearn values."""
    stored = block["sep_pos_vs_neg"]
    for key, mine in (("roc_auc", computed["roc_auc"]["estimate"]),
                      ("average_precision", computed["average_precision"]["estimate"])):
        if abs(stored[key] - mine) > 1e-9:
            raise SystemExit(
                f"{enc}: {key} mismatch vs baselines.json "
                f"(stored {stored[key]!r}, recomputed {mine!r})")
    if abs(block["operating_point"]["tar"] - computed["tar"]["estimate"]) > 1e-9:
        raise SystemExit(f"{enc}: TAR mismatch vs baselines.json")
    if block["crosswearer_probe"]["k_above_operating_point"] != computed["attr_identical_k"]["estimate"]:
        raise SystemExit(f"{enc}: attribute-identical k mismatch vs baselines.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--far", type=float, default=0.10)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    src = json.load(open(SRC))
    out = {"experiment": src["experiment"], "eval": src["eval"],
           "n_boot": args.boot, "far_target": args.far,
           "resampling": "stratified (positives, negatives, attribute-identical "
                         "positives resampled independently to original sizes)",
           "encoders": {}}

    for enc in ORDER:
        if enc not in src["encoders"]:
            continue
        block = src["encoders"][enc]
        pos, neg, attr_pos = pools(block)
        res = bootstrap(pos, neg, attr_pos, args.far, args.boot, seed=args.seed)
        check_against_stored(enc, block, res)
        res["n_pos"], res["n_neg"] = len(pos), len(neg)
        out["encoders"][enc] = res

    out["paired_auc_contrasts"] = {}
    for name, a, b in CONTRASTS:
        if a not in src["encoders"] or b not in src["encoders"]:
            continue
        pa, na, _ = pools(src["encoders"][a])
        pb, nb, _ = pools(src["encoders"][b])
        out["paired_auc_contrasts"][name] = dict(
            paired_auc_delta(pa, na, pb, nb, args.boot, seed=args.seed + 1),
            a=a, b=b)

    json.dump(out, open(OUT, "w"), indent=2)

    print(f"bootstrap CIs (B={args.boot}, stratified, percentile)\n")
    head = f"{'recogniser':<28}{'ROC-AUC':<22}{'AP':<22}{'TAR@10%FAR':<22}attr-ident."
    print(head)
    print("-" * len(head))
    for enc, r in out["encoders"].items():
        def cell(k, fmt="{:.3f}"):
            c = r[k]
            lo, hi = c["ci95"]
            return (fmt + " [" + fmt + ", " + fmt + "]").format(c["estimate"], lo, hi)
        k = r["attr_identical_k"]
        print(f"{LABELS[enc]:<28}{cell('roc_auc'):<22}{cell('average_precision'):<22}"
              f"{cell('tar', '{:.2f}'):<22}"
              f"{k['estimate']}/5 [{k['ci95'][0]}, {k['ci95'][1]}]")

    print(f"\nchance AP = {src['encoders'][ORDER[0]]['sep_pos_vs_neg']['prevalence']:.3f}")

    print("\npaired AUC deltas (shared pair resample)\n")
    for name, c in out["paired_auc_contrasts"].items():
        print(f"  {name:<44}{c['estimate']:+.3f} "
              f"[{c['ci95'][0]:+.3f}, {c['ci95'][1]:+.3f}]  p={c['p_two_sided']:.3f}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
