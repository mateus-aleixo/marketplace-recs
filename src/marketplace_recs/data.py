"""Fetch a month of marketplace events and store it as typed Parquet.

    python -m marketplace_recs.data             # October 2019: a 1.7 GB download

The data is REES46's "eCommerce behavior data from multi category store": every
product view, cart addition and purchase on a large multi-category online store,
collected by the Open CDP project. Its terms ask only for the source to be named:
https://www.kaggle.com/mkechinov/ecommerce-behavior-data-from-multi-category-store and
https://rees46.com. The file comes from REES46's own server and is never committed.

The gzip is streamed through pyarrow's CSV reader a batch at a time, so the 5.3 GB of
text never sits in RAM. Columns are typed on the way (event_type becomes 0 view, 1 cart,
2 purchase), and the session UUIDs become dense int32 ids numbered in order of each
session's first event, so a session id also says roughly when it started.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

DATA = Path("data")
URL = "https://data.rees46.com/datasets/marketplace/{month}.csv.gz"
EVENT_TYPES = {"view": 0, "cart": 1, "purchase": 2}

COLUMN_TYPES = {
    "event_time": pa.string(),
    "event_type": pa.string(),
    "product_id": pa.int32(),
    "category_id": pa.int64(),
    "category_code": pa.string(),
    "brand": pa.string(),
    "price": pa.float32(),
    "user_id": pa.int32(),
    "user_session": pa.string(),
}


def download(month: str, dest: Path) -> None:
    """Resumable: an interrupted download continues from the bytes on disk."""
    import httpx

    if dest.exists():
        return
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with httpx.stream("GET", URL.format(month=month), headers=headers, timeout=60) as r:
        r.raise_for_status()
        with open(part, "ab" if have and r.status_code == 206 else "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    part.replace(dest)


def typed_batches(src: Path):
    """The CSV as typed polars frames, one pyarrow batch at a time."""
    convert = pacsv.ConvertOptions(column_types=COLUMN_TYPES, strings_can_be_null=True)
    read = pacsv.ReadOptions(block_size=64 << 20)
    with pa.input_stream(str(src), compression="gzip") as stream:
        for batch in pacsv.open_csv(stream, read_options=read, convert_options=convert):
            yield pl.from_arrow(batch).with_columns(
                pl.col("event_time").str.strptime(
                    pl.Datetime("us", "UTC"), "%Y-%m-%d %H:%M:%S UTC"
                ),
                pl.col("event_type").replace_strict(EVENT_TYPES, return_dtype=pl.UInt8),
            )


def convert(src: Path, dst: Path) -> dict:
    """Two streaming passes, so peak memory is the session map plus one batch.

    The CSV is already in time order, and the output keeps that order: a time split is a
    range filter, and a replay is a sequential read."""
    staged = dst.with_name(dst.stem + ".staged.parquet")
    writer, rows, unordered, last = None, 0, 0, None
    for df in typed_batches(src):
        table = df.to_arrow()
        if writer is None:
            writer = pq.ParquetWriter(staged, table.schema, compression="zstd")
        writer.write_table(table)
        rows += df.height
        t = df["event_time"].to_physical()  # microseconds since the epoch
        unordered += int((t.diff() < 0).sum()) + int(last is not None and t[0] < last)
        last = t[-1]
    writer.close()

    sessions = (
        pl.scan_parquet(staged)
        .filter(pl.col("user_session").is_not_null())
        .group_by("user_session")
        .agg(pl.col("event_time").min().alias("first"))
        .collect(engine="streaming")
        .sort("first", "user_session")
        .with_row_index("session")
        .select("user_session", pl.col("session").cast(pl.Int32))
    )
    out = None
    for batch in pq.ParquetFile(staged).iter_batches(batch_size=4_000_000):
        df = pl.from_arrow(batch).join(
            sessions, on="user_session", how="left", maintain_order="left"
        )
        table = df.drop("user_session").to_arrow()
        if out is None:
            out = pq.ParquetWriter(dst, table.schema, compression="zstd")
        out.write_table(table)
    out.close()
    staged.unlink()
    return {"rows": rows, "sessions": sessions.height, "out_of_order_events": unordered}


def stats(path: Path) -> dict:
    df = pl.read_parquet(path)
    per_session = df.group_by("session").len()
    return {
        "events": df.height,
        "views": int((df["event_type"] == 0).sum()),
        "carts": int((df["event_type"] == 1).sum()),
        "purchases": int((df["event_type"] == 2).sum()),
        "users": df["user_id"].n_unique(),
        "sessions": df["session"].n_unique(),
        "products": df["product_id"].n_unique(),
        "events_without_session": int(df["session"].null_count()),
        "first_event": str(df["event_time"].min()),
        "last_event": str(df["event_time"].max()),
        "events_per_session_median": float(per_session["len"].median()),
        "events_per_session_p99": float(per_session["len"].quantile(0.99)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2019-Oct")
    a = ap.parse_args(argv)
    raw = DATA / "raw" / f"{a.month}.csv.gz"
    raw.parent.mkdir(parents=True, exist_ok=True)
    dst = DATA / f"events-{a.month}.parquet"
    t0 = time.perf_counter()
    download(a.month, raw)
    if not dst.exists():
        print(convert(raw, dst), f"{time.perf_counter() - t0:.0f} s", flush=True)
    print(stats(dst))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
