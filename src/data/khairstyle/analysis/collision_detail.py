#!/usr/bin/env python3
"""Characterize the actual collisions found by uniqueness_analysis.py:
who shares an attribute vector, intra- vs cross-style, concrete examples.
"""
import sqlite3, json, os
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
VISUAL = GROUPS["shape"] + GROUPS["color"] + GROUPS["scalp"]
MISS = "\u2205"

def load():
    con = sqlite3.connect(DB)
    cols = [STYLE_NAME] + ATTRS
    sel = ", ".join(f'MAX("{c}") AS "{c}"' for c in cols)
    sel += ", COUNT(*) AS n_views"
    q = f"SELECT source, {sel} FROM images GROUP BY source"
    rows = []
    for r in con.execute(q):
        src = r[0]; nv = r[-1]
        vals = {}
        for c, v in zip(cols, r[1:-1]):
            vals[c] = MISS if (v is None or v == "") else str(v)
        rows.append((src, vals, nv))
    con.close()
    return rows

def collisions(records, subset):
    g = defaultdict(list)
    for src, vals, nv in records:
        g[tuple(vals[a] for a in subset)].append((src, vals, nv))
    return {k: v for k, v in g.items() if len(v) > 1}

def summarize(records, subset, name):
    col = collisions(records, subset)
    n = len(records)
    in_col = sum(len(v) for v in col.values())
    sizes = Counter(len(v) for v in col.values())
    intra = cross = 0
    for v in col.values():
        styles = {r[1][STYLE_NAME] for r in v}
        if len(styles) == 1: intra += 1
        else: cross += 1
    print(f"\n[{name}] |S|={len(subset)} attrs")
    print(f"  sources in a collision: {in_col}/{n} ({in_col/n*100:.1f}%)  "
          f"unique: {n-in_col} ({(n-in_col)/n*100:.1f}%)")
    print(f"  collision groups: {len(col)}  (size hist: {dict(sorted(sizes.items()))})")
    print(f"  intra-style groups: {intra}  cross-style groups: {cross}")
    return {"name": name, "subset": subset, "n_in_collision": in_col,
            "frac_unique": (n-in_col)/n, "n_groups": len(col),
            "size_hist": dict(sizes), "intra_groups": intra, "cross_groups": cross,
            "groups": col}

def main():
    records = load()
    n = len(records)
    out = {"n_sources": n}

    # A. full 21-attr vector
    full = summarize(records, ATTRS, "ALL features (21)")
    out["full"] = {k: full[k] for k in full if k != "groups"}

    # examples: a few cross-style and intra-style collision groups
    examples = {"cross_style": [], "intra_style": []}
    for k, v in sorted(full["groups"].items(), key=lambda kv: -len(kv[1])):
        styles = {r[1][STYLE_NAME] for r in v}
        rec = {
            "n_sources": len(v),
            "styles": sorted(styles),
            "sources": [{"source": r[0], "n_views": r[2],
                         "basestyle": r[1][STYLE_NAME],
                         "age": r[1]["age"], "sex": r[1]["sex"]} for r in v],
            "shared_attrs": {a: v[0][1][a] for a in ATTRS},
        }
        bucket = "cross_style" if len(styles) > 1 else "intra_style"
        if len(examples[bucket]) < 6:
            examples[bucket].append(rec)
    out["examples"] = examples
    print("\n-- biggest cross-style collision groups (identical 21-attr vector, different style) --")
    for r in examples["cross_style"][:4]:
        print(f"  {r['n_sources']} sources, styles={r['styles']}, "
              f"srcs={[s['source'] for s in r['sources']]}")
    print("-- biggest intra-style collision groups --")
    for r in examples["intra_style"][:4]:
        print(f"  {r['n_sources']} sources in '{r['styles'][0]}', "
              f"srcs={[s['source'] for s in r['sources']]}")

    # B. visual-only (no person/rating) collapse
    summ_vis = summarize(records, VISUAL, "VISUAL only (17, no person/rating)")
    out["visual"] = {k: summ_vis[k] for k in summ_vis if k != "groups"}

    # C. shape-only worst group
    shape = collisions(records, GROUPS["shape"])
    biggest = max(shape.values(), key=len)
    bstyles = Counter(r[1][STYLE_NAME] for r in biggest)
    print(f"\n[SHAPE only (8)] biggest collision group = {len(biggest)} sources")
    print(f"  shared shape attrs: " +
          ", ".join(f"{a}={biggest[0][1][a]}" for a in GROUPS['shape']))
    print(f"  spanning styles: {dict(bstyles)}")
    out["shape_biggest_group"] = {
        "n_sources": len(biggest),
        "shared": {a: biggest[0][1][a] for a in GROUPS["shape"]},
        "styles": dict(bstyles),
    }

    # D. cross-style identical-attribute pairs (does full feature set determine style?)
    cross_groups = [v for v in full["groups"].values()
                    if len({r[1][STYLE_NAME] for r in v}) > 1]
    n_cross_pairs = sum(len(v)-1 for v in cross_groups)
    print(f"\n[FD check] sources with identical 21-attr vector but DIFFERENT style: "
          f"{len(cross_groups)} groups -> features do NOT determine style")
    out["cross_style_groups"] = len(cross_groups)

    with open(os.path.join(HERE, "collisions.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nWrote {os.path.join(HERE,'collisions.json')}")

if __name__ == "__main__":
    main()
