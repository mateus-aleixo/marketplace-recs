"""The ranker against the item-to-item baseline, replayed as an online experiment would
have seen them in the test week.

    python -m marketplace_recs.replay                  # runs/abtest_C.json, every session
    python -m marketplace_recs.replay --cases 200000   # runs/abtest_C_200000.json, the sample

Each session of window C that touches two products is cut as the evaluation cuts it,
and both lists are built for it: the baseline's top 20 (control) and the ranker's
(treatment). A session clicks when its target is in the list it is shown, so every
session's outcome under both arms is known, and an experiment that shows each user one
arm can be checked against the true effect. A session belongs to the user of its first
event and to the day it starts.

The ranker's lift is detectable within hours. To trace power at smaller effects, a
diluted treatment shows the ranker's list in a seeded random share q of sessions and the
baseline's in the rest, for q in 1, 0.5, 0.2, 0.1 and 0.05. The share is drawn once, so
its true effect is known exactly.

    realised      one test as production would run it: users split by a salted hash of
                  their id, read after the first day and after the week
    power         by day, the minimum detectable effect, and for every q the analytic
                  power and the share of bootstrap replicates that reject, for the A/B
                  test, the A/B test with CUPED, and interleaving
    aa            the same replicates with both arms on the control
    cuped         covariates from before 24 October: item-to-item hits and cases on the
                  user's cuttable sessions in window B, cut and scored with B's tables;
                  log1p of the user's events and sessions; whether they had any
    interleaving  team draft of the treatment's list with the control's, which every user
                  sees, with its coins drawn once per session

The lists and covariates are cached under data/replay/, so a rerun repeats only the
analysis. On the cached sample, the two arms must reproduce the published recall@20 of
the item-to-item baseline and the ranker, and the output says whether they do.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import timedelta

import lightgbm as lgb
import numpy as np
import polars as pl

from . import abtest as ab
from . import candidates as cand
from . import experiment as ex
from . import ranker, refresh
from .features import product_stats
from .metrics import evaluate, target_rank
from .split import WINDOWS, cases, events, stats_events

WINDOW = "C"
REPLAY = ex.DATA / "replay"
MODEL = ex.DATA / "models" / "ranker.txt"
# Shares of sessions shown the ranker. The first is the ranker itself, which the realised
# test and the interleaving shares report.
SHARES = (1.0, 0.5, 0.2, 0.1, 0.05)
REPLICATES = 1000
SALT = "ranker-vs-item2item"
COVARIATES = ("b_hits", "b_cases", "log_events", "log_sessions", "active")
# Sessions a chunk: what ranker.chunks gives the cached sample, so its features are
# built exactly as the published run built them.
CHUNK = 25_000
EVERY = 10**9  # more sessions than any window has, so split.cases cuts them all
COLUMNS = (
    "event_time",
    "event_type",
    "product_id",
    "category_id",
    "brand",
    "price",
    "user_id",
    "session",
)
DAY = 86_400


def log(*args) -> None:
    print(*args, flush=True)


# -- the lists ---------------------------------------------------------------------


def sessions_of(history: pl.DataFrame, start) -> pl.DataFrame:
    """Per session, the user of its first event and the day it starts, 0 for `start`. A
    first event without a user would make the session its own user, with a negative id
    that no pre-period covariate matches."""
    s = int(start.timestamp())
    return history.filter(pl.col("pos") == 0).select(
        "session",
        pl.col("user_id").fill_null(-1 - pl.col("session")),
        ((pl.col("event_time").dt.epoch("s") - s) // DAY).cast(pl.Int32).alias("day"),
    )


def item2item(h: pl.DataFrame, c: pl.DataFrame, matrices, pop) -> np.ndarray:
    return cand.top_k(cand.baseline_scores("item2item", h, matrices, pop), c, pop)


def lists(history: pl.DataFrame, cs: pl.DataFrame, tables, booster) -> pl.DataFrame:
    """Per session, in session order: its user and day, the target, and the top 20 of the
    control (item to item) and of the treatment (the ranker, scored as refresh.score
    does), built a chunk of sessions at a time."""
    matrices, _, pop = tables
    parts = []
    n = max(ranker.CHUNKS, -(-cs.height // CHUNK))
    for i, (h, c) in enumerate(ranker.chunks(history, cs, n)):
        control = item2item(h, c, matrices, pop)
        treatment = refresh.score(h, c, tables, booster)
        parts.append(
            c.select("session", "target").with_columns(
                pl.Series("control", control.astype(np.int32)),
                pl.Series("treatment", treatment.astype(np.int32)),
            )
        )
        if (i + 1) % 10 == 0:
            log(f"  lists: chunk {i + 1} of {n}")
    return (
        pl.concat(parts)
        .join(sessions_of(history, WINDOWS[WINDOW][0]), on="session")
        .sort("session")
        .select("session", "user_id", "day", "target", "control", "treatment")
    )


def population(n: int | None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(history, cases): the cached sample when n is its size, otherwise n sessions of
    window C, every cuttable one when n is None, cut by split.cases as the evaluation
    cuts them."""
    if n == ex.N_CASES[WINDOW]:
        return ex.cases_for(WINDOW)
    return cases(WINDOW, n or EVERY, ex.SEED, lf=events().select(COLUMNS))


