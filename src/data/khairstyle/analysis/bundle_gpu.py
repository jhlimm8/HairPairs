#!/usr/bin/env python3
"""Compute the minimal file set to ship to a GPU box for the baseline runs.

Outputs (into analysis/):
  * gpu_needed_files.txt  -- image paths (relative to data/extracted) to rsync
  * train_manifest.json   -- [{gid, path, source, basestyle}] for the trained baseline
It copies NOTHING; the transfer step uses `rsync --files-from=gpu_needed_files.txt`.

Needed images = the canonical view triples for (a) every adjudicated EVAL source
(the pairs we score) and (b) a leakage-free TRAIN subset (mq-train sources NOT in
the eval set) used only to fit the in-domain trained baseline.
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import baselines as B  # noqa: E402  (reuse DB path, load_populations, two_triples)


def main():
    con = sqlite3.connect(B.DB)
    con.row_factory = sqlite3.Row

    pops, eval_sources = B.load_populations(con)
    eval_set = set(eval_sources)

    need = {}  # gid -> image_path
    # (a) eval sources: exactly the gids baselines.py will embed
    for s in eval_sources:
        prim, alt = B.two_triples(con, s)
        for g, p in prim + alt:
            need[g] = p
    n_eval_views = len(need)

    # (b) training subset: mq-train sources not used in eval, with a basestyle label
    rows = con.execute(
        "SELECT source, basestyle FROM images_clean "
        "WHERE split_clean='mq-train' AND basestyle IS NOT NULL AND basestyle<>'' "
        "GROUP BY source"
    ).fetchall()
    train_manifest = []
    for r in rows:
        s = r["source"]
        if s in eval_set:
            continue
        prim, alt = B.two_triples(con, s)
        for g, p in prim + alt:
            need[g] = p
            train_manifest.append({"gid": g, "path": p, "source": s,
                                   "basestyle": r["basestyle"]})
    con.close()

    files = sorted(set(need.values()))
    with open(os.path.join(HERE, "gpu_needed_files.txt"), "w") as f:
        f.write("\n".join(files) + "\n")
    json.dump(train_manifest, open(os.path.join(HERE, "train_manifest.json"), "w"))

    # report total size
    total = 0
    missing = 0
    for p in files:
        fp = os.path.join(B.EXTRACTED, p)
        if os.path.exists(fp):
            total += os.path.getsize(fp)
        else:
            missing += 1
    print(f"eval_sources      = {len(eval_sources)}")
    print(f"eval view images  = {n_eval_views}")
    print(f"train sources     = {len({t['source'] for t in train_manifest})}")
    print(f"train view images = {len(train_manifest)}")
    print(f"unique files      = {len(files)}  (missing on disk: {missing})")
    print(f"total image size  = {total/1e9:.2f} GB  (+ index.sqlite ~0.4 GB)")
    print(f"basestyle classes = {len({t['basestyle'] for t in train_manifest})}")


if __name__ == "__main__":
    main()
