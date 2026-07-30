#!/usr/bin/env python3
"""Adjudication server for Experiment 1 (attribute-sufficiency).

A SEPARATE, blinded labeling app from `../ui/serve.py` (which is the attribute
browser). This one serves pre-registered FRAMES and collects true-hairstyle
verdicts:

  * MERGE task  -> partition a collision group's members into same-hairstyle
                   clusters.
  * SPLIT task  -> decide same / different on a one-attribute-apart pair.

Each frozen frame is its own EXPERIMENT, living in `experiments/<exp>/frame.json`
(produced by `../analysis/mine_frame.py --exp <exp>`). The UI exposes a dropdown
to pick the experiment; verdicts are stored per (experiment, item_id, rater) in
the `adjudications` table, so frames can be revised without colliding with or
invalidating labels collected against an earlier frame.

Blinding is enforced server-side: the client never receives source ids, styles,
attributes, or pool provenance — only opaque member indices (m0, m1, ...) and the
three canonical view crops per member, in a per-item randomized order. The index
-> source mapping stays here and is written, with the verdict, to the
`adjudications` table in `../data/index.sqlite`.

Stdlib only (http.server + sqlite3), matching the rest of the folder.

    python3 serve.py                 # http://127.0.0.1:8770
    python3 serve.py --port 9000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import random
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from views import pick_views  # noqa: E402

DATA = HERE.parent / "data"
DB = DATA / "index.sqlite"
EXTRACTED = DATA / "extracted"
STATIC = HERE / "static"
EXPERIMENTS = HERE / "experiments"

SLOT_ORDER = ["frontal", "profile", "back"]


def connect():
    con = sqlite3.connect(DB, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def ensure_table(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS adjudications ("
        "  experiment TEXT, item_id TEXT, kind TEXT, lens TEXT, rater TEXT,"
        "  members TEXT,"        # JSON list of source ids; list index == member id shown
        "  shown_gids TEXT,"     # JSON {member_index: {slot: gid}}
        "  verdict TEXT,"        # JSON: {clusters:[[..]]} | {relation:'same'|'different'}
        "  created_at TEXT, updated_at TEXT,"
        "  PRIMARY KEY (experiment, item_id, rater))"
    )
    # Free-text rater notes, one per (experiment, item_id, rater). Kept separate
    # from verdicts so a note can be left/edited independently and every rater's
    # note on an item is visible to the others.
    con.execute(
        "CREATE TABLE IF NOT EXISTS item_comments ("
        "  experiment TEXT, item_id TEXT, rater TEXT,"
        "  comment TEXT, created_at TEXT, updated_at TEXT,"
        "  PRIMARY KEY (experiment, item_id, rater))"
    )
    con.commit()


def load_experiment(d: Path):
    """Build the in-memory experiment record from experiments/<exp>/frame.json."""
    frame = json.loads((d / "frame.json").read_text())
    items = {}
    for g in frame.get("merge_sample", []):
        items[g["group_id"]] = {"kind": "merge",
                                "members": [m["source"] for m in g["members"]]}
    for p in frame.get("split_sample", []):
        items[p["pair_id"]] = {"kind": "split",
                               "members": [p["a"]["source"], p["b"]["source"]]}
    return {"id": d.name, "label": frame.get("label", d.name),
            "frame": frame, "items": items,
            "assignment": frame.get("assignment"),
            "labelers": frame.get("labelers", [])}


class App:
    def __init__(self):
        if not DB.exists():
            sys.exit(f"index not found: {DB}\nRun build_index.py first.")
        self.experiments = {}
        if EXPERIMENTS.exists():
            for d in sorted(EXPERIMENTS.iterdir()):
                if d.is_dir() and (d / "frame.json").exists():
                    self.experiments[d.name] = load_experiment(d)
        if not self.experiments:
            sys.exit(f"no experiments under {EXPERIMENTS}\n"
                     f"Run analysis/mine_frame.py --exp <name> first.")
        # default to the latest (last sorted) experiment
        self.default_exp = sorted(self.experiments)[-1]
        con = connect()
        ensure_table(con)
        con.close()
        self._views_cache = {}

    def exp(self, exp_id):
        return self.experiments.get(exp_id) or self.experiments[self.default_exp]

    def assigned_items(self, e, rater):
        """Item_ids a rater should see. For a known labeler -> their shared IAA
        subset + their disjoint coverage partition. For any other rater (e.g. the
        admin/expert id, anon, or a frame without an assignment block) -> None,
        meaning no restriction (the full frame, for review)."""
        asn = e.get("assignment")
        if not asn or rater not in (e.get("labelers") or []):
            return None
        ids = set(asn.get("shared", []))
        ids |= set(asn.get("coverage", {}).get(rater, []))
        return ids

    def raters(self, exp_id):
        e = self.exp(exp_id)
        return {"experiment": e["id"], "raters": e.get("labelers", [])}

    def shared_set(self, e):
        """Item_ids in the shared IAA subset (labelled by every annotator)."""
        return set((e.get("assignment") or {}).get("shared", []))

    # ---- canonical views -----------------------------------------------------
    def source_views(self, source):
        """{slot: gid} for a source (after-only), picked once and cached."""
        if source in self._views_cache:
            return self._views_cache[source]
        con = connect()
        rows = [dict(r) for r in con.execute(
            "SELECT gid, view, horizontal, vertical, front FROM images "
            "WHERE source=? AND before_after='after'",
            (source,))]
        con.close()
        gids = {slot: r["gid"] for slot, r in pick_views(rows).items()}
        self._views_cache[source] = gids
        return gids

    def shown_order(self, item_id, n):
        """Deterministic per-item permutation of member positions -> display order."""
        seed = int(hashlib.sha1(item_id.encode()).hexdigest()[:12], 16)
        order = list(range(n))
        random.Random(seed).shuffle(order)
        return order

    # ---- API -----------------------------------------------------------------
    def experiments_list(self, rater):
        con = connect()
        done = {(r["experiment"], r["item_id"]) for r in con.execute(
            "SELECT experiment, item_id FROM adjudications WHERE rater=?", (rater,))}
        con.close()
        out = []
        for eid in sorted(self.experiments):
            e = self.experiments[eid]
            fr = e["frame"]
            assigned = self.assigned_items(e, rater)
            ids = [i for i in e["items"] if assigned is None or i in assigned]
            n = len(ids)
            d = sum((eid, i) in done for i in ids)
            out.append({"id": eid, "label": e["label"],
                        "id_gap_filter": fr.get("id_gap_filter", False),
                        "assigned": assigned is not None,
                        "total": n, "done": d})
        return {"experiments": out, "default": self.default_exp}

    def frame_meta(self, exp_id, rater, scope="all"):
        e = self.exp(exp_id)
        fr = e["frame"]
        con = connect()
        done = {(r["item_id"], r["kind"]) for r in con.execute(
            "SELECT item_id, kind FROM adjudications WHERE experiment=? AND rater=?",
            (e["id"], rater))}
        con.close()
        assigned = self.assigned_items(e, rater)
        shared = self.shared_set(e)
        only_shared = scope == "shared"

        def keep(i):
            if assigned is not None and i not in assigned:
                return False
            if only_shared and i not in shared:
                return False
            return True

        merge_ids = [g["group_id"] for g in fr["merge_sample"] if keep(g["group_id"])]
        split_ids = [p["pair_id"] for p in fr["split_sample"] if keep(p["pair_id"])]
        return {
            "experiment": e["id"],
            "label": e["label"],
            "lens": fr.get("lens"),
            "seed": fr.get("seed"),
            "rater": rater,
            "assigned": assigned is not None,
            "has_shared": len(shared) > 0,
            "shared_total": len(shared),
            "counts": {
                "merge": {"total": len(merge_ids),
                          "done": sum((i, "merge") in done for i in merge_ids)},
                "split": {"total": len(split_ids),
                          "done": sum((i, "split") in done for i in split_ids)},
            },
        }

    def tasks(self, exp_id, kind, rater, scope="all"):
        e = self.exp(exp_id)
        fr = e["frame"]
        con = connect()
        done = {r["item_id"] for r in con.execute(
            "SELECT item_id FROM adjudications WHERE experiment=? AND rater=? AND kind=?",
            (e["id"], rater, kind))}
        con.close()
        assigned = self.assigned_items(e, rater)
        shared = self.shared_set(e) if scope == "shared" else None
        out = []
        if kind == "merge":
            for g in fr["merge_sample"]:
                gid = g["group_id"]
                if assigned is not None and gid not in assigned:
                    continue
                if shared is not None and gid not in shared:
                    continue
                out.append({"item_id": gid, "kind": "merge",
                            "n_members": g["size"], "done": gid in done})
        else:
            for p in fr["split_sample"]:
                pid = p["pair_id"]
                if assigned is not None and pid not in assigned:
                    continue
                if shared is not None and pid not in shared:
                    continue
                out.append({"item_id": pid, "kind": "split",
                            "n_members": 2, "done": pid in done})
        return {"experiment": e["id"], "kind": kind, "tasks": out}

    def item(self, exp_id, item_id, rater):
        e = self.exp(exp_id)
        meta = e["items"].get(item_id)
        if not meta:
            return None
        members = meta["members"]
        order = self.shown_order(item_id, len(members))
        cards = []
        for disp_i, pos in enumerate(order):
            src = members[pos]
            views = self.source_views(src)
            cards.append({
                "member": disp_i,
                "views": [{"slot": s, "image_url": f"/api/crop/{views[s]}"}
                          for s in SLOT_ORDER if s in views],
            })
        con = connect()
        row = con.execute(
            "SELECT verdict, members, updated_at FROM adjudications "
            "WHERE experiment=? AND item_id=? AND rater=?",
            (e["id"], item_id, rater)).fetchone()
        crows = con.execute(
            "SELECT rater, comment, updated_at FROM item_comments "
            "WHERE experiment=? AND item_id=? AND comment<>'' ORDER BY updated_at",
            (e["id"], item_id)).fetchall()
        con.close()
        existing = None
        if row:
            existing = self._verdict_to_display(meta, json.loads(row["members"]),
                                                 json.loads(row["verdict"]), order)
            existing["updated_at"] = row["updated_at"]
        comments = [{"rater": c["rater"], "comment": c["comment"],
                     "updated_at": c["updated_at"], "mine": c["rater"] == rater}
                    for c in crows]
        my_comment = next((c["comment"] for c in comments if c["mine"]), "")
        return {"item_id": item_id, "experiment": e["id"], "kind": meta["kind"],
                "n_members": len(members), "cards": cards, "existing": existing,
                "comments": comments, "my_comment": my_comment}

    def save_comment(self, exp_id, body):
        e = self.exp(exp_id)
        item_id = body.get("item_id")
        rater = (body.get("rater") or "").strip()
        comment = (body.get("comment") or "").strip()
        if item_id not in e["items"]:
            return {"error": "unknown item_id"}, 404
        if not rater:
            return {"error": "rater required"}, 400
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        con = connect()
        if not comment:
            con.execute("DELETE FROM item_comments WHERE experiment=? AND item_id=? AND rater=?",
                        (e["id"], item_id, rater))
        else:
            prior = con.execute(
                "SELECT created_at FROM item_comments WHERE experiment=? AND item_id=? AND rater=?",
                (e["id"], item_id, rater)).fetchone()
            created = prior["created_at"] if prior else now
            con.execute(
                "INSERT INTO item_comments (experiment, item_id, rater, comment, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(experiment, item_id, rater) DO UPDATE SET "
                "comment=excluded.comment, updated_at=excluded.updated_at",
                (e["id"], item_id, rater, comment, created, now))
        con.commit()
        con.close()
        return {"ok": True, "item_id": item_id, "updated_at": now}, 200

    def _verdict_to_display(self, meta, stored_members, verdict, order):
        """Translate a stored verdict (keyed by source) to current display indices."""
        src_to_disp = {}
        for disp_i, pos in enumerate(order):
            src_to_disp[meta["members"][pos]] = disp_i
        if meta["kind"] == "split":
            return {"relation": verdict.get("relation")}
        clusters = []
        for cl in verdict.get("clusters", []):
            disp = sorted(src_to_disp[s] for s in cl if s in src_to_disp)
            if disp:
                clusters.append(disp)
        return {"clusters": clusters}

    def save_verdict(self, exp_id, body):
        e = self.exp(exp_id)
        item_id = body.get("item_id")
        rater = (body.get("rater") or "").strip()
        meta = e["items"].get(item_id)
        if not meta:
            return {"error": "unknown item_id"}, 404
        if not rater:
            return {"error": "rater required"}, 400
        members = meta["members"]
        order = self.shown_order(item_id, len(members))
        disp_to_src = {disp_i: members[pos] for disp_i, pos in enumerate(order)}

        v = body.get("verdict", {})
        if meta["kind"] == "split":
            rel = v.get("relation")
            if rel not in ("same", "different"):
                return {"error": "relation must be same|different"}, 400
            stored_verdict = {"relation": rel}
        else:
            clusters = v.get("clusters")
            if not isinstance(clusters, list):
                return {"error": "clusters required"}, 400
            seen = set()
            src_clusters = []
            for cl in clusters:
                srcs = []
                for d in cl:
                    if d not in disp_to_src or d in seen:
                        return {"error": f"bad/duplicate member index {d}"}, 400
                    seen.add(d)
                    srcs.append(disp_to_src[d])
                if srcs:
                    src_clusters.append(sorted(srcs))
            if seen != set(range(len(members))):
                return {"error": "every member must be in exactly one cluster"}, 400
            stored_verdict = {"clusters": src_clusters}

        shown = {}
        for disp_i, pos in enumerate(order):
            shown[disp_i] = self.source_views(members[pos])

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        con = connect()
        prior = con.execute(
            "SELECT created_at FROM adjudications "
            "WHERE experiment=? AND item_id=? AND rater=?",
            (e["id"], item_id, rater)).fetchone()
        created = prior["created_at"] if prior else now
        con.execute(
            "INSERT INTO adjudications "
            "(experiment, item_id, kind, lens, rater, members, shown_gids, verdict, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(experiment, item_id, rater) DO UPDATE SET "
            "verdict=excluded.verdict, shown_gids=excluded.shown_gids, updated_at=excluded.updated_at",
            (e["id"], item_id, meta["kind"], e["frame"].get("lens"), rater,
             json.dumps(members), json.dumps(shown), json.dumps(stored_verdict),
             created, now))
        con.commit()
        con.close()
        return {"ok": True, "experiment": e["id"], "item_id": item_id, "updated_at": now}, 200

    def delete_verdict(self, exp_id, item_id, rater):
        e = self.exp(exp_id)
        con = connect()
        con.execute("DELETE FROM adjudications WHERE experiment=? AND item_id=? AND rater=?",
                    (e["id"], item_id, rater))
        con.commit()
        con.close()
        return {"ok": True}, 200

    def image_path(self, gid):
        con = connect()
        row = con.execute("SELECT image_path FROM images WHERE gid=?", (gid,)).fetchone()
        con.close()
        if row is None:
            return None
        full = (EXTRACTED / row["image_path"]).resolve()
        if not str(full).startswith(str(EXTRACTED.resolve())) or not full.exists():
            return None
        return full


APP = App()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype, status=200, cache=True):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel):
        fp = (STATIC / rel).resolve()
        if not str(fp).startswith(str(STATIC.resolve())) or not fp.exists():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        self._bytes(fp.read_bytes(), ctype, cache=False)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        rater = q.get("rater", ["anon"])[0].strip() or "anon"
        exp = q.get("exp", [APP.default_exp])[0]
        scope = q.get("scope", ["all"])[0]
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/experiments":
                return self._json(APP.experiments_list(rater))
            if path == "/api/raters":
                return self._json(APP.raters(exp))
            if path == "/api/frame":
                return self._json(APP.frame_meta(exp, rater, scope))
            if path == "/api/tasks":
                return self._json(APP.tasks(exp, q.get("kind", ["merge"])[0], rater, scope))
            if path.startswith("/api/item/"):
                d = APP.item(exp, unquote(path[len("/api/item/"):]), rater)
                return self._json(d or {"error": "not found"}, 200 if d else 404)
            if path.startswith("/api/crop/"):
                fp = APP.image_path(unquote(path[len("/api/crop/"):]))
                if fp is None:
                    return self._json({"error": "not found"}, 404)
                ctype = mimetypes.guess_type(str(fp))[0] or "image/jpeg"
                return self._bytes(fp.read_bytes(), ctype)
            return self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        exp = q.get("exp", [APP.default_exp])[0]
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        try:
            if u.path == "/api/verdict":
                obj, status = APP.save_verdict(exp, body)
                return self._json(obj, status)
            if u.path == "/api/comment":
                obj, status = APP.save_comment(exp, body)
                return self._json(obj, status)
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    def do_DELETE(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        rater = q.get("rater", ["anon"])[0].strip() or "anon"
        exp = q.get("exp", [APP.default_exp])[0]
        if u.path.startswith("/api/verdict/"):
            obj, status = APP.delete_verdict(exp, unquote(u.path[len("/api/verdict/"):]), rater)
            return self._json(obj, status)
        return self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("HairPairs adjudication  ·  blinded true-hairstyle labeling")
    for eid in sorted(APP.experiments):
        fr = APP.experiments[eid]["frame"]
        mark = " (default)" if eid == APP.default_exp else ""
        print(f"  experiment {eid}{mark}: merge={len(fr['merge_sample'])} "
              f"split={len(fr['split_sample'])}  [{APP.experiments[eid]['label']}]")
    print(f"  http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
