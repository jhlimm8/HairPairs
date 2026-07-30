#!/usr/bin/env python3
"""In-domain TRAINED baseline for [HairPairs] (no test leakage).

We freeze a DINOv2 ViT-B/14 backbone and train a small projection+classifier head
on K-Hairstyle `basestyle` labels, using ONLY leakage-free mq-train sources that are
disjoint from every adjudicated eval source (train_manifest.json, produced by
bundle_gpu.py). The learned 256-d L2-normalised projection is the representation.

This asks: does an embedding *explicitly supervised on the schema* separate the
collision hard-negatives that the schema itself collapses? We export the eval-source
embeddings to emb_cache/dinov2_trained.npz, so `baselines.py --encoders dinov2_trained`
scores it with the exact same verification protocol as the frozen encoders.

    python train_baseline.py                 # trains head, writes emb_cache/dinov2_trained.npz
    python -m baselines --encoders dinov2 clip siglip dinov2_trained   # then score it
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import baselines as B  # noqa: E402

BACKBONE = "dinov2"          # frozen feature extractor (reuses emb_cache/dinov2.npz)
PROJ_DIM = 256
EPOCHS = 30
LR = 1e-3
WD = 1e-4
SEED = 20260616


def pick_device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def main():
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    torch.manual_seed(SEED)
    np.random.seed(SEED)

    device = pick_device()
    con = sqlite3.connect(B.DB)
    con.row_factory = sqlite3.Row

    pops, eval_sources = B.load_populations(con)

    tm = json.load(open(os.path.join(HERE, "train_manifest.json")))
    classes = sorted({t["basestyle"] for t in tm})
    cidx = {c: i for i, c in enumerate(classes)}

    # ---- gather all gids we must embed with the frozen backbone (train + eval)
    need = {t["gid"]: t["path"] for t in tm}
    eval_gp = []
    for s in eval_sources:
        prim, alt = B.two_triples(con, s)
        for g, p in prim + alt:
            need[g] = p
            eval_gp.append((g, p))
    con.close()

    print(f"embedding {len(need)} views with frozen {BACKBONE} on {device} ...")
    feat = B.embed_all(sorted(need.items()), BACKBONE, device)  # {gid: 768-d L2}
    dim = len(next(iter(feat.values())))

    # ---- build train tensors (one row per train view)
    Xtr = np.stack([feat[t["gid"]] for t in tm if t["gid"] in feat]).astype(np.float32)
    ytr = np.array([cidx[t["basestyle"]] for t in tm if t["gid"] in feat], dtype=np.int64)
    print(f"train views={len(Xtr)}  dim={dim}  classes={len(classes)}")

    Xtr_t = torch.from_numpy(Xtr).to(device)
    ytr_t = torch.from_numpy(ytr).to(device)

    class Head(nn.Module):
        def __init__(self, d_in, d_proj, n_cls):
            super().__init__()
            self.proj = nn.Sequential(nn.Linear(d_in, d_proj), nn.GELU(),
                                      nn.Linear(d_proj, d_proj))
            self.cls = nn.Linear(d_proj, n_cls)

        def embed(self, x):
            return F.normalize(self.proj(x), dim=-1)

        def forward(self, x):
            z = self.embed(x)
            return self.cls(z)

    head = Head(dim, PROJ_DIM, len(classes)).to(device)
    opt = torch.optim.AdamW(head.parameters(), lr=LR, weight_decay=WD)
    lossf = nn.CrossEntropyLoss()

    bs = 512
    n = len(Xtr_t)
    for ep in range(EPOCHS):
        perm = torch.randperm(n, device=device)
        tot = 0.0
        head.train()
        for i in range(0, n, bs):
            idx = perm[i:i + bs]
            opt.zero_grad()
            out = head(Xtr_t[idx])
            loss = lossf(out, ytr_t[idx])
            loss.backward()
            opt.step()
            tot += loss.item() * len(idx)
        if ep % 5 == 0 or ep == EPOCHS - 1:
            head.eval()
            with torch.no_grad():
                acc = (head(Xtr_t).argmax(1) == ytr_t).float().mean().item()
            print(f"  epoch {ep:2d}  loss={tot/n:.4f}  train_acc={acc:.3f}")

    # ---- export eval-source embeddings under the frozen protocol
    head.eval()
    out_emb = {}
    with torch.no_grad():
        for g, _ in eval_gp:
            if g not in feat:
                continue
            x = torch.from_numpy(feat[g][None, :]).to(device)
            z = head.embed(x).cpu().numpy()[0].astype(np.float32)
            out_emb[g] = z
    os.makedirs(B.CACHE, exist_ok=True)
    np.savez(os.path.join(B.CACHE, "dinov2_trained.npz"), **out_emb)
    print(f"wrote {os.path.join(B.CACHE, 'dinov2_trained.npz')}  ({len(out_emb)} eval views)")


if __name__ == "__main__":
    main()
