#!/usr/bin/env python3
"""Rigorous test of the hypothesis: does each hairstyle+person (`source`) have a
unique set of (salon/visual) attributes?

Unit of analysis = `source` (one capture session = one person + one haircut). All
salon attributes are constant within a source (verified separately), so each source
has a single well-defined attribute vector. Pose/camera attributes (horizontal,
vertical, front), provenance, geometry, and the within-session `before_after` flag
are excluded from "style identity".

Outputs:
  - analysis/results.json   (machine-readable, consumed by the canvas)
  - stdout report
"""
import sqlite3, json, math, itertools, random, os
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "data", "index.sqlite")
random.seed(0)

# ---- Attribute universe -----------------------------------------------------
# Grouped by semantic role. basestyle == category (1:1), kept separate as the
# "style label" rather than a feature.
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
ATTRS = [a for g in GROUPS.values() for a in g]          # 18 feature attributes
ATTR_GROUP = {a: g for g, al in GROUPS.items() for a in al}
MISS = "\u2205"   # token for NULL/empty

# ---- Load one row per source ------------------------------------------------
def load():
    con = sqlite3.connect(DB)
    cols = [STYLE_NAME] + ATTRS
    # values are constant within source (verified), so MAX() picks the value
    sel = ", ".join(f'MAX("{c}") AS "{c}"' for c in cols)
    q = f"SELECT source, {sel} FROM images GROUP BY source"
    rows = []
    for r in con.execute(q):
        src = r[0]
        vals = {}
        for c, v in zip(cols, r[1:]):
            if v is None or v == "":
                v = MISS
            vals[c] = str(v)
        rows.append((src, vals))
    con.close()
    return rows

# ---- Helpers ----------------------------------------------------------------
def key_tuple(vals, subset):
    return tuple(vals[a] for a in subset)

def group_sizes(records, subset):
    """Return Counter of collision-group sizes when grouping sources by `subset`."""
    g = Counter()
    for _, vals in records:
        g[key_tuple(vals, subset)] += 1
    return Counter(g.values())  # size-of-group histogram

def uniqueness(records, subset):
    """Return (n_distinct_vectors, n_unique_sources, max_group, frac_unique)."""
    g = Counter()
    for _, vals in records:
        g[key_tuple(vals, subset)] += 1
    n = len(records)
    sizes = list(g.values())
    n_unique = sum(1 for s in sizes if s == 1)
    return {
        "n_records": n,
        "n_distinct": len(g),
        "n_unique_sources": n_unique,
        "max_group": max(sizes),
        "frac_unique": n_unique / n,
        "size_hist": dict(Counter(sizes)),
    }

def entropy_bits(counts):
    tot = sum(counts)
    return -sum((c/tot) * math.log2(c/tot) for c in counts if c > 0)

def cond_entropy_source_given_subset(records, subset):
    """H(source | subset) in bits: residual uncertainty about which source."""
    g = Counter(key_tuple(vals, subset) for _, vals in records)
    n = len(records)
    # within a group of size m, identifying the source costs log2(m) bits
    return sum((m/n) * math.log2(m) for m in g.values())

# ---- 1. Per-attribute descriptive stats -------------------------------------
def per_attribute(records):
    n = len(records)
    out = {}
    for a in ATTRS + [STYLE_NAME]:
        counts = Counter(vals[a] for _, vals in records)
        miss = counts.get(MISS, 0)
        H = entropy_bits(list(counts.values()))
        out[a] = {
            "group": ATTR_GROUP.get(a, "style_name"),
            "cardinality": len(counts),
            "missing_frac": miss / n,
            "entropy_bits": H,
            "norm_entropy": H / math.log2(len(counts)) if len(counts) > 1 else 0.0,
            "frac_unique_alone": uniqueness(records, [a])["frac_unique"],
            "top_values": counts.most_common(5),
        }
    return out

# ---- 2/3. Full-vector uniqueness for various attribute universes ------------
def universe_tests(records):
    universes = {
        "ALL_18_features": ATTRS,
        "feat_minus_age": [a for a in ATTRS if a != "age"],
        "feat_minus_person": [a for a in ATTRS if a not in GROUPS["person"]],
        "feat_minus_person_rating": [a for a in ATTRS
                                     if a not in GROUPS["person"] + GROUPS["rating"]],
        "shape_only": GROUPS["shape"],
        "shape_plus_color": GROUPS["shape"] + GROUPS["color"],
        "ALL_plus_basestyle": [STYLE_NAME] + ATTRS,
    }
    return {name: uniqueness(records, subset) | {"attrs": subset}
            for name, subset in universes.items()}

