"""Ranking metrics for one target item per test case.

A run is an (n_cases, k) array of product ids in rank order, padded with -1. Every
metric averages over all cases, so a case whose target was never retrieved counts as a
zero rather than dropping out.
"""

from __future__ import annotations

import numpy as np


def target_rank(run: np.ndarray, target: np.ndarray) -> np.ndarray:
    """1-based rank of each case's target within its row, 0 when absent."""
    hit = run == target[:, None]
    found = hit.any(axis=1)
    return np.where(found, hit.argmax(axis=1) + 1, 0)


def evaluate(run: np.ndarray, target: np.ndarray) -> dict:
    rank = target_rank(run, target)
    found = rank > 0
    inv = np.where(found, 1.0 / np.maximum(rank, 1), 0.0)
    gain = np.where(found & (rank <= 10), 1.0 / np.log2(np.maximum(rank, 1) + 1), 0.0)
    return {
        "cases": int(len(target)),
        "recall@20": round(float((found & (rank <= 20)).mean()), 4),
        "recall@10": round(float((found & (rank <= 10)).mean()), 4),
        "ndcg@10": round(float(gain.mean()), 4),
        "mrr@20": round(float(np.where(rank <= 20, inv, 0.0).mean()), 4),
    }
