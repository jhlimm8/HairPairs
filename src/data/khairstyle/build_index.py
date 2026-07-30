#!/usr/bin/env python3
"""Build the kHairStyle data index.

Produces a single queryable manifest (SQLite + JSONL) with ONE row per real
image, with the dataset's two integrity issues resolved:

  * Duplicate labels: many images carry their annotation twice -- once as a
    proper `*_NNN.json` and once as a byte-identical copy mis-saved with a
    `.jpg`/`.jpeg` extension. We keep the `.json` as canonical, record the
    duplicate path(s), and never double-count.
  * Orphan labels: labels that point at an image which was never extracted
    (the two malformed `*.<num>` source folders). These get no image row; they
    are recorded in a separate `orphan_labels` table instead.

File extensions in this dataset are unreliable, so every file is classified by
content (JPEG magic vs JSON), not extension. Each label's internal `filename`
field authoritatively names its image; pairing is done on that, per folder.

IMPORTANT -- core benchmark design constraint: the K-hairstyle salon
attributes captured here (basestyle, length, curl, ...) are **mining-only
signal (B)** -- they may seed candidate pairs but must NEVER serve as benchmark
ground truth. `source` is the same-capture session key (multi-view invariance
positives; leakage-guarded across splits). Manual ground-truth annotations live
in the separate, initially-empty `manual_annotations` table.

Usage:
  python3 build_index.py                 # -> data/index.sqlite + data/index.jsonl
  python3 build_index.py --no-jsonl      # skip the JSONL export
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "data" / "extracted"
JPEG_MAGIC = b"\xff\xd8\xff"

# Salon attribute fields kept in the index. Flagged mining-only in docs/meta.
# Mapping: json_key -> column_name (column != key only where the source key is
# awkward, e.g. the upstream typo "user-stisfied").
ATTR_FIELDS = {
    "basestyle": "basestyle",
    "basestyle-type": "basestyle_type",
    "length": "length",
    "curl": "curl",
    "bang": "bang",
    "loss": "loss",
    "side": "side",
    "partition": "partition",
    "color": "color",
    "sex": "sex",
    "vertical": "vertical",
    "exceptional": "exceptional",
    "before-after": "before_after",
    "hair-width": "hair_width",
    "water-repellency": "water_repellency",
    "natural-curl": "natural_curl",
    "damage": "damage",
    "melanin-color": "melanin_color",
    "black-colorize": "black_colorize",
    "patch-test": "patch_test",
    "decolorize-history": "decolorize_history",
    "comment": "comment",
}
PROV_FIELDS = {
    "collect-type": "collect_type",
    "author": "author",
    "collect-date": "collect_date",
    "device": "device",
    "make": "make",
    "model": "model",
    "datetime_original": "datetime_original",
    "restype": "restype",
}

EMPTY_STRINGS = {"", "nan", "none", "null", "na", "n/a"}


def classify(path: str):
    """Return ('image', None) | ('label', dict) | ('other', None)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(3)
            if head == JPEG_MAGIC:
                return "image", None
            rest = fh.read()
    except OSError:
        return "other", None
    raw = head + rest
    if raw.lstrip()[:1] not in (b"{", b"["):
        return "other", None
    try:
        return "label", json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "other", None


