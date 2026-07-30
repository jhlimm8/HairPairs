#!/usr/bin/env python3
"""Canonical 3-view azimuth selection for the adjudication stimulus.

The K-hairstyle capture rings each source at azimuth `horizontal` in 0..360 deg
(6-9 deg steps) at one or two elevations `vertical` ('중' mid, '상' high), with
`front=1` marking the canonical 0-deg shot. To kill pose as a confound, every
source is shown with the SAME fixed three views, chosen by one rule:

    frontal     azimuth nearest 0   (prefer front=1)
    profile     azimuth nearest 90
    back        azimuth nearest 180 (fall back to 135 if 180 is uncovered)

Within a slot we prefer the mid elevation ('중') so the triplet is elevation-
consistent, then prefer `front` where relevant, then the lowest view id, so the
choice is fully deterministic for a given source.

This module has no DB or web dependency; `pick_views` takes plain rows so it can
be unit-tested and reused by the server and any offline crop export.
"""
from __future__ import annotations

PREF_VERTICAL = "중"          # mid elevation, eye-level-ish
BACK_TARGET = 180
BACK_FALLBACK = 135
BACK_FALLBACK_IF_GAP = 30     # deg; if nothing within this of 180, use 135

# slot name -> target azimuth
SLOTS = (("frontal", 0), ("profile", 90), ("back", BACK_TARGET))


def circ_dist(a: int, b: int) -> int:
    """Smallest angular distance on a 360-deg circle."""
    d = abs(int(a) - int(b)) % 360
    return min(d, 360 - d)


def _best(rows, target, prefer_front=False):
    """Pick one row nearest `target` azimuth with deterministic tie-breaks."""
    def rank(r):
        return (
            circ_dist(r["horizontal"], target),
            0 if r.get("vertical") == PREF_VERTICAL else 1,
            0 if (prefer_front and r.get("front") == 1) else 1,
            str(r.get("view") or ""),
        )
    return min(rows, key=rank)


def pick_views(rows):
    """Return {slot: row} for a source's image rows.

    `rows` is a non-empty iterable of dicts with at least: gid, view, horizontal,
    vertical, front. Falls back gracefully when a source has few views (slots may
    then share a row).
    """
    rows = [r for r in rows if r.get("horizontal") is not None]
    if not rows:
        raise ValueError("no rows with a horizontal angle")
    chosen = {}
    for slot, target in SLOTS:
        t = target
        if slot == "back":
            nearest = min(circ_dist(r["horizontal"], BACK_TARGET) for r in rows)
            if nearest > BACK_FALLBACK_IF_GAP:
                t = BACK_FALLBACK
        chosen[slot] = _best(rows, t, prefer_front=(slot == "frontal"))
    return chosen


def pick_gids(rows):
    """Convenience: {slot: gid}."""
    return {slot: r["gid"] for slot, r in pick_views(rows).items()}


if __name__ == "__main__":
    import os
    import sqlite3

    DB = os.path.join(os.path.dirname(__file__), "..", "data", "index.sqlite")
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    # NOTE: the stimulus is drawn from AFTER images only (pre-cut "before" images
    # show a different hairstyle); the server applies the same filter.
    for src in ("AP062542", "AP106814", "JS022744"):
        rows = [dict(r) for r in con.execute(
            "SELECT gid, view, horizontal, vertical, front FROM images "
            "WHERE source=? AND before_after='after'",
            (src,))]
        sel = pick_views(rows)
        print(f"\n{src}  ({len(rows)} views)")
        for slot, r in sel.items():
            print(f"  {slot:8s} view={r['view']:>4s} h={r['horizontal']:>3d} "
                  f"vert={r['vertical']} front={r['front']}")
    con.close()
