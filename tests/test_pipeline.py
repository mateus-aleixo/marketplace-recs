"""The nightly job's gates and its hand-over to the API, on synthetic data."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest
from fastapi.testclient import TestClient

from marketplace_recs.pipeline import GateError, build, check, publish, serving_dir, validate
from marketplace_recs.serve import app as app_module


def day(d: int) -> datetime:
    return datetime(2019, 10, d, tzinfo=UTC)


def daily(per_day: dict[int, int], last_hour: dict[int, int] | None = None) -> pl.LazyFrame:
    """`per_day[d]` events spread over October d, the last one at 23:59 unless
    `last_hour[d]` cuts the day short."""
    rows = []
    for d, n in per_day.items():
        end = (last_hour or {}).get(d, 23) * 60 + 59
        for i in range(n):
            minute = round(i * end / max(n - 1, 1))
            rows.append((day(d) + timedelta(minutes=minute), 0, i % 50 + 1, 1, "acme", 9.5, d))
    return pl.DataFrame(
        rows,
        schema={
            "event_time": pl.Datetime("us", "UTC"),
            "event_type": pl.UInt8,
            "product_id": pl.Int32,
            "category_id": pl.Int64,
            "brand": pl.String,
            "price": pl.Float32,
            "session": pl.Int32,
        },
        orient="row",
    ).lazy()


def week(today: int = 100, **override) -> dict[int, int]:
    days = {d: 100 for d in range(18, 25)}
    days[25] = today
    days.update(override)
    return days


def test_check_passes_a_normal_day():
    report = check(day(25), daily(week()))
    assert report["events"] == 100
    assert report["ratio_to_trailing_median"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("per_day", "last_hour", "message"),
    [
        ({**week(), 25: 0}, None, "no events"),
        (week(), {25: 10}, "not complete"),
        (week(today=400), None, "trailing median"),
        (week(today=20), None, "trailing median"),
    ],
)
def test_check_refuses_a_missing_short_or_strange_day(per_day, last_hour, message):
    per_day = {d: n for d, n in per_day.items() if n}
    with pytest.raises(GateError, match=message):
        check(day(25), daily(per_day, last_hour))


def baked_model(world, tmp_path):
    """A model directory as the image ships it: flat files, no versions."""
    model = tmp_path / "model"
    shutil.copytree(world["dir"], model)
    return model


def test_build_validate_publish_and_the_api_swaps_without_a_restart(world, tmp_path, monkeypatch):
    model = baked_model(world, tmp_path)
    assert serving_dir(model) == model

    monkeypatch.setattr(app_module, "MODEL_DIR", model)
    app_module.model.cache_clear()
    client = TestClient(app_module.app)
    assert client.get("/health").json()["version"] == "baked"
    body = {"events": [{"product_id": 11}, {"product_id": 12}], "k": 5}
    assert client.post("/recommend", json=body).status_code == 200

    version = build(day(25), world["events"].lazy(), model)
    report = validate(version, model, min_overlap=0.0)
    assert report["probe_sessions_ranked"] > 0
    assert serving_dir(model) == model  # built and validated, not yet serving

    publish(version, model)
    assert serving_dir(model).name == version
    assert client.get("/health").json()["version"] == version
    r = client.post("/recommend", json=body)
    assert r.status_code == 200 and len(r.json()["recommendations"]) == 5
    assert app_module._loaded["dir"].name == version
    app_module.model.cache_clear()


def test_publish_refuses_an_incomplete_version(world, tmp_path):
    model = baked_model(world, tmp_path)
    (model / "versions" / "2019-10-26").mkdir(parents=True)
    with pytest.raises(GateError, match="not complete"):
        publish("2019-10-26", model)
    assert serving_dir(model) == model


def test_validate_refuses_a_version_that_lost_its_catalogue(world, tmp_path):
    model = baked_model(world, tmp_path)
    version = build(day(25), world["events"].lazy().filter(pl.col("product_id") <= 20), model)
    with pytest.raises(GateError, match="product count"):
        validate(version, model)