def tables_for(window: str) -> tuple[dict, pl.DataFrame, pl.DataFrame]:
    """The tables window C's evaluation scores with, built from every event before it."""
    return (
        ex.matrices_for(window),
        product_stats(stats_events(window), WINDOWS[window][0]),
        ex.popular_for(window),
    )


def lists_for(n: int | None, seconds: dict) -> pl.DataFrame:
    """The lists for --cases n, from data/replay/ when they are there."""
    path = REPLAY / f"lists-{'all' if n is None else n}.parquet"
    t0 = time.perf_counter()
    if not path.exists():
        history, cs = population(n)
        seconds["cases"] = time.perf_counter() - t0
        log(f"cases: {cs.height:,} sessions, {seconds['cases']:.0f} s")
        t0 = time.perf_counter()
        booster = lgb.Booster(model_file=str(MODEL))
        out = lists(history, cs, tables_for(WINDOW), booster)
        REPLAY.mkdir(parents=True, exist_ok=True)
        out.write_parquet(path)
        seconds["lists"] = time.perf_counter() - t0
        log(f"lists: {seconds['lists']:.0f} s")
    else:
        seconds["cases"] = 0.0
        seconds["lists"] = time.perf_counter() - t0
    return pl.read_parquet(path)


# -- covariates ------------------------------------------------------------------


