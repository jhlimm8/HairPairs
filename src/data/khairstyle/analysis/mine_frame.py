#!/usr/bin/env python3
"""Pre-registration sampler for Experiment 1 (attribute-sufficiency adjudication).

Draws the FROZEN candidate set the rater will adjudicate, from the VISUAL_17 lens
pools, with a fixed seed, and writes adjudicate/experiments/<exp>/frame.json (the
adjudication server serves one frame per experiment). Run this ONCE per experiment
and commit the output BEFORE any labelling: the conditional / pre-registered rate
claims in the
paper (Section 4) depend on the drawn ids being fixed in advance.

Three enforced eligibility / balance rules:
  * AFTER-ONLY. A capture session contains pre-cut ("before") and post-cut
    ("after") images; only the AFTER images show the labelled haircut. We use
    only after images: a source is eligible iff it has >= MIN_AFTER_VIEWS after
    images, and the 3-view stimulus is always drawn from after images (see
    adjudicate/views.py / serve.py, which filter before_after='after').
  * SEX 50:50. The corpus is ~76% female; we artificially balance the drawn
    sample to equal male/female so neither error direction is dominated by
    women's cuts. Sex is NOT part of the VISUAL lens, so it never affects which
    sources collide or neighbour — it is only a sampling stratum here.
  * SAME-PERSON GUARD (ID_GAP). `source` is a capture session, not a person:
    one client's A/B styling is filed as two sources with the same id prefix and
    near-consecutive folder indices. Those pairs are same-person, not the
    cross-wearer comparison we want, so we drop split pairs and de-duplicate
    merge members within ID_GAP folder positions of the same prefix.

Frame lens = VISUAL (17 attributes). Two pools, one per error direction:
  * MERGE  (false-merge / collision): groups of sources sharing every VISUAL
           attribute but different sessions (size > 1); rater clusters the crops.
  * SPLIT  (false-split / miss): pairs whose VISUAL vectors differ in EXACTLY one
           attribute (Hamming-1 neighbours); rater answers same/different.

Sampling targets (design Sec 6, now sex-split):
  MERGE  150 groups = 75 male + 75 female; within each sex, cross-category
         oversampled (~60%) with coverage of sizes {2,3,4,>=5}.
  SPLIT  250 pairs  = 125 male + 125 female; within each sex the per-attribute
         allocation (curl 35 . parting 17 . colour 17 . bang 15 . damage 12 .
         natural_curl 10 . strand_width 7 . other 10, topped up to 125),
         cross-category oversampled. Mixed-sex pairs are excluded from the draw.

Usage:
    python3 mine_frame.py                 # draw with the registered seed
    python3 mine_frame.py --seed 123      # alternative seed (NOT the registered draw)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import time
from collections import Counter, defaultdict

from predictive_power import VISUAL, GROUPS, STYLE_NAME, key

SHAPE = GROUPS["shape"]                       # 8 colour-free cut/shape fields
LENSES = {"shape": SHAPE, "visual": VISUAL}   # `shape` is the headline frame
LENS_LABELS = {"shape": "SHAPE_8", "visual": "VISUAL_17"}

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "data", "index.sqlite")
EXP_ROOT = os.path.join(HERE, "..", "adjudicate", "experiments")
MISS = "\u2205"

# --- pre-registered constants ------------------------------------------------
SEED = 20260616            # design "Date opened"; the registered draw
MIN_AFTER_VIEWS = 20       # source must have >= this many AFTER images
AFTER = "after"
SEX_LABELS = {"\ub0a8": "M", "\uc5ec": "F"}   # 남 / 여
SEXES = ("M", "F")

# Same-person guard. `source` is a capture session, not a person; one client's
# A/B styling (e.g. a curl change) is filed as two sources with the SAME id
# prefix and near-consecutive extraction (folder) indices. Such pairs are
# same-person, not the cross-wearer comparison the benchmark wants. We exclude
# split pairs and de-duplicate merge members whose two sources share an id
# prefix AND lie within GAP_IDX folder positions. Heuristic (folder index ~=
# global capture order), not a guarantee; raters are also blinded to ids. The
# same-visit cluster sits at |delta| <= ~12 with a long gap to the bulk (>50).
ID_GAP = 16

MERGE_PER_SEX = 75
MERGE_CROSS_FRAC = 0.60
SIZE_BUCKETS = [("2", 2, 2), ("3", 3, 3), ("4", 4, 4), (">=5", 5, 10**9)]

SPLIT_PER_SEX = 125
# Per differing-attribute stratum, TOTAL across sexes. Lens-specific: under the
# colour-free SHAPE lens only cut/shape attributes can differ in a neighbour
# pair, so colour/chemistry strata do not exist. The remainder up to 250 is the
# "other" catch-all (e.g. base type, sides).
SPLIT_ALLOC_BY_LENS = {
    "shape": {"curl": 80, "partition": 45, "bang": 35,
              "natural_curl": 30, "length": 25, "hair_width": 20},
    "visual": {"curl": 70, "partition": 35, "color": 35, "bang": 30,
               "damage": 25, "natural_curl": 20, "hair_width": 15},
}
SPLIT_CROSS_FRAC = 0.70

# --- multi-rater rollout (v4) ------------------------------------------------
# A small SHARED subset is labelled by *every* labeler (the inter-annotator
# agreement / IAA subset; Fleiss kappa / Krippendorff alpha). The remaining
# drawn items are partitioned DISJOINTLY across labelers (one verdict each) to
# maximise the number of distinct labelled items for a fixed verdict budget.
SHARED_SET_SIZE = 50               # IAA subset size; configurable via --shared-size
N_LABELERS = 3                     # disjoint coverage partitioned across this many
RATER_IDS = ["L1", "L2", "L3"]
# Prior experiments whose already-labelled items are PINNED into the new frame so
# no past adjudication is wasted. Verdicts are keyed by item_id (sorted source
# ids) and ask a lens-independent question ("same true hairstyle?"), so any prior
# verdict on a pair/group that reappears here is valid to reuse.
REUSE_FROM = ["attr-suff-v3"]


def group_id(sources):
    return "+".join(sorted(sources))


def pair_id(sa, sb):
    return "__".join(sorted((sa, sb)))


def size_bucket(n):
    for name, lo, hi in SIZE_BUCKETS:
        if lo <= n <= hi:
            return name
    return ">=5"


# --- after-only loader -------------------------------------------------------
def _id_parts(source, any_path):
    """(prefix, folder_index) for a source. prefix = leading letters of the id;
    folder_index = the `<NNNN>.<source>` extraction counter in its path (~ global
    capture order). Either may be None if unparseable."""
    pm = re.match(r"^([A-Za-z]+)", source)
    prefix = pm.group(1) if pm else None
    idx = None
    if any_path:
        m = re.search(r"/(\d+)\." + re.escape(source) + r"/", any_path)
        if m:
            idx = int(m.group(1))
    return prefix, idx


def load_after(con):
    """One eligible record per source: VISUAL vector + style + sex + after-view
    count + id prefix/folder-index (for the same-person guard). Attributes are
    constant within a source, so MAX over all rows == the (single) value; the
    after count is what gates eligibility."""
    cols = VISUAL + [STYLE_NAME]
    sel = ", ".join(f'MAX("{c}") AS "{c}"' for c in cols)
    q = (f"SELECT source, MAX(sex) AS sex, {sel}, "
         f"MIN(image_path) AS any_path, "
         f"SUM(CASE WHEN before_after=? THEN 1 ELSE 0 END) AS n_after "
         f"FROM images GROUP BY source")
    recs = []
    for r in con.execute(q, (AFTER,)):
        d = dict(r)
        sex = SEX_LABELS.get(d["sex"])
        if sex is None or d["n_after"] < MIN_AFTER_VIEWS:
            continue
        vals = {c: (MISS if (d[c] is None or d[c] == "") else str(d[c])) for c in cols}
        prefix, idx = _id_parts(d["source"], d["any_path"])
        recs.append({"source": d["source"], "sex": sex,
                     "n_after": d["n_after"], "vals": vals,
                     "prefix": prefix, "idx": idx})
    return recs


def make_near(gap):
    """Predicate: are two source records likely the SAME person (same salon
    visit)? True iff same id prefix and folder indices within `gap`. gap<=0
    disables the guard."""
    def near(ra, rb):
        if gap <= 0:
            return False
        if ra["prefix"] is None or ra["prefix"] != rb["prefix"]:
            return False
        if ra["idx"] is None or rb["idx"] is None:
            return False
        return abs(ra["idx"] - rb["idx"]) <= gap
    return near


# --- pool construction -------------------------------------------------------
def dedup_same_person(recs, near):
    """Collapse same-person captures: keep one representative per same-visit
    cluster (greedy over id order). Returns (kept_recs, n_dropped)."""
    kept = []
    dropped = 0
    for rec in sorted(recs, key=lambda r: r["source"]):
        if any(near(rec, k) for k in kept):
            dropped += 1
        else:
            kept.append(rec)
    return kept, dropped


def merge_pool(records, lens, near):
    groups = defaultdict(list)
    for rec in records:
        groups[key(rec["vals"], lens)].append(rec)
    pool = []
    n_members_dropped = 0
    n_groups_dropped = 0
    for g in groups.values():
        if len(g) < 2:
            continue
        g, dropped = dedup_same_person(g, near)
        n_members_dropped += dropped
        if len(g) < 2:                 # collapsed to a single wearer -> not a group
            n_groups_dropped += 1
            continue
        sexes = {r["sex"] for r in g}
        members = sorted(
            ({"source": r["source"], "n_views": r["n_after"],
              "style": r["vals"][STYLE_NAME], "sex": r["sex"]} for r in g),
            key=lambda m: m["source"])
        styles = sorted({m["style"] for m in members})
        pool.append({
            "group_id": group_id([m["source"] for m in members]),
            "size": len(members),
            "cross_style": len(styles) > 1,
            "styles": styles,
            "sex": next(iter(sexes)) if len(sexes) == 1 else "mixed",
            "members": members,
        })
    pool.sort(key=lambda d: d["group_id"])
    return pool, {"members_dropped": n_members_dropped, "groups_dropped": n_groups_dropped}


def split_pool(records, lens, near):
    """{(diff_attr, sex): [pair, ...]} over same-sex Hamming-1 neighbour pairs.
    Same-person (same-visit) pairs are excluded."""
    by_attr_sex = defaultdict(list)
    n_mixed = 0
    n_same_person = 0
    for p in lens:
        rest = [a for a in lens if a != p]
        buckets = defaultdict(list)
        for rec in records:
            buckets[key(rec["vals"], rest)].append(rec)
        for vs in buckets.values():
            if len(vs) < 2:
                continue
            for i in range(len(vs)):
                ra = vs[i]
                for j in range(i + 1, len(vs)):
                    rb = vs[j]
                    if ra["vals"][p] == rb["vals"][p]:
                        continue
                    if ra["sex"] != rb["sex"]:
                        n_mixed += 1
                        continue
                    if near(ra, rb):
                        n_same_person += 1
                        continue
                    a, b = sorted((ra, rb), key=lambda x: x["source"])
                    by_attr_sex[(p, ra["sex"])].append({
                        "pair_id": pair_id(ra["source"], rb["source"]),
                        "diff_attr": p,
                        "sex": ra["sex"],
                        "cross_style": a["vals"][STYLE_NAME] != b["vals"][STYLE_NAME],
                        "a": {"source": a["source"], "val": a["vals"][p],
                              "style": a["vals"][STYLE_NAME], "n_views": a["n_after"]},
                        "b": {"source": b["source"], "val": b["vals"][p],
                              "style": b["vals"][STYLE_NAME], "n_views": b["n_after"]},
                    })
    for k in by_attr_sex:
        by_attr_sex[k].sort(key=lambda d: d["pair_id"])
    return by_attr_sex, n_mixed, n_same_person


# --- sampling ----------------------------------------------------------------
def allocate_across_buckets(buckets, total, rng):
    """Largest-remainder proportional allocation across buckets, then sample."""
    avail = {b: len(v) for b, v in buckets.items()}
    tot = sum(avail.values())
    if tot <= total:
        return [x for v in buckets.values() for x in v]
    raw = {b: avail[b] / tot * total for b in buckets}
    alloc = {b: int(raw[b]) for b in buckets}
    order = sorted(buckets, key=lambda b: raw[b] - int(raw[b]), reverse=True)
    i = 0
    while sum(alloc.values()) < total and order:
        b = order[i % len(order)]
        if alloc[b] < avail[b]:
            alloc[b] += 1
        i += 1
        if i > 100000:
            break
    chosen = []
    for b, items in buckets.items():
        chosen += rng.sample(items, min(alloc[b], avail[b]))
    return chosen


def by_size(items):
    d = defaultdict(list)
    for g in items:
        d[size_bucket(g["size"])].append(g)
    return d


def sample_with_cross(items, n, rng):
    if not items or n <= 0:
        return []
    if len(items) <= n:
        return list(items)
    cross = [x for x in items if x["cross_style"]]
    intra = [x for x in items if not x["cross_style"]]
    n_cross = min(len(cross), int(round(SPLIT_CROSS_FRAC * n)))
    n_intra = n - n_cross
    if n_intra > len(intra):
        n_intra = len(intra)
        n_cross = min(len(cross), n - n_intra)
    return rng.sample(cross, n_cross) + rng.sample(intra, n_intra)


def draw_merge(pool, rng, merge_per_sex=MERGE_PER_SEX):
    # The same-person guard can shrink one sex's clean-collision pool below the
    # target; cap BOTH sexes at the smaller supply so the draw stays 50:50.
    avail = {sl: len([g for g in pool if g["sex"] == sl]) for sl in SEXES}
    per_sex = min(merge_per_sex, min(avail.values()))
    chosen = []
    for sl in SEXES:
        sp = [g for g in pool if g["sex"] == sl]
        cross = [g for g in sp if g["cross_style"]]
        intra = [g for g in sp if not g["cross_style"]]
        tgt_cross = min(len(cross), round(MERGE_CROSS_FRAC * per_sex))
        picked = allocate_across_buckets(by_size(cross), tgt_cross, rng)
        tgt_intra = per_sex - len(picked)
        picked += allocate_across_buckets(by_size(intra), min(len(intra), tgt_intra), rng)
        if len(picked) < per_sex:                 # top up to keep sex exact
            ids = {g["group_id"] for g in picked}
            left = [g for g in sp if g["group_id"] not in ids]
            rng.shuffle(left)
            picked += left[:per_sex - len(picked)]
        chosen += picked
    chosen.sort(key=lambda d: d["group_id"])
    return chosen


def draw_split(by_attr_sex, alloc, rng, split_per_sex=SPLIT_PER_SEX):
    other_total = max(0, split_per_sex * 2 - sum(alloc.values()))
    chosen = []
    for sl in SEXES:
        picked = []
        for attr, n_total in alloc.items():
            picked += sample_with_cross(by_attr_sex.get((attr, sl), []), n_total // 2, rng)
        other = [p for (a, s), items in by_attr_sex.items()
                 if s == sl and a not in alloc for p in items]
        other.sort(key=lambda d: d["pair_id"])
        picked += sample_with_cross(other, other_total // 2, rng)
        ids = {p["pair_id"] for p in picked}
        if len(picked) < split_per_sex:           # top up to keep sex exact
            left = [p for (a, s), items in by_attr_sex.items() if s == sl
                    for p in items if p["pair_id"] not in ids]
            left.sort(key=lambda d: d["pair_id"])
            picked += sample_with_cross(left, split_per_sex - len(picked), rng)
        elif len(picked) > split_per_sex:
            rng.shuffle(picked)
            picked = picked[:split_per_sex]
        chosen += picked
    chosen.sort(key=lambda d: d["pair_id"])
    return chosen


def labeled_item_ids(con, experiments):
    """Distinct item_ids that already carry >=1 verdict in any `experiments`.
    Returns empty set if the table does not exist yet."""
    if not experiments:
        return set()
    qs = ",".join("?" for _ in experiments)
    try:
        rows = con.execute(
            f"SELECT DISTINCT item_id FROM adjudications WHERE experiment IN ({qs})",
            tuple(experiments)).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {r[0] for r in rows}


def build_assignment(merge_sample, split_sample, shared_size, rater_ids, rng,
                     pre_covered=frozenset()):
    """Split the drawn frame into (a) a SHARED IAA subset every labeler labels,
    drawn proportionally across merge/split, and (b) DISJOINT coverage, the
    remaining FRESH items round-robin partitioned across rater_ids. Items in
    `pre_covered` (already carry a verdict from a prior rater that we reuse) stay
    in the frame and may land in the shared subset, but are NOT re-assigned as
    fresh coverage so labeler budget is not spent re-labelling them. Deterministic
    given rng."""
    merge_ids = [g["group_id"] for g in merge_sample]
    split_ids = [p["pair_id"] for p in split_sample]
    all_ids = merge_ids + split_ids
    n_total = len(all_ids)
    shared_size = max(0, min(shared_size, n_total))
    n_m = len(merge_ids)
    m_share = min(n_m, round(shared_size * n_m / n_total)) if n_total else 0
    s_share = min(len(split_ids), shared_size - m_share)
    shared = rng.sample(merge_ids, m_share) + rng.sample(split_ids, s_share)
    shared_set = set(shared)
    if len(shared) < shared_size:                  # rounding deficit -> top up
        left = [i for i in all_ids if i not in shared_set]
        rng.shuffle(left)
        shared += left[: shared_size - len(shared)]
        shared_set = set(shared)
    rest = [i for i in all_ids if i not in shared_set and i not in pre_covered]
    rng.shuffle(rest)
    coverage = {rid: [] for rid in rater_ids}
    for idx, item in enumerate(rest):
        coverage[rater_ids[idx % len(rater_ids)]].append(item)
    return {
        "shared": sorted(shared),
        "coverage": {rid: sorted(v) for rid, v in coverage.items()},
        "pre_covered": sorted(i for i in pre_covered if i in set(all_ids)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--lens", choices=list(LENSES), default="shape",
                    help="grouping lens for both pools; `shape` is colour-free (headline)")
    ap.add_argument("--exp", default="attr-suff-v4",
                    help="experiment id; frame is written to experiments/<exp>/frame.json")
    ap.add_argument("--label", default=None, help="human-readable experiment label")
    ap.add_argument("--id-gap", type=int, default=ID_GAP,
                    help="same-person guard: drop same-prefix pairs within this many "
                         "folder positions (0 disables)")
    ap.add_argument("--merge-per-sex", type=int, default=MERGE_PER_SEX,
                    help="merge groups drawn per sex (total = 2x)")
    ap.add_argument("--split-per-sex", type=int, default=250,
                    help="split pairs drawn per sex (total = 2x); v4 expands coverage")
    ap.add_argument("--shared-size", type=int, default=SHARED_SET_SIZE,
                    help="IAA subset size labelled by every labeler")
    ap.add_argument("--n-labelers", type=int, default=N_LABELERS,
                    help="labelers to partition disjoint coverage across (ignored if --rater-ids)")
    ap.add_argument("--rater-ids", default=None,
                    help="comma-separated labeler ids (overrides --n-labelers)")
    ap.add_argument("--reuse-from", default=",".join(REUSE_FROM),
                    help="comma-separated prior experiments whose labelled items are pinned in "
                         "(empty string to disable)")
    ap.add_argument("--out", default=None, help="override output path")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    near = make_near(args.id_gap)
    lens = LENSES[args.lens]
    if args.rater_ids is not None:
        rater_ids = [s.strip() for s in args.rater_ids.split(",") if s.strip()]
    else:
        rater_ids = [f"L{i}" for i in range(1, args.n_labelers + 1)]
    reuse_from = [s.strip() for s in args.reuse_from.split(",") if s.strip()]
    # Scale the per-attribute split allocation to the (possibly expanded) target so
    # the differing-attribute strata keep their relative proportions.
    base_alloc = SPLIT_ALLOC_BY_LENS[args.lens]
    scale = args.split_per_sex / SPLIT_PER_SEX
    alloc = {k: max(1, round(v * scale)) for k, v in base_alloc.items()}

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    records = load_after(con)
    labeled_prior = labeled_item_ids(con, reuse_from)
    con.close()

    mpool, merge_drop = merge_pool(records, lens, near)
    by_attr_sex, n_mixed_pairs, n_same_person_pairs = split_pool(records, lens, near)

    merge_sample = draw_merge(mpool, rng, args.merge_per_sex)
    split_sample = draw_split(by_attr_sex, alloc, rng, args.split_per_sex)

    # --- pin already-labelled prior items so no past adjudication is wasted ----
    split_index = {p["pair_id"]: p for v in by_attr_sex.values() for p in v}
    pool_merge_ids = {g["group_id"] for g in mpool}
    pool_split_ids = set(split_index)
    drawn_merge_ids = {g["group_id"] for g in merge_sample}
    drawn_split_ids = {p["pair_id"] for p in split_sample}
    pinned_merge = [g for g in mpool
                    if g["group_id"] in labeled_prior and g["group_id"] not in drawn_merge_ids]
    pinned_split = [split_index[i] for i in sorted(labeled_prior)
                    if i in split_index and i not in drawn_split_ids]
    merge_sample = sorted(merge_sample + pinned_merge, key=lambda d: d["group_id"])
    split_sample = sorted(split_sample + pinned_split, key=lambda d: d["pair_id"])
    reuse_matched = labeled_prior & (pool_merge_ids | pool_split_ids)
    reuse_unmatched = sorted(labeled_prior - (pool_merge_ids | pool_split_ids))

    # --- shared IAA subset + disjoint coverage partition ----------------------
    # Items already labelled by a prior rater (reuse_matched) are pre-covered:
    # kept in the frame and eligible for the shared subset, but not handed to a
    # fresh labeler as new coverage.
    assignment = build_assignment(merge_sample, split_sample, args.shared_size,
                                  rater_ids, rng, pre_covered=reuse_matched)

    m_sex = Counter(g["sex"] for g in merge_sample)
    m_cross = sum(g["cross_style"] for g in merge_sample)
    m_sizes = Counter(size_bucket(g["size"]) for g in merge_sample)
    s_sex = Counter(p["sex"] for p in split_sample)
    s_attr = Counter(p["diff_attr"] for p in split_sample)
    s_cross = sum(p["cross_style"] for p in split_sample)

    pool_merge_sex = Counter(g["sex"] for g in mpool)
    pool_split = {f"{a}/{s}": {"total": len(v), "cross": sum(x["cross_style"] for x in v)}
                  for (a, s), v in sorted(by_attr_sex.items())}

    guard = (f" + same-person guard (id-gap {args.id_gap})") if args.id_gap > 0 else ""
    default_label = f"{args.lens.capitalize()} lens · after-only + sex-balanced{guard}"
    frame = {
        "experiment": args.exp,
        "label": args.label or default_label,
        "lens": LENS_LABELS[args.lens],
        "lens_attrs": lens,
        "seed": args.seed,
        "registered_seed": SEED,
        "after_only": True,
        "min_after_views": MIN_AFTER_VIEWS,
        "sex_balanced": True,
        "id_gap_filter": args.id_gap > 0,
        "id_gap": args.id_gap,
        "same_person_excluded": {
            "split_pairs": n_same_person_pairs,
            "merge_members": merge_drop["members_dropped"],
            "merge_groups_collapsed": merge_drop["groups_dropped"],
        },
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_sources_eligible": len(records),
        "targets": {
            "merge_groups": args.merge_per_sex * 2, "merge_per_sex": args.merge_per_sex,
            "merge_cross_frac": MERGE_CROSS_FRAC,
            "split_pairs": args.split_per_sex * 2, "split_per_sex": args.split_per_sex,
            "split_alloc_total": {**alloc,
                                  "other": max(0, args.split_per_sex * 2 - sum(alloc.values()))},
            "split_cross_frac": SPLIT_CROSS_FRAC,
        },
        "labelers": rater_ids,
        "shared_size": args.shared_size,
        "reuse": {
            "from": reuse_from,
            "labeled_prior": len(labeled_prior),
            "matched_in_pool": len(reuse_matched),
            "pinned_merge": len(pinned_merge),
            "pinned_split": len(pinned_split),
            "pre_covered": len(assignment["pre_covered"]),
            "unmatched": reuse_unmatched,
        },
        "assignment": assignment,
        "pools": {
            "merge": {"n_groups_adjudicable": len(mpool), "by_sex": dict(pool_merge_sex)},
            "split": {"n_pairs_same_sex": sum(len(v) for v in by_attr_sex.values()),
                      "n_pairs_mixed_excluded": n_mixed_pairs,
                      "n_pairs_same_person_excluded": n_same_person_pairs,
                      "by_attr_sex": pool_split},
        },
        "realized": {
            "merge": {"n": len(merge_sample), "by_sex": dict(m_sex), "cross": m_cross,
                      "intra": len(merge_sample) - m_cross, "size_buckets": dict(m_sizes)},
            "split": {"n": len(split_sample), "by_sex": dict(s_sex), "cross": s_cross,
                      "by_attr": dict(s_attr)},
        },
        "merge_sample": merge_sample,
        "split_sample": split_sample,
    }

    out = args.out or os.path.join(EXP_ROOT, args.exp, "frame.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(frame, f, indent=2, ensure_ascii=False, default=str)

    print(f"experiment: {args.exp}  (lens={args.lens}/{LENS_LABELS[args.lens]}, "
          f"id_gap={args.id_gap}, seed={args.seed})")
    print(f"eligible (after>={MIN_AFTER_VIEWS}, sexed) sources: {len(records)}")
    if args.id_gap > 0:
        print(f"same-person guard: excluded {n_same_person_pairs} split pairs, "
              f"{merge_drop['members_dropped']} merge members "
              f"({merge_drop['groups_dropped']} groups collapsed to <2)")
    print(f"MERGE pool: {len(mpool)} adjudicable groups by sex={dict(pool_merge_sex)}")
    print(f"  drawn: {len(merge_sample)} groups  sex={dict(m_sex)} "
          f"cross={m_cross} intra={len(merge_sample)-m_cross} sizes={dict(m_sizes)}")
    print(f"SPLIT pool: {sum(len(v) for v in by_attr_sex.values())} same-sex pairs "
          f"({n_mixed_pairs} mixed-sex excluded)")
    print(f"  drawn: {len(split_sample)} pairs  sex={dict(s_sex)} cross={s_cross} "
          f"by_attr={dict(s_attr)}")
    if reuse_from:
        print(f"REUSE from {reuse_from}: {len(labeled_prior)} prior-labelled items, "
              f"{len(reuse_matched)} matched the pool "
              f"(pinned merge={len(pinned_merge)}, split={len(pinned_split)}; "
              f"unmatched={len(reuse_unmatched)})")
    n_frame = len(merge_sample) + len(split_sample)
    cov = {rid: len(ids) for rid, ids in assignment["coverage"].items()}
    per_labeler = {rid: len(assignment["shared"]) + n for rid, n in cov.items()}
    print(f"ASSIGNMENT: frame={n_frame} items  shared={len(assignment['shared'])} "
          f"(all {len(rater_ids)} labelers)  pre_covered={len(assignment['pre_covered'])}")
    print(f"  fresh coverage/labeler={cov}  -> total/labeler={per_labeler}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
