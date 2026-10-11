"""The experiment statistics on synthetic users with known answers, and the offline replay
end to end on a small synthetic store with users and a pre-period."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from marketplace_recs import abtest as ab

Z = ab.z_alpha()


def clustered_users(users: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Hits and sessions per user. Each user has their own hit propensity and several
    sessions, so one user's sessions are not independent."""
    rng = np.random.default_rng(seed)
    sessions = 1 + rng.poisson(2.0, users)
    return rng.binomial(sessions, rng.beta(2, 2, users)), sessions


def synthetic_panel(users=3000, days=3, lifts=(0.0,), pre_share=1.0, seed=0) -> ab.Panel:
    """Clustered users over a few days. Each lift plants a treatment that adds hits to
    sessions the control misses; a session's interleaving credit goes to the list that
    alone holds the target, or to a coin when both do. The covariates are a noisy copy
    of each user's propensity and an indicator, both 0 for users without a pre-period."""
    rng = np.random.default_rng(seed)
    propensity = rng.beta(2, 2, users)
    user = np.repeat(np.arange(users), 1 + rng.poisson(1.5, users))
    day = rng.integers(0, days, len(user))
    p = propensity[user]
    control = rng.random(len(user)) < p
    coin = np.where(rng.random(len(user)) < 0.5, 1, -1)
    treatment, credit = [], []
    for lift in lifts:
        t = control | (rng.random(len(user)) < np.clip(lift / (1 - p), 0, 1))
        both = np.where(t & control, coin, 0)
        treatment.append(t)
        credit.append(np.where(t & ~control, 1, np.where(control & ~t, -1, both)))
    pre = rng.random(users) < pre_share
    x = np.column_stack([np.where(pre, propensity + rng.normal(0, 0.1, users), 0.0), pre])
    return ab.Panel.from_sessions(
        user,
        day,
        days,
        control,
        np.array(treatment),
        np.array(credit),
        np.where(control, coin, 0),
        x,
    )


def rejected(effect, se) -> np.ndarray:
    return np.abs(effect / se) > Z


# -- ratio metrics ---------------------------------------------------------------


def test_delta_method_se_matches_the_spread_over_random_assignments():
    hits, sessions = clustered_users(3000, seed=1)
    treated = np.random.default_rng(2).random((3000, 3000)) < 0.5  # one assignment per row
    effect, se, se_naive = ab.difference(
        ab.sums(hits, sessions, treated), ab.sums(hits, sessions, ~treated)
    )
    spread = effect.std()
    assert se.mean() == pytest.approx(spread, rel=0.05)
    # Treating each session as independent ignores that a user's sessions hit together.
    assert se_naive.mean() < 0.85 * spread
    assert 0.03 < rejected(effect, se).mean() < 0.07
    assert rejected(effect, se_naive).mean() > 0.09


def test_the_aa_false_positive_rate_is_near_five_percent():
    sim = ab.simulate(synthetic_panel(), replicates=2000, seed=0)
    for key in ("aa", "aa_cuped", "aa_il"):
        rate = rejected(sim[key], sim[f"{key}_se"]).mean(0)
        assert ((rate > 0.03) & (rate < 0.07)).all(), (key, rate)
    # By the last day users have had more sessions, and the naive SE misses more.
    assert rejected(sim["aa"], sim["aa_se_naive"]).mean(0)[-1] > 0.08


def test_the_bootstrap_sums_are_the_plain_sums_of_its_weights():
    panel = synthetic_panel(users=400, lifts=(0.05,))
    sim = ab.simulate(panel, replicates=3, seed=7, block=2)
    d = 1
    u = panel.active[d]
    t, c = ab.replicate_weights(len(panel.order), 7, 2)
    n, x = panel.sessions[:u, d], panel.x[:u]
    treated = ab.sums(panel.treatment[0, :u, d], n, t[:u], x)
    control = ab.sums(panel.control[:u, d], n, c[:u], x)
    effect, se, _ = ab.difference(treated, control)
    adjusted, adjusted_se = ab.cuped(treated, control)
    assert sim["ab"][2, 0, d] == pytest.approx(effect)
    assert sim["ab_se"][2, 0, d] == pytest.approx(se)
    assert sim["cuped"][2, 0, d] == pytest.approx(adjusted)
    assert sim["cuped_se"][2, 0, d] == pytest.approx(adjusted_se)
    pref, var = ab.ratio(ab.sums(panel.credit[0, :u, d], n, t[:u] + c[:u]))
    assert sim["il"][2, 0, d] == pytest.approx(pref)
    assert sim["il_se"][2, 0, d] == pytest.approx(np.sqrt(var))