# ---- 4. Within-style uniqueness ---------------------------------------------
def within_style(records, feats):
    """For each basestyle, how unique are sources using non-name features?"""
    by_style = defaultdict(list)
    for src, vals in records:
        by_style[vals[STYLE_NAME]].append((src, vals))
    per = {}
    tot_sources = 0
    tot_unique = 0
    worst = []
    for style, recs in by_style.items():
        u = uniqueness(recs, feats)
        per[style] = {"n_sources": u["n_records"], "frac_unique": u["frac_unique"],
                      "max_group": u["max_group"], "n_distinct": u["n_distinct"]}
        tot_sources += u["n_records"]
        tot_unique += u["n_unique_sources"]
        worst.append((style, u["frac_unique"], u["max_group"], u["n_records"]))
    worst.sort(key=lambda x: x[1])
    return {
        "feats_used": feats,
        "pooled_frac_unique": tot_unique / tot_sources,
        "per_style": per,
        "worst_styles": worst[:10],
    }

# ---- 5. Minimal Unique Column Combinations (keys) via apriori ---------------
def minimal_ucc(records, universe, kmax=6):
    """Find minimal subsets of `universe` whose grouping makes every source unique.
    If the full universe is not unique, NO key exists -> returns residual info."""
    n = len(records)
    full = uniqueness(records, universe)
    if full["max_group"] > 1:
        return {"exists": False, "full_max_group": full["max_group"],
                "full_frac_unique": full["frac_unique"],
                "residual_collisions": n - full["n_unique_sources"]}
    keys = []           # list of frozensets (minimal keys found)
    # level-wise
    level = [frozenset([a]) for a in universe]
    size = 1
    while level and size <= kmax:
        next_nonunique = []
        for cand in level:
            # prune: skip supersets of an existing minimal key
            if any(k <= cand for k in keys):
                continue
            u = uniqueness(records, list(cand))
            if u["max_group"] == 1:
                keys.append(cand)
            else:
                next_nonunique.append(cand)
        # generate next level from non-unique survivors
        size += 1
        gen = set()
        nn = next_nonunique
        for i in range(len(nn)):
            for j in range(i+1, len(nn)):
                u = nn[i] | nn[j]
                if len(u) == size:
                    if not any(k <= u for k in keys):
                        gen.add(u)
        level = list(gen)
    return {"exists": True,
            "min_key_size": min(len(k) for k in keys) if keys else None,
            "n_minimal_keys_up_to_kmax": len(keys),
            "minimal_keys": sorted([sorted(k) for k in keys], key=len)[:40],
            "kmax": kmax}

# ---- 6. Greedy minimal fingerprint + maximal non-unique subsets -------------
def greedy_fingerprint(records, universe):
    """Forward selection: greedily add attribute that most reduces H(source|S)."""
    chosen = []
    remaining = list(universe)
    curve = []
    while remaining:
        best, best_u = None, -1
        for a in remaining:
            u = uniqueness(records, chosen + [a])["frac_unique"]
            if u > best_u:
                best_u, best = u, a
        chosen.append(best); remaining.remove(best)
        u = uniqueness(records, chosen)
        curve.append({"added": best, "frac_unique": u["frac_unique"],
                      "max_group": u["max_group"], "n_distinct": u["n_distinct"],
                      "cond_entropy_bits": cond_entropy_source_given_subset(records, chosen)})
        if u["max_group"] == 1:
            break
    return {"order": chosen, "curve": curve}

def maximal_nonunique(records, universe, samples=20000):
    """Largest subsets that STILL have a collision (>=2 sources share the tuple).
    A subset is non-unique iff it contains no key. We search from the top: for
    each attribute, is universe\{a} still non-unique? Report the frontier."""
    full = uniqueness(records, universe)
    res = {"full_is_unique": full["max_group"] == 1}
    # Single-attribute drop: which single removals keep/lose uniqueness
    drops = {}
    for a in universe:
        sub = [x for x in universe if x != a]
        u = uniqueness(records, sub)
        drops[a] = {"frac_unique": u["frac_unique"], "max_group": u["max_group"],
                    "still_unique": u["max_group"] == 1}
    res["drop_one"] = drops
    # the attributes that are *individually droppable* without losing uniqueness
    res["redundant_singletons"] = [a for a, d in drops.items() if d["still_unique"]]
    res["essential_singletons"] = [a for a, d in drops.items() if not d["still_unique"]]
    return res

