#!/usr/bin/env python3
"""Sample label JSONs and report per-key coverage + example values.

Helps decide which fields belong in the data index. Walks the extracted tree,
takes every Nth *.json (well-named labels only), and tallies how often each key
is present and non-empty, plus a few distinct example values.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "data" / "extracted"
STRIDE = 200  # sample 1 / STRIDE labels

present = Counter()
nonempty = Counter()
examples: dict[str, list] = defaultdict(list)
n = 0
seen = 0

EMPTY = ("", None, 0, "0", "[]", "{}")

for dirpath, _dn, filenames in os.walk(ROOT):
    for fn in sorted(filenames):
        if not fn.lower().endswith(".json"):
            continue
        seen += 1
        if seen % STRIDE:
            continue
        try:
            d = json.load(open(os.path.join(dirpath, fn), encoding="utf-8"))
        except Exception:
            continue
        n += 1
        for k, v in d.items():
            present[k] += 1
            is_empty = v in EMPTY or (isinstance(v, str) and not v.strip())
            if not is_empty:
                nonempty[k] += 1
                if len(examples[k]) < 4:
                    sval = str(v)
                    sval = sval[:60] + ("…" if len(sval) > 60 else "")
                    if sval not in examples[k]:
                        examples[k].append(sval)

print(f"sampled {n} labels (1/{STRIDE})\n")
print(f"{'key':<26}{'present%':>9}{'nonempty%':>11}  examples")
for k, _ in present.most_common():
    p = 100 * present[k] / n
    ne = 100 * nonempty[k] / n
    ex = " | ".join(examples[k][:3])
    print(f"{k:<26}{p:>8.0f}%{ne:>10.0f}%  {ex}")
