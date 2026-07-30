#!/usr/bin/env python3
"""Authoritative image/label audit for the kHairStyle extracted dataset.

WARNING: file extensions in this dataset are unreliable. Many *.jpg / *.jpeg
files are actually JSON label text, not images. So we classify every file by
its leading bytes (content), not its extension:

  - real image  := file whose first bytes are the JPEG magic (FF D8 FF)
  - label       := file whose content parses as a JSON object

Each label JSON carries a `filename` (and `path`) field naming the exact image
it describes. We pair label -> image on that authoritative basename, within the
same folder. An image is "labeled" iff at least one label in its folder points
to it.

Outputs (under data/):
  - data/unlabeled_images.txt     : real images that NO label references
  - data/orphan_labels.txt        : labels pointing at a missing image
  - data/label_audit_summary.json : machine-readable counts
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "data" / "extracted"

JPEG_MAGIC = b"\xff\xd8\xff"


def classify(path: str):
    """Return ('image', None) or ('label', parsed_dict) or ('other', None)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(3)
            if head == JPEG_MAGIC:
                return "image", None
            rest = fh.read()
    except OSError:
        return "other", None
    raw = head + rest
    text = raw.lstrip()
    if not (text[:1] in (b"{", b"[")):
        return "other", None
    try:
        return "label", json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "other", None


def referenced_name(label: dict) -> str | None:
    for key in ("filename", "path"):
        val = label.get(key)
        if isinstance(val, str) and val:
            return os.path.basename(val)
    return None


def main() -> None:
    if not ROOT.is_dir():
        raise SystemExit(f"Dataset root not found: {ROOT}")

    unlabeled_images: list[str] = []
    orphan_labels: list[str] = []
    n_images = 0
    n_labels = 0
    n_other = 0
    n_labels_no_ref = 0
    multi_labeled_images = 0

    for dirpath, _dn, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT)
        images: set[str] = set()
        label_refs: dict[str, int] = {}  # referenced basename -> count
        local_labels: list[tuple[str, str | None]] = []  # (label_basename, ref)

        for fn in filenames:
            kind, payload = classify(os.path.join(dirpath, fn))
            if kind == "image":
                images.add(fn)
                n_images += 1
            elif kind == "label":
                n_labels += 1
                ref = referenced_name(payload)
                if ref is None:
                    n_labels_no_ref += 1
                else:
                    label_refs[ref] = label_refs.get(ref, 0) + 1
                local_labels.append((fn, ref))
            else:
                n_other += 1

        for img in images:
            cnt = label_refs.get(img, 0)
            if cnt == 0:
                unlabeled_images.append(os.path.join(rel_dir, img))
            elif cnt > 1:
                multi_labeled_images += 1
        for label_fn, ref in local_labels:
            if ref is not None and ref not in images:
                orphan_labels.append(os.path.join(rel_dir, label_fn))

    unlabeled_images.sort()
    orphan_labels.sort()

    out_dir = HERE / "data"
    (out_dir / "unlabeled_images.txt").write_text(
        "\n".join(unlabeled_images) + ("\n" if unlabeled_images else "")
    )
    (out_dir / "orphan_labels.txt").write_text(
        "\n".join(orphan_labels) + ("\n" if orphan_labels else "")
    )

    summary = {
        "dataset_root": str(ROOT),
        "real_images": n_images,
        "label_files": n_labels,
        "non_image_non_label_files": n_other,
        "images_with_label": n_images - len(unlabeled_images),
        "images_without_label": len(unlabeled_images),
        "images_with_multiple_labels": multi_labeled_images,
        "labels_missing_filename_field": n_labels_no_ref,
        "orphan_labels_pointing_to_missing_image": len(orphan_labels),
        "all_images_labeled": len(unlabeled_images) == 0,
    }
    (out_dir / "label_audit_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nWrote:\n  {out_dir/'unlabeled_images.txt'}"
          f"\n  {out_dir/'orphan_labels.txt'}"
          f"\n  {out_dir/'label_audit_summary.json'}")


if __name__ == "__main__":
    main()