# ---- 7. Style-specific subset (functional dependency S -> basestyle) --------
def determines_style(records, universe, kmax=4):
    """Minimal subset S such that S functionally determines basestyle
    (grouping by S => basestyle constant in each group)."""
    def fd_ok(subset):
        g = defaultdict(set)
        for _, vals in records:
            g[key_tuple(vals, subset)].add(vals[STYLE_NAME])
        return all(len(v) == 1 for v in g.values())
    found = []
    for size in range(1, kmax+1):
        for cand in itertools.combinations(universe, size):
            if any(set(f) <= set(cand) for f in found):
                continue
            if fd_ok(list(cand)):
                found.append(cand)
        if found:
            break
    # purity per attribute: fraction of styles for which the attr is constant
    style_invariants = {}
    by_style = defaultdict(list)
    for src, vals in records:
        by_style[vals[STYLE_NAME]].append(vals)
    for a in universe:
        const_styles = sum(1 for recs in by_style.values()
                           if len({v[a] for v in recs}) == 1)
        style_invariants[a] = const_styles / len(by_style)
    return {"min_fd_size": (len(found[0]) if found else None),
            "fd_examples": [sorted(f) for f in found[:20]],
            "attr_const_within_style_frac": style_invariants}

# ---- 8. Information theory + independence null model ------------------------
def info_and_null(records, universe, n_perm=300):
    n = len(records)
    H_source = math.log2(n)
    H_marg = {a: entropy_bits(list(Counter(v[a] for _, v in records).values()))
              for a in universe}
    sum_marg = sum(H_marg.values())
    # joint entropy over the universe tuple (source-weighted)
    gj = Counter(key_tuple(v, universe) for _, v in records)
    H_joint = entropy_bits(list(gj.values()))
    total_correlation = sum_marg - H_joint  # redundancy among attributes
    obs = uniqueness(records, universe)
    obs_collisions = n - obs["n_unique_sources"]

    # Independence null: shuffle each column independently across sources.
    cols = {a: [v[a] for _, v in records] for a in universe}
    null_unique = []
    null_distinct = []
    for _ in range(n_perm):
        shuffled = {}
        for a in universe:
            c = cols[a][:]
            random.shuffle(c)
            shuffled[a] = c
        g = Counter(tuple(shuffled[a][i] for a in universe) for i in range(n))
        sizes = Counter(g.values())
        null_unique.append(sizes.get(1, 0))
        null_distinct.append(len(g))
    mu = sum(null_unique) / len(null_unique)
    var = sum((x-mu)**2 for x in null_unique) / len(null_unique)
    sd = var ** 0.5
    null_coll = [n - x for x in null_unique]
    mu_coll = sum(null_coll)/len(null_coll)
    return {
        "H_source_bits": H_source,
        "sum_marginal_entropy_bits": sum_marg,
        "H_joint_bits": H_joint,
        "total_correlation_bits": total_correlation,
        "redundancy_frac": total_correlation / sum_marg,
        "effective_distinct_profiles": 2 ** H_joint,
        "observed_unique_sources": obs["n_unique_sources"],
        "observed_collisions": obs_collisions,
        "null_mean_unique": mu,
        "null_sd_unique": sd,
        "null_mean_collisions": mu_coll,
        "z_collisions": (obs_collisions - mu_coll) / sd if sd > 0 else None,
        "null_mean_distinct": sum(null_distinct)/len(null_distinct),
        "observed_distinct": obs["n_distinct"],
        "n_perm": n_perm,
    }

