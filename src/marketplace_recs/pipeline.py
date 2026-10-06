"""The nightly job, as plain functions: the Airflow DAG only wires them together.

    python -m marketplace_recs.pipeline 2019-10-25       # one night, without Airflow

    check     the day's events are complete, and their volume is within a sane range of the
              trailing week, so a broken upstream feed fails the run instead of quietly
              shrinking the tables
    build     co-visitation matrices, product statistics and popularity from every event
              up to the end of the day, exported as a new version under model/versions/
    validate  the new version loads, its sizes are close to the serving version's, its
              neighbour lists are stable, and it ranks a fixed set of sessions cleanly
    publish   point model/CURRENT at the new version; the API swaps on its next request

The ranker itself is not retrained nightly: each version copies the serving version's
model. refresh.py measures what the nightly tables are worth offline; this job's gates
only stop a bad version from reaching the API.

Where check and build read the events is configuration: RECS_WAREHOUSE unset scans
data/*.parquet with polars on this machine; RECS_WAREHOUSE=bigquery runs the same
statistics as SQL in BigQuery (warehouse.py) and reads back only the finished tables.

    python -m marketplace_recs.pipeline 2019-10-25 --warehouse bigquery
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from .online import Event, Model, write_artifacts

MODEL = Path(os.environ.get("RECS_MODEL_DIR", "model"))
WAREHOUSE = os.environ.get("RECS_WAREHOUSE", "local")
VERSIONS = "versions"
CURRENT = "CURRENT"


class GateError(RuntimeError):
    """A check or validation refused the night's data or tables."""


def serving_dir(model: Path = MODEL) -> Path:
    """The directory the API serves: model/versions/<CURRENT>, or model/ itself."""
    pointer = model / CURRENT
    if pointer.exists():
        return model / VERSIONS / pointer.read_text(encoding="utf-8").strip()
    return model


def check(day: datetime, events, min_ratio: float = 0.5, max_ratio: float = 2.0) -> dict:
    """The day's events against the median of the seven days before it."""
    return gate(day, daily_counts(day, events), min_ratio, max_ratio)


def daily_counts(day: datetime, events):
    """Events per day over `day` and the seven days before it, and each day's last."""
    import polars as pl

    start = day - timedelta(days=7)
    return (
        events.filter(
            (pl.col("event_time") >= start) & (pl.col("event_time") < day + timedelta(days=1))
        )
        .group_by(pl.col("event_time").dt.truncate("1d").alias("d"))
        .agg(pl.len().alias("n"), pl.col("event_time").max().alias("last"))
        .collect()
        .sort("d")
    )


def gate(day: datetime, counts, min_ratio: float = 0.5, max_ratio: float = 2.0) -> dict:
    """Refuse a day with no events, one that stops before 23:00, or one whose volume is
    outside [min_ratio, max_ratio] of the trailing median."""
    import polars as pl

    today = counts.filter(pl.col("d") == day)
    if today.height == 0:
        raise GateError(f"no events at all for {day:%Y-%m-%d}")
    n, last = int(today["n"][0]), today["last"][0]
    if last < day + timedelta(hours=23):
        raise GateError(f"{day:%Y-%m-%d} ends at {last}: the day is not complete")
    before = counts.filter(pl.col("d") < day)["n"]
    if before.len():
        ratio = n / float(before.median())
        if not min_ratio <= ratio <= max_ratio:
            raise GateError(f"{n:,} events is {ratio:.2f} times the trailing median")
    else:
        ratio = None
    return {"day": f"{day:%Y-%m-%d}", "events": n, "ratio_to_trailing_median": ratio}


