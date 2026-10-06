"""Where a session's recent events live between requests.

    MemorySessions   in one process: fine for one worker, wrong for two
    RedisSessions    shared by every worker and replica, written by the stream consumer

Both keep the 200 most recent events of a session. Fewer than 2 sessions in 100,000 in
the October data are longer. Redis also expires a session after 30 minutes without an
event, which is how REES46 itself ends a session.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict, deque

from .online import Event

MAX_EVENTS = 200
MAX_SESSIONS = 200_000
TTL_SECONDS = 30 * 60


def encode(e: Event) -> str:
    return json.dumps(
        [e.time, e.event_type, e.product_id, e.category_id, e.brand, e.price], separators=(",", ":")
    )


def decode(raw: str | bytes) -> Event:
    t, et, p, c, b, pr = json.loads(raw)
    return Event(t, et, p, c, b, pr)


class MemorySessions:
    """Bounded, thread-safe histories in this process: the least recently active
    session is dropped first."""

    def __init__(self, max_sessions: int = MAX_SESSIONS, max_events: int = MAX_EVENTS):
        self._data: OrderedDict[str, deque[Event]] = OrderedDict()
        self._lock = threading.Lock()
        self.max_sessions, self.max_events = max_sessions, max_events

    def append(self, session: str, event: Event) -> int:
        with self._lock:
            events = self._data.pop(session, None) or deque(maxlen=self.max_events)
            events.append(event)
            self._data[session] = events
            while len(self._data) > self.max_sessions:
                self._data.popitem(last=False)
            return len(events)

    def get(self, session: str) -> list[Event]:
        with self._lock:
            events = self._data.get(session)
            return list(events) if events else []

    def __len__(self) -> int:
        return len(self._data)


class RedisSessions:
    """One Redis list per session: appended, trimmed and given a fresh expiry in a single
    round trip, so a reader never sees more than MAX_EVENTS or a list without a TTL."""

    def __init__(self, url_or_client, max_events: int = MAX_EVENTS, ttl: int = TTL_SECONDS):
        if isinstance(url_or_client, str):
            import redis

            url_or_client = redis.Redis.from_url(url_or_client)
        self.r = url_or_client
        self.max_events, self.ttl = max_events, ttl

    @staticmethod
    def key(session: str) -> str:
        return f"s:{session}"

    def append(self, session: str, event: Event) -> int:
        return self.append_many([(session, event)])[0]

    def append_many(self, items: list[tuple[str, Event]]) -> list[int]:
        """Many events in one round trip, in order: what the stream consumer writes."""
        pipe = self.r.pipeline(transaction=False)
        for session, event in items:
            k = self.key(session)
            pipe.rpush(k, encode(event))
            pipe.ltrim(k, -self.max_events, -1)
            pipe.expire(k, self.ttl)
        out = pipe.execute()
        return [min(n, self.max_events) for n in out[0::3]]

    def get(self, session: str) -> list[Event]:
        return [decode(x) for x in self.r.lrange(self.key(session), 0, -1)]

    def __len__(self) -> int:
        return int(self.r.dbsize())
