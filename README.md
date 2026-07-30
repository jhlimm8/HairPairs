# HairPairs

Code, analysis, and release tooling for **HairPairs**, a cross-session, wearer-invariant,
pairwise-judged hairstyle instance dataset built on de-identified
[K-Hairstyle](https://psh01087.github.io/K-Hairstyle/) crops.

> **Neither Name nor Schema: Pairwise Instance Labels for Hairstyle**
> ILR+G 2026 workshop at ECCV 2026 (short paper, non-archival).

Instance-level recognition presupposes a *catalogue*. Hairstyle has none: its space is too
dense to name (the **token** framing) and K-Hairstyle's cut-and-shape attribute schema does
not separate instances (the **configuration** framing). HairPairs instead specifies instances
by *relative observed position* — a human oracle judges pairs of crops same/different
directly, committing to no attribute schema.

Two results fall out. The attribute schema is nowhere near sufficient: sources sharing
*every* cut-and-shape attribute are judged different hairstyles in **98.8%** of pairs
(410/415). And evaluated strictly on these labels, five recognisers separate genuine matches
from adjudicated non-matches only weakly, with neither scale nor in-domain supervision
helping.

## Dataset composition

558 adjudicated cross-session pairs, of which only 10 (1.8%) are positive — deliberately
small and imbalanced, and released as a **verification** protocol rather than retrieval,
which would presuppose the ranked catalogue this setting lacks.

| Regime | Pairs | Meaning |
| --- | --- | --- |
| `collision_hard_negative` | 410 | Share every cut-and-shape attribute, judged **different** |
| `collision_positive` | 5 | Share every attribute, judged **same** |
| `miss_diff` | 138 | Differ in exactly one attribute, judged **different** |
| `miss_same` | 5 | Differ in exactly one attribute, judged **same** |

Reliability, on a 50-item triple-annotated subset: raw agreement 93% on splits, 95% on
merges. Chance-corrected coefficients read low (Fleiss $\kappa$ 0.17–0.27) only through the
prevalence paradox — with ~96% "different" verdicts, chance agreement is already high.

## Verification baselines

Each source is the mean of its three canonical view embeddings; pairs are scored by cosine.
Chance AP at this prevalence is **0.018**. Intervals are stratified bootstrap 95% CIs (2,000
replicates, positives and negatives resampled independently so prevalence is held fixed).

| Recogniser | ROC-AUC [95% CI] | AP | TAR@10%FAR | Attribute-identical matches found (of 5) |
| --- | --- | --- | --- | --- |
| CLIP ViT-B/16 (OpenAI) | 0.804 [0.677, 0.906] | 0.156 | 0.30 | 2 |
| SigLIP ViT-B/16 (WebLI) | 0.733 [0.597, 0.845] | 0.074 | 0.20 | 1 |
| DINOv2 ViT-B/14 | 0.715 [0.573, 0.839] | 0.062 | 0.30 | 2 |
| DINOv2 ViT-B/14 + in-domain head | 0.675 [0.490, 0.832] | 0.054 | 0.20 | 1 |
| DINOv2 ViT-L/14 | 0.671 [0.488, 0.825] | 0.085 | 0.30 | 2 |

Paired bootstrap contrasts (which cancel the sampling noise two recognisers share) show
scale does not help — ViT-L vs ViT-B is −0.044 AUC (p = 0.028) — and neither does training a
head on K-Hairstyle's own `basestyle` labels (−0.041 AUC vs the frozen backbone, p = 0.50).
Representations capture what a hairstyle *looks like*, not *which* one it is.

Full numbers: [`baselines.json`](src/data/khairstyle/analysis/baselines.json),
[`baselines_ci.json`](src/data/khairstyle/analysis/baselines_ci.json),
[`composition.json`](src/data/khairstyle/analysis/composition.json),
[`agreement.json`](src/data/khairstyle/analysis/agreement.json).

## What is and isn't in this repo

Everything here is either source code or a small derived result. Three things are
deliberately absent, all reconstructible with the steps below:

- **K-Hairstyle imagery** — redistributed by its authors only, under their terms. Fetch it
  with `khairstyle_dl.py`.
- **`data/index.sqlite`** (~390 MB) — the derived image/label index, rebuilt by
  `build_index.py`.
- **`analysis/emb_cache/*.npz`** (~68 MB) and `train_manifest.json` — embedding caches and
  the training manifest, rebuilt by `run_gpu.sh` and `bundle_gpu.py`.

Start with [`src/data/khairstyle/DATA_NOTES.md`](src/data/khairstyle/DATA_NOTES.md), which
records what the source data *is*, every fix applied to it, and why — including the
cross-split session leakage in the authors' original train/val split. Read it before
querying the index or labelling.

## Reproducing end to end

Python 3 with stdlib only for acquisition and indexing; `aria2c` for the download; see
[`requirements-gpu.txt`](src/data/khairstyle/analysis/requirements-gpu.txt) for the
encoder runs. All commands run from `src/data/khairstyle/`.

```sh
# 1. acquire and unpack (~22 GB for mq-train + mq-val)
python3 khairstyle_dl.py --set mq-train mq-val --dest ./data/khairstyle --run
python3 extract.py

# 2. audit, index, and assign leakage-free splits
python3 check_labels.py
python3 build_index.py
python3 make_splits.py

# 3. draw the FROZEN adjudication frame, then label it
python3 analysis/mine_frame.py          # writes adjudicate/experiments/<exp>/frame.json
python3 adjudicate/serve.py             # labelling UI on :8765

# 4. dataset-level analysis
python3 analysis/composition.py         # pair composition + sufficiency/minimality rates
python3 analysis/agreement.py           # inter-annotator agreement
python3 analysis/export_labels.py       # flatten verdicts -> hairpairs_labels.json

# 5. baselines (GPU; see analysis/UPLOAD.md for the rented-pod recipe)
python3 analysis/bundle_gpu.py          # minimal file set to ship
bash analysis/run_gpu.sh                # train_baseline.py then baselines.py
python3 analysis/baselines_ci.py        # bootstrap CIs (CPU-only, reads baselines.json)
```

`mine_frame.py` is a **pre-registration** step: it draws the candidate set with a fixed seed
and must be run once and committed *before* any labelling, since the conditional rates
reported in the paper depend on the drawn ids being fixed in advance.

### Layout

| Path | Contents |
| --- | --- |
| `src/data/khairstyle/` | Acquisition, extraction, auditing, and indexing |
| `src/data/khairstyle/ui/` | Browsing/labelling web app for the indexed corpus |
| `src/data/khairstyle/adjudicate/` | Adjudication server, canonical 3-view selection, frozen per-experiment frames |
| `src/data/khairstyle/analysis/` | Sampling, composition, agreement, baselines, and export |

## Ethics and scope

No new imagery was captured and no subjects were recruited. The source is an existing
salon-capture dataset whose faces are already de-identified; labellers saw hair crops only,
so no wearer identity was shown or inferable.

A hairstyle matcher is a soft biometric, and the re-identification literature treats
hairstyle as precisely the cue to *suppress* when linking people across cameras. Hairstyle is
mutable and therefore a weak long-horizon identifier, but within a short window instance-level
matching could support linking without consent. What is released here is accordingly a small,
imbalanced verification protocol for studying representation — not a deployable matcher.
K-Hairstyle also records one population and one salon vocabulary, so both the labels and the
conclusions drawn from them are scoped to that setting.

## License

A code license for this repository has not been chosen yet; until one is added, no rights
are granted beyond viewing. The underlying K-Hairstyle imagery will not be covered by it in
any case, and remains subject to the terms set by its authors (Kim et al., ICIP 2021).