def test_the_panel_cumulates_by_day_and_puts_early_users_first():
    # user 0 starts on day 1, user 1 on day 0
    panel = ab.Panel.from_sessions(
        user=[0, 1, 1, 0],
        day=[1, 0, 2, 2],
        days=3,
        control=[1, 0, 1, 1],
        treatment=np.array([[1, 1, 1, 0]]),
        credit=np.array([[1, 1, 0, -1]]),
        credit_aa=[0, 0, 1, -1],
        x=np.array([[5.0], [7.0]]),
    )
    assert panel.order.tolist() == [1, 0]
    assert panel.active.tolist() == [1, 2, 2]
    assert panel.sessions.tolist() == [[1, 1, 2], [0, 1, 2]]
    assert panel.control.tolist() == [[0, 0, 1], [0, 1, 2]]
    assert panel.credit[0].tolist() == [[1, 1, 1], [0, 1, 0]]
    assert panel.x[:, 0].tolist() == [7.0, 5.0]


def test_analytic_power_agrees_with_simulated_power_at_a_planted_effect():
    panel = synthetic_panel(lifts=(0.02, 0.03))
    sim = ab.simulate(panel, replicates=1000, seed=0)
    d = 2
    F, index = panel.columns(d)
    S = F.sum(0)
    control = ab.take(S, index, "c")
    for j in range(2):
        effect = ab.ratio(ab.take(S, index, f"t{j}"))[0] - ab.ratio(control)[0]
        analytic = ab.power(effect, ab.planned_se(control))
        simulated = rejected(sim["ab"][:, j, d], sim["ab_se"][:, j, d]).mean()
        assert 0.2 < analytic < 0.8
        assert simulated == pytest.approx(analytic, abs=0.05)
        assert sim["ab"][:, j, d].mean() == pytest.approx(effect, abs=0.002)


def test_mde_is_the_effect_detected_with_eighty_percent_power():
    se = 0.01
    assert ab.power(ab.mde(se), se) == pytest.approx(0.8, abs=1e-6)
    assert ab.power(0.0, se) == pytest.approx(0.05)
    k = ab.mde(1.0)
    assert ab.users_needed(0.1, 4.0) == pytest.approx(k * k * 4.0 / 0.01)


# -- CUPED -----------------------------------------------------------------------


def known_correlation(users: int, rho: float, seed: int, pre_share: float = 1.0):
    """One session per user and an outcome correlated rho with the covariate among the
    users who have one; the others have covariates of 0, as users new to the store."""
    rng = np.random.default_rng(seed)
    x = rng.normal(size=users)
    y = rho * x + np.sqrt(1 - rho**2) * rng.normal(size=users)
    pre = rng.random(users) < pre_share
    return y, np.column_stack([np.where(pre, x, 0.0), pre.astype(float)])


def cuped_trials(y, x, effect: float, trials: int = 2000, seed: int = 3):
    sessions = np.ones(len(y))
    treated = np.random.default_rng(seed).random((trials, len(y))) < 0.5
    t = ab.sums(y + effect, sessions, treated, x)
    c = ab.sums(y, sessions, ~treated, x)
    plain, plain_se, _ = ab.difference(t, c)
    adjusted, adjusted_se = ab.cuped(t, c)
    return plain, plain_se, adjusted, adjusted_se


def test_cuped_removes_rho_squared_of_the_variance_and_stays_unbiased():
    rho = 0.6
    y, x = known_correlation(4000, rho, seed=4)
    assert ab.reduction(ab.sums(y, np.ones(len(y)), x=x)) == pytest.approx(rho**2, abs=0.03)
    plain, _, adjusted, adjusted_se = cuped_trials(y, x, effect=0.05)
    assert 1 - adjusted.var() / plain.var() == pytest.approx(rho**2, abs=0.04)
    assert adjusted.mean() == pytest.approx(0.05, abs=3 * adjusted.std() / np.sqrt(len(adjusted)))
    assert adjusted_se.mean() == pytest.approx(adjusted.std(), rel=0.05)


def test_cuped_does_no_harm_with_an_uncorrelated_covariate():
    y, x = known_correlation(4000, 0.0, seed=5)
    assert abs(ab.reduction(ab.sums(y, np.ones(len(y)), x=x))) < 0.01
    plain, _, adjusted, adjusted_se = cuped_trials(y, x, effect=0.0)
    assert adjusted.var() < 1.01 * plain.var()
    assert 0.03 < rejected(adjusted, adjusted_se).mean() < 0.07


