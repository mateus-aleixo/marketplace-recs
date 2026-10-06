"""A small synthetic marketplace, run through the whole offline pipeline once per session.

Shared by the parity, API and pipeline tests, so CI checks the serving path end to end
with no data and no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from marketplace_recs import candidates as cand
from marketplace_recs.covisit import build as covis
from marketplace_recs.features import build, product_stats
from marketplace_recs.online import write_artifacts
from marketplace_recs.spec import FEATURES
from marketplace_recs.split import WINDOWS, cases


def synthetic_events(seed: int = 0) -> pl.DataFrame:
    """300 sessions before the test window, 120 inside it. Products 1..80 in 8 categories;
    sessions wander within a category and sometimes jump, view mostly, cart sometimes."""
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(420):
        day = datetime(2019, 10, 20, tzinfo=UTC) if s < 300 else datetime(2019, 10, 25, tzinfo=UTC)
        t = day + timedelta(minutes=int(rng.integers(0, 60 * 20)))
        p = int(rng.integers(1, 81))
        for _ in range(int(rng.integers(2, 9))):
            if rng.random() < 0.3:
                p = int(rng.integers(1, 81))
            else:
                p = (p // 10) * 10 + int(rng.integers(0, 10)) or 1
            etype = 1 if rng.random() < 0.1 else 0
            rows.append((t, etype, p, p // 10, f"brand{p % 4}" if p % 7 else None, 10.0 + p, s))
            t += timedelta(seconds=int(rng.integers(5, 300)))
    return pl.DataFrame(
        rows,
        schema={
            "event_time": pl.Datetime("us", "UTC"),
            "event_type": pl.UInt8,
            "product_id": pl.Int32,
            "category_id": pl.Int64,
            "brand": pl.String,
            "price": pl.Float32,
            "session": pl.Int32,
        },
        orient="row",
    ).sort("event_time", "session", maintain_order=True)


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    import lightgbm as lgb  # here, so the DAG tests can run without it

    ev = synthetic_events()
    start = WINDOWS["C"][0]
    stats = ev.lazy().filter(pl.col("event_time") < start)
    matrices = covis(stats, parts=2)
    products = product_stats(stats, start)
    history, cs = cases("C", 200, seed=0, lf=ev.lazy())
    offline = build(history, cs, matrices, products).with_columns(pl.col(FEATURES).cast(pl.Float32))
    pop = cand.popular(stats, start)

    # A real (tiny) LambdaRank model, so the exported file is what serving loads.
    groups = offline.group_by("session", maintain_order=True).len()["len"].to_numpy()
    booster = lgb.train(
        {"objective": "lambdarank", "min_data_in_leaf": 1, "verbose": -1, "seed": 0},
        lgb.Dataset(offline.select(FEATURES).to_numpy(), offline["label"].to_numpy(), group=groups),
        num_boost_round=5,
    )
    out = tmp_path_factory.mktemp("serve")
    booster.save_model(str(out / "trained.txt"))
    write_artifacts(out, matrices, products, pop["product_id"].to_list(), out / "trained.txt")
    return {
        "events": ev,
        "history": history,
        "cases": cs,
        "offline": offline,
        "pop": pop,
        "dir": out,
    }