# ---- main -------------------------------------------------------------------
def main():
    records = load()
    n = len(records)
    print(f"Loaded {n} sources, {len(ATTRS)} feature attributes.\n")

    results = {"n_sources": n, "attrs": ATTRS, "groups": GROUPS}

    print("== 1. Per-attribute stats ==")
    pa = per_attribute(records); results["per_attribute"] = pa
    for a in ATTRS + [STYLE_NAME]:
        s = pa[a]
        print(f"  {a:20s} card={s['cardinality']:3d} H={s['entropy_bits']:5.2f}b "
              f"normH={s['norm_entropy']:.2f} miss={s['missing_frac']:.2f} "
              f"uniq_alone={s['frac_unique_alone']:.3f}")

    print("\n== 2/3. Full-vector uniqueness by attribute universe ==")
    ut = universe_tests(records); results["universe_tests"] = ut
    for name, u in ut.items():
        print(f"  {name:26s} distinct={u['n_distinct']:5d} uniqueSrc={u['n_unique_sources']:5d} "
              f"({u['frac_unique']*100:5.1f}%) maxGroup={u['max_group']}")

    print("\n== 4. Within-style uniqueness (features excl. name) ==")
    feats_ns = [a for a in ATTRS]
    ws = within_style(records, feats_ns); results["within_style_all"] = ws
    print(f"  pooled frac_unique (ALL features within style) = {ws['pooled_frac_unique']:.3f}")
    ws_shape = within_style(records, GROUPS["shape"]); results["within_style_shape"] = ws_shape
    print(f"  pooled frac_unique (shape only within style)   = {ws_shape['pooled_frac_unique']:.3f}")
    ws_noperson = within_style(records, [a for a in ATTRS if a not in GROUPS['person']+GROUPS['rating']])
    results["within_style_visual"] = ws_noperson
    print(f"  pooled frac_unique (visual+chem, no person/rating) = {ws_noperson['pooled_frac_unique']:.3f}")

    print("\n== 5. Minimal unique column combinations (keys) ==")
    for uname in ["ALL_18_features", "feat_minus_person_rating", "shape_plus_color"]:
        uni = ut[uname]["attrs"]
        k = minimal_ucc(records, uni, kmax=5)
        results.setdefault("minimal_ucc", {})[uname] = k
        if k.get("exists"):
            print(f"  {uname:26s} min_key_size={k['min_key_size']} "
                  f"#keys(<=5)={k['n_minimal_keys_up_to_kmax']}")
        else:
            print(f"  {uname:26s} NO KEY (full not unique): residual_collisions="
                  f"{k['residual_collisions']} maxGroup={k['full_max_group']}")

    print("\n== 6. Greedy fingerprint + redundancy ==")
    gf = greedy_fingerprint(records, ATTRS); results["greedy_fingerprint"] = gf
    for step in gf["curve"]:
        print(f"  +{step['added']:18s} -> uniq={step['frac_unique']*100:5.1f}% "
              f"maxGrp={step['max_group']:3d} Hsrc|S={step['cond_entropy_bits']:.2f}b")
    mnu = maximal_nonunique(records, ATTRS); results["maximal_nonunique"] = mnu
    print(f"  full_is_unique={mnu['full_is_unique']}")
    print(f"  redundant singletons (droppable, stay unique): {mnu['redundant_singletons']}")
    print(f"  essential singletons (drop -> collision): {mnu['essential_singletons']}")

    print("\n== 7. Style-determining subset (S -> basestyle) ==")
    ds = determines_style(records, [a for a in ATTRS if a not in GROUPS['person']+GROUPS['rating']], kmax=4)
    results["determines_style"] = ds
    print(f"  minimal FD size = {ds['min_fd_size']}")
    print(f"  example minimal style-determining subsets: {ds['fd_examples'][:5]}")
    inv = sorted(ds["attr_const_within_style_frac"].items(), key=lambda x:-x[1])
    print("  most style-invariant attrs (constant within a style):")
    for a, f in inv[:8]:
        print(f"    {a:18s} const-within-style in {f*100:5.1f}% of styles")

    print("\n== 8. Information theory + independence null model ==")
    for uname in ["ALL_18_features", "feat_minus_person_rating"]:
        uni = ut[uname]["attrs"]
        info = info_and_null(records, uni, n_perm=300)
        results.setdefault("info_null", {})[uname] = info
        print(f"  [{uname}]")
        print(f"    H(source)={info['H_source_bits']:.2f}b  sum H(attr)={info['sum_marginal_entropy_bits']:.2f}b  "
              f"H(joint)={info['H_joint_bits']:.2f}b")
        print(f"    total correlation (redundancy)={info['total_correlation_bits']:.2f}b "
              f"({info['redundancy_frac']*100:.1f}% of marginal info)")
        print(f"    observed collisions={info['observed_collisions']}  "
              f"null collisions={info['null_mean_collisions']:.1f}+-{info['null_sd_unique']:.1f}  "
              f"z={info['z_collisions']}")
        print(f"    observed distinct={info['observed_distinct']}  null distinct={info['null_mean_distinct']:.1f}")

    out = os.path.join(HERE, "results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nWrote {out}")

if __name__ == "__main__":
    main()
