#!/usr/bin/env python3
"""Pull concrete, English-glossed example groups/pairs to illustrate the
S-choice (false-merge collision groups + false-split Hamming-1 neighbors)."""
import sqlite3, os, sys, json
from collections import defaultdict, Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "data", "index.sqlite")
sys.path.insert(0, os.path.join(HERE, "..", "ui"))
from translations import value_en, FIELD_LABELS, CATEGORY_EN  # noqa

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

def g(field, val):
    e = value_en(field, val)
    return str(e) if e is not None else str(val)

def load():
    con = sqlite3.connect(DB)
    cols = ["basestyle", "category_id"] + ATTRS
    sel = ", ".join(f'MAX("{c}") AS "{c}"' for c in cols) + ", COUNT(*) nv"
    rows = []
    for r in con.execute(f"SELECT source, {sel} FROM images GROUP BY source"):
        src, nv = r[0], r[-1]
        vals = {c: (MISS if (v is None or v == "") else str(v)) for c, v in zip(cols, r[1:-1])}
        rows.append((src, vals, nv))
    con.close()
    return rows

def style_en(v):
    return CATEGORY_EN.get(v["category_id"], v["basestyle"])

def collision_groups(records, S):
    b = defaultdict(list)
    for src, v, nv in records:
        b[tuple(v[a] for a in S)].append((src, v, nv))
    return {k: g for k, g in b.items() if len(g) > 1}

def show_group(grp, S):
    members = [{"source": s, "style": style_en(v), "age": v["age"], "views": nv}
               for s, v, nv in grp]
    shared = {FIELD_LABELS.get(a, a): g(a, grp[0][1][a]) for a in S}
    return {"n": len(grp), "styles": sorted({m["style"] for m in members}),
            "members": members, "shared_attrs": shared}

def hamming1_example(records, S, diff_attr, want_cross=True):
    rest = [a for a in S if a != diff_attr]
    b = defaultdict(list)
    for src, v, nv in records:
        b[tuple(v[a] for a in rest)].append((src, v, nv))
    for vs in b.values():
        if len(vs) < 2:
            continue
        for i in range(len(vs)):
            for j in range(i+1, len(vs)):
                a, c = vs[i], vs[j]
                if a[1][diff_attr] == c[1][diff_attr]:
                    continue
                cross = style_en(a[1]) != style_en(c[1])
                if want_cross and not cross:
                    continue
                return {
                    "diff_attr": FIELD_LABELS.get(diff_attr, diff_attr),
                    "cross_style": cross,
                    "a": {"source": a[0], "style": style_en(a[1]),
                          "val": g(diff_attr, a[1][diff_attr]), "views": a[2]},
                    "b": {"source": c[0], "style": style_en(c[1]),
                          "val": g(diff_attr, c[1][diff_attr]), "views": c[2]},
                    "identical_on": {FIELD_LABELS.get(x, x): g(x, a[1][x]) for x in rest},
                }
    return None

def main():
    records = load()
    out = {}

    # 1. ALL_21 cross-style collision group (biggest)
    col21 = collision_groups(records, ATTRS)
    cross21 = [grp for grp in col21.values()
               if len({style_en(v) for _, v, _ in grp}) > 1]
    cross21.sort(key=len, reverse=True)
    out["all21_cross_group"] = show_group(cross21[0], ATTRS)

    # 2. ALL_21 intra-style collision group
    intra21 = [grp for grp in col21.values()
               if len({style_en(v) for _, v, _ in grp}) == 1]
    intra21.sort(key=len, reverse=True)
    out["all21_intra_group"] = show_group(intra21[0], ATTRS)

    # 3. shape-only biggest group (too coarse illustration)
    cols8 = collision_groups(records, GROUPS["shape"])
    biggest8 = max(cols8.values(), key=len)
    out["shape8_group"] = show_group(biggest8, GROUPS["shape"])
    out["shape8_group"]["style_counts"] = dict(Counter(style_en(v) for _, v, _ in biggest8))

    # 4. Hamming-1 examples under ALL_21
    out["h1_curl_all21"] = hamming1_example(records, ATTRS, "curl", want_cross=True)
    out["h1_age_all21"] = hamming1_example(records, ATTRS, "age", want_cross=True)
    # 5. Hamming-1 under VISUAL (curl)
    out["h1_curl_visual"] = hamming1_example(records, VISUAL, "curl", want_cross=True)

    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    with open(os.path.join(HERE, "examples.json"), "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)

if __name__ == "__main__":
    main()
