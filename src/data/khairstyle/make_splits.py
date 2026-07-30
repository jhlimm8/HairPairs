#!/usr/bin/env python3
"""Derive leakage-free splits over the kHairStyle index.

The authors' train/val split leaks: 400 capture sessions (`source`) have
*different views* on both sides (same person/hairstyle/session in train AND
val). For an instance-retrieval benchmark whose ground truth includes
same-capture multi-view positives, a session straddling splits is leakage:
the benchmark requires that no capture session be shared across splits.

This script assigns every `source` to exactly ONE split and writes the result
back into `index.sqlite` as:
  * table  `splits`        -- source -> clean split (+ provenance counts)
  * view   `images_clean`  -- images joined to their clean split (`split_clean`)

No images are dropped; conflicting sessions' minority-side views are simply
relabeled to the chosen split.

Conflict policies (`--policy`):
  conflicts-to-train (DEFAULT) -- any session that appears in val at all but
      also in train goes wholly to TRAIN. Result: val ⊆ authors' val, so no
      train-origin image ever leaks into evaluation. Most conservative for eval.
  conflicts-to-val   -- the mirror: conflicting sessions go wholly to VAL.
  majority           -- session goes to whichever split held more of its views
      (ties -> train). Minimizes images relocated; mixes image origins in val.

Usage:
  python3 make_splits.py                      # conflicts-to-train
  python3 make_splits.py --policy majority
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "index.sqlite"


def choose(tr: int, va: int, policy: str) -> str:
    conflict = tr > 0 and va > 0
    if not conflict:
        return "mq-train" if tr > 0 else "mq-val"
    if policy == "conflicts-to-train":
        return "mq-train"
    if policy == "conflicts-to-val":
        return "mq-val"
    # majority (ties -> train)
    return "mq-train" if tr >= va else "mq-val"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument(
        "--policy",
        choices=["conflicts-to-train", "conflicts-to-val", "majority"],
        default="conflicts-to-train",
    )
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    cur = con.cursor()

    rows = cur.execute(
        "SELECT source, SUM(split='mq-train'), SUM(split='mq-val') "
        "FROM images GROUP BY source"
    ).fetchall()

    cur.execute("DROP VIEW IF EXISTS images_clean")
    cur.execute("DROP TABLE IF EXISTS splits")
    cur.execute(
        "CREATE TABLE splits ("
        "source TEXT PRIMARY KEY, n_train_views INTEGER, n_val_views INTEGER, "
        "was_conflict INTEGER, split_clean TEXT, relocated_views INTEGER)"
    )

    payload = []
    n_conflict = 0
    relocated_total = 0
    for source, tr, va in rows:
        tr, va = int(tr), int(va)
        conflict = 1 if (tr > 0 and va > 0) else 0
        n_conflict += conflict
        clean = choose(tr, va, args.policy)
        relocated = va if clean == "mq-train" else tr  # views leaving their origin
        if not conflict:
            relocated = 0
        relocated_total += relocated
        payload.append((source, tr, va, conflict, clean, relocated))
    cur.executemany("INSERT INTO splits VALUES (?,?,?,?,?,?)", payload)

    cur.execute(
        "CREATE VIEW images_clean AS "
        "SELECT i.*, s.split_clean FROM images i JOIN splits s USING(source)"
    )

    # ---- verification + summary ----
    leak = cur.execute(
        "SELECT COUNT(*) FROM (SELECT source FROM splits GROUP BY source "
        "HAVING COUNT(DISTINCT split_clean) > 1)"
    ).fetchone()[0]
    assert leak == 0, "clean split still leaks (should be impossible)"

    def counts(col_table):
        return dict(cur.execute(
            f"SELECT {col_table[1]}, COUNT(*) FROM {col_table[0]} "
            f"GROUP BY {col_table[1]}"
        ).fetchall())

    img_clean = dict(cur.execute(
        "SELECT split_clean, COUNT(*) FROM images_clean GROUP BY split_clean"
    ).fetchall())
    src_clean = dict(cur.execute(
        "SELECT split_clean, COUNT(*) FROM splits GROUP BY split_clean"
    ).fetchall())
    img_orig = dict(cur.execute(
        "SELECT split, COUNT(*) FROM images GROUP BY split"
    ).fetchall())

    summary = {
        "policy": args.policy,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "conflicting_sessions_resolved": n_conflict,
        "views_relocated": relocated_total,
        "leakage_sessions_after": leak,
        "sources_by_clean_split": src_clean,
        "images_by_clean_split": img_clean,
        "images_by_original_split": img_orig,
    }
    cur.executemany(
        "INSERT OR REPLACE INTO meta VALUES (?,?)",
        [(f"splits__{k}", json.dumps(v, ensure_ascii=False))
         for k, v in summary.items()],
    )
    con.commit()

    # per-category strata of the clean split (sanity / balance)
    strata = cur.execute(
        "SELECT category, "
        "SUM(split_clean='mq-train'), SUM(split_clean='mq-val') "
        "FROM images_clean GROUP BY category ORDER BY category"
    ).fetchall()
    con.close()

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\nclean split by category (train / val images):")
    for cat, tr, va in strata:
        print(f"  {cat:<16} {int(tr):>7} / {int(va):>6}")
    print("\nWrote table `splits` and view `images_clean` into", args.db)


if __name__ == "__main__":
    main()
