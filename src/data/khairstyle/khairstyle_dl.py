#!/usr/bin/env python3
"""
Parallel downloader for the K-Hairstyle dataset.

The dataset is published only through the authors' Synology Drive public shares
(https://psh01087.github.io/K-Hairstyle/), served via a low-bandwidth QuickConnect
relay. Clicking "Download" gives a single slow stream. Each share, however, is just
a couple of large .zip files behind a Range-capable endpoint, so we enumerate the
files via the (reverse-engineered) Synology Drive sharing API and hand per-file,
range-splittable URLs to aria2c for many-connection parallel download + resume.

Mechanism (per public share `/d/s/<permanent_link>/<sharing_link>`):
  1. GET the share landing -> Set-Cookie `drive-sharing-<sharing_link>=<token>`.
     That cookie value is the `sharing_token` every Drive API call must carry.
  2. SYNO.SynologyDrive.Files/list (v2) with path="link:<permanent_link>" lists the
     root; subfolders are listed with path="id:<file_id>".
  3. SYNO.SynologyDrive.Files/download (v2) with files=["id:<file_id>"] streams a
     file and honors HTTP Range (verified: 206 Partial Content), so aria2c can split.

The download URL carries `sharing_token` as a query param, so aria2c needs no cookies.
Tokens can expire on long transfers; `--run` regenerates the token + input file and
re-invokes aria2c (which resumes via -c) until everything completes.

Usage:
  # list the file tree + sizes for a set (no download)
  python3 khairstyle_dl.py --set mq-train --list

  # generate an aria2c input file only
  python3 khairstyle_dl.py --set mq-train --dest ./data/khairstyle --dry-run

  # generate + download (train and val together) with resume/refresh loop
  python3 khairstyle_dl.py --set mq-train mq-val --dest ./data/khairstyle --run

Sets: mq-train mq-val hq-train hq-val raw-train raw-val
  (mq = 512x512 cropped, hq = 1024x1024 cropped, raw = 4032x3024)
"""

import argparse
import http.cookiejar
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# --- Known K-Hairstyle public shares (from psh01087.github.io/K-Hairstyle) ---
# (permanent_link, sharing_link)
SHARES = {
    "mq-train":  ("p9A5kbK4danKU4WeoqSMiudPFX7Qmiau", "6cgl793b8R2Pp6u4rPT-NtuwmLfYI5Vi-_7SAyvXimwk"),
    "hq-train":  ("p9AVf0sKXczzq8YrDUCh4d0LxO8Av0PC", "ckm7D6Dqm3xn56PT0szjEDaQqMbPtdop-8bIgNcTimwk"),
    "raw-train": ("p9A6AZu4nvwmc5R3LZ0qz8dmheCtPrc3", "C0mub_CFpZsu2jGatNDxPAmoHDKltYLM-GLSgquXimwk"),
    "mq-val":    ("p9B00klYuWYhBeWDVeUFF4wRbrKeOjM5", "i3gjyLgJLSD8EijSHy8LieSn4ahmK-hl-YrYA4Bvjmwk"),
    "hq-val":    ("p9B00femZ2XhVmBYjk3GMT45NUZJwLAg", "4AOA9MkcNjsKkYgLF4IHpTPpcUP-IY6R--bWAsRDjmwk"),
    "raw-val":   ("p9B00oskIlK77WtNWJZ0wQ5W0d2qSRcs", "4EUN4qujmV6K64kw99HNyBljokmjXgal-3bagQijjmwk"),
}

SERVER_ID = "davian-lab"
SHARED_PREFIX = "/shared-with-me/"  # display_path root we strip for local layout


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f}{unit}"
        n /= 1024


