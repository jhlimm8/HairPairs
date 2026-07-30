#!/usr/bin/env python3
"""Zero-dependency browsing + labeling server for the K-Hairstyle index.

Serves a small JSON API over `data/index.sqlite` plus the extracted image files,
and the static frontend in `ui/static/`. Stdlib only (http.server + sqlite3), to
match the rest of this folder's tooling — just run it and open the browser.

    python3 serve.py                 # http://127.0.0.1:8765
    python3 serve.py --port 9000

Reads the leakage-free `images_clean` view (falls back to `images` if splits
haven't been built). Manual labels are written to the `manual_annotations`
table, keyed by `gid`, never mixed with the mining-only salon attributes.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import translations as T  # noqa: E402

DATA = HERE.parent / "data"
DB = DATA / "index.sqlite"
EXTRACTED = DATA / "extracted"
STATIC = HERE / "static"

# Attribute columns surfaced in the detail panel (order matters for display).
ATTR_COLUMNS = [
    "basestyle", "basestyle_type", "length", "curl", "bang", "side",
    "partition", "color", "sex", "age", "loss", "natural_curl", "hair_width",
    "damage", "melanin_color", "water_repellency", "black_colorize",
    "patch_test", "decolorize_history", "exceptional", "before_after",
    "vertical", "horizontal", "front", "user_satisfied", "designer_satisfied",
    "collect_type", "device",
]

# Categorical attributes exposed as multi-select ("specific set") filters.
# Whitelist — only these column names are ever interpolated into SQL.
FILTER_FIELDS = [
    "length", "basestyle_type", "curl", "bang", "side", "natural_curl",
    "hair_width", "damage", "loss", "color", "partition", "melanin_color",
    "water_repellency", "black_colorize", "patch_test", "before_after",
    "vertical", "collect_type", "exceptional", "front",
]
# Numeric attributes exposed as range filters.
RANGE_FIELDS = ["age", "user_satisfied", "designer_satisfied", "horizontal",
                "decolorize_history"]


def connect():
    con = sqlite3.connect(DB, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def has_clean_view(con) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='images_clean'"
    ).fetchone()
    return row is not None


class App:
    def __init__(self):
        if not DB.exists():
            sys.exit(f"index not found: {DB}\nRun build_index.py first.")
        con = connect()
        self.clean = has_clean_view(con)
        self.table = "images_clean" if self.clean else "images"
        self.split_col = "split_clean" if self.clean else "split"
        con.close()

    # ---- API handlers (return python objects; serialized by caller) ----

    def meta(self):
        con = connect()
        cats = con.execute(
            f"SELECT category_id, category, COUNT(*) n, "
            f"SUM({self.split_col}='mq-train') n_train, "
            f"SUM({self.split_col}='mq-val') n_val "
            f"FROM {self.table} GROUP BY category_id, category ORDER BY category_id"
        ).fetchall()
        splits = con.execute(
            f"SELECT {self.split_col} split, COUNT(*) n, COUNT(DISTINCT source) s "
            f"FROM {self.table} GROUP BY {self.split_col}"
        ).fetchall()
        n_anno = con.execute("SELECT COUNT(*) c FROM manual_annotations").fetchone()["c"]
        total = con.execute(f"SELECT COUNT(*) c FROM {self.table}").fetchone()["c"]
        con.close()
        return {
            "total_images": total,
            "leakage_free_split": self.clean,
            "split_field": self.split_col,
            "annotations": n_anno,
            "splits": [dict(r) for r in splits],
            "categories": [
                {**dict(r), "category_en": T.category_en(r["category_id"], r["category"])}
                for r in cats
            ],
        }

    def glossary(self):
        return {
            "field_labels": T.FIELD_LABELS,
            "value_en": T.VALUE_EN,
            "category_en": T.CATEGORY_EN,
        }

    def facets(self):
        if getattr(self, "_facets", None) is not None:
            return self._facets
        con = connect()
        categorical = []
        for field in FILTER_FIELDS:
            rows = con.execute(
                f"SELECT {field} v, COUNT(*) n FROM {self.table} "
                f"WHERE {field} IS NOT NULL AND {field} != '' "
                f"GROUP BY {field} ORDER BY n DESC"
            ).fetchall()
            categorical.append({
                "field": field,
                "label": T.FIELD_LABELS.get(field, field),
                "n_values": len(rows),
                "values": [
                    {"value": r["v"], "en": T.value_en(field, r["v"]), "count": r["n"]}
                    for r in rows
                ],
            })
        numeric = []
        for field in RANGE_FIELDS:
            r = con.execute(
                f"SELECT MIN({field}) lo, MAX({field}) hi FROM {self.table} "
                f"WHERE {field} IS NOT NULL"
            ).fetchone()
            if r["lo"] is None:
                continue
            numeric.append({
                "field": field,
                "label": T.FIELD_LABELS.get(field, field),
                "min": r["lo"], "max": r["hi"],
            })
        con.close()
        self._facets = {"categorical": categorical, "numeric": numeric}
        return self._facets

    def images(self, q):
        page = max(1, int(q.get("page", ["1"])[0]))
        size = min(120, max(12, int(q.get("page_size", ["60"])[0])))
        where, args = [], []
        split = q.get("split", [""])[0]
        if split in ("mq-train", "mq-val"):
            where.append(f"{self.split_col} = ?")
            args.append(split)
        if q.get("category_id", [""])[0]:
            where.append("category_id = ?")
            args.append(q["category_id"][0])
        if q.get("sex", [""])[0]:
            where.append("sex = ?")
            args.append(q["sex"][0])
        search = q.get("q", [""])[0].strip()
        if search:
            where.append("source LIKE ?")
            args.append(f"%{search}%")
        # multi-select "specific set" filters (whitelisted columns only)
        for field in FILTER_FIELDS:
            vals = [v for v in q.get(f"f_{field}", []) if v != ""]
            if vals:
                where.append(f"{field} IN ({','.join('?' * len(vals))})")
                args.extend(vals)
        # numeric range filters
        for field in RANGE_FIELDS:
            lo = q.get(f"r_{field}_min", [""])[0]
            hi = q.get(f"r_{field}_max", [""])[0]
            if lo != "":
                where.append(f"{field} >= ?")
                args.append(lo)
            if hi != "":
                where.append(f"{field} <= ?")
                args.append(hi)
        if q.get("annotated", [""])[0] == "1":
            where.append("gid IN (SELECT gid FROM manual_annotations)")
        elif q.get("annotated", [""])[0] == "0":
            where.append("gid NOT IN (SELECT gid FROM manual_annotations)")
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        con = connect()
        total = con.execute(
            f"SELECT COUNT(*) c FROM {self.table}{clause}", args
        ).fetchone()["c"]
        rows = con.execute(
            f"SELECT gid, source, view, category_id, category, {self.split_col} split, "
            f"basestyle, sex, color, curl, hair_bbox, crop_w, crop_h, "
            f"(gid IN (SELECT gid FROM manual_annotations)) annotated "
            f"FROM {self.table}{clause} "
            f"ORDER BY category_id, source, view "
            f"LIMIT ? OFFSET ?",
            (*args, size, (page - 1) * size),
        ).fetchall()
        con.close()
        items = []
        for r in rows:
            d = dict(r)
            d["category_en"] = T.category_en(r["category_id"], r["category"])
            d["image_url"] = f"/api/image/{r['gid']}"
            items.append(d)
        return {"total": total, "page": page, "page_size": size, "items": items}

    def detail(self, gid):
        con = connect()
        row = con.execute(
            f"SELECT * FROM {self.table} WHERE gid = ?", (gid,)
        ).fetchone()
        if row is None:
            con.close()
            return None
        d = dict(row)
        attrs = []
        for col in ATTR_COLUMNS:
            val = d.get(col)
            if val in (None, ""):
                continue
            attrs.append({
                "field": col,
                "label": T.FIELD_LABELS.get(col, col),
                "ko": val,
                "en": T.value_en(col, val),
            })
        anno = con.execute(
            "SELECT * FROM manual_annotations WHERE gid = ?", (gid,)
        ).fetchone()
        # sibling views (same capture session)
        siblings = con.execute(
            f"SELECT gid, view FROM {self.table} WHERE source = ? "
            f"ORDER BY view LIMIT 200",
            (d["source"],),
        ).fetchall()
        con.close()
        return {
            "gid": d["gid"],
            "source": d["source"],
            "view": d["view"],
            "split": d.get(self.split_col),
            "category_id": d["category_id"],
            "category": d["category"],
            "category_en": T.category_en(d["category_id"], d["category"]),
            "image_url": f"/api/image/{gid}",
            "image_path": d["image_path"],
            "label_path": d["label_path"],
            "crop": [d.get("crop_w"), d.get("crop_h")],
            "orig": [d.get("orig_w"), d.get("orig_h")],
            "hair_bbox": json.loads(d["hair_bbox"]) if d.get("hair_bbox") else None,
            "rgb": [d.get("rgb_r"), d.get("rgb_g"), d.get("rgb_b")],
            "dup_label_count": d.get("dup_label_count"),
            "n_session_views": len(siblings),
            "attrs": attrs,
            "siblings": [{"gid": s["gid"], "view": s["view"],
                          "image_url": f"/api/image/{s['gid']}"} for s in siblings],
            "annotation": dict(anno) if anno else None,
        }

    def image_path(self, gid):
        con = connect()
        row = con.execute(
            f"SELECT image_path FROM {self.table} WHERE gid = ?", (gid,)
        ).fetchone()
        con.close()
        if row is None:
            return None
        full = (EXTRACTED / row["image_path"]).resolve()
        # guard against path traversal
        if not str(full).startswith(str(EXTRACTED.resolve())) or not full.exists():
            return None
        return full

    def annotate(self, body):
        gid = body.get("gid")
        if not gid:
            return {"error": "gid required"}, 400
        con = connect()
        exists = con.execute(
            f"SELECT 1 FROM {self.table} WHERE gid = ?", (gid,)
        ).fetchone()
        if not exists:
            con.close()
            return {"error": "unknown gid"}, 404
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        annotator = body.get("annotator") or "anon"
        payload = json.dumps(body.get("payload", {}), ensure_ascii=False)
        prior = con.execute(
            "SELECT created_at FROM manual_annotations WHERE gid = ?", (gid,)
        ).fetchone()
        created = prior["created_at"] if prior else now
        con.execute(
            "INSERT INTO manual_annotations (gid, annotator, created_at, updated_at, payload) "
            "VALUES (?,?,?,?,?) ON CONFLICT(gid) DO UPDATE SET "
            "annotator=excluded.annotator, updated_at=excluded.updated_at, payload=excluded.payload",
            (gid, annotator, created, now, payload),
        )
        con.commit()
        con.close()
        return {"ok": True, "gid": gid, "updated_at": now}, 200

    def delete_annotation(self, gid):
        con = connect()
        con.execute("DELETE FROM manual_annotations WHERE gid = ?", (gid,))
        con.commit()
        con.close()
        return {"ok": True}, 200


APP = App()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, ctype, status=200, cache=True):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        try:
            if path == "/" or path == "/index.html":
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if path == "/api/meta":
                return self._send_json(APP.meta())
            if path == "/api/glossary":
                return self._send_json(APP.glossary())
            if path == "/api/facets":
                return self._send_json(APP.facets())
            if path == "/api/images":
                return self._send_json(APP.images(q))
            if path.startswith("/api/detail/"):
                d = APP.detail(unquote(path[len("/api/detail/"):]))
                return self._send_json(d or {"error": "not found"}, 200 if d else 404)
            if path.startswith("/api/image/"):
                fp = APP.image_path(unquote(path[len("/api/image/"):]))
                if fp is None:
                    return self._send_json({"error": "not found"}, 404)
                ctype = mimetypes.guess_type(str(fp))[0] or "image/jpeg"
                return self._send_bytes(fp.read_bytes(), ctype)
            return self._send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send_json({"error": "bad json"}, 400)
        try:
            if u.path == "/api/annotate":
                obj, status = APP.annotate(body)
                return self._send_json(obj, status)
            return self._send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, 500)

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/annotate/"):
            obj, status = APP.delete_annotation(unquote(u.path[len("/api/annotate/"):]))
            return self._send_json(obj, status)
        return self._send_json({"error": "not found"}, 404)

    def _serve_static(self, rel):
        fp = (STATIC / rel).resolve()
        if not str(fp).startswith(str(STATIC.resolve())) or not fp.exists():
            return self._send_json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        self._send_bytes(fp.read_bytes(), ctype, cache=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    split = "images_clean (leakage-free)" if APP.clean else "images (RAW split!)"
    print(f"kHairStyle browser  ·  serving {split}")
    print(f"  http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
