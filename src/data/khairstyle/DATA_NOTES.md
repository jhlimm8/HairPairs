# K-Hairstyle data: state, fixes, and how to use the index

**Audience:** anyone touching the K-Hairstyle data after 2026-06. Read this before
querying images, building splits, or labeling. It records what the data *is*, every
fix applied to it, *why* those fixes were shaped the way they were, and how to use the
derived index.

- **What this data is for:** the image source behind HairPairs — a *cross-wearer,
  cross-session hairstyle instance verification* benchmark. The design requirements
  (source roles, attribute independence, no-leakage) directly shaped the index; they
  are summarised in [§2](#2-problem-statements-that-shaped-the-index).
- **Where things live:** raw download tooling + extraction → this folder
  (`src/data/khairstyle/`); the data payloads + derived artifacts → `data/`
  (git-ignored — large + contains sharing tokens in logs).

---

## TL;DR / quickstart

```bash
# the index is a single SQLite file (rebuildable; see "Reproduce" below)
sqlite3 data/index.sqlite

# ALWAYS use the leakage-free split, not the authors' raw split:
SELECT * FROM images_clean WHERE split_clean = 'mq-val' LIMIT 5;

# multi-view positives for a session (same person/capture, invariance tier):
SELECT gid, view, image_path FROM images WHERE source = 'CP032677';
```

Three things that will bite you if you skip this doc:

1. **File extensions lie.** ~16k files named `*.jpg` / `*.jpeg` are actually JSON label
   text. Classify by *content*, never by suffix.
2. **The salon attributes (basestyle, curl, …) are MINING-ONLY.** They must never be used
   as benchmark ground truth (attribute independence). Ground truth goes in
   `manual_annotations`, written by the labeling step.
3. **The authors' train/val split leaks.** Use the `images_clean` view / `splits` table,
   which assigns each capture session to exactly one split.

---

## 1. What the dataset is

- **Source:** [K-Hairstyle](https://psh01087.github.io/K-Hairstyle/) (Kim et al., ICIP
  2021), a Korean salon multi-view hairstyle corpus. We use the **minimally-cropped
  512×512 set (`mqset`)**, train + val. Acquisition tooling + the QuickConnect/Synology
  reverse-engineering story are in `README.md`.
- **Scale (content-verified, see §3):** **451,333 real images**, **467,791 label files**,
  **4,347 capture sessions**, **31 Korean style categories**, two splits (`mq-train`,
  `mq-val`).
- **Extracted layout** (produced by `extract.py`, which also repairs cp437-mangled Korean
  folder names):

  ```
  data/extracted/<split>/0002.mqset/<NNNN.basestyle>/<NNNN.source>/<source>-<view>.{jpg,json}
                   │              │          │                 │
                mq-train/      single    31 style          per-session/person folder;
                mq-val         "mqset"   categories        id prefixes: CP/JS/DSS/JSS/MN/AP/DS
  ```

  - **`source`** (e.g. `CP032677`) = one capture **session / person**. Same source = the
    same haircut shot from many angles → the dataset's *same-capture multi-view*
    positives (objective invariance tier).
  - Each image has a sibling label naming it exactly via the label's internal `filename`
    field.

## 2. Problem statements that shaped the index

The benchmark design imposed the requirements below. These are *why* the index looks
the way it does:

| Requirement | Consequence for the data/index |
|---|---|
| same-capture multi-view = invariance positives | `source` is a first-class, indexed column; a `sessions` view rolls views up per session. |
| leakage-guarded — **no session shared across splits** | We detected + fixed cross-split session leakage; use `images_clean.split_clean`, never the raw `split`. (§5) |
| **attribute independence** — salon attrs are *candidate-mining signal*, **never ground truth** | Salon attributes are kept (for mining) but **explicitly flagged MINING-ONLY**; ground truth is segregated into `manual_annotations`. |
| de-identified hair crops only (non-re-ID ethics) | Hair segmentation polygons are surfaced as `hair_bbox` + flags to support de-id cropping; full polygons remain in the label JSON. |
| Open-set, long-tailed, **instance = provenance-linked equivalence class** | Grain is one row per image with stable `gid` + `source`; equivalence/positives are defined later by humans, not by these attrs. |

## 3. Data quirks discovered & how they were handled

Verified 2026-06-11 by content-scanning all 919,124 files (`scan_content.py`).

### 3a. Extensions are unreliable — classify by content

| extension | true content | count |
|---|---|---|
| `.jpg` / `.jpeg` | **real JPEG image** | 451,333 |
| `.jpg` / `.jpeg` | JSON label text (mis-extensioned) | 16,173 |
| `.json` | JSON label | 451,618 |

All tooling here sniffs the leading bytes (`FF D8 FF` = JPEG; `{`/`[` = JSON), never the
suffix. **Do the same in any new code.**

### 3b. Duplicate labels (16,173) — collapsed

Some images carry their annotation twice: a proper `*_NNN.json` **and** a *byte-identical*
copy saved with a `.jpg`/`.jpeg` extension. The index keeps the `.json` as canonical,
records the extra path in `dup_label_count` / `dup_label_paths`, and never double-counts.

### 3c. Orphan labels (285) — quarantined

Two malformed source folders contain **only labels, no images**:
`mq-train/.../0011.바디/4323.JSS912.63/` and `mq-train/.../0024.여자일반숏/3627.JS848.59/`
(note the stray `.` in the id). Their labels point at images that were never extracted.
These are **excluded from `images`** and listed in the `orphan_labels` table (and
`data/orphan_labels.txt`).

### 3d. Every real image is labeled

`check_labels.py` pairs each label to its image via the label's authoritative internal
`filename` field (per folder). Result: **0 unlabeled images** (`data/unlabeled_images.txt`
is empty). See `data/label_audit_summary.json` for the machine-readable audit.

## 4. The index (`data/index.sqlite`, + `data/index.jsonl`)

Built by `build_index.py`. **Grain: one row per real image (451,333).**

### Tables / views

| object | what it is |
|---|---|
| `images` | the immutable manifest (one row per real image) |
| `images_clean` | **view**: `images` ⨝ `splits`, adds `split_clean` — *use this for splits* |
| `sessions` | **view**: per-`source` rollup (`n_views`, `split`, `category`) |
| `splits` | `source` → leakage-free split assignment (+ provenance counts), see §5 |
| `orphan_labels` | the 285 image-less labels (§3c) |
| `manual_annotations` | **empty**; ground-truth written here by the labeling UI, keyed by `gid` |
| `meta` | build + split provenance, counts, and the mining-only caveat |

### Key `images` columns

- **identity / provenance:** `gid` (PK = `split|source|view`), `label_uuid`, `split`,
  `category_id`, `category`, `source`, `view`, `image_path`, `label_path`, `image_ext`,
  `image_bytes`, `dup_label_count`, `dup_label_paths`.
- **geometry:** `crop_w`/`crop_h` (512²), `orig_w`/`orig_h` (camera pixels),
  `orientation`, `focal_length`.
- **hair segmentation:** `has_polygon1`/`has_polygon2`, `poly1_pts`/`poly2_pts`,
  `hair_bbox` = `[x0,y0,x1,y1]` in crop coords (union of polygons; for de-id crops).
  *Full polygon vertices are read from `label_path` on demand — not duplicated here.*
- **mean hair color:** `rgb_r`/`rgb_g`/`rgb_b`.
- **⚠ salon attributes — MINING-ONLY (signal B); NEVER ground truth:** `basestyle`,
  `basestyle_type`, `length`, `curl`, `bang`, `loss`, `side`, `partition`, `color`,
  `sex`, `age`, `vertical`, `horizontal`, `front`, `exceptional`, `before_after`,
  `hair_width`, `water_repellency`, `natural_curl`, `damage`, `melanin_color`,
  `black_colorize`, `patch_test`, `decolorize_history`, `user_satisfied`,
  `designer_satisfied`, `comment`.
- **capture:** `collect_type`, `author`, `collect_date`, `device`, `make`, `model`,
  `datetime_original`, `restype`.

Indexed on `source, split, category, basestyle, length, curl, side, sex, color,
partition, bang`. The ~90 mostly-empty EXIF fields are intentionally **not** ingested;
read them from `label_path` if ever needed.

### Example queries

```bash
# category distribution
sqlite3 data/index.sqlite "SELECT basestyle, COUNT(*) c FROM images GROUP BY 1 ORDER BY c DESC;"
# big multi-view sessions (good invariance-probe candidates)
sqlite3 data/index.sqlite "SELECT * FROM sessions WHERE n_views >= 100 ORDER BY n_views DESC LIMIT 10;"
# a clean held-out eval pool
sqlite3 data/index.sqlite "SELECT gid, image_path, hair_bbox FROM images_clean WHERE split_clean='mq-val';"
```

## 5. Leakage-free splits (`splits` table / `images_clean` view)

**The problem:** the authors' split leaks. **400 of 4,347 sessions appear in *both*
train and val** — *different views of the same session* on each side (0 `(source,view)`
pairs are literally duplicated). That covers **57,586 images**, including **19,961 of the
48,252 real val images (~41%)**. A session straddling splits violates the leakage
guard for same-capture positives.

**The fix:** `make_splits.py` assigns every `source` to exactly one split and writes the
`splits` table + `images_clean` view. No images are dropped — minority-side views are
relabeled.

**Default policy = `conflicts-to-train`** (any session appearing in val that also appears
in train goes wholly to **train**), so **val ⊆ authors' val** and no train-origin image
leaks into evaluation. Result:

| | sessions | images |
|---|---|---|
| `split_clean = mq-train` | 4,147 | 423,042 |
| `split_clean = mq-val` | 200 | 28,291 |
| leakage after | **0** | — |

**Trade-off to know:** this policy leaves **5 categories (댄디, 베이비, 보브, 쉼표, 테슬)
with zero val images** (all their val sessions were conflicting → moved to train). If you
need every category represented in val, re-run with a different policy:

```bash
python3 make_splits.py --policy majority           # min images moved; mixes val origins
python3 make_splits.py --policy conflicts-to-val    # mirror of the default
```

The split is a thin, instant-to-recompute layer over the index — switching policy does
**not** require rebuilding `index.sqlite`.

## 6. Browsing + labeling UI (`ui/`)

A zero-dependency local web app for browsing the corpus and writing manual
ground-truth labels. Stdlib only (Python `http.server` + `sqlite3`) — no `pip
install`.

```bash
cd ui
python3 serve.py            # -> http://127.0.0.1:8765   (use --port to change)
```

- Serves the leakage-free `images_clean` view (warns loudly if `make_splits.py`
  hasn't run and it falls back to the raw split).
- **Korean categories are shown with English glosses** (e.g. *Side Part* 가르마,
  *See-Through Dandy* 시스루댄디); salon attribute values are translated too where
  they're controlled vocabulary. Glosses live in `ui/translations.py` — edit there;
  unknown values fall back to the raw Korean. These are display aids only, never
  ground truth.
- **Filters:** split, sex, labeled/unlabeled, session-id search, a category rail with
  per-class counts, and an **attribute panel** — collapsible per-attribute groups for
  multi-select value sets (e.g. curl ∈ {None, S-curl}; counts + EN labels via
  `/api/facets`) plus numeric **range** filters (age, ratings, camera azimuth, bleach
  history). Active filters show as removable chips above the grid with "clear all", and
  the full filter state is mirrored into the URL (`?f_curl=X&f_curl=S&r_age_min=20…`) so
  a filtered view is shareable/bookmarkable.
- **Detail drawer:** large frame, a togglable **hair-bbox** overlay (from the index)
  + mean-color chip, the full salon-attribute table (KO + EN, flagged mining-only),
  and a **session-views strip** (all same-capture views of that `source`).
- **Filter-by-example:** in the detail drawer, click any attribute to toggle it as a
  gallery filter (with a live match count), or hit **"use all as filter"** to populate
  every attribute from the current image — i.e. "find frames like this one". This drives
  categorical `f_*` filters, numeric `r_*` ranges (set to the exact value), the **sex**
  control and the **category**, so the result is fully shareable via URL.
  - Only `basestyle` (shown as **Base style**, equivalent to the picked **Category**)
    and `device` are not wired as filters.
  - **Frontal** (`front`, 0/1) is a categorical filter: set **Frontal = 1** to keep
    ~one representative shot per person+hairstyle (10,420 frontal frames vs 451,333
    total) instead of every camera view of the same session.
  - Caveat: "use all" includes **pose/capture** attributes (camera azimuth/height). Since
    azimuth is per-view, applying it tends to collapse the result to the single source
    frame; deselect the **Camera azimuth/height** chips to find the *same hairstyle across
    poses* (the pose-invariant query the benchmark cares about).
- **Labeling:** writes to `manual_annotations` (keyed by `gid`) — a `keep/review/
  discard` status, free tags, a note, and the annotator id. Keyboard:
  `K`/`R`/`D` label-and-save, `←`/`→` prev/next frame, `Esc` close. Deep-link a
  frame via `#gid=<gid>`. **This is the only place ground truth is written; it is
  kept entirely separate from the mining-only source attributes.**

The annotation `payload` is a free-form JSON blob, so the schema can evolve into the
plan's multi-rater cross-wearer verification without a migration.

## 7. Tooling reference (all in this folder)

| script | does | outputs |
|---|---|---|
| `khairstyle_dl.py` | parallel download from the Synology share | `data/<split>/mqset/{images,labels}/*.zip` |
| `extract.py` | unzip + fix Korean names + merge images/labels | `data/extracted/...` |
| `scan_content.py` | content-vs-extension census | stdout |
| `check_labels.py` | authoritative image↔label audit (content-based) | `data/unlabeled_images.txt`, `data/orphan_labels.txt`, `data/label_audit_summary.json` |
| `sample_fields.py` | label field-coverage sampler (decides which fields to index) | stdout |
| `build_index.py` | build the index (dedups labels, quarantines orphans, extracts fields) | `data/index.sqlite`, `data/index.jsonl` |
| `make_splits.py` | leakage-free split assignment | `splits` table + `images_clean` view inside `index.sqlite` |
| `ui/serve.py` | browsing + labeling web app | writes `manual_annotations` in `index.sqlite` |

## 8. Reproduce from scratch

```bash
cd src/data/khairstyle
python3 khairstyle_dl.py --set mq-train mq-val --dest ./data --run   # ~22 GiB, slow relay
python3 extract.py                                                   # -> data/extracted
python3 check_labels.py                                              # audit (optional but recommended)
python3 build_index.py                                               # -> data/index.sqlite (+ .jsonl), ~5 min
python3 make_splits.py                                               # -> splits table + images_clean view
python3 ui/serve.py                                                  # browse + label at :8765
```

## 9. Do / don't

- **Do** classify files by content; **don't** trust `.jpg`/`.jpeg`/`.json` suffixes.
- **Do** read ground truth from `manual_annotations`; **don't** treat salon attributes as
  labels for the benchmark (it would break attribute independence and make results circular).
- **Do** split on `images_clean.split_clean` (or `source`); **don't** use the raw
  `split` / authors' train-val for any leakage-sensitive evaluation.
- **Do** treat `data/` as ephemeral/derived (git-ignored, rebuildable); **don't** commit
  the multi-hundred-MB index or the dataset payloads.
