"""The asynchronous path: events through Redpanda (the Kafka API) into shared session state.

    python -m marketplace_recs.stream consume                 # topic -> Redis, until stopped
    python -m marketplace_recs.stream replay --speed 0        # the test week, as fast as it goes
    python -m marketplace_recs.stream replay --speed 600      # ... at 600 times real time

Each event is published keyed by its session, so one partition holds a session's events
in order. The consumer writes them to Redis in batches, and every API worker and replica
reads the same state. Each message carries the wall-clock time it was published, so the
consumer measures staleness: the time from publishing an event to it being readable.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import time
from datetime import UTC, datetime, timedelta

import numpy as np

from .online import Event
from .sessions import RedisSessions

TOPIC = "events"
PARTITIONS = 6
BOOTSTRAP = os.environ.get("RECS_KAFKA", "localhost:19092")
REDIS = os.environ.get("RECS_REDIS", "redis://localhost:6379/0")


def message(session: str, e: Event, sent: float | None = None) -> bytes:
    body = {
        "s": session,
        "e": [e.time, e.event_type, e.product_id, e.category_id, e.brand, e.price],
        "sent": time.time() if sent is None else sent,
    }
    return json.dumps(body, separators=(",", ":")).encode()


def parse(raw: bytes) -> tuple[str, Event, float]:
    d = json.loads(raw)
    return d["s"], Event(*d["e"]), d["sent"]


def producer(bootstrap: str = BOOTSTRAP):
    from confluent_kafka import Producer

    return Producer(
        {
            "bootstrap.servers": bootstrap,
            "linger.ms": 5,
            "compression.type": "lz4",
            "acks": "1",
            "socket.nagle.disable": True,
            "queue.buffering.max.messages": 1_000_000,
        }
    )


def ensure_topic(bootstrap: str = BOOTSTRAP, partitions: int = PARTITIONS) -> None:
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": bootstrap})
    if TOPIC not in admin.list_topics(timeout=10).topics:
        for f in admin.create_topics([NewTopic(TOPIC, partitions, 1)]).values():
            f.result()


def publish(prod, session: str, e: Event) -> None:
    while True:
        try:
            prod.produce(TOPIC, key=session.encode(), value=message(session, e))
            break
        except BufferError:  # local queue full: let librdkafka drain it
            prod.poll(0.05)
    prod.poll(0)


def replay(start: datetime, hours: float, speed: float, bootstrap: str = BOOTSTRAP) -> dict:
    """Publish the recorded events of [start, start + hours) in time order. speed 0 is
    as fast as possible; otherwise `speed` seconds of recorded time per wall second."""
    import polars as pl

    from .split import events

    end = start + timedelta(hours=hours)
    rows = (
        events()
        .filter((pl.col("event_time") >= start) & (pl.col("event_time") < end))
        .select(
            pl.col("event_time").dt.epoch("s"),
            "event_type",
            "product_id",
            "category_id",
            "brand",
            "price",
            "session",
        )
        .collect()
    )
    ensure_topic(bootstrap)
    prod = producer(bootstrap)
    t0_wall, t0_data = time.perf_counter(), None
    for t, et, p, c, b, pr, s in rows.iter_rows():
        if speed > 0:
            t0_data = t if t0_data is None else t0_data
            ahead = (t - t0_data) / speed - (time.perf_counter() - t0_wall)
            if ahead > 0.001:
                prod.poll(0)
                time.sleep(ahead)
        publish(prod, str(s), Event(t, et, p, c, b, pr))
    prod.flush(60)
    seconds = time.perf_counter() - t0_wall
    return {
        "events": rows.height,
        "seconds": round(seconds, 1),
        "events_per_s": round(rows.height / seconds),
        "speed": speed,
    }


def consume(
    bootstrap: str = BOOTSTRAP,
    redis_url: str = REDIS,
    group: str = "session-state",
    batch: int = 2000,
    idle_exit: float | None = None,
    report_every: float = 10.0,
    offset: str = "earliest",
    max_wait: float = 0.01,
) -> dict:
    """Topic to Redis until stopped, or until `idle_exit` seconds pass with nothing new.

    A poll returns as soon as `batch` messages are waiting or `max_wait` seconds pass, and
    when the topic is caught up that wait is a floor on staleness. Measured on the same
    hour of traffic at 2,480 events a second, publish to readable in Redis, median: 367 ms
    waiting 0.5 s, 45 ms waiting 50 ms, 49 ms waiting 10 ms with Nagle's algorithm on (the
    librdkafka default), and 20 ms with it off. A backlog fills batches at once, so the
    short wait costs no throughput."""
    from confluent_kafka import Consumer

    ensure_topic(bootstrap)
    c = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group,
            "auto.offset.reset": offset,
            "enable.auto.commit": True,
            "socket.nagle.disable": True,
        }
    )
    c.subscribe([TOPIC])
    store = RedisSessions(redis_url)
    # Docker stops a container with SIGTERM. Without this the consumer died without
    # leaving its group, and a restarted one waited out the old member's session timeout
    # before it was given any partition: 7 sessions in 200 missed a 5 s deadline.
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    n, stale, last_msg, t_first = 0, [], time.perf_counter(), None
    t_report, n_report = time.perf_counter(), 0
    try:
        while not stopping:
            msgs = c.consume(num_messages=batch, timeout=max_wait)
            now = time.perf_counter()
            if not msgs:
                if idle_exit is not None and now - last_msg > idle_exit:
                    break
                continue
            last_msg = now
            t_first = now if t_first is None else t_first
            items, sent = [], []
            for m in msgs:
                if m.error():
                    continue
                s, e, at = parse(m.value())
                items.append((s, e))
                sent.append(at)
            store.append_many(items)
            done = time.time()
            stale.extend(done - at for at in sent[:: max(1, len(sent) // 50)])
            n += len(items)
            if now - t_report >= report_every:
                rate = (n - n_report) / (now - t_report)
                print(f"  {n:>10,} events  {rate:,.0f}/s", flush=True)
                t_report, n_report = now, n
    finally:
        c.close()
    st = np.array(stale) * 1000 if stale else np.zeros(1)
    busy = (last_msg - t_first) if t_first is not None else 0.0
    return {
        "events": n,
        "busy_seconds": round(busy, 1),
        "events_per_s": round(n / busy) if busy > 0 else None,
        "staleness_ms": {
            "p50": round(float(np.percentile(st, 50)), 1),
            "p95": round(float(np.percentile(st, 95)), 1),
            "p99": round(float(np.percentile(st, 99)), 1),
        },
    }


def e2e(
    base_url: str = "http://localhost:8080",
    n: int = 200,
    timeout: float = 5.0,
    bodies: str = "loadtest/sessions.json",
) -> dict:
    """Through the API: post a real test session's events one by one, then ask for
    recommendations until the answer includes the last of them. Measures how long an
    event takes to become usable across every hop: HTTP, Redpanda, the consumer, Redis.

    Sessions come from the load test's request bodies, so this runs from inside the
    compose network with only the serving image. Measured from Windows instead, every
    POST took about 44 ms whatever the client did: Docker Desktop's port forwarding
    relays a request's two segments, headers then body, and the second waits on a
    delayed ACK. Inside the network a POST takes milliseconds."""
    import http.client
    from urllib.parse import urlparse

    with open(bodies, encoding="utf-8") as f:
        sessions = json.load(f)[-n:]
    url = urlparse(base_url)
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=10)
    conn.connect()
    conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if data else {}
        conn.request(method, path, data, headers)
        r = conn.getresponse()
        return r.status, json.loads(r.read() or b"null")

    run = int(time.time())
    # Warm up: one probe event must make the full round trip before anything is timed,
    # so a consumer still joining its group does not count against the pipeline.
    probe = f"e2e-{run}-probe"
    call("POST", f"/sessions/{probe}/events", sessions[0][0])
    deadline = time.perf_counter() + 120
    while call("GET", f"/sessions/{probe}/recommendations?k=1")[0] != 200:
        if time.perf_counter() > deadline:
            raise RuntimeError("the probe event never became readable: is the consumer up?")
        time.sleep(0.05)
    post, get, visible, polls, missed = [], [], [], [], 0
    for i, events in enumerate(sessions):
        sid = f"e2e-{run}-{i}"
        for e in events:
            t0 = time.perf_counter()
            status, _ = call("POST", f"/sessions/{sid}/events", e)
            post.append(time.perf_counter() - t0)
            assert status in (200, 202), status
        t_last, tries = time.perf_counter(), 0
        while True:
            t0 = time.perf_counter()
            status, body = call("GET", f"/sessions/{sid}/recommendations?k=20")
            tries += 1
            if status == 200 and body["events"] == len(events):
                get.append(time.perf_counter() - t0)  # only answers that rank something
                visible.append(time.perf_counter() - t_last)
                polls.append(tries)
                break
            if time.perf_counter() - t_last > timeout:
                missed += 1
                break

    def pct(xs):
        a = np.array(xs) * 1000
        return {
            "p50": round(float(np.percentile(a, 50)), 2),
            "p95": round(float(np.percentile(a, 95)), 2),
            "p99": round(float(np.percentile(a, 99)), 2),
        }

    return {
        "sessions": len(sessions),
        "events": len(post),
        "post_ms": pct(post),
        "recommendation_ms": pct(get),
        "event_to_recommendation_ms": pct(visible),
        "polls_per_session_mean": round(float(np.mean(polls)), 2),
        "missed": missed,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["replay", "consume", "e2e"])
    ap.add_argument("--bootstrap", default=BOOTSTRAP)
    ap.add_argument("--redis", default=REDIS)
    ap.add_argument("--start", default="2019-10-24T00:00:00")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--speed", type=float, default=0.0)
    ap.add_argument("--idle-exit", type=float, default=None)
    ap.add_argument("--group", default="session-state")
    ap.add_argument("--offset", choices=["earliest", "latest"], default="earliest")
    ap.add_argument("--api", default="http://localhost:8080")
    ap.add_argument("--max-wait", type=float, default=0.01)
    a = ap.parse_args(argv)
    if a.what == "replay":
        start = datetime.fromisoformat(a.start).replace(tzinfo=UTC)
        out = replay(start, a.hours, a.speed, a.bootstrap)
    elif a.what == "consume":
        out = consume(
            a.bootstrap,
            a.redis,
            a.group,
            idle_exit=a.idle_exit,
            offset=a.offset,
            max_wait=a.max_wait,
        )
    else:
        out = e2e(a.api)
    print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
