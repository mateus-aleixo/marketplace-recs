"""The nightly SQL against the polars build it stands in for, on the synthetic store.

BigQuery is out of CI's reach, so the SQL in marketplace_recs/sql is transpiled to DuckDB
with sqlglot and run over the same events: every table must match the polars build, row
for row and to the last float32.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from marketplace_recs import candidates as cand
from marketplace_recs import pipeline, warehouse
from marketplace_recs.covisit import build as covis
from marketplace_recs.features import product_stats

duckdb = pytest.importorskip("duckdb")
sqlglot = pytest.importorskip("sqlglot")

NIGHT = datetime(2019, 10, 23, tzinfo=UTC)  # tables from every event before 24 October
END = NIGHT + timedelta(days=1)
LONG = 9_999  # 40 distinct products: only the session's 30 most recent count
SPAN = 9_998  # weeks long: pairs either side of the 24-hour and 14-day limits


def with_edge_sessions(events: pl.DataFrame) -> pl.DataFrame:
    start = datetime(2019, 10, 21, 9, tzinfo=UTC)
    long = [
        (start + timedelta(seconds=30 * i), 1 if i % 5 == 0 else 0, 1001 + i, LONG)
        for i in range(40)
    ]
    t = datetime(2019, 10, 2, 10, tzinfo=UTC)
    span = [
        (t, 1, 2001, SPAN),  # cart
        (t + timedelta(hours=23), 0, 2002, SPAN),  # within 24 hours of 2001
        (t + timedelta(hours=24, minutes=30), 0, 2003, SPAN),  # just outside them
        (t + timedelta(days=13, hours=12), 1, 2004, SPAN),  # cart, within 14 days of 2001
        (t + timedelta(days=15), 2, 2005, SPAN),  # purchase, outside them
    ]
    extra = pl.DataFrame(
        [(ts, et, p, 100, "acme", 5.0, s) for ts, et, p, s in long + span],
        schema=events.schema,
        orient="row",
    )
    return pl.concat([events, extra]).sort("event_time", "session", maintain_order=True)


@pytest.fixture(scope="module")
def store(world):
    """The events as the warehouse holds them: numbered in file order."""
    events = with_edge_sessions(world["events"])
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA recs")
    staged = events.with_row_index("seq").with_columns(pl.col("seq").cast(pl.Int64)).to_arrow()
    con.register("staged", staged)
    con.execute("CREATE TABLE recs.events AS SELECT * FROM staged")
    return {"events": events, "con": con}


def run_sql(con, name: str, **params) -> pl.DataFrame:
    query = sqlglot.transpile(warehouse.sql(name), read="bigquery", write="duckdb")[0]
    return con.execute(query, params).pl()


def sql_tables(con, night: datetime = NIGHT):
    params = {"end_ts": night + timedelta(days=1), "version": night.date()}
    products = warehouse.as_product_stats(run_sql(con, "product_stats", **params))
    return (
        warehouse.as_matrices(run_sql(con, "covis", **params)),
        products,
        warehouse.popular(products),
    )


def before(events: pl.DataFrame, end: datetime) -> pl.LazyFrame:
    return events.lazy().filter(pl.col("event_time") < end)


def test_covisitation_matches_polars_row_for_row(store):
    sql_m, _, _ = sql_tables(store["con"])
    report = warehouse.compare_matrices(sql_m, covis(before(store["events"], END), parts=2))
    for kind, r in report.items():
        assert r["products"] > 0, kind
        assert r["only_in_first"] == r["only_in_second"] == 0, (kind, r)
        assert r["shared_rows_same_weight"] == r["shared_rows"], (kind, r)
    # The long session's 10 oldest products fall outside its 30 most recent.
    products = set(sql_m["time"]["product_id"].to_list())
    assert products.isdisjoint(range(1001, 1011))
    assert products >= set(range(1011, 1041))
    # 24 hours and 14 days are limits: pairs just inside count, pairs just outside do not.
    pairs = {k: set(zip(m["product_id"], m["neighbour"], strict=True)) for k, m in sql_m.items()}
    assert (2001, 2002) in pairs["time"] and (2001, 2003) not in pairs["time"]
    assert (2001, 2004) in pairs["buy2buy"] and (2001, 2005) not in pairs["buy2buy"]


def test_product_statistics_and_popularity_match_polars(store):
    _, products, pop = sql_tables(store["con"])
    stats = before(store["events"], END)
    expected = product_stats(stats, END).sort("product_id")
    assert products.schema == expected.schema
    assert warehouse.compare_frames(products, expected) == {
        "rows": [expected.height, expected.height],
        "keys_in_one_only": 0,
        "columns_differing": {},
    }
    assert pop.equals(cand.popular(stats, END))


def test_daily_counts_match_polars(store):
    day = datetime(2019, 10, 21, tzinfo=UTC)
    got = run_sql(
        store["con"],
        "daily_counts",
        start_ts=day - timedelta(days=7),
        end_ts=day + timedelta(days=1),
    ).with_columns(pl.col("d").dt.cast_time_unit("us"), pl.col("n").cast(pl.UInt32))
    expected = pipeline.daily_counts(day, store["events"].lazy())
    assert got.select("d", "n").equals(expected.select("d", "n"))
    assert got["last"].dt.cast_time_unit("us").equals(expected["last"])


def test_a_night_built_in_the_warehouse_serves_the_same_arrays(store, world, tmp_path, monkeypatch):
    """The nightly job with RECS_WAREHOUSE=bigquery, the warehouse played by DuckDB: the
    version it builds is the one the polars build makes, file for file."""
    con = store["con"]
    monkeypatch.setattr(warehouse, "tables", lambda day, c=None: (sql_tables(con, day), {}))
    dirs = {}
    for name in ("bigquery", "local"):
        model = tmp_path / name
        shutil.copytree(world["dir"], model)
        if name == "bigquery":
            version = pipeline.build_night(NIGHT, model, warehouse="bigquery")
        else:
            version = pipeline.build(NIGHT, store["events"].lazy(), model)
        dirs[name] = model / "versions" / version
    for f in sorted(dirs["local"].iterdir()):
        if f.suffix == ".npy":
            a, b = np.load(dirs["bigquery"] / f.name), np.load(f)
            assert a.dtype == b.dtype and np.array_equal(a, b, equal_nan=a.dtype.kind == "f"), f
        elif f.suffix == ".npz":
            with np.load(dirs["bigquery"] / f.name) as a, np.load(f) as b:
                for k in b.files:
                    assert np.array_equal(a[k], b[k], equal_nan=b[k].dtype.kind == "f"), k
        elif f.name == "brands.json":
            assert json.loads(f.read_text()) == json.loads((dirs["bigquery"] / f.name).read_text())