def resolve_base():
    """Resolve the QuickConnect relay region host for this NAS.

    Returns e.g. https://davian-lab.tw2.quickconnect.to . Falls back to tw2 if the
    coordination service is unreachable.
    """
    fallback = f"https://{SERVER_ID}.tw2.quickconnect.to"
    body = json.dumps({
        "version": 1, "command": "get_server_info",
        "stop_when_error": False, "stop_when_success": False,
        "id": "dsm_portal_https", "serverID": SERVER_ID, "is_gofile": False,
    }).encode()
    req = urllib.request.Request(
        "https://global.quickconnect.to/Serv.php", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            info = json.load(r)
        region = info.get("env", {}).get("relay_region")
        if region:
            return f"https://{SERVER_ID}.{region}.quickconnect.to"
    except Exception as e:
        print(f"[warn] QuickConnect resolve failed ({e}); using {fallback}", file=sys.stderr)
    return fallback


class Share:
    """A single public Synology Drive share + its API session."""

    def __init__(self, base, permanent_link, sharing_link, ctx):
        self.base = base
        self.pl = permanent_link
        self.sl = sharing_link
        self.ctx = ctx
        self.cookiejar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(self.cookiejar),
        )
        self.token = None

    def refresh_token(self):
        """GET the share landing to obtain a fresh `sharing_token` cookie value."""
        url = f"{self.base}/d/s/{self.pl}/{self.sl}"
        with self.opener.open(url, timeout=30) as r:
            r.read(1)  # ensure Set-Cookie is processed
        for c in self.cookiejar:
            if c.name == f"drive-sharing-{self.sl}":
                self.token = c.value
                return self.token
        raise RuntimeError(f"could not obtain sharing token for {self.pl}")

    def _api(self, params):
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"{self.base}/webapi/entry.cgi", data=data)
        with self.opener.open(req, timeout=60) as r:
            out = json.load(r)
        if not out.get("success"):
            raise RuntimeError(f"API error: {out.get('error')}")
        return out["data"]

    def list_path(self, path):
        """List all children of a Drive path (handles pagination)."""
        items, offset = [], 0
        while True:
            data = self._api({
                "api": "SYNO.SynologyDrive.Files", "version": "2", "method": "list",
                "path": json.dumps(path), "offset": str(offset), "limit": "1000",
                "sort_by": json.dumps("name"), "sort_direction": json.dumps("asc"),
                "sharing_token": self.token,
            })
            batch = data.get("items", [])
            items.extend(batch)
            total = data.get("total", len(items))
            offset += len(batch)
            if not batch or offset >= total:
                break
        return items

    def walk(self):
        """Yield every file (recursively) as dict(name, file_id, size, rel_path)."""
        root = f"link:{self.pl}"
        stack = [root]
        while stack:
            path = stack.pop()
            for it in self.list_path(path):
                if it["content_type"] == "dir":
                    stack.append(f"id:{it['file_id']}")
                else:
                    disp = it.get("display_path", it["name"])
                    rel = disp[len(SHARED_PREFIX):] if disp.startswith(SHARED_PREFIX) else disp.lstrip("/")
                    yield {
                        "name": it["name"],
                        "file_id": it["file_id"],
                        "size": it.get("size", 0),
                        "rel_path": rel,
                    }

    def download_url(self, file_id):
        q = urllib.parse.urlencode({
            "api": "SYNO.SynologyDrive.Files", "method": "download", "version": "2",
            "force_download": "true", "download_type": "download",
            "files": json.dumps([f"id:{file_id}"]),
            "sharing_token": self.token,
        })
        return f"{self.base}/webapi/entry.cgi?{q}"


def build_ssl(insecure):
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def gather(base, set_names, ctx):
    """Return list of (set_name, Share, [file dicts])."""
    result = []
    for name in set_names:
        if name not in SHARES:
            sys.exit(f"unknown set '{name}'. choices: {', '.join(SHARES)}")
        pl, sl = SHARES[name]
        sh = Share(base, pl, sl, ctx)
        sh.refresh_token()
        files = list(sh.walk())
        result.append((name, sh, files))
    return result


