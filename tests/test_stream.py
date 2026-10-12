"""Session stores and the stream's message format, with no Redis or Redpanda running."""

from __future__ import annotations

import fakeredis

from marketplace_recs.online import Event
from marketplace_recs.sessions import MemorySessions, RedisSessions, decode, encode
from marketplace_recs.stream import message, parse


def ev(i: int, product: int | None = None) -> Event:
    return Event(1_571_900_000 + i, i % 3, 100 + i if product is None else product, 7, "acme", 9.99)


def test_event_codec_round_trips_missing_fields():
    e = Event(1_571_900_000, 2, 42, None, None, None)
    assert decode(encode(e)) == e
    assert decode(encode(ev(1)).encode()) == ev(1)


def test_stream_message_round_trips():
    s, e, sent = parse(message("abc", ev(3), sent=123.5))
    assert (s, e, sent) == ("abc", ev(3), 123.5)


def test_redis_store_keeps_order_trims_and_expires():
    r = fakeredis.FakeRedis()
    store = RedisSessions(r, max_events=3, ttl=60)
    for i in range(5):
        store.append("a", ev(i))
    assert [e.product_id for e in store.get("a")] == [102, 103, 104]
    assert 0 < r.ttl("s:a") <= 60
    assert store.get("nobody") == []


def test_append_many_keeps_each_sessions_order_across_interleaving():
    store = RedisSessions(fakeredis.FakeRedis(), max_events=10)
    items = [("a", ev(0)), ("b", ev(1)), ("a", ev(2)), ("b", ev(3)), ("a", ev(4))]
    assert store.append_many(items) == [1, 1, 2, 2, 3]
    assert [e.product_id for e in store.get("a")] == [100, 102, 104]
    assert [e.product_id for e in store.get("b")] == [101, 103]


def test_memory_and_redis_stores_agree():
    mem, red = MemorySessions(max_events=4), RedisSessions(fakeredis.FakeRedis(), max_events=4)
    for i in range(7):
        s = "x" if i % 2 else "y"
        mem.append(s, ev(i))
        red.append(s, ev(i))
    assert mem.get("x") == red.get("x")
    assert mem.get("y") == red.get("y")


def test_the_consumer_counts_what_it_writes(monkeypatch):
    """With metrics on, the consumer still writes every event and counts them all."""
    import time

    import confluent_kafka

    from marketplace_recs import stream

    class Msg:
        def __init__(self, raw):
            self.raw = raw

        def error(self):
            return None

        def value(self):
            return self.raw

    batches = [[Msg(message("s1", ev(i), sent=time.time())) for i in range(5)]]

    class FakeConsumer:
        def __init__(self, conf):
            pass

        def subscribe(self, topics):
            pass

        def consume(self, num_messages, timeout):
            return batches.pop() if batches else []

        def assignment(self):
            return []

        def position(self, partitions):
            return []

        def close(self):
            pass

    monkeypatch.setattr(confluent_kafka, "Consumer", FakeConsumer)
    monkeypatch.setattr(stream, "ensure_topic", lambda *a, **k: None)
    monkeypatch.setattr("prometheus_client.start_http_server", lambda port: None)
    r = fakeredis.FakeRedis()
    before = stream.metrics()["events"]._value.get()
    out = stream.consume("fake:9092", r, idle_exit=0.05, metrics_port=9101)
    assert out["events"] == 5
    assert stream.metrics()["events"]._value.get() - before == 5
    assert len(RedisSessions(r).get("s1")) == 5