def covariates(lf: pl.LazyFrame, matrices, pop) -> pl.DataFrame:
    """Per user with any event before window C: events and sessions, and the item-to-item
    hits and cases of their cuttable sessions in window B, cut and scored as the
    evaluation does with B's tables, from the events before C only."""
    start = WINDOWS[WINDOW][0]
    before = lf.filter(pl.col("event_time") < start)
    activity = (
        before.group_by("user_id")
        .agg(pl.len().alias("events"), pl.col("session").n_unique().alias("sessions"))
        .collect()
    )
    history, cs = cases("B", EVERY, ex.SEED, lf=before)
    hits = []
    for h, c in ranker.chunks(history, cs, max(1, -(-cs.height // CHUNK))):
        run = item2item(h, c, matrices, pop)
        hit = target_rank(run, c["target"].to_numpy()) > 0
        hits.append(c.select("session").with_columns(pl.Series("hit", hit)))
    window_b = (
        pl.concat(hits)
        .join(sessions_of(history, WINDOWS["B"][0]), on="session")
        .group_by("user_id")
        .agg(
            pl.col("hit").sum().cast(pl.Int64).alias("b_hits"),
            pl.len().cast(pl.Int64).alias("b_cases"),
        )
    )
    return (
        activity.join(window_b, on="user_id", how="left")
        .with_columns(pl.col("b_hits", "b_cases").fill_null(0))
        .select("user_id", "b_hits", "b_cases", "events", "sessions")
        .sort("user_id")
    )


def covariates_for(seconds: dict) -> pl.DataFrame:
    path = REPLAY / "covariates.parquet"
    t0 = time.perf_counter()
    if not path.exists():
        out = covariates(events().select(COLUMNS), ex.matrices_for("B"), ex.popular_for("B"))
        REPLAY.mkdir(parents=True, exist_ok=True)
        out.write_parquet(path)
        log(f"covariates: {out.height:,} users before {WINDOWS[WINDOW][0]:%d %B}")
    seconds["covariates"] = time.perf_counter() - t0
    return pl.read_parquet(path)


def covariate_matrix(user_ids: np.ndarray, covs: pl.DataFrame) -> np.ndarray:
    """(users, 5) in the order of user_ids; 0 everywhere for a user new in window C."""
    x = (
        pl.DataFrame({"user_id": user_ids})
        .join(covs, on="user_id", how="left", maintain_order="left")
        .with_columns(pl.col("b_hits", "b_cases", "events", "sessions").fill_null(0))
        .select(
            pl.col("b_hits").cast(pl.Float64),
            pl.col("b_cases").cast(pl.Float64),
            pl.col("events").cast(pl.Float64).log1p().alias("log_events"),
            pl.col("sessions").cast(pl.Float64).log1p().alias("log_sessions"),
            (pl.col("events") > 0).cast(pl.Float64).alias("active"),
        )
    )
    return x.select(COVARIATES).to_numpy()


# -- the analysis ------------------------------------------------------------------


def rate(x) -> float:
    return round(float(x), 4)


def fine(x) -> float | None:
    """Effects and standard errors, which are often smaller than a rate's last decimal."""
    x = float(x)
    return round(x, 6) if np.isfinite(x) else None


def count(x) -> int | None:
    x = float(x)
    return int(round(x)) if np.isfinite(x) else None


def times(x) -> float | None:
    x = float(x)
    return round(x, 2) if np.isfinite(x) else None


def tested(effect, se) -> dict:
    """An estimate with its standard error, 95% interval and two-sided p-value."""
    z = ab.z_alpha()
    return {
        "effect": fine(effect),
        "se": fine(se),
        "ci95": [fine(effect - z * se), fine(effect + z * se)],
        "p": float(f"{float(ab.p_value(effect, se)):.3g}"),
    }


def interleave(a, b, coins, target, size: int = 200_000) -> tuple[np.ndarray, np.ndarray]:
    """Per session, the team-draft credit of list a against list b, and whether the
    target is in the interleaved list, a chunk of sessions at a time."""
    credits, hits = [], []
    for lo in range(0, len(target), size):
        s = slice(lo, lo + size)
        items, team = ab.team_draft(a[s], b[s], coins[s])
        credits.append(ab.credit(items, team, target[s]))
        hits.append((items == target[s, None]).any(1))
    return np.concatenate(credits), np.concatenate(hits)


def truths(panel: ab.Panel) -> dict[str, np.ndarray]:
    """Per day, over every user active by then with weight 1: the true effects and
    preferences, the planning view of an A/B test (the control's variance per user in
    both arms) and CUPED's analytic variance reduction on the control."""
    q, _, days = panel.treatment.shape
    out = {k: np.empty((q, days)) for k in ("effect", "pref", "pref_var")}
    out.update({k: np.empty(days) for k in ("control", "se", "reduction", "aa", "aa_var")})
    out["sessions"] = np.empty(days)
    for d in range(days):
        F, index = panel.columns(d)
        S = F.sum(0)
        control = ab.take(S, index, "c")
        out["control"][d] = ab.ratio(control)[0]
        out["sessions"][d] = control.N
        out["se"][d] = ab.planned_se(control)
        out["reduction"][d] = ab.reduction(control)
        for j in range(q):
            out["effect"][j, d] = ab.ratio(ab.take(S, index, f"t{j}"))[0] - out["control"][d]
            out["pref"][j, d], out["pref_var"][j, d] = ab.ratio(ab.take(S, index, f"i{j}"))
        out["aa"][d], out["aa_var"][d] = ab.ratio(ab.take(S, index, "a"))
    return out


def realised(panel: ab.Panel, user_ids: np.ndarray, labels: list[str]) -> dict:
    """The ranker (q = 1) as production would test it: each user's arm from a salted hash
    of their id, read after the first day and after the week."""
    treated = ab.assign(user_ids[panel.order], SALT)
    w = np.stack([treated, ~treated]).astype(np.float64)
    out: dict = {"salt": SALT, "share_on_treatment": rate(treated.mean())}
    for name, d in (("after_first_day", 0), ("after_week", len(labels) - 1)):
        F, index = panel.columns(d)
        T, C = w[:, : panel.active[d]] @ F
        t, c = ab.take(T, index, "t0"), ab.take(C, index, "c")
        effect, se, se_naive = ab.difference(t, c)
        out[name] = {
            "day": labels[d],
            "users": {"treatment": count(t.n), "control": count(c.n)},
            "sessions": {"treatment": count(t.N), "control": count(c.N)},
            "recall@20": {"treatment": rate(t.Y / t.N), "control": rate(c.Y / c.N)},
            "delta_method": tested(effect, se),
            "naive": tested(effect, se_naive),
            "cuped": tested(*ab.cuped(t, c)),
        }
    return out


def first_day(power: np.ndarray, target: float = ab.POWER) -> int | None:
    """Days of the test until power first reaches target, or None within the window."""
    reached = np.flatnonzero(power >= target)
    return int(reached[0]) + 1 if reached.size else None


def analyse(
    replay: pl.DataFrame, covs: pl.DataFrame, replicates: int, seed: int, seconds: dict
) -> dict:
    start, end = WINDOWS[WINDOW]
    days = (end - start).days
    labels = [f"{start + timedelta(days=d):%Y-%m-%d}" for d in range(days)]
    target = replay["target"].to_numpy()
    control = replay["control"].to_numpy()
    treatment = replay["treatment"].to_numpy()
    n = len(target)

    t0 = time.perf_counter()
    hit_c = target_rank(control, target) > 0
    hit_r = target_rank(treatment, target) > 0
    # One seeded order of the sessions: q shows the ranker in its first round(q n), so
    # each share's sessions hold the next smaller share's.
    order = np.empty(n, dtype=np.int64)
    order[np.random.default_rng([seed, 1]).permutation(n)] = np.arange(n)
    shown = np.stack([order < round(q * n) for q in SHARES])
    coins = np.random.default_rng([seed, 2]).random((n, ab.K)) < 0.5
    credit_r, hit_il = interleave(treatment, control, coins, target)
    credit_aa, _ = interleave(control, control, coins, target)
    seconds["interleaving"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    user_ids, user = np.unique(replay["user_id"].to_numpy(), return_inverse=True)
    day = replay["day"].to_numpy()
    panel = ab.Panel.from_sessions(
        user,
        day,
        days,
        hit_c,
        np.where(shown, hit_r, hit_c),
        np.where(shown, credit_r, credit_aa),
        credit_aa,
        covariate_matrix(user_ids, covs),
    )
    truth = truths(panel)
    out: dict = {
        "population": {
            "sessions": n,
            "users": len(user_ids),
            "by_day": [
                {
                    "day": label,
                    "sessions": int((day == d).sum()),
                    "users_that_day": int(np.unique(user[day == d]).size),
                    "users_so_far": int(panel.active[d]),
                }
                for d, label in enumerate(labels)
            ],
        },
        "arms": {
            "control": {"model": "item2item", **evaluate(control, target)},
            "treatment": {"model": "ranker", **evaluate(treatment, target)},
        },
        "dilution": [
            {
                "q": q,
                "sessions_on_ranker": int(shown[j].sum()),
                "true_effect": fine(truth["effect"][j, -1]),
                "true_preference": fine(truth["pref"][j, -1]),
            }
            for j, q in enumerate(SHARES)
        ],
        "realised": realised(panel, user_ids, labels),
    }
    seconds["analysis"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    sim = ab.simulate(panel, replicates, seed)
    seconds["bootstrap"] = time.perf_counter() - t0
    z = ab.z_alpha()

    def rejects(key: str) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            return (np.abs(sim[key] / sim[f"{key}_se"]) > z).mean(0)

    simulated = {"ab": rejects("ab"), "ab_cuped": rejects("cuped"), "interleaving": rejects("il")}
    cuped_se = truth["se"] * np.sqrt(1 - truth["reduction"])
    analytic = {
        "ab": ab.power(truth["effect"], truth["se"]),
        "ab_cuped": ab.power(truth["effect"], cuped_se),
        "interleaving": ab.power(truth["pref"], np.sqrt(truth["pref_var"])),
    }
    with np.errstate(invalid="ignore", divide="ignore"):
        aa = {
            "delta_method": rejects("aa"),
            "naive": (np.abs(sim["aa"] / sim["aa_se_naive"]) > z).mean(0),
            "cuped": rejects("aa_cuped"),
            "interleaving": rejects("aa_il"),
        }
    out["aa"] = {
        "replicates": replicates,
        "by_day": [
            {"day": label, **{k: rate(v[d]) for k, v in aa.items()}}
            for d, label in enumerate(labels)
        ],
    }
    out["mde"] = [
        {
            "day": label,
            "users": int(panel.active[d]),
            "sessions": count(truth["sessions"][d]),
            "control": rate(truth["control"][d]),
            "ab": fine(ab.mde(truth["se"][d])),
            "ab_cuped": fine(ab.mde(cuped_se[d])),
        }
        for d, label in enumerate(labels)
    ]
    out["power"] = [
        {
            "q": q,
            "day": label,
            "users": int(panel.active[d]),
            "true_effect": fine(truth["effect"][j, d]),
            "true_preference": fine(truth["pref"][j, d]),
            **{
                k: {"analytic": rate(analytic[k][j, d]), "simulated": rate(simulated[k][j, d])}
                for k in simulated
            },
        }
        for j, q in enumerate(SHARES)
        for d, label in enumerate(labels)
    ]
    out["cuped"] = cuped_summary(sim, truth, panel, labels)
    out["interleaving"] = interleaving_summary(credit_r, hit_il, credit_aa, truth)
    out["sensitivity"] = sensitivity(truth, analytic, simulated, panel)
    out["replicates"] = replicates
    out["seeds"] = {
        "cases": ex.SEED,
        "seed": seed,
        "dilution": f"numpy default_rng([{seed}, 1]).permutation of the sessions",
        "coins": f"numpy default_rng([{seed}, 2]), one per session and position",
        "bootstrap": f"replicate r from the r-th child of numpy SeedSequence({seed})",
        "salt": SALT,
    }
    return out


def cuped_summary(sim: dict, truth: dict, panel: ab.Panel, labels: list[str]) -> dict:
    """CUPED's variance reduction, analytic on the control and measured across the A/A
    replicates, the users it saves, and its mean estimate against the true effect."""
    measured_aa = 1 - sim["aa_cuped"].var(0) / sim["aa"].var(0)
    measured = 1 - sim["cuped"].var(0) / sim["ab"].var(0)
    return {
        "covariates": list(COVARIATES),
        "by_day": [
            {
                "day": label,
                "reduction_analytic": rate(truth["reduction"][d]),
                "reduction_measured": rate(measured_aa[d]),
                "users_saved": count(truth["reduction"][d] * panel.active[d]),
            }
            for d, label in enumerate(labels)
        ],
        "after_week": [
            {
                "q": q,
                "true_effect": fine(truth["effect"][j, -1]),
                "mean_estimate": fine(sim["ab"][:, j, -1].mean()),
                "mean_estimate_cuped": fine(sim["cuped"][:, j, -1].mean()),
                "reduction_measured": rate(measured[j, -1]),
            }
            for j, q in enumerate(SHARES)
        ],
    }


def interleaving_summary(credit, hit, credit_aa, truth: dict) -> dict:
    """The ranker interleaved with the baseline (q = 1), and the baseline with itself."""
    return {
        "ranker_wins": rate((credit == 1).mean()),
        "baseline_wins": rate((credit == -1).mean()),
        "no_click": rate((credit == 0).mean()),
        "hit_rate": rate(hit.mean()),
        "realised": {
            "after_first_day": tested(truth["pref"][0, 0], np.sqrt(truth["pref_var"][0, 0])),
            "after_week": tested(truth["pref"][0, -1], np.sqrt(truth["pref_var"][0, -1])),
        },
        "aa": {
            "first_team_wins": rate((credit_aa == 1).mean()),
            "second_team_wins": rate((credit_aa == -1).mean()),
            "after_week": tested(truth["aa"][-1], np.sqrt(truth["aa_var"][-1])),
        },
    }


def sensitivity(truth: dict, analytic: dict, simulated: dict, panel: ab.Panel) -> list[dict]:
    """Per q: the days each design needs for 80% power and the users active by then; the
    users it needs analytically, each followed for the week, from the week's variance
    per user; and how many times as many users the A/B test needs as interleaving, and
    as the A/B test with CUPED."""
    users = panel.active[-1]
    var_ab = truth["se"][-1] ** 2 * users  # the difference's variance per user
    rows = []
    for j, q in enumerate(SHARES):
        # An effect of exactly zero needs infinitely many users: reported as null.
        with np.errstate(divide="ignore", invalid="ignore"):
            need = {
                "ab": ab.users_needed(truth["effect"][j, -1], var_ab),
                "ab_cuped": ab.users_needed(
                    truth["effect"][j, -1], var_ab * (1 - truth["reduction"][-1])
                ),
                "interleaving": ab.users_needed(
                    truth["pref"][j, -1], truth["pref_var"][j, -1] * users
                ),
            }
            ratios = {
                "ab_over_interleaving": times(need["ab"] / need["interleaving"]),
                "ab_over_cuped": times(need["ab"] / need["ab_cuped"]),
            }
        days = {
            k: {"analytic": first_day(analytic[k][j]), "simulated": first_day(simulated[k][j])}
            for k in need
        }
        rows.append(
            {
                "q": q,
                "days_to_80_power": days,
                "users_by_then": {
                    k: int(panel.active[v["simulated"] - 1]) if v["simulated"] else None
                    for k, v in days.items()
                },
                "users_needed": {k: count(v) for k, v in need.items()},
                **ratios,
            }
        )
    return rows


def sample_check(arms: dict) -> dict:
    """The arms' recall@20 on the cached sample against the published runs."""
    published = {}
    for name, file, key in (
        ("item2item", "baselines_C.json", "item2item"),
        ("ranker", "ranker_C.json", "test_C"),
    ):
        path = ex.RUNS / file
        published[name] = json.loads(path.read_text())[key]["recall@20"] if path.exists() else None
    replay = {"item2item": arms["control"]["recall@20"], "ranker": arms["treatment"]["recall@20"]}
    out: dict = {k: {"replay": replay[k], "published": published[k]} for k in replay}
    # null when a published run is missing, so there is nothing to compare with
    out["match"] = (
        all(v["replay"] == v["published"] for v in out.values())
        if all(v is not None for v in published.values())
        else None
    )
    return out


def dumps(obj, width: int = 120, level: int = 0) -> str:
    """JSON as json.dumps(indent=2) writes it, except that a list or object that fits in
    `width` characters stays on one line, so the tables read row by row."""
    flat = json.dumps(obj)
    if not isinstance(obj, dict | list) or not obj or len(flat) + 2 * level <= width:
        return flat
    pad, end = "  " * (level + 1), "  " * level
    if isinstance(obj, dict):
        rows = [f"{pad}{json.dumps(k)}: {dumps(v, width, level + 1)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(rows) + "\n" + end + "}"
    return "[\n" + ",\n".join(pad + dumps(v, width, level + 1) for v in obj) + "\n" + end + "]"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--cases",
        type=int,
        default=None,
        help="sessions to replay: every cuttable one by default; 200000 is the cached sample",
    )
    ap.add_argument("--replicates", type=int, default=REPLICATES)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    t0 = time.perf_counter()
    seconds: dict[str, float] = {}
    replay = lists_for(a.cases, seconds)
    covs = covariates_for(seconds)
    sample = a.cases == ex.N_CASES[WINDOW]
    analysis = analyse(replay, covs, a.replicates, a.seed, seconds)
    result = {
        "window": WINDOW,
        "cases": "the cached sample" if sample else a.cases or "every cuttable session",
        "population": analysis.pop("population"),
        "arms": analysis.pop("arms"),
    }
    if sample:
        result["sample_check"] = check = sample_check(result["arms"])
        verdict = {True: "match", False: "NO MATCH", None: "no published run to compare"}
        log(
            f"sample check: item2item {check['item2item']['replay']} against "
            f"{check['item2item']['published']} published, ranker {check['ranker']['replay']} "
            f"against {check['ranker']['published']}: {verdict[check['match']]}"
        )
    result.update(analysis)
    seconds["total"] = time.perf_counter() - t0
    result["seconds"] = {k: round(v, 1) for k, v in seconds.items()}
    ex.RUNS.mkdir(exist_ok=True)
    path = ex.RUNS / ("abtest_C.json" if a.cases is None else f"abtest_C_{a.cases}.json")
    path.write_text(dumps(result) + "\n")
    log(f"{path}: {seconds['total']:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
