"""Request bodies for the load test: real test sessions, as the API receives them.

    python -m marketplace_recs.loadtest      # loadtest/sessions.json, never committed

Each body is one session's history up to its cut, exactly what a product page would send
after the visitor's latest event.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import experiment as ex
from .online import to_events

OUT = Path("loadtest") / "sessions.json"
TYPES = ["view", "cart", "purchase"]


def main(n: int = 2000) -> int:
    history, cases = ex.cases_for("C")
    picked = cases.tail(n).select("session")
    per_session = to_events(history.join(picked, on="session", how="semi"))
    bodies = [
        [
            {
                "product_id": e.product_id,
                "event_type": TYPES[e.event_type],
                "time": e.time,
                "category_id": e.category_id,
                "brand": e.brand,
                "price": None if e.price is None else round(e.price, 2),
            }
            for e in events[-200:]
        ]
        for events in per_session.values()
    ]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(bodies), encoding="utf-8")
    print(f"{len(bodies)} sessions -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
