"""Build and cache what a window's evaluation needs, then score the baselines on it.

    python -m marketplace_recs.experiment baselines          # window C, 200,000 cases

Co-visitation matrices and test cases are cached under data/, keyed by window, so the
ranker and the baselines are scored on the very same cases.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import polars as pl

from . import candidates as cand
from .covisit import build
from .metrics import evaluate
from .split import STATS_FOR, WINDOWS, cases, stats_events

DATA = Path("data")
RUNS = Path("runs")
N_CASES = {"B": 300_000, "C": 200_000}
SEED = 0


def matrices_for(window: str) -> dict[str, pl.DataFrame]:
    d = DATA / "covis" / window
    kinds = ("time", "type", "buy2buy")
    if not all((d / f"{k}.parquet").exists() for k in kinds):
        d.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        for kind, m in build(stats_events(window)).items():
            m.write_parquet(d / f"{kind}.parquet")
        print(
            f"co-visitation for {window} ({'+'.join(STATS_FOR[window])}): "
            f"{time.perf_counter() - t0:.0f} s",
            flush=True,
        )
    return {k: pl.read_parquet(d / f"{k}.parquet") for k in kinds}


def cases_for(window: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    d = DATA / "cases"
    h, c = d / f"{window}-history.parquet", d / f"{window}-cases.parquet"
    if not (h.exists() and c.exists()):
        d.mkdir(parents=True, exist_ok=True)
        history, cs = cases(window, N_CASES[window], SEED)
        history.write_parquet(h)
        cs.write_parquet(c)
    return pl.read_parquet(h), pl.read_parquet(c)


def popular_for(window: str) -> pl.DataFrame:
    return cand.popular(stats_events(window), WINDOWS[window][0])


def baselines(window: str = "C") -> dict:
    matrices = matrices_for(window)
    history, cs = cases_for(window)
    pop = popular_for(window)
    target = cs["target"].to_numpy()
    out = {}
    for name in ("popular", "history", "item2item", "covis"):
        t0 = time.perf_counter()
        scores = cand.baseline_scores(name, history, matrices, pop)
        run = cand.top_k(scores, cs, pop)
        out[name] = {**evaluate(run, target), "seconds": round(time.perf_counter() - t0, 1)}
        print(name, out[name], flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["baselines"])
    ap.add_argument("--window", default="C")
    a = ap.parse_args(argv)
    RUNS.mkdir(exist_ok=True)
    result = {"window": a.window, "cases": N_CASES[a.window], "seed": SEED, **baselines(a.window)}
    (RUNS / f"baselines_{a.window}.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
