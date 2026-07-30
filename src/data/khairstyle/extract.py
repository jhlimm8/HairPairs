#!/usr/bin/env python3
"""
Extract downloaded K-Hairstyle zips into a usable tree.

The archives store filenames as UTF-8 but leave the zip "UTF-8 name" flag unset,
so stock `unzip` mangles the Korean category folders (cp437 mojibake). This extractor
recovers the correct names (cp437 -> utf-8) and, per set, merges the `images` and
`labels` archives into one tree so each source folder holds its .jpg and .json
side by side:

    <out>/<set>/0002.mqset/<basestyle>/<source-id>/<source-id>_<view>.{jpg,json}

It also skips the redundant per-source `<source-id>.zip` bundles nested inside the
labels archive.

Usage:
  python3 extract.py                       # extract every zip under ./data into ./data/extracted
  python3 extract.py --src data --out data/extracted
"""
import argparse
import os
import sys
import zipfile


def fix_name(name):
    try:
        return name.encode("cp437").decode("utf-8")
    except Exception:
        return name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="data", help="root containing <set>/.../*.zip")
    ap.add_argument("--out", default=None, help="output root (default: <src>/extracted)")
    ap.add_argument("--skip-nested-zip", action="store_true", default=True,
                    help="skip redundant per-source <id>.zip entries (default on)")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    out = os.path.abspath(args.out or os.path.join(src, "extracted"))

    zips = []
    for root, _, files in os.walk(src):
        if os.path.abspath(root).startswith(out):
            continue  # don't re-ingest our own output
        for f in files:
            if f.lower().endswith(".zip"):
                zips.append(os.path.join(root, f))
    zips.sort()
    if not zips:
        sys.exit(f"no .zip files under {src}")

    print(f"found {len(zips)} archive(s); extracting to {out}\n")
    grand = 0
    for zp in zips:
        rel = os.path.relpath(zp, src)
        set_name = rel.split(os.sep)[0]            # e.g. mq-train
        dest_root = os.path.join(out, set_name)
        zf = zipfile.ZipFile(zp)
        n = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = fix_name(info.filename)
            if name.lower().endswith(".zip"):
                continue
            dest = os.path.join(dest_root, name)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as s, open(dest, "wb") as o:
                while True:
                    b = s.read(1 << 20)
                    if not b:
                        break
                    o.write(b)
            n += 1
            grand += 1
            if grand % 25000 == 0:
                print(f"  ...{grand} files", flush=True)
        print(f"[{rel}] -> {dest_root}  ({n} files)", flush=True)
    print(f"\nEXTRACT COMPLETE: {grand} files into {out}")


if __name__ == "__main__":
    main()
