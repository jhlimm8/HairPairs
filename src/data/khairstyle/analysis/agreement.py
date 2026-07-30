#!/usr/bin/env python3
"""Inter-annotator agreement + conditional rates for the attribute-sufficiency
adjudication (Experiment 1).

Two outputs, both from the `adjudications` table for one experiment frame:

  (A) AGREEMENT on the SHARED IAA subset (frame.assignment.shared) -- the items
      every labeler labels. We follow standard annotation practice of measuring
      agreement on a multiply-annotated subset (Artstein & Poesio 2008; Bowman
      et al. 2015 / SNLI). Coefficients:
        * SPLIT (binary same/different): Fleiss' kappa AND Krippendorff's alpha
          (nominal). Fleiss/alpha (not Cohen's kappa) because the shared set is
          labelled by >2 rotating raters.
        * MERGE (clusterings): converted to binary CO-ASSIGNMENT judgments over
          each within-group member pair ("same cluster?"), then the same
          Fleiss/alpha are applied. Bootstrap resamples at the GROUP level so the
          non-independence of pairs within a group is respected (cluster-robust).
      CIs are nonparametric bootstrap (Hayes & Krippendorff 2007).

  (B) CONDITIONAL RATES over ALL collected verdicts (not just the shared set):
        * pi_split = P(rater calls a Hamming-1 neighbour pair "same hairstyle")
          -> over-fragmentation. Reported verdict-level and item-level (majority).
        * pi_merge = P(a collision group is judged impure / a co-block member
          pair is split) -> false-merge. Reported group-level and pair-level.
      Proportions get Wilson 95% CIs. All rates are CONDITIONAL on the mined
      frame (collision / neighbour membership) by construction.

Stdlib only.  python3 agreement.py [--exp attr-suff-v4] [--boot 2000]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "data", "index.sqlite")
EXP_ROOT = os.path.join(HERE, "..", "adjudicate", "experiments")

MIN_UNITS = 8          # don't report a coefficient on fewer doubly-rated units
MIN_RATERS = 2         # a unit needs >= this many raters to inform agreement


# ---- coefficients (units = list of per-rater label lists) -------------------
def _usable(units):
    return [u for u in units if len(u) >= MIN_RATERS]


def fleiss_kappa(units):
    """Generalised Fleiss' kappa allowing a variable number of raters per item
    (per-item observed agreement uses that item's own rater count)."""
    units = _usable(units)
    if len(units) < MIN_UNITS:
        return None
    counts = [Counter(u) for u in units]
    total = sum(sum(c.values()) for c in counts)
    cats = {k for c in counts for k in c}
    p = {k: sum(c.get(k, 0) for c in counts) / total for k in cats}
    Pe = sum(v * v for v in p.values())
    Pbar = 0.0
    for c in counts:
        n = sum(c.values())
        Pbar += sum(v * (v - 1) for v in c.values()) / (n * (n - 1))
    Pbar /= len(counts)
    if Pe >= 1.0:
        return 1.0
    return (Pbar - Pe) / (1 - Pe)


def krippendorff_alpha(units):
    """Krippendorff's alpha, nominal metric, via the coincidence matrix; handles
    variable raters per unit and missing data."""
    units = _usable(units)
    if len(units) < MIN_UNITS:
        return None
    val_counts = Counter()
    disagree = 0.0
    for u in units:
        m = len(u)
        cu = Counter(u)
        for x in u:
            val_counts[x] += 1
        # sum over ordered pairs (a!=b) within the unit of 1/(m-1)
        same_pairs = sum(v * (v - 1) for v in cu.values())  # ordered same pairs
        all_pairs = m * (m - 1)
        disagree += (all_pairs - same_pairs) / (m - 1)
    n = sum(val_counts.values())
    if n < 2:
        return None
    Nc = sum(val_counts[a] * val_counts[b]
             for a in val_counts for b in val_counts if a != b)
    if Nc == 0:
        return 1.0
    return 1 - (n - 1) * disagree / Nc


def observed_agreement(units):
    """Raw observed pairwise agreement P_o: mean over multi-rater units of the
    fraction of agreeing (unordered) rater pairs. Reported alongside kappa/alpha
    because, under the extreme 'different'-heavy base rate of this frame, the
    chance-corrected coefficients are deflated by the prevalence paradox
    (Feinstein & Cicchetti 1990) while raw agreement stays high and interpretable."""
    units = _usable(units)
    if len(units) < MIN_UNITS:
        return None
    tot = 0.0
    for u in units:
        m = len(u)
        c = Counter(u)
        same = sum(v * (v - 1) for v in c.values())   # ordered agreeing pairs
        tot += same / (m * (m - 1))
    return tot / len(units)


def bootstrap(clusters, coef_fn, B, seed=0):
    """clusters: list of lists of units (resampling unit = a cluster). For split
    each item is its own cluster; for merge each group is a cluster of its pairs."""
    flat = [u for cl in clusters for u in cl]
    base = coef_fn(flat)
    if base is None:
        return None
    rng = random.Random(seed)
    K = len(clusters)
    vals = []
    for _ in range(B):
        samp = [clusters[rng.randrange(K)] for _ in range(K)]
        v = coef_fn([u for cl in samp for u in cl])
        if v is not None:
            vals.append(v)
    if not vals:
        return {"estimate": base, "ci95": None, "n_units": len(_usable(flat))}
    vals.sort()
    lo = vals[max(0, int(0.025 * len(vals)))]
    hi = vals[min(len(vals) - 1, int(0.975 * len(vals)))]
    return {"estimate": base, "ci95": [lo, hi], "n_units": len(_usable(flat)),
            "n_boot": len(vals)}


def wilson(k, n, z=1.96):
    if n == 0:
        return {"p": None, "ci95": None, "k": 0, "n": 0}
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return {"p": p, "ci95": [(centre - half) / d, (centre + half) / d], "k": k, "n": n}


# ---- data loading -----------------------------------------------------------
def load_frame(exp):
    fr = json.load(open(os.path.join(EXP_ROOT, exp, "frame.json")))
    shared = set(fr.get("assignment", {}).get("shared", []))
    split_meta = {p["pair_id"]: p for p in fr.get("split_sample", [])}
    merge_meta = {g["group_id"]: g for g in fr.get("merge_sample", [])}
    return fr, shared, split_meta, merge_meta


def load_verdicts(con, exp):
    split = defaultdict(dict)   # item_id -> {rater: "same"|"different"}
    merge = defaultdict(dict)   # item_id -> {rater: {source: cluster_idx}}
    for r in con.execute(
            "SELECT item_id, kind, rater, verdict FROM adjudications WHERE experiment=?",
            (exp,)):
        v = json.loads(r["verdict"])
        if r["kind"] == "split":
            rel = v.get("relation")
            if rel in ("same", "different"):
                split[r["item_id"]][r["rater"]] = rel
        else:
            idx = {}
            for ci, cl in enumerate(v.get("clusters", [])):
                for s in cl:
                    idx[s] = ci
            merge[r["item_id"]][r["rater"]] = idx
    return split, merge


# ---- agreement on the shared set --------------------------------------------
def split_units(shared, split_meta, split_v, stratify=None):
    """Return {stratum: [unit,...]} where unit = list of rater relations, plus the
    flat per-item cluster list for bootstrap (each item is its own cluster)."""
    out = defaultdict(list)
    for iid in shared:
        if iid not in split_meta:
            continue
        labels = list(split_v.get(iid, {}).values())
        key = "all"
        meta = split_meta[iid]
        strat = {"all": "all",
                 "diff_attr": meta.get("diff_attr"),
                 "cross_style": "cross" if meta.get("cross_style") else "intra"}
        for sk, sv in strat.items():
            if stratify is None or stratify == sk:
                out[(sk, sv)].append(labels)
    return out


def merge_pair_units(shared, merge_meta, merge_v):
    """For each shared merge group, build co-assignment units over member pairs.
    Returns clusters = [[unit,...] per group] and a flat list, plus strata."""
    clusters = []                 # group-level clusters for bootstrap
    strata = defaultdict(list)    # (stratum_key, val) -> [unit,...]
    for iid in shared:
        if iid not in merge_meta:
            continue
        meta = merge_meta[iid]
        sources = [m["source"] for m in meta["members"]]
        raters = merge_v.get(iid, {})
        group_units = []
        for i in range(len(sources)):
            for j in range(i + 1, len(sources)):
                a, b = sources[i], sources[j]
                labels = []
                for idx in raters.values():
                    if a in idx and b in idx:
                        labels.append("same" if idx[a] == idx[b] else "diff")
                group_units.append(labels)
        if group_units:
            clusters.append(group_units)
            sk = "cross" if meta.get("cross_style") else "intra"
            for u in group_units:
                strata[("all", "all")].append(u)
                strata[("cross_style", sk)].append(u)
    return clusters, strata


def agreement_block(shared, split_meta, merge_meta, split_v, merge_v, B):
    res = {"shared_n": len(shared)}

    # SPLIT
    su = split_units(shared, split_meta, split_v)
    split_res = {}
    for (sk, sv), units in sorted(su.items()):
        clusters = [[u] for u in units]            # each item its own cluster
        fk = bootstrap(clusters, fleiss_kappa, B, seed=1)
        ka = bootstrap(clusters, krippendorff_alpha, B, seed=2)
        po = bootstrap(clusters, observed_agreement, B, seed=5)
        if fk is None and ka is None:
            continue
        split_res.setdefault(sk, {})[sv] = {
            "n_items_multirated": len(_usable(units)),
            "observed_agreement": po,
            "fleiss_kappa": fk, "krippendorff_alpha": ka}
    res["split"] = split_res

    # MERGE (pairwise co-assignment, group-clustered bootstrap)
    clusters, strata = merge_pair_units(shared, merge_meta, merge_v)
    merge_res = {}
    # overall + cross/intra strata use the same group clusters for the bootstrap;
    # for strata we filter the clusters to groups of that stratum.
    def clusters_for(stratum_key, stratum_val):
        if stratum_key == "all":
            return clusters
        sub = []
        for iid in shared:
            if iid not in merge_meta:
                continue
            meta = merge_meta[iid]
            sk = "cross" if meta.get("cross_style") else "intra"
            if sk != stratum_val:
                continue
            sources = [m["source"] for m in meta["members"]]
            raters = merge_v.get(iid, {})
            gu = []
            for i in range(len(sources)):
                for j in range(i + 1, len(sources)):
                    a, b = sources[i], sources[j]
                    labels = [("same" if idx[a] == idx[b] else "diff")
                              for idx in raters.values() if a in idx and b in idx]
                    gu.append(labels)
            if gu:
                sub.append(gu)
        return sub
    for (sk, sv) in sorted(strata):
        cl = clusters_for(sk, sv)
        fk = bootstrap(cl, fleiss_kappa, B, seed=3)
        ka = bootstrap(cl, krippendorff_alpha, B, seed=4)
        po = bootstrap(cl, observed_agreement, B, seed=6)
        if fk is None and ka is None:
            continue
        flat = [u for c in cl for u in c]
        merge_res.setdefault(sk, {})[sv] = {
            "n_pairs_multirated": len(_usable(flat)),
            "n_groups": len(cl),
            "observed_agreement": po,
            "fleiss_kappa": fk, "krippendorff_alpha": ka}
    res["merge"] = merge_res
    return res


# ---- conditional rates over ALL verdicts ------------------------------------
def majority_same(rel_counts):
    s = rel_counts.get("same", 0)
    d = rel_counts.get("different", 0)
    if s == d:
        return None
    return s > d


def conditional_rates(split_meta, merge_meta, split_v, merge_v):
    out = {}

    # pi_split (over-fragmentation): verdict-level and item-level majority
    v_same = v_tot = 0
    i_same = i_tot = 0
    by_attr = defaultdict(lambda: [0, 0])     # diff_attr -> [same_items, items]
    for iid, raters in split_v.items():
        if iid not in split_meta:
            continue
        rc = Counter(raters.values())
        v_same += rc.get("same", 0)
        v_tot += sum(rc.values())
        maj = majority_same(rc)
        if maj is not None:
            i_tot += 1
            i_same += 1 if maj else 0
            a = split_meta[iid].get("diff_attr")
            by_attr[a][1] += 1
            by_attr[a][0] += 1 if maj else 0
    out["pi_split"] = {
        "verdict_level": wilson(v_same, v_tot),
        "item_level_majority": wilson(i_same, i_tot),
        "by_diff_attr": {a: wilson(k, n) for a, (k, n) in sorted(by_attr.items())},
    }

    # pi_merge (false-merge): group-level impurity + pair-level split rate
    g_impure = g_tot = 0
    pair_split = pair_tot = 0
    for iid, raters in merge_v.items():
        if iid not in merge_meta:
            continue
        sources = [m["source"] for m in merge_meta[iid]["members"]]
        # group-level: majority of raters judged it impure (>1 cluster)?
        impure_votes = sum(1 for idx in raters.values()
                           if len({idx[s] for s in sources if s in idx}) > 1)
        n_r = len(raters)
        if n_r:
            g_tot += 1
            if impure_votes * 2 > n_r:
                g_impure += 1
        # pair-level: over all member pairs x raters, fraction split
        for i in range(len(sources)):
            for j in range(i + 1, len(sources)):
                a, b = sources[i], sources[j]
                for idx in raters.values():
                    if a in idx and b in idx:
                        pair_tot += 1
                        if idx[a] != idx[b]:
                            pair_split += 1
    out["pi_merge"] = {
        "group_level_impurity": wilson(g_impure, g_tot),
        "pair_level_split": wilson(pair_split, pair_tot),
    }
    return out


def _fmt(coef):
    if not coef or coef.get("estimate") is None:
        return "n/a"
    e = coef["estimate"]
    ci = coef.get("ci95")
    if ci:
        return f"{e:+.3f} [{ci[0]:+.3f},{ci[1]:+.3f}] (n={coef.get('n_units','?')})"
    return f"{e:+.3f} (n={coef.get('n_units','?')})"


def _fmtw(w):
    if not w or w.get("p") is None:
        return "n/a"
    ci = w["ci95"]
    return f"{w['p']*100:5.1f}% [{ci[0]*100:.1f},{ci[1]*100:.1f}]  ({w['k']}/{w['n']})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default="attr-suff-v4")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    fr, shared, split_meta, merge_meta = load_frame(args.exp)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    split_v, merge_v = load_verdicts(con, args.exp)
    con.close()

    agree = agreement_block(shared, split_meta, merge_meta, split_v, merge_v, args.boot)
    rates = conditional_rates(split_meta, merge_meta, split_v, merge_v)
    out = {"experiment": args.exp, "lens": fr.get("lens"),
           "labelers": fr.get("labelers", []), "shared_size": fr.get("shared_size"),
           "min_units": MIN_UNITS, "min_raters": MIN_RATERS,
           "agreement": agree, "conditional_rates": rates}

    outp = args.out or os.path.join(HERE, "agreement.json")
    with open(outp, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)

    print(f"== Agreement (shared IAA subset, n={len(shared)}) — {args.exp} ==")
    print("  [needs >=2 raters per item; run after labelers have worked the shared set]")
    for task in ("split", "merge"):
        print(f"  {task.upper()}:")
        for sk, vals in agree.get(task, {}).items():
            for sv, d in vals.items():
                po = _fmt(d.get("observed_agreement"))
                fk = _fmt(d.get("fleiss_kappa")); ka = _fmt(d.get("krippendorff_alpha"))
                print(f"    {sk}={sv:8s} P_o={po}  Fleiss kappa={fk}  Kripp alpha={ka}")
    print("\n== Conditional rates (ALL verdicts; conditional on the mined frame) ==")
    ps = rates["pi_split"]
    print(f"  pi_split verdict-level : {_fmtw(ps['verdict_level'])}")
    print(f"  pi_split item-majority : {_fmtw(ps['item_level_majority'])}")
    for a, w in ps["by_diff_attr"].items():
        print(f"      differ-in {a:14s}: {_fmtw(w)}")
    pm = rates["pi_merge"]
    print(f"  pi_merge group-impurity: {_fmtw(pm['group_level_impurity'])}")
    print(f"  pi_merge pair-split    : {_fmtw(pm['pair_level_split'])}")
    print(f"\nWrote {outp}")


if __name__ == "__main__":
    main()
