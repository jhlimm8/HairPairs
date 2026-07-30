# K-Hairstyle parallel downloader

Tooling to pull the [K-Hairstyle](https://psh01087.github.io/K-Hairstyle/) dataset
(Kim et al., ICIP 2021), the image source from which HairPairs is built.

## Why this exists

The dataset is published **only** through the authors' Synology Drive public shares,
served over a **QuickConnect relay** that throttles each connection to **~140 KiB/s**.
A browser "Download" click uses a single stream, so the full set crawls in at that rate.

Measured on the relay (`davian-lab.tw2.quickconnect.to`):

| connections | throughput |
|---|---|
| 1 | ~141 KiB/s |
| 16 | ~2.3 MiB/s (≈ linear) |

The throttle is **per connection**, and the endpoint honors **HTTP Range** (returns
`206 Partial Content`). So the win is splitting each file across many ranged
connections with `aria2c`. This tool enumerates a share's files and feeds per-file,
range-splittable URLs to aria2c.

## Scope

Focus is the **minimally-cropped 512×512 set (`mqset`)**, train + val:

| set | images.zip | labels.zip | total |
|---|---|---|---|
| `mq-train` | 15.4 GiB | 4.55 GiB | **~20 GiB** |
| `mq-val` | 1.78 GiB | 0.58 GiB | **~2.4 GiB** |

Each share is just two `.zip` files (`images/`, `labels/`), so this fits a laptop.
`hq-*` (1024²) and `raw-*` (4032×3024, ~1 TB) shares are also wired up if needed.

## Requirements

- Python 3 (stdlib only)
- `aria2c` (`brew install aria2`)

## Usage

```bash
# inspect file tree + sizes (no download)
python3 khairstyle_dl.py --set mq-train mq-val --list

# generate the aria2c input file only (then run aria2c yourself)
python3 khairstyle_dl.py --set mq-train --dest ./data/khairstyle --dry-run

# download train + val with resume + automatic token refresh
python3 khairstyle_dl.py --set mq-train mq-val --dest ./data/khairstyle --run
```

Files land under `<dest>/<set>/mqset/{images,labels}/*.zip`. Downloads resume
(`aria2c -c`); re-run the same command to continue an interrupted transfer.

### Tuning

- `--connections N` (default 16): ranged connections/splits per file (`aria2 -x/-s`).
- `--jobs N` (default 2): files downloaded in parallel (`aria2 -j`).

Total relay connections ≈ `connections * jobs`. Keep it **≤ ~32**: opening too many
too fast gets the IP temporarily rate-limited by the relay. If a transfer stalls or
errors, just wait and re-run — aria2c resumes from where it left off.

## How it works (reverse-engineered Synology Drive sharing API)

For a public share `/d/s/<permanent_link>/<sharing_link>`:

1. **Session.** `GET` the share landing → `Set-Cookie: drive-sharing-<sharing_link>=<token>`.
   That cookie value is the `sharing_token` every Drive API call must carry.
2. **List.** `SYNO.SynologyDrive.Files/list` (v2) with `path="link:<permanent_link>"`
   lists the root; subfolders via `path="id:<file_id>"`. Pagination via `offset`/`limit`.
3. **Download.** `SYNO.SynologyDrive.Files/download` (v2) with `files=["id:<file_id>"]`,
   `force_download=true`. `sharing_token` rides in the query string, so aria2c needs no
   cookies, and the response supports Range → splittable.

The relay region host is resolved at runtime via QuickConnect's `Serv.php`
(`get_server_info`), falling back to `davian-lab.tw2.quickconnect.to`.

Tokens can expire on long transfers; `--run` refreshes the token, rewrites the input
file, and relaunches aria2c (with backoff) until everything completes.

## Extracted layout, data fixes & the index

`extract.py` unpacks the zips (recovering the cp437-mangled Korean folder names) into
`data/extracted/<split>/0002.mqset/<NNNN.basestyle>/<NNNN.source>/<source>-<view>.{jpg,json}`.

This `README` is just the **acquisition + extraction** tool. Everything about the data's
*state* — the content-vs-extension quirks, the label audit, the queryable index
(`data/index.sqlite`), the leakage-free splits, and the problem statements that shaped
them — is documented in **[`DATA_NOTES.md`](./DATA_NOTES.md)**. Read that before querying
or labeling.

Quick reference of the tooling (all rebuildable; `data/` is git-ignored):

| script | does |
|---|---|
| `scan_content.py` | content-vs-extension census |
| `check_labels.py` | authoritative image↔label audit (content-based) |
| `sample_fields.py` | label field-coverage sampler |
| `build_index.py` | build the index → `data/index.sqlite` (+ `.jsonl`) |
| `make_splits.py` | leakage-free split assignment → `splits` table + `images_clean` view |
| `ui/serve.py` | browsing + labeling web app (`python3 ui/serve.py` → :8765) |
