# [HairPairs] baseline re-run: upload + run on a CUDA pod

Full re-inference of the six recognisers under the **label-based, cross-session**
protocol (`baselines.py`). The rental disk was wiped, so this rebuilds every
embedding cache from scratch. Total transfer is small: **0.69 GB images + 0.39 GB
`index.sqlite`** (the manifest `gpu_needed_files.txt` lists exactly 16,381 files).

Output artefacts to pull back: `baselines.json`, `fig_distance_dist.png`, and
`emb_cache/*.npz`. Bringing the `.npz` back means every later metric/figure tweak
is CPU-only and never needs the GPU again.

## Directory layout the scripts expect (mirror of the repo subtree)

```
khairstyle/
  analysis/    baselines.py  train_baseline.py  bundle_gpu.py  train_manifest.json
               requirements-gpu.txt  run_gpu.sh
  adjudicate/  views.py
               experiments/attr-suff-v4/frame.json
  data/        index.sqlite
               extracted/<images from gpu_needed_files.txt>
```

## Steps

```bash
# --- 0) connection (from the rental dashboard) ---
POD="root@POD_HOST"          # add -p PORT if your pod uses a non-22 SSH port
REMOTE="khairstyle"          # remote path, relative to the pod's home dir
LOCAL="$(git rev-parse --show-toplevel)/src/data/khairstyle"

# --- 1) remote skeleton ---
ssh $POD "mkdir -p $REMOTE/analysis $REMOTE/adjudicate/experiments/attr-suff-v4 $REMOTE/data/extracted"

# --- 2) code + run scripts ---
rsync -avz \
  "$LOCAL/analysis/baselines.py" "$LOCAL/analysis/train_baseline.py" \
  "$LOCAL/analysis/bundle_gpu.py" "$LOCAL/analysis/train_manifest.json" \
  "$LOCAL/analysis/requirements-gpu.txt" "$LOCAL/analysis/run_gpu.sh" \
  $POD:$REMOTE/analysis/
rsync -avz "$LOCAL/adjudicate/views.py" $POD:$REMOTE/adjudicate/
rsync -avz "$LOCAL/adjudicate/experiments/attr-suff-v4/frame.json" \
  $POD:$REMOTE/adjudicate/experiments/attr-suff-v4/

# --- 3) data: DB (387 MB) + image subset (0.69 GB, paths relative to data/extracted) ---
rsync -avz "$LOCAL/data/index.sqlite" $POD:$REMOTE/data/
rsync -avz --files-from="$LOCAL/analysis/gpu_needed_files.txt" \
  "$LOCAL/data/extracted/" $POD:$REMOTE/data/extracted/

# --- 4) run (installs a venv, trains the head, scores all six) ---
ssh $POD "cd $REMOTE/analysis && bash run_gpu.sh"

# --- 5) pull results back (incl. the embedding caches) ---
rsync -avz $POD:$REMOTE/analysis/baselines.json "$LOCAL/analysis/"
rsync -avz $POD:$REMOTE/analysis/fig_distance_dist.png "$LOCAL/analysis/"
rsync -avz $POD:$REMOTE/analysis/emb_cache/ "$LOCAL/analysis/emb_cache/"
```

## Notes

- `run_gpu.sh` runs `train_baseline.py` **before** `baselines.py` on purpose: the
  in-domain head must write `dinov2_trained.npz` (and populate `dinov2.npz`) before
  `--encoders dinov2_trained` is scored.
- SigLIP runs on the GPU here; the CPU fallback in `baselines.py` triggers only on
  Apple MPS.
- If `torch.cuda.is_available()` prints `False`, the pip wheel is CPU-only; install
  the CUDA build per the comment in `requirements-gpu.txt`, then re-run.
- Sanity: the run should print `POS=10 (crosswearer=5 + miss_same=5) NEG=548`.
