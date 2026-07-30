#!/usr/bin/env python3
"""Decision-support analysis for the true-hairstyle adjudication experiment.

Computes:
  1. A -> C : how well attribute (sub)sets predict the style category
              (majority-vote accuracy, H(C|S), normalized).
  2. C -> A : how much the style category determines each attribute
              (mutual information, fraction of attribute entropy explained).
  3. Collision-group ladder (false-MERGE candidates): for several attribute
     sets S, the collision structure + intra/cross-style composition + a
     'labelability' lens (multi-view support per member).
  4. Hamming-1 neighbor pool (false-SPLIT candidates): pairs of sources whose
     attribute vectors differ in exactly one attribute, by attribute and by
     intra/cross-style.

Outputs analysis/predictive.json + stdout.
"""
import sqlite3, json, math, os, itertools
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "data", "index.sqlite")

STYLE_NAME = "basestyle"
GROUPS = {
    "shape":  ["length", "basestyle_type", "curl", "bang", "side", "partition",
               "hair_width", "natural_curl"],
    "color":  ["color", "melanin_color", "black_colorize", "decolorize_history",
               "water_repellency", "patch_test", "damage"],
    "scalp":  ["loss", "exceptional"],
    "person": ["sex", "age"],
    "rating": ["user_satisfied", "designer_satisfied"],
}
ATTRS = [a for g in GROUPS.values() for a in g]
VISUAL = GROUPS["shape"] + GROUPS["color"] + GROUPS["scalp"]   # 17, the "look"
MISS = "\u2205"

def load():
    con = sqlite3.connect(DB)
    cols = [STYLE_NAME] + ATTRS
    sel = ", ".join(f'MAX("{c}") AS "{c}"' for c in cols) + ", COUNT(*) AS nv"
    rows = []
    for r in con.execute(f"SELECT source, {sel} FROM images GROUP BY source"):
        src, nv = r[0], r[-1]
        vals = {c: (MISS if (v is None or v == "") else str(v))
                for c, v in zip(cols, r[1:-1])}
        rows.append((src, vals, nv))
    con.close()
    return rows

def H(counts):
    tot = sum(counts)
    return -sum((c/tot)*math.log2(c/tot) for c in counts if c > 0)

def key(vals, S):
    return tuple(vals[a] for a in S)

# ---- 1. A -> C --------------------------------------------------------------
def predict_category(records, S, name):
    """Group sources by S; predict each group's category = its majority style.
    Returns majority-vote accuracy and H(C|S)."""
    groups = defaultdict(Counter)
    for _, v, _ in records:
        groups[key(v, S)][v[STYLE_NAME]] += 1
    n = len(records)
    correct = 0
    hcs = 0.0
    pure = 0
    for g, cc in groups.items():
        m = sum(cc.values())
        correct += max(cc.values())
        hcs += (m/n) * H(list(cc.values()))
        if len(cc) == 1:
            pure += m
    return {"name": name, "n_attrs": len(S), "n_groups": len(groups),
            "majority_accuracy": correct/n, "H_C_given_S": hcs,
            "frac_in_style_pure_group": pure/n}

# ---- 2. C -> A (and component MI, symmetric) --------------------------------
def mi_attr_category(records):
    n = len(records)
    cC = Counter(v[STYLE_NAME] for _, v, _ in records)
    HC = H(list(cC.values()))
    out = {}
    for a in ATTRS:
        cA = Counter(v[a] for _, v, _ in records)
        HA = H(list(cA.values()))
        cJ = Counter((v[a], v[STYLE_NAME]) for _, v, _ in records)
        HJ = H(list(cJ.values()))
        mi = HA + HC - HJ
        out[a] = {"H_attr": HA, "MI_with_category": mi,
                  "frac_attr_explained_by_C": mi/HA if HA > 0 else 0.0,
                  "frac_C_explained_by_attr": mi/HC if HC > 0 else 0.0}
    return HC, out

# ---- 3. collision-group ladder (false-merge candidates) ---------------------
def collision_ladder(records):
    ladders = {
        "ALL_21": ATTRS,
        "VISUAL_17": VISUAL,
        "shape+color_15": GROUPS["shape"] + GROUPS["color"],
        "shape_8": GROUPS["shape"],
        "VISUAL_minus_chem_11": GROUPS["shape"] + ["color", "damage"] + GROUPS["scalp"],
    }
    res = {}
    for name, S in ladders.items():
        groups = defaultdict(list)
        for src, v, nv in records:
            groups[key(v, S)].append((src, v, nv))
        col = {k: g for k, g in groups.items() if len(g) > 1}
        in_col = sum(len(g) for g in col.values())
        sizes = Counter(len(g) for g in col.values())
        intra = cross = 0
        cross_src = 0
        multiview_ok = 0  # groups where every member has >=20 views (adjudicable)
        for g in col.values():
            styles = {r[1][STYLE_NAME] for r in g}
            if len(styles) == 1:
                intra += 1
            else:
                cross += 1
                cross_src += len(g)
            if all(r[2] >= 20 for r in g):
                multiview_ok += 1
        res[name] = {
            "n_attrs": len(S), "n_sources_in_collision": in_col,
            "frac_in_collision": in_col/len(records),
            "n_groups": len(col), "intra_groups": intra, "cross_groups": cross,
            "sources_in_cross_groups": cross_src,
            "groups_all_members_multiview": multiview_ok,
            "size_hist": dict(sorted(sizes.items())),
        }
    return res

