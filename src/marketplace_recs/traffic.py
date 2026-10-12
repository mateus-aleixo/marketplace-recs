"""Real traffic through the API, for the dashboard and the live drift check.

    python -m marketplace_recs.traffic --minutes 20
    python -m marketplace_recs.traffic --minutes 20 --fault price_in_cents --share 0.25 \\
        --fault-from 8 --fault-to 14

The recorded events of the test week are posted to the API in the order they happened,
`speed` times faster than real time. Each event is followed by a recommendation request
for its session, as a product page would send one. With --fault, a share of sessions
comes from a broken client between two minutes of the run, so the drift check has
something to find.

A product page sends the whole session, so every session starts with the events it had
before the run began. Without them, the first minutes are full of sessions with short,
truncated histories, and the drift check's baseline looked unlike everything after it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import zlib
from datetime import UTC, datetime, timedelta

import numpy as np

from .drift import FAULTS
from .online import Event

TYPES = ["view", "cart", "purchase"]
EARLIER = timedelta(hours=6)  # how far back a session's earlier events are looked up


def body(e: Event) -> dict:
    return {
        "product_id": e.product_id,
        "event_type": TYPES[e.event_type],
        "time": e.time,
        "category_id": e.category_id,
        "brand": e.brand,
        "price": None if e.price is None else round(e.price, 2),
    }


def recorded(start: datetime, end: datetime, sessions=None) -> list[tuple[int, Event]]:
    """The events of [start, end), in time order, with their sessions; only those of
    `sessions` when it is given."""
    import polars as pl

    from .split import events

    lf = events().filter((pl.col("event_time") >= start) & (pl.col("event_time") < end))
    if sessions is not None:
        lf = lf.filter(pl.col("session").is_in(list(sessions)))
    rows = lf.select(
        pl.col("event_time").dt.epoch("s"),
        "event_type",
        "product_id",
        "category_id",
        "brand",
        "price",
        "session",
    ).collect()
    return [(s, Event(t, et, p, c, b, pr)) for t, et, p, c, b, pr, s in rows.iter_rows()]


async def run(
    api: str,
    start: datetime,
    minutes: float,
    speed: float,
    fault: str | None = None,
    share: float = 0.0,
    fault_from: float = 0.0,
    fault_to: float = 0.0,
    in_flight: int = 64,
) -> dict:
    import httpx

    stream = recorded(start, start + timedelta(minutes=minutes * speed))
    broken = FAULTS[fault] if fault else None
    sessions: dict[int, list[dict]] = {}
    for session, e in recorded(start - EARLIER, start, {s for s, _ in stream}):
        sessions.setdefault(session, []).append(body(e))
    latency: list[float] = []
    codes: dict[int, int] = {}
    gate = asyncio.Semaphore(in_flight)
    t0_data = stream[0][1].time if stream else 0

    async with httpx.AsyncClient(
        base_url=api, limits=httpx.Limits(max_connections=in_flight)
    ) as http:

        async def one(session: int, e: Event, faulty: bool) -> None:
            async with gate:
                event = body(broken(e) if faulty else e)
                history = sessions.setdefault(session, [])
                history.append(event)
                del history[:-200]
                try:
                    r = await http.post(f"/sessions/{session}/events", json=event)
                    codes[r.status_code] = codes.get(r.status_code, 0) + 1
                    t = time.perf_counter()
                    r = await http.post("/recommend", json={"events": list(history), "k": 20})
                    latency.append(time.perf_counter() - t)
                    codes[r.status_code] = codes.get(r.status_code, 0) + 1
                except httpx.HTTPError:
                    codes[0] = codes.get(0, 0) + 1

        t0 = time.perf_counter()
        tasks = []
        for session, e in stream:
            ahead = (e.time - t0_data) / speed - (time.perf_counter() - t0)
            if ahead > 0:
                await asyncio.sleep(ahead)
            minute = (time.perf_counter() - t0) / 60
            faulty = (
                broken is not None
                and fault_from <= minute < fault_to
                and zlib.crc32(str(session).encode()) < share * 2**32
            )
            tasks.append(asyncio.create_task(one(session, e, faulty)))
        await asyncio.gather(*tasks)
        seconds = time.perf_counter() - t0

    ms = np.array(latency) * 1000 if latency else np.zeros(1)
    return {
        "events": len(stream),
        "seconds": round(seconds, 1),
        "requests_per_s": round(2 * len(stream) / seconds, 1) if seconds else None,
        "status": {str(k): v for k, v in sorted(codes.items())},
        "recommend_ms": {
            p: round(float(np.percentile(ms, int(p[1:]))), 1) for p in ("p50", "p95", "p99")
        },
        "fault": {"name": fault, "share": share, "minutes": [fault_from, fault_to]}
        if fault
        else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8080")
    ap.add_argument("--start", default="2019-10-25T09:00:00", help="UTC, inside the test week")
    ap.add_argument("--minutes", type=float, default=20.0, help="wall-clock length of the run")
    ap.add_argument("--speed", type=float, default=4.0, help="recorded seconds per wall second")
    ap.add_argument("--fault", choices=sorted(FAULTS), default=None)
    ap.add_argument("--share", type=float, default=0.25, help="share of sessions the fault hits")
    ap.add_argument("--fault-from", type=float, default=8.0, help="minute of the run it starts")
    ap.add_argument("--fault-to", type=float, default=14.0, help="minute of the run it stops")
    a = ap.parse_args(argv)
    start = datetime.fromisoformat(a.start).replace(tzinfo=UTC)
    out = asyncio.run(
        run(a.api, start, a.minutes, a.speed, a.fault, a.share, a.fault_from, a.fault_to)
    )
    print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
