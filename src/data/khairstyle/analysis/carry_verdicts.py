#!/usr/bin/env python3
"""Carry prior adjudication verdicts (and notes) into a new experiment by item_id.

Verdicts in the `adjudications` table are keyed by item_id (sorted source ids)
and answer a LENS-INDEPENDENT question ("same true hairstyle?"). So any verdict
collected against an earlier frame (e.g. attr-suff-v1/v2/v3) is valid to reuse
for any later frame that contains the same item_id. This script imports the most
recent prior verdict/comment per (item_id, rater) into the target experiment so
no past labelling is wasted.

It is IDEMPOTENT: a (item_id, rater) that already has a row in the target
experiment is never overwritten. Run as many times as you like.

  python3 carry_verdicts.py                 # apply into attr-suff-v4
  python3 carry_verdicts.py --dry-run       # preview only, no writes
  python3 carry_verdicts.py --exp attr-suff-v4 --reuse-from attr-suff-v3,attr-suff-v2
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "data", "index.sqlite")
EXP_ROOT = os.path.join(HERE, "..", "adjudicate", "experiments")

REUSE_FROM = ["attr-suff-v3"]


def frame_items(exp):
    """item_ids in the target frame + its lens label (for the carried rows)."""
    fr = json.load(open(os.path.join(EXP_ROOT, exp, "frame.json")))
    ids = {g["group_id"] for g in fr.get("merge_sample", [])}
    ids |= {p["pair_id"] for p in fr.get("split_sample", [])}
    return ids, fr.get("lens")


def latest_per_rater(rows):
    """Keep the most recent row per (item_id, rater) by updated_at."""
    best = {}
    for r in rows:
        k = (r["item_id"], r["rater"])
        cur = best.get(k)
        if cur is None or (r["updated_at"] or "") > (cur["updated_at"] or ""):
            best[k] = r
    return best


def carry(con, exp, reuse_from, lens, dry_run):
    items, _ = (None, None)
    items, _frame_lens = frame_items(exp)
    qmarks = ",".join("?" for _ in reuse_from)

    # ---- verdicts ----
    adj = [dict(r) for r in con.execute(
        f"SELECT * FROM adjudications WHERE experiment IN ({qmarks})", tuple(reuse_from))]
    adj = [r for r in adj if r["item_id"] in items]
    best = latest_per_rater(adj)
    existing = {(r["item_id"], r["rater"]) for r in con.execute(
        "SELECT item_id, rater FROM adjudications WHERE experiment=?", (exp,))}
    to_insert = {k: r for k, r in best.items() if k not in existing}

    # ---- comments ----
    com = [dict(r) for r in con.execute(
        f"SELECT * FROM item_comments WHERE experiment IN ({qmarks})", tuple(reuse_from))]
    com = [r for r in com if r["item_id"] in items]
    best_com = latest_per_rater(com)
    existing_com = {(r["item_id"], r["rater"]) for r in con.execute(
        "SELECT item_id, rater FROM item_comments WHERE experiment=?", (exp,))}
    to_insert_com = {k: r for k, r in best_com.items() if k not in existing_com}

    if not dry_run:
        for r in to_insert.values():
            con.execute(
                "INSERT INTO adjudications "
                "(experiment, item_id, kind, lens, rater, members, shown_gids, verdict, "
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (exp, r["item_id"], r["kind"], lens or r["lens"], r["rater"],
                 r["members"], r["shown_gids"], r["verdict"],
                 r["created_at"], r["updated_at"]))
        for r in to_insert_com.values():
            con.execute(
                "INSERT INTO item_comments "
                "(experiment, item_id, rater, comment, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (exp, r["item_id"], r["rater"], r["comment"],
                 r["created_at"], r["updated_at"]))
        con.commit()

    return {
        "frame_items": len(items),
        "verdicts_candidates": len(best),
        "verdicts_already_present": len(best) - len(to_insert),
        "verdicts_carried": len(to_insert),
        "verdict_raters": sorted({k[1] for k in to_insert}),
        "comments_carried": len(to_insert_com),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--exp", default="attr-suff-v4")
    ap.add_argument("--reuse-from", default=",".join(REUSE_FROM))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    reuse_from = [s.strip() for s in args.reuse_from.split(",") if s.strip()]

    _, lens = frame_items(args.exp)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    summary = carry(con, args.exp, reuse_from, lens, args.dry_run)
    con.close()

    mode = "DRY-RUN (no writes)" if args.dry_run else "APPLIED"
    print(f"carry_verdicts -> {args.exp}  [{mode}]  at {time.strftime('%Y-%m-%dT%H:%M:%S')}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