def build(day: datetime, events, model: Path = MODEL, tables=None) -> str:
    """A new version from every event before the end of `day`. `tables` may hand over
    (matrices, product stats, popular ids) already built, as the experiments cache them."""
    import polars as pl

    from . import candidates as cand
    from .covisit import build as build_matrices
    from .features import product_stats

    end = day + timedelta(days=1)
    version = f"{day:%Y-%m-%d}"
    out = model / VERSIONS / version
    t0 = time.perf_counter()
    if tables is None:
        stats = events.filter(pl.col("event_time") < end)
        tables = (build_matrices(stats), product_stats(stats, end), cand.popular(stats, end))
    matrices, products, pop = tables
    ranker = serving_dir(model) / "ranker.txt"
    staging = out.with_name(out.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    meta = write_artifacts(staging, matrices, products, pop["product_id"].to_list(), ranker)
    meta.update(
        {
            "version": version,
            "events_until": f"{end:%Y-%m-%dT%H:%M:%SZ}",
            "built_in_s": round(time.perf_counter() - t0, 1),
        }
    )
    (staging / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    shutil.rmtree(out, ignore_errors=True)
    staging.replace(out)  # a version directory is either complete or absent
    return version


def probe_sessions(model: Model, n: int = 50) -> list[list[Event]]:
    """Fixed two-event sessions over the most popular products: enough to show the
    version ranks, deterministic from one night to the next."""
    pop = model.popular[: n + 1]
    return [[Event(0, 0, pop[i]), Event(60, 0, pop[i + 1])] for i in range(min(n, len(pop) - 1))]


def validate(
    version: str, model: Path = MODEL, min_overlap: float = 0.2, max_size_change: float = 0.25
) -> dict:
    new = Model(model / VERSIONS / version)
    report: dict = {"version": version, "products": len(new.products)}
    current_dir = serving_dir(model)
    if (current_dir / "ranker.txt").exists() and current_dir != model / VERSIONS / version:
        cur = Model(current_dir, booster=new.booster)
        change = abs(len(new.products) - len(cur.products)) / max(len(cur.products), 1)
        report["product_count_change"] = round(change, 4)
        if change > max_size_change:
            raise GateError(f"product count moved {change:.0%} since the serving version")
        overlaps = []
        for p in cur.popular[:1000]:
            a, b = _neighbours(cur, p), _neighbours(new, p)
            if a and b:
                overlaps.append(len(a & b) / len(a | b))
        report["neighbour_overlap_mean"] = round(float(np.mean(overlaps)), 4) if overlaps else None
        if overlaps and np.mean(overlaps) < min_overlap:
            raise GateError(f"neighbour lists changed too much: overlap {np.mean(overlaps):.2f}")
    ranked = 0
    for events in probe_sessions(new):
        recs = new.recommend(events, k=20)
        if len(recs) != 20 or any(not np.isfinite(s) for _, s in recs):
            raise GateError(f"probe session ranked badly: {recs[:3]}")
        ranked += 1
    report["probe_sessions_ranked"] = ranked
    return report


def _neighbours(model: Model, product: int, kind: str = "time") -> set[int]:
    i = model.index.get(product)
    if i is None:
        return set()
    indptr, nbr, _ = model.csr[kind]
    return set(nbr[indptr[i] : indptr[i + 1]].tolist())


def publish(version: str, model: Path = MODEL) -> str:
    """Point CURRENT at `version`, atomically: a reader sees the old name or the new one."""
    if not (model / VERSIONS / version / "ranker.txt").exists():
        raise GateError(f"version {version} is not complete")
    tmp = model / (CURRENT + ".tmp")
    tmp.write_text(version, encoding="utf-8")
    os.replace(tmp, model / CURRENT)
    return version


def check_night(day: datetime, warehouse: str | None = None) -> dict:
    """The check step, wherever the events live."""
    if (warehouse or WAREHOUSE) == "bigquery":
        from . import warehouse as wh

        return gate(day, wh.daily_counts(day))
    from .split import events

    return check(day, events())


def build_night(day: datetime, model: Path = MODEL, warehouse: str | None = None) -> str:
    """The build step, wherever the events live: BigQuery builds the tables and only they
    come back; locally polars builds them from the Parquet file."""
    if (warehouse or WAREHOUSE) == "bigquery":
        from . import warehouse as wh

        tables, report = wh.tables(day)
        print(json.dumps({"bigquery": report}))
        return build(day, None, model, tables)
    from .split import events

    return build(day, events(), model)


def run(day: datetime, model: Path = MODEL, warehouse: str | None = None) -> dict:
    t0 = time.perf_counter()
    out = {"check": check_night(day, warehouse)}
    version = build_night(day, model, warehouse)
    out["validate"] = validate(version, model)
    out["published"] = publish(version, model)
    out["seconds"] = round(time.perf_counter() - t0, 1)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("day", help="the night to run, e.g. 2019-10-25: tables include that day")
    ap.add_argument("--warehouse", choices=["local", "bigquery"], default=WAREHOUSE)
    a = ap.parse_args(argv)
    day = datetime.strptime(a.day, "%Y-%m-%d").replace(tzinfo=UTC)
    print(json.dumps(run(day, warehouse=a.warehouse), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