def write_aria2_input(gathered, dest, input_path):
    """(Re)write the aria2c input file with current tokens. dest is absolute."""
    lines = []
    for name, sh, files in gathered:
        for f in files:
            local = os.path.join(name, f["rel_path"])  # e.g. mq-train/mqset/images/...zip
            out_dir = os.path.join(dest, os.path.dirname(local))
            lines.append(sh.download_url(f["file_id"]))
            lines.append(f"  dir={out_dir}")
            lines.append(f"  out={os.path.basename(local)}")
    with open(input_path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def run_aria2(gathered, dest, input_path, args):
    aria = [
        "aria2c",
        "-i", input_path,
        "-c",                                   # resume partial files
        f"-x{args.connections}",                # connections per server (per file)
        f"-s{args.connections}",                # splits per file
        "-k", "1M",                             # min split size
        f"-j{args.jobs}",                       # parallel files
        "--file-allocation=none",
        "--auto-file-renaming=false",
        "--allow-overwrite=false",
        "--retry-wait=5",
        "--max-tries=5",
        "--summary-interval=15",
        "--console-log-level=warn",
        "--show-console-readout=true",
        f"--check-certificate={'false' if args.insecure else 'true'}",
    ]
    attempt = 0
    while True:
        attempt += 1
        print(f"\n=== aria2c attempt {attempt} ===", flush=True)
        rc = subprocess.call(aria)
        if rc == 0:
            print("\nAll downloads complete.")
            return 0
        if attempt >= args.max_refresh:
            print(f"\naria2c exited rc={rc}; giving up after {attempt} attempts.", file=sys.stderr)
            return rc
        # Failure is usually an expired sharing token or transient relay throttling.
        # Back off (the relay rate-limits an IP that opens too many connections too
        # fast), refresh tokens, rewrite the input, and resume (aria2c -c).
        backoff = min(15 * attempt, 120)
        print(f"\naria2c exited rc={rc}; backing off {backoff}s, refreshing tokens, resuming...",
              file=sys.stderr)
        time.sleep(backoff)
        for _, sh, _ in gathered:
            try:
                sh.refresh_token()
            except Exception as e:
                print(f"[warn] token refresh failed: {e}", file=sys.stderr)
        write_aria2_input(gathered, dest, input_path)


def main():
    ap = argparse.ArgumentParser(description="Parallel K-Hairstyle dataset downloader (aria2c).")
    ap.add_argument("--set", nargs="+", required=True, metavar="NAME",
                    help=f"one or more sets: {', '.join(SHARES)}")
    ap.add_argument("--dest", default="./data/khairstyle", help="download destination root")
    ap.add_argument("--list", action="store_true", help="list files + sizes, then exit")
    ap.add_argument("--dry-run", action="store_true", help="write aria2c input file but do not download")
    ap.add_argument("--run", action="store_true", help="download with aria2c (resume + token refresh)")
    ap.add_argument("--connections", type=int, default=16, help="connections/splits per file (aria2 -x/-s)")
    ap.add_argument("--jobs", type=int, default=2, help="parallel file downloads (aria2 -j); total relay "
                    "connections ~= connections*jobs, keep <=32 to avoid relay throttling")
    ap.add_argument("--max-refresh", type=int, default=20, help="max aria2c (re)launch attempts")
    ap.add_argument("--input", default=None, help="aria2c input file path (default: <dest>/.aria2_input.txt)")
    ap.add_argument("--base", default=None, help="override relay base URL")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification")
    args = ap.parse_args()

    ctx = build_ssl(args.insecure)
    base = args.base or resolve_base()
    print(f"relay base: {base}")

    dest = os.path.abspath(args.dest)
    input_path = args.input or os.path.join(dest, ".aria2_input.txt")

    print(f"enumerating: {', '.join(args.set)} ...")
    gathered = gather(base, args.set, ctx)

    grand = 0
    for name, _, files in gathered:
        subtotal = sum(f["size"] for f in files)
        grand += subtotal
        print(f"\n[{name}] {len(files)} file(s), {human(subtotal)}")
        if args.list:
            for f in files:
                print(f"  {human(f['size']):>10}  {f['rel_path']}")
    print(f"\nTOTAL: {human(grand)}")

    if args.list:
        return

    os.makedirs(dest, exist_ok=True)
    write_aria2_input(gathered, dest, input_path)
    print(f"\naria2c input written: {input_path}")

    if args.dry_run:
        print("dry-run: not downloading. Run aria2c yourself with:")
        print(f"  aria2c -i {input_path} -c -x{args.connections} -s{args.connections} -k1M -j{args.jobs}")
        return
    if args.run:
        sys.exit(run_aria2(gathered, dest, input_path, args))
    print("\nNothing to do. Pass --list, --dry-run, or --run.")


if __name__ == "__main__":
    main()