# ---- 4. Hamming-1 neighbor pool (false-split candidates) --------------------
def hamming1_pool(records, S, name):
    """Count source-pairs whose S-vectors differ in EXACTLY one attribute.
    Trick: for each position p, group by (all attrs except p); within a group,
    pairs with different p-value differ at exactly p (agree elsewhere)."""
    n = len(records)
    by_attr = {}
    total_pairs = 0
    cross_pairs = 0
    for pi, p in enumerate(S):
        rest = [a for a in S if a != p]
        buckets = defaultdict(list)
        for src, v, nv in records:
            buckets[key(v, rest)].append(v)
        pairs = 0
        cpairs = 0
        for vs in buckets.values():
            if len(vs) < 2:
                continue
            pv = Counter(x[p] for x in vs)
            m = len(vs)
            same = sum(c*(c-1)//2 for c in pv.values())
            allp = m*(m-1)//2
            diff = allp - same          # pairs differing exactly at p
            pairs += diff
            # cross-style among those diff pairs
            sv = Counter(x[STYLE_NAME] for x in vs)
            same_style = sum(c*(c-1)//2 for c in sv.values())
            # approximate cross-style diff-at-p pairs:
            # count pairs differing at p AND differing style
            # exact: build joint counts of (p_value, style)
            jc = Counter((x[p], x[STYLE_NAME]) for x in vs)
            same_p_or_style = 0
            # pairs sharing p value (already 'same') -> not diff; we want diff-at-p
            # diff-at-p pairs = total - same_p ; among diff-at-p, cross-style =
            # diff-at-p total minus diff-at-p-but-same-style
            # same-style pairs that also differ at p:
            byst = defaultdict(Counter)
            for (pp, ss), c in jc.items():
                byst[ss][pp] += c
            same_style_diff_p = 0
            for ss, pc in byst.items():
                msst = sum(pc.values())
                samep = sum(c*(c-1)//2 for c in pc.values())
                same_style_diff_p += msst*(msst-1)//2 - samep
            cpairs += diff - same_style_diff_p
        by_attr[p] = {"hamming1_pairs": pairs, "cross_style": cpairs}
        total_pairs += pairs
        cross_pairs += cpairs
    return {"name": name, "n_attrs": len(S), "total_hamming1_pairs": total_pairs,
            "cross_style_pairs": cross_pairs, "by_attribute": by_attr}

def main():
    records = load()
    n = len(records)
    print(f"N={n} sources\n")
    out = {"n": n}

    print("== 1. A -> C (attribute sets predicting style category) ==")
    HC = H(list(Counter(v[STYLE_NAME] for _, v, _ in records).values()))
    print(f"  H(C) baseline = {HC:.2f} bits, base rate (largest style) = "
          f"{max(Counter(v[STYLE_NAME] for _,v,_ in records).values())/n:.3f}")
    a2c = []
    for name, S in [("ALL_21", ATTRS), ("VISUAL_17", VISUAL),
                    ("shape+color_15", GROUPS['shape']+GROUPS['color']),
                    ("shape_8", GROUPS['shape']), ("curl+length+bang+side", ["curl","length","bang","side"])]:
        r = predict_category(records, S, name)
        a2c.append(r)
        print(f"  {name:22s} acc={r['majority_accuracy']:.3f} "
              f"H(C|S)={r['H_C_given_S']:.2f}b purefrac={r['frac_in_style_pure_group']:.3f} "
              f"groups={r['n_groups']}")
    out["A_to_C"] = a2c; out["H_C"] = HC

    print("\n== 2. C -> A (style category determining each attribute) ==")
    HC2, mi = mi_attr_category(records)
    out["C_to_A"] = mi
    for a, d in sorted(mi.items(), key=lambda kv: -kv[1]["frac_attr_explained_by_C"]):
        print(f"  {a:18s} MI={d['MI_with_category']:.2f}b  "
              f"attr_explained_by_C={d['frac_attr_explained_by_C']*100:5.1f}%  "
              f"C_explained_by_attr={d['frac_C_explained_by_attr']*100:5.1f}%")

    print("\n== 3. Collision-group ladder (false-MERGE candidates) ==")
    cl = collision_ladder(records); out["collision_ladder"] = cl
    for name, r in cl.items():
        print(f"  {name:22s} k={r['n_attrs']:2d} inColl={r['n_sources_in_collision']:5d} "
              f"({r['frac_in_collision']*100:4.1f}%) groups={r['n_groups']:4d} "
              f"intra={r['intra_groups']:4d} cross={r['cross_groups']:4d} "
              f"multiviewOK={r['groups_all_members_multiview']:4d}")

    print("\n== 4. Hamming-1 neighbor pool (false-SPLIT candidates) ==")
    for name, S in [("ALL_21", ATTRS), ("VISUAL_17", VISUAL)]:
        h = hamming1_pool(records, S, name)
        out.setdefault("hamming1", {})[name] = h
        print(f"  [{name}] total Hamming-1 pairs={h['total_hamming1_pairs']} "
              f"cross-style={h['cross_style_pairs']}")
        top = sorted(h["by_attribute"].items(), key=lambda kv:-kv[1]["hamming1_pairs"])[:8]
        for a, d in top:
            print(f"     differ only in {a:18s}: {d['hamming1_pairs']:5d} pairs "
                  f"({d['cross_style']} cross-style)")

    with open(os.path.join(HERE, "predictive.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nWrote {os.path.join(HERE,'predictive.json')}")

if __name__ == "__main__":
    main()
