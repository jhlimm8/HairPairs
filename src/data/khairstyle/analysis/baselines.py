#!/usr/bin/env python3
"""Frozen-encoder verification baselines for [HairPairs] (paper Experiments).

We mirror exactly what the human raters saw: the SAME canonical de-identified
views per source (frontal / near-profile / back, from views.pick_views), full
frame, no cropping. Each view is embedded by a frozen encoder; a source is the
L2-normalised mean of its canonical view triple. Pairs are compared by cosine
similarity.

LABEL-BASED, CROSS-SESSION EVALUATION
-------------------------------------
Every scored pair is one of our human adjudications, and every pair is
cross-session (two different sources). We do NOT fabricate positives from
same-session view splits, and we do NOT score random easy-negatives -- the
evaluation is exactly the labels the dataset ships.

  positives (rater said SAME, n=10):
      * crosswearer_pos -- within-cluster pairs of an attribute-identical group
                           the rater merged (same style, different wearer; the
                           crux slice, n=5)
      * miss_same       -- one-attribute-apart pairs judged the same (n=5)
  negatives (rater said DIFFERENT, n=548):
      * collision_hardneg -- cross-cluster pairs of an attribute-identical group
                             (share every shape attribute, judged different)
      * miss_diff         -- one-attribute-apart pairs judged different

Encoders:
  * attribute-lookup  -- predict 'same' iff the shape attributes match (the
                         floor; a fixed (TAR, FAR) point, no threshold sweep).
  * dinov2            -- timm ViT-B/14 DINOv2 (self-supervised).
  * dinov2_large      -- timm ViT-L/14 DINOv2 (scale control).
  * clip              -- open_clip ViT-B/16 (OpenAI, language-image).
  * siglip            -- open_clip ViT-B/16 SigLIP (WebLI, language-image).
  * dinov2_trained    -- in-domain head on frozen DINOv2-B, supervised on the
                         schema's own basestyle labels (schema-supervision
                         control; produced by train_baseline.py).

Metrics (imbalance-aware; all over the adjudicated labels):
  * ROC-AUC and Average Precision for (positives vs negatives), prevalence
    stated (~1.8%).
  * Operating point: threshold at a fixed FAR on the NEGATIVE pool; report TAR
    over the positives and negative rejection.
  * Cross-wearer probe (n=5): each cross-wearer positive's cosine and its rank
    vs the negative distribution; 'k of 5 above the operating point'.
  * Raw per-pair cosines are dumped per encoder so any later metric/figure
    change is CPU-only and never needs the GPU again.

Outputs: analysis/baselines.json  and  analysis/fig_distance_dist.png
Embeddings cached in analysis/emb_cache/<encoder>.npz keyed by gid.

    python -m baselines                                   # dinov2 + clip
    python -m baselines --encoders dinov2 dinov2_large clip siglip dinov2_trained
    python -m baselines --device cuda --encoders clip     # force a device
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from itertools import combinations

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
KH = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(KH, "adjudicate"))
from views import pick_views  # noqa: E402
from views import circ_dist, PREF_VERTICAL, SLOTS  # noqa: E402

DB = os.path.join(KH, "data", "index.sqlite")
EXTRACTED = os.path.join(KH, "data", "extracted")
CACHE = os.path.join(HERE, "emb_cache")
EXP = "attr-suff-v4"
PRIMARY = "L0"
SHAPE_ATTRS = ["length", "basestyle_type", "curl", "bang", "side",
               "partition", "hair_width", "natural_curl"]
SEED = 20260616


# ---------------------------------------------------------------- data / pairs
def load_populations(con):
    """Return (pops, sources) for the label-based, cross-session evaluation.

    pops holds only adjudicated pairs (every pair is two distinct sources):
      collision_hardneg, crosswearer_pos  (from the merge/attribute-identical task)
      miss_same, miss_diff                (from the split/one-attribute-apart task)
    sources is every source that appears in any adjudicated pair.
    """
    fr = json.load(open(os.path.join(KH, "adjudicate", "experiments", EXP, "frame.json")))
    sm = {p["pair_id"]: p for p in fr["split_sample"]}

    coll_hardneg, crosswearer_pos = [], []
    group_of = {}                      # source -> merge group_id
    for r in con.execute("SELECT item_id, verdict FROM adjudications "
                         "WHERE experiment=? AND kind='merge' AND rater=?", (EXP, PRIMARY)):
        idx = {}
        for ci, cl in enumerate(json.loads(r["verdict"]).get("clusters", [])):
            for s in cl:
                idx[s] = ci
                group_of[s] = r["item_id"]
        ss = list(idx)
        for a, b in combinations(ss, 2):
            (crosswearer_pos if idx[a] == idx[b] else coll_hardneg).append((a, b))

    miss_same, miss_diff = [], []
    for r in con.execute("SELECT item_id, verdict FROM adjudications "
                         "WHERE experiment=? AND kind='split' AND rater=?", (EXP, PRIMARY)):
        p = sm.get(r["item_id"])
        if not p:
            continue
        pair = (p["a"]["source"], p["b"]["source"])
        (miss_same if json.loads(r["verdict"]).get("relation") == "same" else miss_diff).append(pair)

    pops = {"collision_hardneg": coll_hardneg, "crosswearer_pos": crosswearer_pos,
            "miss_same": miss_same, "miss_diff": miss_diff}
    all_srcs = set(group_of) | {s for p in miss_same + miss_diff for s in p}
    return pops, sorted(all_srcs)


def canonical_views(con, source):
    rows = [dict(r) for r in con.execute(
        "SELECT gid, view, horizontal, vertical, front, image_path FROM images "
        "WHERE source=? AND before_after='after'", (source,))]
    picked = pick_views(rows)
    path = {r["gid"]: r["image_path"] for r in rows}
    return [(v["gid"], path[v["gid"]]) for slot, v in picked.items()]


def two_triples(con, source):
    """Two DISJOINT frontal/profile/back triples for one source: the best view per
    slot (the primary template) and the second-best (an alternate template).

    The evaluation now uses only the PRIMARY triple. The alternate triple is kept
    so bundle_gpu.py / train_baseline.py stay API-compatible; callers may ignore it.
    """
    rows = [dict(r) for r in con.execute(
        "SELECT gid, view, horizontal, vertical, front, image_path FROM images "
        "WHERE source=? AND before_after='after'", (source,))]
    rows = [r for r in rows if r.get("horizontal") is not None]
    path = {r["gid"]: r["image_path"] for r in rows}

    def ranked(target):
        return sorted(rows, key=lambda r: (
            circ_dist(r["horizontal"], target),
            0 if r.get("vertical") == PREF_VERTICAL else 1,
            str(r.get("view") or "")))
    primary, alt, used = [], [], set()
    for slot, target in SLOTS:
        rk = [r for r in ranked(target) if r["gid"] not in used]
        if not rk:
            continue
        primary.append(rk[0]["gid"]); used.add(rk[0]["gid"])
        rk2 = [r for r in ranked(target) if r["gid"] not in used]
        if rk2:
            alt.append(rk2[0]["gid"]); used.add(rk2[0]["gid"])
    prim = [(g, path[g]) for g in primary]
    altl = [(g, path[g]) for g in alt]
    return prim, altl


# ---------------------------------------------------------------- device
def pick_device(requested=None):
    import torch
    if requested and requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# ---------------------------------------------------------------- encoders
def embed_all(gid_paths, encoder, device):
    """Return {gid: np.float32 embedding (L2-normalised)}, cached to disk."""
    os.makedirs(CACHE, exist_ok=True)
    cpath = os.path.join(CACHE, f"{encoder}.npz")
    cache = {}
    if os.path.exists(cpath):
        z = np.load(cpath)
        cache = {k: z[k] for k in z.files}
    todo = [(g, p) for g, p in gid_paths if g not in cache]
    if todo:
        import torch
        from PIL import Image
        model, preprocess = build_model(encoder, device)
        bs = 32
        with torch.no_grad():
            for i in range(0, len(todo), bs):
                chunk = todo[i:i + bs]
                ims = []
                for g, p in chunk:
                    im = Image.open(os.path.join(EXTRACTED, p)).convert("RGB")
                    ims.append(preprocess(im))
                x = torch.stack(ims).to(device)
                feats = model(x) if encoder.startswith("dinov2") else model.encode_image(x)
                feats = feats.float().cpu().numpy()
                for (g, _), f in zip(chunk, feats):
                    f = f / (np.linalg.norm(f) + 1e-8)
                    cache[g] = f.astype(np.float32)
                print(f"  [{encoder}] embedded {min(i+bs,len(todo))}/{len(todo)}", end="\r")
        print()
        np.savez(cpath, **cache)
    return cache


TIMM_BACKBONES = {
    "dinov2": "vit_base_patch14_dinov2.lvd142m",        # ViT-B/14 self-supervised
    "dinov2_large": "vit_large_patch14_dinov2.lvd142m",  # ViT-L/14 self-supervised
}
OPENCLIP_BACKBONES = {
    "clip": ("ViT-B-16", "openai"),                  # OpenAI language-image
    "siglip": ("ViT-B-16-SigLIP", "webli"),          # SigLIP language-image (WebLI)
}
# SigLIP triggers a hard Metal/MPS buffer assertion on Apple's torch build, so we
# fall back to CPU there ONLY. On CUDA it runs on the GPU like everything else.
FORCE_CPU_ON_MPS = {"siglip"}


def build_model(encoder, device):
    import torch  # noqa: F401
    if encoder in TIMM_BACKBONES:
        import timm
        m = timm.create_model(TIMM_BACKBONES[encoder], pretrained=True, num_classes=0)
        m = m.eval().to(device)
        cfg = timm.data.resolve_data_config({}, model=m)
        tf = timm.data.create_transform(**cfg)
        return m, tf
    if encoder in OPENCLIP_BACKBONES:
        import open_clip
        name, pretrained = OPENCLIP_BACKBONES[encoder]
        m, _, pre = open_clip.create_model_and_transforms(name, pretrained=pretrained)
        return m.eval().to(device), pre
    raise ValueError(encoder)


# ---------------------------------------------------------------- metrics
def _template(view_emb, gids):
    vs = [view_emb[g] for g, _ in gids if g in view_emb]
    if not vs:
        return None
    m = np.mean(vs, axis=0)
    return m / (np.linalg.norm(m) + 1e-8)


def source_embeddings(con, sources, view_emb):
    """source -> primary template (mean of the best frontal/profile/back triple)."""
    src_emb = {}
    for s in sources:
        prim, _alt = two_triples(con, s)
        pe = _template(view_emb, prim)
        if pe is not None:
            src_emb[s] = pe
    return src_emb


def cos(a, b):
    return float(np.dot(a, b))


def pair_scores(pairs, src_emb):
    """[{pair, cos}] for pairs whose both sources have an embedding."""
    out = []
    for a, b in pairs:
        if a in src_emb and b in src_emb:
            out.append({"pair": [a, b], "cos": round(cos(src_emb[a], src_emb[b]), 6)})
    return out


def _vals(scores):
    return np.array([s["cos"] for s in scores], dtype=np.float64)


def auc_ap(pos, neg):
    from sklearn.metrics import roc_auc_score, average_precision_score
    if len(pos) == 0 or len(neg) == 0:
        return None
    y = np.r_[np.ones(len(pos)), np.zeros(len(neg))]
    s = np.r_[pos, neg]
    return {"roc_auc": float(roc_auc_score(y, s)),
            "average_precision": float(average_precision_score(y, s)),
            "prevalence": float(len(pos) / (len(pos) + len(neg))),
            "n_pos": int(len(pos)), "n_neg": int(len(neg))}


def operating_point(pos, neg, far=0.10):
    """Threshold at FAR=far on the NEGATIVE (adjudicated 'different') pool; report
    TAR over positives and negative rejection."""
    thr = float(np.quantile(neg, 1 - far))
    return {"far_target": far, "threshold": thr,
            "tar": float(np.mean(pos >= thr)),
            "neg_reject": float(np.mean(neg < thr)),
            "n_pos": int(len(pos)), "n_neg": int(len(neg))}


def crosswearer_probe(cw_sims, neg, thr):
    ranks = [float(np.mean(neg < s)) for s in cw_sims]   # fraction of negatives below
    return {"n": int(len(cw_sims)),
            "cos": [round(float(s), 4) for s in cw_sims],
            "frac_neg_below": [round(r, 4) for r in ranks],
            "k_above_operating_point": int(np.sum(cw_sims >= thr))}


def attribute_lookup(con, pops):
    """Shape-attribute match as a recogniser: predict 'same' iff all shape attrs
    equal. This is a fixed (TAR, FAR) point on the label set, not a threshold sweep."""
    attr = {}
    for r in con.execute("SELECT source," + ",".join(SHAPE_ATTRS) +
                         " FROM images GROUP BY source"):
        attr[r[0]] = tuple(r[i + 1] for i in range(len(SHAPE_ATTRS)))

    def match(pair):
        a, b = pair
        return int(attr.get(a) == attr.get(b))

    per_pop = {}
    for name, pairs in pops.items():
        if not pairs:
            continue
        preds = [match(p) for p in pairs]
        per_pop[name] = {"n": len(preds), "pred_same_rate": float(np.mean(preds))}

    pos_pairs = pops["crosswearer_pos"] + pops["miss_same"]
    neg_pairs = pops["collision_hardneg"] + pops["miss_diff"]
    tar = float(np.mean([match(p) for p in pos_pairs])) if pos_pairs else None
    far = float(np.mean([match(p) for p in neg_pairs])) if neg_pairs else None
    return {"per_pop": per_pop, "tar": tar, "far": far,
            "n_pos": len(pos_pairs), "n_neg": len(neg_pairs)}


# ---------------------------------------------------------------- figure
def make_figure(results_by_enc, outpath):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    encs = list(results_by_enc)
    fig, axes = plt.subplots(1, len(encs), figsize=(5.4 * len(encs), 4.2), squeeze=False)
    for ax, enc in zip(axes[0], encs):
        r = results_by_enc[enc]
        d = r["_dists"]
        neg = np.array(d["neg"])
        cw = np.array(d["crosswearer_pos"])
        ms = np.array(d["miss_same"])
        allv = np.concatenate([neg, cw, ms]) if len(cw) or len(ms) else neg
        lo = float(allv.min())
        bins = np.linspace(max(-0.1, lo - 0.05), 1.0, 45)
        ax.hist(neg, bins=bins, density=True, alpha=0.5, color="#1f77b4",
                label=f"negatives (rater 'different', n={len(neg)})")
        thr = r["_op_thr"]
        ax.axvline(thr, color="#333", lw=1.6, label="op. point (FAR=10% on neg)")
        y = ax.get_ylim()[1]
        if len(cw):
            ax.plot(cw, np.full_like(cw, y * 0.10), "D", color="#2ca02c", ms=8,
                    label=f"cross-wearer pos (n={len(cw)})")
        if len(ms):
            ax.plot(ms, np.full_like(ms, y * 0.04), "s", color="#ff7f0e", ms=7,
                    label=f"one-attr-apart pos (n={len(ms)})")
        auc = r["_auc"]
        ax.set_title(f"{enc}   (pos-vs-neg AUC={auc:.2f}, TAR@10%FAR={r['_tar']:.2f})",
                     fontsize=9)
        ax.set_xlabel("cosine similarity"); ax.set_ylabel("density")
        ax.legend(fontsize=7.5, loc="upper left")
    fig.suptitle("Cross-session labels only: the 10 genuine positives (green/orange) sit inside the negative mass;\n"
                 "a threshold that rejects 90% of adjudicated negatives (vertical line) keeps few positives.",
                 fontsize=8.5)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(outpath, dpi=140)
    print("wrote", outpath)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--encoders", nargs="+", default=["dinov2", "clip"])
    ap.add_argument("--far", type=float, default=0.10)
    ap.add_argument("--device", default="auto", help="auto|cuda|mps|cpu")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    pops, sources = load_populations(con)
    n_pos = len(pops["crosswearer_pos"]) + len(pops["miss_same"])
    n_neg = len(pops["collision_hardneg"]) + len(pops["miss_diff"])
    print(f"sources={len(sources)}  POS={n_pos} (crosswearer={len(pops['crosswearer_pos'])} "
          f"+ miss_same={len(pops['miss_same'])})  NEG={n_neg} "
          f"(collision={len(pops['collision_hardneg'])} + miss_diff={len(pops['miss_diff'])})")

    # gids to embed: the primary canonical triple of every adjudicated source
    seen_g = set()
    gid_paths = []
    for s in sources:
        prim, _alt = two_triples(con, s)
        for g, p in prim:
            if g not in seen_g:
                seen_g.add(g); gid_paths.append((g, p))
    print(f"embedding {len(gid_paths)} view images ({len(sources)} sources x up to 3 views)")

    out = {"experiment": EXP, "primary": PRIMARY,
           "eval": "label-based cross-session", "encoders": {}}
    out["attribute_lookup"] = attribute_lookup(con, pops)

    device = pick_device(args.device)
    print(f"device = {device}")
    results_by_enc = {}
    for enc in args.encoders:
        enc_device = "cpu" if (enc in FORCE_CPU_ON_MPS and device == "mps") else device
        print(f"== encoder: {enc} (device={enc_device}) ==")
        view_emb = embed_all(gid_paths, enc, enc_device)
        src_emb = source_embeddings(con, sources, view_emb)

        sc = {k: pair_scores(pops[k], src_emb)
              for k in ("collision_hardneg", "crosswearer_pos", "miss_same", "miss_diff")}
        pos = np.r_[_vals(sc["crosswearer_pos"]), _vals(sc["miss_same"])]
        neg = np.r_[_vals(sc["collision_hardneg"]), _vals(sc["miss_diff"])]
        cw = _vals(sc["crosswearer_pos"])

        op = operating_point(pos, neg, far=args.far)
        sep = auc_ap(pos, neg)
        out["encoders"][enc] = {
            "sep_pos_vs_neg": sep,
            "operating_point": op,
            "crosswearer_probe": crosswearer_probe(cw, neg, op["threshold"]),
            "cosine_means": {k: (float(_vals(v).mean()) if len(v) else None)
                             for k, v in sc.items()},
            "scores": sc,   # raw per-pair cosines -> all future tweaks are CPU-only
        }
        results_by_enc[enc] = {
            "_dists": {"neg": neg.tolist(), "crosswearer_pos": cw.tolist(),
                       "miss_same": _vals(sc["miss_same"]).tolist()},
            "_op_thr": op["threshold"], "_tar": op["tar"],
            "_auc": sep["roc_auc"] if sep else float("nan")}
    con.close()

    json.dump(out, open(os.path.join(HERE, "baselines.json"), "w"), indent=2)
    print("wrote", os.path.join(HERE, "baselines.json"))
    if results_by_enc:
        make_figure(results_by_enc, os.path.join(HERE, "fig_distance_dist.png"))

    # console summary
    print("\n== SUMMARY (label-based, cross-session) ==")
    al = out["attribute_lookup"]
    print(f"attribute-lookup floor: TAR={al['tar']:.3f}  FAR={al['far']:.3f}  "
          f"(pos={al['n_pos']}, neg={al['n_neg']})")
    for enc, r in out["encoders"].items():
        s = r["sep_pos_vs_neg"]
        op = r["operating_point"]
        cwp = r["crosswearer_probe"]
        print(f"\n{enc}:")
        print(f"    pos vs neg : AUC={s['roc_auc']:.3f}  AP={s['average_precision']:.3f}  "
              f"prev={s['prevalence']:.3f}  (n_pos={s['n_pos']}, n_neg={s['n_neg']})")
        print(f"    operating pt (FAR={op['far_target']} on neg): TAR={op['tar']:.3f}")
        print(f"    cross-wearer probe (n={cwp['n']}): k_above_op={cwp['k_above_operating_point']}  "
              f"frac_neg_below={cwp['frac_neg_below']}")


if __name__ == "__main__":
    main()
