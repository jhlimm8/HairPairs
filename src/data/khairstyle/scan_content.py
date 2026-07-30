#!/usr/bin/env python3
"""Quick content-type vs extension scan of the extracted dataset.

Extensions in this dataset are unreliable (some *.jpeg files are actually JSON
label text). Classify each file by its leading bytes and cross-tab against its
extension so we know the true composition before auditing labels.
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "data" / "extracted"


def sniff(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return "unreadable"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    stripped = head.lstrip()
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        return "json-text"
    return "other"


def main() -> None:
    table: Counter[tuple[str, str]] = Counter()
    n = 0
    for dirpath, _dn, filenames in os.walk(ROOT):
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower() or "<none>"
            table[(ext, sniff(os.path.join(dirpath, fn)))] += 1
            n += 1
    print(f"scanned {n} files\n")
    print(f"{'extension':<10}{'content':<12}{'count':>10}")
    for (ext, content), count in sorted(table.items()):
        print(f"{ext:<10}{content:<12}{count:>10}")


if __name__ == "__main__":
    main()
