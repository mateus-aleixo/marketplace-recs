"""What a nightly rebuild buys: statistics frozen for the week against rebuilt each day.

    python -m marketplace_recs.refresh            # runs/refresh_C.json

Every number so far scores the test week with tables built from the events before it
starts, so by the last day they are a week old. Here the same 200,000 cases are scored day
by day twice: with those frozen tables, and with co-visitation matrices, product counts
and popularity rebuilt from every event before the day. The ranker is the same model
both times. Only the tables it reads change, which is exactly what a nightly job would
refresh, and each rebuild's time is the job's running time.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

from . import candidates as cand
from . import experiment as ex
from .covisit import build as build_matrices
from .features import build as build_features
from .features import product_stats
from .metrics import evaluate
from .spec import FEATURES
from .split import WINDOWS, events, stats_events

DAILY = Path("data") / "covis" / "daily"
KINDS = ("time", "type", "buy2buy")


def tables_before(day) -> tuple[dict, pl.DataFrame, pl.DataFrame, float]:
    """Matrices, product statistics and popularity from every event before `day`."""
    d = DAILY / day.strftime("%Y-%m-%d")
    stats = events().filter(pl.col("event_time") < day)
    seconds = 0.0
    if not all((d / f"{k}.parquet").exists() for k in KINDS):
        d.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        for kind, m in build_matrices(stats).items():
            m.write_parquet(d / f"{kind}.parquet")
        seconds = time.perf_counter() - t0
    matrices = {k: pl.read_parquet(d / f"{k}.parquet") for k in KINDS}
    return matrices, product_stats(stats, day), cand.popular(stats, day), seconds


def score(history, cases, tables, booster) -> np.ndarray:
    matrices, products, pop = tables
    df = build_features(history, cases, matrices, products).with_columns(
        pl.col(FEATURES).cast(pl.Float32)
    )
    pred = booster.predict(df.select(FEATURES).to_numpy())
    scores = df.select("session", "product_id").with_columns(pl.Series("score", pred))
    return cand.top_k(scores, cases, pop)


def main() -> int:
    history, cases = ex.cases_for("C")
    booster = lgb.Booster(model_file=str(Path("data") / "models" / "ranker.txt"))
    start = WINDOWS["C"][0]
    frozen = (
        ex.matrices_for("C"),
        product_stats(stats_events("C"), start),
        ex.popular_for("C"),
    )
    days, runs = [], {"frozen": [], "nightly": []}
    targets = []
    for i in range(8):
        day = start + timedelta(days=i)
        part = cases.filter(
            (pl.col("cut_time") >= day) & (pl.col("cut_time") < day + timedelta(days=1))
        ).sort("session")
        h = history.join(part.select("session"), on="session", how="semi")
        if i == 0:
            nightly, seconds = frozen, 0.0  # the same tables on the first day
        else:
            m, p, pop, seconds = tables_before(day)
            nightly = (m, p, pop)
        t = part["target"].to_numpy()
        row = {
            "day": day.strftime("%Y-%m-%d"),
            "cases": part.height,
            "rebuild_s": round(seconds, 1),
        }
        for name, tables in (("frozen", frozen), ("nightly", nightly)):
            run = score(h, part, tables, booster)
            runs[name].append(run)
            row[name] = evaluate(run, t)
        targets.append(t)
        days.append(row)
        print(json.dumps(row), flush=True)
    t_all = np.concatenate(targets)
    out = {
        "days": days,
        "overall": {name: evaluate(np.concatenate(r), t_all) for name, r in runs.items()},
    }
    ex.RUNS.mkdir(exist_ok=True)
    (ex.RUNS / "refresh_C.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out["overall"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
