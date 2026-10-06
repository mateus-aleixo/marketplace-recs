"""The nightly tables in BigQuery: the statistics covisit.py and features.py build with
polars, as SQL over the warehouse's copy of the events.

    python -m marketplace_recs.warehouse load               # the month into recs.events
    python -m marketplace_recs.warehouse build 2019-10-23   # one night's tables
    python -m marketplace_recs.warehouse parity 2019-10-23  # against the polars build

A night's tables come from every event up to the end of that day, as in pipeline.build,
and replace that night's partition of recs.covis and recs.product_stats, so a night can
be run again. They are read back as the frames the polars build returns, so everything
after them (export, validation, the API) is the code that already runs.

The SQL lives in sql/. CI cannot reach BigQuery, so tests/test_warehouse.py transpiles it
to DuckDB and checks it against the polars build on the synthetic store. Terraform
(infra/core) creates the dataset and its tables; the project is GOOGLE_CLOUD_PROJECT.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

DATASET = "recs"
LOCATION = os.environ.get("RECS_BQ_LOCATION", "europe-west1")
SQL = Path(__file__).with_name("sql")
KINDS = ("time", "type", "buy2buy")
RUNS = Path("runs")


def sql(name: str) -> str:
    return (SQL / f"{name}.sql").read_text(encoding="utf-8")


def client():
    from google.cloud import bigquery

    return bigquery.Client(location=LOCATION)


def _params(**values) -> list:
    from google.cloud import bigquery

    out = []
    for name, v in values.items():
        kind = "TIMESTAMP" if isinstance(v, datetime) else "DATE"
        out.append(bigquery.ScalarQueryParameter(name, kind, v))
    return out


def _frame(c, query: str, **params) -> pl.DataFrame:
    from google.cloud import bigquery

    job = c.query(query, job_config=bigquery.QueryJobConfig(query_parameters=_params(**params)))
    return pl.from_arrow(job.to_arrow(create_bqstorage_client=True))


def _cost(job, seconds: float) -> dict:
    return {
        "seconds": round(seconds, 1),
        "gb_processed": round((job.total_bytes_processed or 0) / 1e9, 3),
        "gb_billed": round((job.total_bytes_billed or 0) / 1e9, 3),
        "slot_seconds": round((job.slot_millis or 0) / 1000),
    }


# -- loading -------------------------------------------------------------------------


def load(path: Path | None = None, c=None) -> dict:
    """The month of events into recs.events, replacing what is there. `seq` numbers the
    events in file order, which is time order, so SQL can order events within a second
    the way the polars code does."""
    from google.cloud import bigquery

    from .split import EVENTS

    c = c or client()
    table = c.get_table(f"{DATASET}.events")
    with tempfile.TemporaryDirectory(dir="data") as tmp:
        staged = Path(tmp) / "events.parquet"
        pl.scan_parquet(path or EVENTS).with_row_index("seq").with_columns(
            pl.col("seq").cast(pl.Int64)
        ).sink_parquet(staged, compression="zstd")
        cfg = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=table.schema,
            time_partitioning=table.time_partitioning,
            clustering_fields=table.clustering_fields,
        )
        t0 = time.perf_counter()
        with open(staged, "rb") as f:
            job = c.load_table_from_file(f, table, job_config=cfg)
        job.result()
        seconds = time.perf_counter() - t0
        size = staged.stat().st_size
    table = c.get_table(table)
    return {
        "rows": table.num_rows,
        "gb_stored": round(table.num_bytes / 1e9, 2),
        "upload_gb": round(size / 1e9, 2),
        "seconds": round(seconds, 1),
    }


# -- one night -----------------------------------------------------------------------


def build(day: datetime, c=None) -> dict:
    """Night `day`'s partitions of recs.covis and recs.product_stats, from every event
    before the end of the day. Returns what each query cost."""
    from google.cloud import bigquery

    c = c or client()
    params = _params(end_ts=day + timedelta(days=1), version=day.date())
    report = {}
    for table in ("covis", "product_stats"):
        cfg = bigquery.QueryJobConfig(
            query_parameters=params,
            destination=f"{c.project}.{DATASET}.{table}${day:%Y%m%d}",
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        t0 = time.perf_counter()
        job = c.query(sql(table), job_config=cfg)
        job.result()
        report[table] = _cost(job, time.perf_counter() - t0)
    return report


def read(day: datetime, c=None) -> tuple[dict[str, pl.DataFrame], pl.DataFrame, pl.DataFrame]:
    """Night `day`'s tables as (matrices, product statistics, popularity), in the types
    covisit.build, features.product_stats and candidates.popular return."""
    c = c or client()
    covis = _frame(
        c,
        f"SELECT kind, product_id, neighbour, weight, rank FROM {DATASET}.covis "
        "WHERE version = @version",
        version=day.date(),
    )
    products = as_product_stats(
        _frame(
            c,
            f"SELECT * EXCEPT (version) FROM {DATASET}.product_stats WHERE version = @version",
            version=day.date(),
        )
    )
    return as_matrices(covis), products, popular(products)


def as_matrices(covis: pl.DataFrame) -> dict[str, pl.DataFrame]:
    return {
        kind: covis.filter(pl.col("kind") == kind)
        .sort("product_id", "rank")
        .select(
            pl.col("product_id").cast(pl.Int32),
            pl.col("neighbour").cast(pl.Int32),
            pl.col("weight").cast(pl.Float32),
        )
        for kind in KINDS
    }


def as_product_stats(df: pl.DataFrame) -> pl.DataFrame:
    counts = ("p_events_7d", "p_events_1d", "p_carts_7d", "p_purchases_7d")
    return df.select(
        pl.col("product_id").cast(pl.Int32),
        pl.col("p_price").cast(pl.Float32),
        pl.col("p_category").cast(pl.Int64),
        pl.col("p_brand").cast(pl.String),
        *[pl.col(n).cast(pl.UInt32) for n in counts],
    ).sort("product_id")


def popular(products: pl.DataFrame) -> pl.DataFrame:
    """candidates.popular from the statistics: the 7-day counts, most touched first."""
    return (
        products.filter(pl.col("p_events_7d").is_not_null())
        .select("product_id", pl.col("p_events_7d").alias("pop"))
        .sort(["pop", "product_id"], descending=[True, False])
    )


def tables(day: datetime, c=None) -> tuple[tuple, dict]:
    """Build night `day` in BigQuery and read it back: what pipeline.build takes."""
    c = c or client()
    report = build(day, c)
    t0 = time.perf_counter()
    out = read(day, c)
    report["read_seconds"] = round(time.perf_counter() - t0, 1)
    return out, report


def fingerprint(day: datetime, c=None) -> dict[str, int]:
    """One number per table for night `day`'s partition: equal numbers, equal rows."""
    query = " UNION ALL ".join(
        f"SELECT '{t}' AS t, BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(x))) AS fp "
        f"FROM {DATASET}.{t} AS x WHERE version = @version"
        for t in ("covis", "product_stats")
    )
    df = _frame(c or client(), query, version=day.date())
    return dict(zip(df["t"].to_list(), df["fp"].to_list(), strict=True))