def test_cuped_keeps_users_without_a_pre_period():
    rho = 0.6
    y, x = known_correlation(4000, rho, seed=6, pre_share=0.5)
    plain, _, adjusted, adjusted_se = cuped_trials(y, x, effect=0.05)
    # Only the users with a pre-period gain, so about half of rho squared goes.
    assert 1 - adjusted.var() / plain.var() == pytest.approx(rho**2 / 2, abs=0.05)
    assert adjusted.mean() == pytest.approx(0.05, abs=3 * adjusted.std() / np.sqrt(len(adjusted)))
    assert adjusted_se.mean() == pytest.approx(adjusted.std(), rel=0.05)


# -- interleaving ----------------------------------------------------------------


def test_team_draft_by_hand():
    a, b = [1, 2, 3, 4], [2, 5, 1, 6]
    # a wins the first coin and takes 1; b, behind, takes 2; b wins the second coin and
    # takes 5, since 2 is in; a, behind, skips 2 and takes 3.
    items, teams = ab.team_draft_one(a, b, [True, True, False, True], k=4)
    assert items == [1, 2, 5, 3]
    assert teams == [1, -1, -1, 1]
    items, teams = ab.team_draft_one(a, b, [False, True, True, True], k=4)
    assert items == [2, 1, 3, 5]
    assert teams == [-1, 1, 1, -1]
    out, team = ab.team_draft(
        np.array([a]), np.array([b]), np.array([[True, True, False, True]]), k=4
    )
    assert out.tolist() == [[1, 2, 5, 3]]
    target = np.array([5])
    assert ab.credit(out, team, target).tolist() == [-1]
    assert ab.credit(out, team, np.array([4])).tolist() == [0]


def random_lists(sessions: int, seed: int, k: int = 20, products: int = 40, pad: bool = True):
    """Two top-k lists per session from a small catalogue, so they overlap, and in a
    fifth of the lists, -1 padding at the end, as top_k leaves a list it cannot fill."""
    rng = np.random.default_rng(seed)
    a = rng.permuted(np.tile(np.arange(products), (sessions, 1)), axis=1)[:, :k]
    b = rng.permuted(np.tile(np.arange(products), (sessions, 1)), axis=1)[:, :k]
    for lists in (a, b) if pad else ():
        length = np.where(rng.random(sessions) < 0.2, rng.integers(1, k, sessions), k)
        lists[np.arange(k) >= length[:, None]] = -1
    return a, b, rng.random((sessions, k)) < 0.5


def test_team_draft_keeps_each_teams_order_and_never_repeats_an_item():
    a, b, coins = random_lists(500, seed=8)
    out, team = ab.team_draft(a, b, coins)
    for i in range(len(a)):
        row = out[i][out[i] >= 0].tolist()
        assert len(set(row)) == len(row)
        for side, lists in ((1, a), (-1, b)):
            mine = out[i][team[i] == side].tolist()
            ranks = [lists[i].tolist().index(p) for p in mine]
            assert ranks == sorted(ranks)


def test_vectorised_team_draft_equals_the_one_session_version():
    a, b, coins = random_lists(2000, seed=9)
    out, team = ab.team_draft(a, b, coins)
    for i in range(len(a)):
        items, teams = ab.team_draft_one(a[i].tolist(), b[i].tolist(), coins[i].tolist())
        assert out[i].tolist() == items + [-1] * (20 - len(items))
        assert team[i].tolist() == teams + [0] * (20 - len(teams))


def test_identical_lists_give_no_preference_in_expectation():
    a, _, coins = random_lists(20_000, seed=10, pad=False)
    out, team = ab.team_draft(a, a, coins)
    assert (out == a).all()
    target = a[np.arange(len(a)), np.random.default_rng(11).integers(0, 10, len(a))]
    credits = ab.credit(out, team, target)
    assert (credits != 0).all()
    assert abs(credits.mean()) < 3 / np.sqrt(len(a))


def test_a_list_holding_the_target_first_beats_one_that_never_holds_it():
    a, b, coins = random_lists(1000, seed=12)
    target = np.arange(1000) + 100  # in no list yet
    a[:, 0] = target
    out, team = ab.team_draft(a, b, coins)
    assert (ab.credit(out, team, target) == 1).all()


# -- assignment ------------------------------------------------------------------


