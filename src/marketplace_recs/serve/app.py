"""The real-time API: session events in, recommendations out.

    uvicorn marketplace_recs.serve.app:app --port 8000

    POST /sessions/{session}/events             one event for a session
    GET  /sessions/{session}                    how many events the session holds
    GET  /sessions/{session}/recommendations    the next products for that session
    POST /recommend                             stateless: the events come in the body
    GET  /health                                liveness, the model, where state lives
    GET  /metrics                               Prometheus counters and latency histograms

Where session state lives is configuration, not code:

    RECS_SESSIONS unset          in this process: right for one worker, wrong for two
    RECS_SESSIONS=redis://...    in Redis, shared by every worker and replica
    RECS_KAFKA=host:port         events are published to Redpanda and answered 202; the
                                 stream consumer writes them to Redis

Either store keeps a session's 200 most recent events (see sessions.py).
"""

from __future__ import annotations

import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

from ..online import Event, Model
from ..sessions import MAX_EVENTS, MemorySessions, RedisSessions

MODEL_DIR = Path(os.environ.get("RECS_MODEL_DIR", "model"))
SESSIONS_URL = os.environ.get("RECS_SESSIONS")
KAFKA = os.environ.get("RECS_KAFKA")
TYPES = {"view": 0, "cart": 1, "purchase": 2}

REQUESTS = Counter("recs_requests_total", "Requests by route and status", ["route", "status"])
LATENCY = Histogram(
    "recs_latency_seconds",
    "Time spent ranking, by route",
    ["route"],
    buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 1.0),
)

app = FastAPI(
    title="marketplace-recs",
    description="Next-product recommendations for a live marketplace session.",
    version="0.1.0",
)


class EventIn(BaseModel):
    product_id: int
    event_type: Literal["view", "cart", "purchase"] = "view"
    time: int | None = Field(None, description="Epoch seconds; the server's clock if absent.")
    category_id: int | None = None
    brand: str | None = None
    price: float | None = None

    def to_event(self) -> Event:
        t = self.time if self.time is not None else int(time.time())
        return Event(
            t, TYPES[self.event_type], self.product_id, self.category_id, self.brand, self.price
        )


class RecommendIn(BaseModel):
    events: list[EventIn] = Field(..., min_length=1, max_length=MAX_EVENTS)
    k: int = Field(20, ge=1, le=100)


class Recommendation(BaseModel):
    product_id: int
    score: float


class RecommendOut(BaseModel):
    session: str | None = None
    events: int
    recommendations: list[Recommendation]


sessions = RedisSessions(SESSIONS_URL) if SESSIONS_URL else MemorySessions()


@lru_cache(maxsize=1)
def kafka():
    from ..stream import ensure_topic, producer

    ensure_topic(KAFKA)
    return producer(KAFKA)


@lru_cache(maxsize=1)
def model() -> Model:
    if not (MODEL_DIR / "ranker.txt").exists():
        raise HTTPException(
            503, f"no model under {MODEL_DIR}: run python -m marketplace_recs.online export"
        )
    return Model(MODEL_DIR)


def _rank(route: str, events: list[Event], k: int, session: str | None = None) -> RecommendOut:
    start = time.perf_counter()
    try:
        recs = model().recommend(events, k)
    except HTTPException:
        REQUESTS.labels(route, "503").inc()
        raise
    LATENCY.labels(route).observe(time.perf_counter() - start)
    REQUESTS.labels(route, "200").inc()
    return RecommendOut(
        session=session,
        events=len(events),
        recommendations=[Recommendation(product_id=p, score=round(s, 6)) for p, s in recs],
    )


@app.get("/health")
def health() -> dict:
    loaded = (MODEL_DIR / "ranker.txt").exists()
    return {
        "status": "ok",
        "model": "ready" if loaded else "missing",
        "state": "redis" if SESSIONS_URL else "memory",
        "events": "kafka" if KAFKA else "direct",
        "sessions": len(sessions),
    }


@app.post("/sessions/{session}/events")
def add_event(session: str, event: EventIn, response: Response) -> dict:
    if KAFKA:
        from ..stream import publish

        publish(kafka(), session, event.to_event())
        REQUESTS.labels("events", "202").inc()
        response.status_code = 202
        return {"session": session, "accepted": True}
    n = sessions.append(session, event.to_event())
    REQUESTS.labels("events", "200").inc()
    return {"session": session, "events": n}


@app.get("/sessions/{session}")
def session_state(session: str) -> dict:
    """What the store holds for a session. Needs no model, so CI can watch events arrive
    through the stream."""
    n = len(sessions.get(session))
    if not n:
        raise HTTPException(404, f"no events for session {session!r}")
    return {"session": session, "events": n}


@app.get("/sessions/{session}/recommendations", response_model=RecommendOut)
def session_recommendations(session: str, k: int = Query(20, ge=1, le=100)) -> RecommendOut:
    events = sessions.get(session)
    if not events:
        REQUESTS.labels("session", "404").inc()
        raise HTTPException(404, f"no events for session {session!r}")
    return _rank("session", events, k, session)


@app.post("/recommend", response_model=RecommendOut)
def recommend(body: RecommendIn) -> RecommendOut:
    return _rank("recommend", [e.to_event() for e in body.events], body.k)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