def daily_counts(day: datetime, c=None) -> pl.DataFrame:
    """Events per day over `day` and the 7 days before it, as pipeline.daily_counts."""
    df = _frame(
        c or client(),
        sql("daily_counts"),
        start_ts=day - timedelta(days=7),
        end_ts=day + timedelta(days=1),
    )
    return df.with_columns(pl.col("n").cast(pl.UInt32))


# -- parity --------------------------------------------------------------------------


def compare_matrices(a: dict[str, pl.DataFrame], b: dict[str, pl.DataFrame]) -> dict:
    """Matrices `a` against `b`, row by row: what each holds that the other does not, and
    whether shared rows carry the same float32 weight."""
    out = {}
    for kind in KINDS:
        x = a[kind].select("product_id", "neighbour", pl.col("weight").alias("wa"))
        y = b[kind].select("product_id", "neighbour", pl.col("weight").alias("wb"))
        j = x.join(y, on=["product_id", "neighbour"], how="full", coalesce=True)
        both = j.filter(pl.col("wa").is_not_null() & pl.col("wb").is_not_null())
        bad = j.filter(
            pl.col("wa").is_null() | pl.col("wb").is_null() | (pl.col("wa") != pl.col("wb"))
        )
        rel = ((both["wa"] - both["wb"]).abs() / both["wb"].abs()).max() if both.height else 0.0
        out[kind] = {
            "rows": [x.height, y.height],
            "products": x["product_id"].n_unique(),
            "products_differing": bad["product_id"].n_unique(),
            "only_in_first": j.filter(pl.col("wb").is_null()).height,
            "only_in_second": j.filter(pl.col("wa").is_null()).height,
            "shared_rows_same_weight": int((both["wa"] == both["wb"]).sum()),
            "shared_rows": both.height,
            "max_relative_weight_diff": float(rel or 0.0),
        }
    return out


