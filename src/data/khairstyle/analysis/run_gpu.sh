#!/usr/bin/env bash
# Full [HairPairs] baseline re-run on a CUDA pod. Run from khairstyle/analysis/.
#   bash run_gpu.sh
# Produces: baselines.json, fig_distance_dist.png, and emb_cache/*.npz for all 6
# recognisers (attribute-lookup floor is computed inside baselines.py, no GPU).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv-gpu ]; then
  python3 -m venv .venv-gpu
fi
# shellcheck disable=SC1091
source .venv-gpu/bin/activate
pip install --upgrade pip
pip install -r requirements-gpu.txt

python -c "import torch; print('cuda available:', torch.cuda.is_available()); \
print('device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"

# 1) in-domain head: fits on frozen DINOv2-B features (schema/basestyle labels),
#    caching dinov2.npz (train+eval) and writing dinov2_trained.npz (eval).
python train_baseline.py

# 2) score all six recognisers under the label-based, cross-session protocol.
python -m baselines \
  --device cuda \
  --encoders dinov2 dinov2_large clip siglip dinov2_trained

echo
echo "DONE. Pull back: baselines.json, fig_distance_dist.png, emb_cache/*.npz"