def s(v):
    """Normalize a scalar to a clean string or None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    text = str(v).strip()
    if text.lower() in EMPTY_STRINGS:
        return None
    return text


def i(v):
    try:
        if v is None or (isinstance(v, str) and v.strip().lower() in EMPTY_STRINGS):
            return None
        return int(float(v))
    except (ValueError, TypeError):
        return None


def f(v):
    try:
        if v is None or (isinstance(v, str) and v.strip().lower() in EMPTY_STRINGS):
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def parse_rgb(v):
    try:
        arr = json.loads(v) if isinstance(v, str) else v
        if isinstance(arr, (list, tuple)) and len(arr) >= 3:
            return float(arr[0]), float(arr[1]), float(arr[2])
    except (ValueError, TypeError):
        pass
    return None, None, None


def polygon_stats(poly):
    """Return (n_points, [minx,miny,maxx,maxy]) for a K-hairstyle polygon.

    Shape is a list of rings, each a list of {"x":..,"y":..}. May arrive as a
    JSON string. Returns (0, None) if absent/unparseable.
    """
    if poly is None:
        return 0, None
    try:
        rings = json.loads(poly) if isinstance(poly, str) else poly
    except (ValueError, TypeError):
        return 0, None
    n = 0
    xs, ys = [], []
    for ring in rings or []:
        for pt in ring or []:
            try:
                xs.append(float(pt["x"]))
                ys.append(float(pt["y"]))
                n += 1
            except (KeyError, TypeError, ValueError):
                continue
    if not xs:
        return n, None
    return n, [min(xs), min(ys), max(xs), max(ys)]


def union_bbox(b1, b2):
    boxes = [b for b in (b1, b2) if b]
    if not boxes:
        return None
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


IMAGE_COLUMNS = [
    ("gid", "TEXT"),
    ("label_uuid", "TEXT"),
    ("split", "TEXT"),
    ("category_id", "TEXT"),
    ("category", "TEXT"),
    ("source", "TEXT"),
    ("view", "TEXT"),
    ("image_path", "TEXT"),
    ("label_path", "TEXT"),
    ("image_ext", "TEXT"),
    ("image_bytes", "INTEGER"),
    ("dup_label_count", "INTEGER"),
    ("dup_label_paths", "TEXT"),
    # geometry
    ("crop_w", "INTEGER"),
    ("crop_h", "INTEGER"),
    ("orig_w", "INTEGER"),
    ("orig_h", "INTEGER"),
    ("orientation", "INTEGER"),
    ("focal_length", "REAL"),
    # hair segmentation (full polygons read from label_path on demand)
    ("has_polygon1", "INTEGER"),
    ("has_polygon2", "INTEGER"),
    ("poly1_pts", "INTEGER"),
    ("poly2_pts", "INTEGER"),
    ("hair_bbox", "TEXT"),
    ("rgb_r", "REAL"),
    ("rgb_g", "REAL"),
    ("rgb_b", "REAL"),
    # demographics / numeric attrs
    ("age", "INTEGER"),
    ("front", "INTEGER"),
    ("horizontal", "INTEGER"),
    ("user_satisfied", "INTEGER"),
    ("designer_satisfied", "INTEGER"),
]
# salon attr + provenance text columns appended dynamically
IMAGE_COLUMNS += [(c, "TEXT") for c in ATTR_FIELDS.values()]
IMAGE_COLUMNS += [(c, "TEXT") for c in PROV_FIELDS.values()]


def build_row(rel_dir, split, category_id, category, image_fn, label, label_fn,
              dup_paths, image_bytes):
    view = None
    stem = os.path.splitext(image_fn)[0]
    for sep in ("-", "_"):
        if sep in stem:
            view = stem.rsplit(sep, 1)[1]
    source = s(label.get("source")) or stem
    gid = f"{split}|{source}|{view or stem}"

    p1n, p1b = polygon_stats(label.get("polygon1"))
    p2n, p2b = polygon_stats(label.get("polygon2"))
    bbox = union_bbox(p1b, p2b)
    r, g, b = parse_rgb(label.get("rgb"))

    row = {
        "gid": gid,
        "label_uuid": s(label.get("id")),
        "split": split,
        "category_id": category_id,
        "category": category,
        "source": source,
        "view": view,
        "image_path": os.path.join(rel_dir, image_fn),
        "label_path": os.path.join(rel_dir, label_fn),
        "image_ext": os.path.splitext(image_fn)[1].lower().lstrip("."),
        "image_bytes": image_bytes,
        "dup_label_count": len(dup_paths),
        "dup_label_paths": json.dumps(dup_paths) if dup_paths else None,
        "crop_w": i(label.get("width")),
        "crop_h": i(label.get("height")),
        "orig_w": i(label.get("pixel_x_dimension")),
        "orig_h": i(label.get("pixel_y_dimension")),
        "orientation": i(label.get("orientation")),
        "focal_length": f(label.get("focal_length")),
        "has_polygon1": 1 if p1n else 0,
        "has_polygon2": 1 if p2n else 0,
        "poly1_pts": p1n,
        "poly2_pts": p2n,
        "hair_bbox": json.dumps([round(x, 2) for x in bbox]) if bbox else None,
        "rgb_r": r,
        "rgb_g": g,
        "rgb_b": b,
        "age": i(label.get("age")),
        "front": (1 if label.get("front") is True
                  else 0 if label.get("front") is False else None),
        "horizontal": i(label.get("horizontal")),
        "user_satisfied": i(label.get("user-stisfied")),
        "designer_satisfied": i(label.get("designer-satisfied")),
    }
    for key, col in ATTR_FIELDS.items():
        row[col] = s(label.get(key))
    for key, col in PROV_FIELDS.items():
        row[col] = s(label.get(key))
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-db", default=str(HERE / "data" / "index.sqlite"))
    ap.add_argument("--out-jsonl", default=str(HERE / "data" / "index.jsonl"))
    ap.add_argument("--no-jsonl", action="store_true")
    args = ap.parse_args()

    if not ROOT.is_dir():
        raise SystemExit(f"Dataset root not found: {ROOT}")

    db_path = Path(args.out_db)
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cols_sql = ",\n  ".join(f"{name} {typ}" for name, typ in IMAGE_COLUMNS)
    cur.execute(f"CREATE TABLE images (\n  {cols_sql},\n  PRIMARY KEY (gid)\n)")
    cur.execute(
        "CREATE TABLE orphan_labels (label_path TEXT, referenced_image TEXT, "
        "split TEXT, category TEXT, source TEXT)"
    )
    cur.execute(
        "CREATE TABLE manual_annotations ("
        "gid TEXT PRIMARY KEY REFERENCES images(gid), "
        "annotator TEXT, created_at TEXT, updated_at TEXT, payload TEXT)"
    )
    cur.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

    col_names = [c for c, _ in IMAGE_COLUMNS]
    insert_sql = (
        f"INSERT OR IGNORE INTO images ({', '.join(col_names)}) "
        f"VALUES ({', '.join('?' for _ in col_names)})"
    )

    jsonl = None
    if not args.no_jsonl:
        jsonl = open(args.out_jsonl, "w", encoding="utf-8")

    n_images = n_labels = n_dups = n_orphans = n_gid_collision = 0
    t0 = time.time()
    batch = []

    for dirpath, _dn, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT)
        parts = rel_dir.split(os.sep)
        if len(parts) != 4:
            # files only live at depth 4: split/mqset/category/source
            continue
        split, _mqset, cat_dir, _src_dir = parts
        category_id, _, category = cat_dir.partition(".")

        images = {}   # basename -> bytes
        # ref_basename -> list of (label_fn, parsed, is_json_ext)
        labels = {}
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            kind, payload = classify(full)
            if kind == "image":
                images[fn] = os.path.getsize(full)
            elif kind == "label":
                n_labels += 1
                ref = payload.get("filename") or payload.get("path")
                ref = os.path.basename(ref) if ref else None
                labels.setdefault(ref, []).append(
                    (fn, payload, fn.lower().endswith(".json"))
                )

        for img_fn, img_bytes in images.items():
            cands = labels.get(img_fn, [])
            if not cands:
                continue  # unlabeled image (none expected; audited separately)
            cands.sort(key=lambda c: (not c[2], c[0]))  # prefer .json ext
            canon_fn, canon, _ = cands[0]
            dups = [c[0] for c in cands[1:]]
            n_dups += len(dups)
            row = build_row(rel_dir, split, category_id, category, img_fn,
                            canon, canon_fn,
                            [os.path.join(rel_dir, d) for d in dups], img_bytes)
            batch.append(tuple(row[c] for c in col_names))
            if jsonl:
                jsonl.write(json.dumps(row, ensure_ascii=False) + "\n")
            n_images += 1
            if len(batch) >= 5000:
                cur.executemany(insert_sql, batch)
                batch.clear()

        for ref, cands in labels.items():
            if ref is not None and ref not in images:
                for fn, payload, _ in cands:
                    n_orphans += 1
                    cur.execute(
                        "INSERT INTO orphan_labels VALUES (?,?,?,?,?)",
                        (os.path.join(rel_dir, fn), ref, split, category,
                         s(payload.get("source"))),
                    )

    if batch:
        cur.executemany(insert_sql, batch)
    if jsonl:
        jsonl.close()

    # indexes for the UI / mining queries
    for col in ("source", "split", "category", "basestyle", "length", "curl",
                "side", "sex", "color", "partition", "bang"):
        cur.execute(f"CREATE INDEX idx_images_{col} ON images({col})")
    cur.execute(
        "CREATE VIEW sessions AS SELECT source, split, "
        "MIN(category) AS category, COUNT(*) AS n_views, "
        "COUNT(DISTINCT basestyle) AS n_basestyles "
        "FROM images GROUP BY source"
    )

    # leakage guard: a source (session) must not span splits
    cur.execute(
        "SELECT COUNT(*) FROM (SELECT source FROM images "
        "GROUP BY source HAVING COUNT(DISTINCT split) > 1)"
    )
    sources_spanning_splits = cur.fetchone()[0]

    meta = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dataset_root": str(ROOT),
        "row_grain": "one row per real image (content-verified JPEG)",
        "n_images": n_images,
        "n_label_files_seen": n_labels,
        "n_duplicate_labels_collapsed": n_dups,
        "n_orphan_labels": n_orphans,
        "n_gid_collisions": n_gid_collision,
        "sources_spanning_splits": sources_spanning_splits,
        "salon_attrs_role": "MINING-ONLY; never benchmark ground truth",
        "source_field_meaning": "same-capture session/person key; multi-view invariance positives; leakage-guarded across splits",
        "ground_truth_table": "manual_annotations (initially empty; populated by labeling UI)",
        "build_seconds": round(time.time() - t0, 1),
    }
    cur.executemany("INSERT INTO meta VALUES (?,?)",
                    [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
    con.commit()
    con.close()

    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"\nWrote:\n  {db_path}")
    if not args.no_jsonl:
        print(f"  {args.out_jsonl}")


if __name__ == "__main__":
    main()