def compare_frames(a: pl.DataFrame, b: pl.DataFrame, key: str = "product_id") -> dict:
    """Two frames with the same columns, per column: how many keyed rows differ."""
    j = a.join(b, on=key, how="full", coalesce=True, suffix="_b")
    out = {"rows": [a.height, b.height], "keys_in_one_only": 0, "columns_differing": {}}
    out["keys_in_one_only"] = j.height - a.join(b, on=key, how="inner").height
    for col in a.columns:
        if col == key:
            continue
        differ = j.filter(~pl.col(col).eq_missing(pl.col(f"{col}_b"))).height
        if differ:
            out["columns_differing"][col] = differ
    return out


def parity(day: datetime) -> dict:
    """Night `day` built in BigQuery against the polars build of the same night: the
    tables row by row, the serving arrays, and, for the night before the test week, the
    ranker's score on the 200,000 test cases with each."""
    import lightgbm as lgb
    import numpy as np

    from . import candidates as cand
    from . import experiment as ex
    from .covisit import build as build_matrices
    from .features import product_stats
    from .metrics import evaluate
    from .online import Model, to_events, write_artifacts
    from .refresh import score
    from .split import WINDOWS, events

    c = client()
    end = day + timedelta(days=1)
    (bq_m, bq_p, bq_pop), cost = tables(day, c)
    first = fingerprint(day, c)
    build(day, c)  # the same night again: a rerun must write the same rows
    rerun_identical = fingerprint(day, c) == first

    t0 = time.perf_counter()
    stats = events().filter(pl.col("event_time") < end)
    # Before the test week, the cached matrices every published result was scored with.
    cached = end == WINDOWS["C"][0]
    pl_m = ex.matrices_for("C") if cached else build_matrices(stats)
    pl_p = product_stats(stats, end)
    pl_pop = cand.popular(stats, end)
    polars_seconds = time.perf_counter() - t0

    report = {
        "night": f"{day:%Y-%m-%d}",
        "events_until": f"{end:%Y-%m-%dT%H:%M:%SZ}",
        "bigquery": cost,
        "bigquery_rerun_identical": rerun_identical,
        "polars": {
            "matrices": "cached in data/covis/C" if cached else "built",
            "seconds_on_laptop": None if cached else round(polars_seconds, 1),
        },
        "matrices": compare_matrices(bq_m, pl_m),
        "product_stats": compare_frames(bq_p, pl_p.sort("product_id")),
        "popular_top_200_identical": bq_pop["product_id"].head(200).to_list()
        == pl_pop["product_id"].head(200).to_list(),
    }

    # The serving arrays each set of tables exports to, and what the API then answers.
    ranker = Path("data") / "models" / "ranker.txt"
    with tempfile.TemporaryDirectory(dir="data") as tmp:
        built = {"bigquery": (bq_m, bq_p, bq_pop), "polars": (pl_m, pl_p, pl_pop)}
        dirs = {}
        for name, (m, p, pop) in built.items():
            dirs[name] = Path(tmp) / name
            write_artifacts(dirs[name], m, p, pop["product_id"].to_list(), ranker)
        files = sorted(f.name for f in dirs["polars"].iterdir() if f.suffix in (".npy", ".npz"))
        same = {}
        for f in files:
            if f.endswith(".npz"):
                with np.load(dirs["bigquery"] / f) as x, np.load(dirs["polars"] / f) as y:
                    same[f] = all(np.array_equal(x[k], y[k], equal_nan=True) for k in y.files)
            else:
                x, y = np.load(dirs["bigquery"] / f), np.load(dirs["polars"] / f)
                same[f] = x.shape == y.shape and np.array_equal(x, y, equal_nan=x.dtype.kind == "f")
        report["serving_arrays_identical"] = same

        history, cases = ex.cases_for("C")
        picked = cases.tail(5000).select("session")
        sessions = list(to_events(history.join(picked, on="session", how="semi")).values())
        a, b = Model(dirs["bigquery"]), Model(dirs["polars"])
        report["api_same_top_20"] = sum(
            [p for p, _ in a.recommend(s, 20)] == [p for p, _ in b.recommend(s, 20)]
            for s in sessions
        )
        report["api_sessions"] = len(sessions)

    if end == WINDOWS["C"][0]:
        booster = lgb.Booster(model_file=str(ranker))
        t = cases["target"].to_numpy()
        report["test_week"] = {
            name: evaluate(score(history, cases, tabs, booster), t) for name, tabs in built.items()
        }
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["load", "build", "parity"])
    ap.add_argument("day", nargs="?", help="the night, e.g. 2019-10-23: tables include that day")
    a = ap.parse_args(argv)
    if a.what == "load":
        print(json.dumps(load()))
        return 0
    day = datetime.strptime(a.day, "%Y-%m-%d").replace(tzinfo=UTC)
    if a.what == "build":
        print(json.dumps(build(day)))
        return 0
    report = parity(day)
    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"warehouse_{day:%Y-%m-%d}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