def test_the_hash_split_is_stable_and_close_to_half():
    salt = "ranker-vs-item2item"
    # Fixed values: a change of hash or encoding would move users between arms.
    assert ab.bucket([512475445, 1], salt).tolist() == pytest.approx(
        [0.12737002305763723, 0.07605689034847207], abs=1e-15
    )
    assert ab.assign(np.arange(1, 13), salt).astype(int).tolist() == [
        1, 1, 0, 1, 0, 1, 1, 0, 1, 1, 0, 0
    ]  # fmt: skip
    assert (ab.assign(np.arange(1, 13), "another salt") != ab.assign(np.arange(1, 13), salt)).any()
    share = ab.assign(np.arange(100_000), salt).mean()
    assert share == pytest.approx(0.5, abs=0.006)


# -- the replay ------------------------------------------------------------------


def synthetic_month(users: int = 400, seed: int = 0) -> pl.DataFrame:
    """October for a small store with users. Products 1..80 in 8 categories; each user has
    a favourite category and one to several sessions, which wander within it and
    sometimes jump. A third of the users arrive only in the test week, so windows A, B
    and C all have sessions and some users have no pre-period. Sessions are numbered by
    their first event, as in the real data."""
    rng = np.random.default_rng(seed)
    month = datetime(2019, 10, 1, tzinfo=UTC)
    starts = []
    for u in range(users):
        first = 23.0 if rng.random() < 1 / 3 else 0.0
        for _ in range(1 + rng.poisson(1.5)):
            starts.append((month + timedelta(hours=float(rng.uniform(first, 30.9) * 24)), u))
    rows = []
    for s, (t, u) in enumerate(sorted(starts)):
        home = u % 8
        p = home * 10 + int(rng.integers(1, 10))
        for _ in range(int(rng.integers(2, 9))):
            if rng.random() < 0.2:
                p = int(rng.integers(1, 81))
            elif rng.random() < 0.7:
                p = home * 10 + int(rng.integers(0, 10)) or 1
            etype = 1 if rng.random() < 0.1 else 0
            rows.append((t, etype, p, p // 10, f"brand{p % 4}" if p % 7 else None, 10.0 + p, u, s))
            t += timedelta(seconds=int(rng.integers(5, 300)))
    return pl.DataFrame(
        rows,
        schema={
            "event_time": pl.Datetime("us", "UTC"),
            "event_type": pl.UInt8,
            "product_id": pl.Int32,
            "category_id": pl.Int64,
            "brand": pl.String,
            "price": pl.Float32,
            "user_id": pl.Int32,
            "session": pl.Int32,
        },
        orient="row",
    ).sort("event_time", "session", maintain_order=True)


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """A working directory laid out as the replay finds the real one: the month at
    data/events-2019-Oct.parquet, a tiny ranker trained on its test week, and the
    published runs of the item-to-item baseline and the ranker on the cached sample."""
    import lightgbm as lgb

    from marketplace_recs import experiment as ex
    from marketplace_recs import ranker, replay
    from marketplace_recs.features import build, product_stats
    from marketplace_recs.spec import FEATURES
    from marketplace_recs.split import EVENTS, WINDOWS, stats_events

    root = tmp_path_factory.mktemp("replay")
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(root)
        EVENTS.parent.mkdir(parents=True)
        synthetic_month().write_parquet(EVENTS)
        history, cs = ex.cases_for("C")
        products = product_stats(stats_events("C"), WINDOWS["C"][0])
        offline = build(history, cs, ex.matrices_for("C"), products).with_columns(
            pl.col(FEATURES).cast(pl.Float32)
        )
        groups = offline.group_by("session", maintain_order=True).len()["len"].to_numpy()
        booster = lgb.train(
            {"objective": "lambdarank", "min_data_in_leaf": 1, "verbose": -1, "seed": 0},
            lgb.Dataset(
                offline.select(FEATURES).to_numpy(), offline["label"].to_numpy(), group=groups
            ),
            num_boost_round=5,
        )
        replay.MODEL.parent.mkdir(parents=True)
        booster.save_model(str(replay.MODEL))
        ex.main(["baselines"])
        tested = ranker.test(lgb.Booster(model_file=str(replay.MODEL)))
        (ex.RUNS / "ranker_C.json").write_text(json.dumps({"test_C": tested}))
    return root


SECTIONS = {
    "window",
    "cases",
    "population",
    "arms",
    "dilution",
    "realised",
    "aa",
    "mde",
    "power",
    "cuped",
    "interleaving",
    "sensitivity",
    "replicates",
    "seeds",
    "seconds",
}


def test_the_replay_end_to_end(store, monkeypatch):
    from marketplace_recs import experiment as ex
    from marketplace_recs import replay
    from marketplace_recs.metrics import evaluate

    monkeypatch.chdir(store)
    assert replay.main(["--replicates", "20"]) == 0
    result = json.loads((ex.RUNS / "abtest_C.json").read_text())
    assert result.keys() == SECTIONS
    lists = pl.read_parquet(replay.REPLAY / "lists-all.parquet")
    target = lists["target"].to_numpy()
    recall = {
        arm: evaluate(lists[arm].to_numpy(), target)["recall@20"]
        for arm in ("control", "treatment")
    }
    assert {arm: result["arms"][arm]["recall@20"] for arm in recall} == recall
    # The panel's hits, summed over users and days, are the hits metrics.evaluate counts.
    assert result["mde"][-1]["control"] == recall["control"]
    week = [row for row in result["power"] if row["q"] == 1.0][-1]
    assert week["true_effect"] == pytest.approx(recall["treatment"] - recall["control"], abs=1e-4)

    population = result["population"]
    assert (
        population["sessions"] == lists.height == sum(d["sessions"] for d in population["by_day"])
    )
    assert (
        population["users"]
        == population["by_day"][-1]["users_so_far"]
        == lists["user_id"].n_unique()
    )
    assert len(result["power"]) == len(replay.SHARES) * len(population["by_day"])
    assert [row["q"] for row in result["sensitivity"]] == list(replay.SHARES)
    assert result["realised"]["after_week"]["users"]["treatment"] > 0
    assert result["cuped"]["covariates"] == list(replay.COVARIATES)

    # A rerun reads the cached lists and writes the same file apart from the seconds.
    assert replay.main(["--replicates", "20"]) == 0
    again = json.loads((ex.RUNS / "abtest_C.json").read_text())
    assert {k: v for k, v in again.items() if k != "seconds"} == {
        k: v for k, v in result.items() if k != "seconds"
    }


def test_the_replay_reproduces_the_published_numbers_on_the_cached_sample(store, monkeypatch):
    from marketplace_recs import experiment as ex
    from marketplace_recs import replay

    monkeypatch.chdir(store)
    n = ex.N_CASES["C"]
    assert replay.main(["--cases", str(n), "--replicates", "10"]) == 0
    result = json.loads((ex.RUNS / f"abtest_C_{n}.json").read_text())
    assert result.keys() == SECTIONS | {"sample_check"}
    check = result["sample_check"]
    published = json.loads((ex.RUNS / "baselines_C.json").read_text())["item2item"]["recall@20"]
    assert check["item2item"] == {"replay": published, "published": published}
    assert check["ranker"]["replay"] == check["ranker"]["published"]
    assert check["match"]


def test_covariates_count_only_what_happened_before_the_test_week():
    from marketplace_recs import replay

    t = datetime(2019, 10, 23, 23, 50, tzinfo=UTC)
    minutes = [0, 5, 15, 20]
    # User 1's session starts in window B and runs past midnight into the test week;
    # user 2 arrives in the test week.
    rows = [
        (t + timedelta(minutes=m), 0, p, 1, 7) for m, p in zip(minutes, [1, 2, 3, 4], strict=True)
    ]
    rows += [
        (t + timedelta(days=2, minutes=m), 0, p, 2, 8)
        for m, p in zip(minutes, [1, 2, 1, 3], strict=True)
    ]
    lf = pl.DataFrame(
        rows,
        schema={
            "event_time": pl.Datetime("us", "UTC"),
            "event_type": pl.UInt8,
            "product_id": pl.Int32,
            "user_id": pl.Int32,
            "session": pl.Int32,
        },
        orient="row",
    ).lazy()
    schema = {"product_id": pl.Int32, "neighbour": pl.Int32, "weight": pl.Float32}
    matrices = {
        kind: pl.DataFrame({"product_id": [1], "neighbour": [2], "weight": [1.0]}, schema=schema)
        for kind in ("time", "type", "buy2buy")
    }
    pop = pl.DataFrame(
        {"product_id": [9, 8], "pop": [5, 4]}, schema={"product_id": pl.Int32, "pop": pl.UInt32}
    )
    covs = replay.covariates(lf, matrices, pop)
    # Before midnight the session touched 1 then 2: one case, cut after 1, a hit.
    assert covs.to_dicts() == [
        {"user_id": 1, "b_hits": 1, "b_cases": 1, "events": 2, "sessions": 1}
    ]
    x = replay.covariate_matrix(np.array([2, 1]), covs)
    assert x[0].tolist() == [0.0, 0.0, 0.0, 0.0, 0.0]
    assert x[1].tolist() == pytest.approx([1.0, 1.0, np.log(3), np.log(2), 1.0])
