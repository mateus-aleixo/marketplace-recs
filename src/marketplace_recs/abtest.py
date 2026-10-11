"""The statistics of an online experiment, for the offline replay in replay.py.

    assign        a user's arm: a salted SHA-256 of their id, the same on every machine
    Sums, ratio   a ratio metric, hits over sessions, with its delta-method variance over
                  users (Deng, Knoblich and Lu, 2018), and the naive binomial variance
                  that treats sessions as independent
    cuped         the difference adjusted with pre-period covariates (Deng, Xu, Kohavi
                  and Walker, 2013), on the ratio linearised per user
    mde, power    the analytic minimum detectable effect and power of a two-sided z-test
    team_draft    team-draft interleaving (Radlinski, Kurup and Joachims, 2008) across
                  many sessions at once; team_draft_one does one session, plainly
    Panel         per user and day: sessions, hits and interleaving credits so far
    simulate      bootstrap replicates of the A/B test, its A/A, CUPED and interleaving

Every estimate is built from weighted sums over users, so a block of replicates is one
matrix product of their weights with a column per sum.

A replicate resamples users with the Poisson bootstrap and assigns them afresh, 50/50.
Each resampled copy of a user is assigned on its own: in a resampled population the
copies stand for different people, and putting all of a user's copies in one arm would
double the variance of the difference between the arms. A Poisson(1) weight whose
copies each go to the treatment with probability 1/2 splits into two independent
Poisson(1/2) weights, one per arm, and that is how they are drawn.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

K = 20
ALPHA = 0.05
POWER = 0.8
_NORMAL = NormalDist()
_cdf = np.vectorize(_NORMAL.cdf, otypes=[float])


# -- assignment ------------------------------------------------------------------


def bucket(user_ids, salt: str) -> np.ndarray:
    """A number in [0, 1) per user: the first 53 bits of SHA-256 over "salt:user_id".

    Python's hash() is salted per process for strings, so it could move a user between
    arms from one run to the next; a cryptographic hash never does."""
    out = np.empty(len(user_ids), dtype=np.float64)
    for i, u in enumerate(np.asarray(user_ids).tolist()):
        digest = hashlib.sha256(f"{salt}:{u}".encode()).digest()
        out[i] = (int.from_bytes(digest[:8], "big") >> 11) / float(1 << 53)
    return out


def assign(user_ids, salt: str, share: float = 0.5) -> np.ndarray:
    """True for the users on the treatment."""
    return bucket(user_ids, salt) < share


# -- ratio metrics ---------------------------------------------------------------


@dataclass
class Sums:
    """Weighted sums over users for a ratio metric, sum(Y) / sum(N): n users, N sessions,
    Y hits, the squares and products the variance needs and, for CUPED, the covariates x.
    Every field may carry leading dimensions (replicates, treatments); X, XY and XN end
    in the p covariates and XX in (p, p)."""

    n: np.ndarray
    N: np.ndarray
    Y: np.ndarray
    YY: np.ndarray
    YN: np.ndarray
    NN: np.ndarray
    X: np.ndarray | None = None
    XX: np.ndarray | None = None
    XY: np.ndarray | None = None
    XN: np.ndarray | None = None


def columns(n, outcomes: dict, x=None, cross=(), out=None) -> tuple[np.ndarray, dict[str, slice]]:
    """One row per user and one column per sum: users, sessions and their square; for each
    outcome Y, Y squared and Y times sessions; with covariates, x, the upper triangle of
    x x', x times sessions, and x times each outcome named in `cross`. Weights times this
    matrix give every Sums at once. `out`, a column-major matrix with at least as many
    rows, is written into instead of a new one."""
    n = np.asarray(n, dtype=np.float64)
    p = 0 if x is None else x.shape[1]
    layout = [("one", 1), ("N", 1), ("NN", 1)]
    for name in outcomes:
        layout += [(name, 1), (f"{name}.YY", 1), (f"{name}.YN", 1)]
    if p:
        layout += [("X", p), ("XX", p * (p + 1) // 2), ("XN", p)]
        layout += [(f"{name}.XY", p) for name in cross]
    index, start = {}, 0
    for name, width in layout:
        index[name] = slice(start, start + width)
        start += width
    # Column by column into a column-major matrix: each write is one contiguous pass, where
    # gathering and broadcasting blocks of columns took ten times as long on a million
    # users, and the matrix product reads the result as it is.
    out = np.empty((len(n), start), order="F") if out is None else out[: len(n), :start]

    def put(name: str, k: int, v: np.ndarray) -> None:
        out[:, index[name].start + k] = v

    put("one", 0, 1.0)
    put("N", 0, n)
    put("NN", 0, n * n)
    ys = {name: np.asarray(y, dtype=np.float64) for name, y in outcomes.items()}
    for name, y in ys.items():
        put(name, 0, y)
        put(f"{name}.YY", 0, y * y)
        put(f"{name}.YN", 0, y * n)
    if p:
        xs = [np.ascontiguousarray(x[:, k], dtype=np.float64) for k in range(p)]
        for k, (a, b) in enumerate(zip(*np.triu_indices(p), strict=True)):
            put("XX", k, xs[a] * xs[b])
        for k, v in enumerate(xs):
            put("X", k, v)
            put("XN", k, v * n)
            for name in cross:
                put(f"{name}.XY", k, v * ys[name])
    return out, index


def take(S: np.ndarray, index: dict[str, slice], y: str) -> Sums:
    """The Sums of outcome `y` from weighted column sums S (last axis: columns)."""

    def col(name: str) -> np.ndarray:
        return S[..., index[name]][..., 0]

    s = Sums(
        S[..., index["one"]][..., 0], col("N"), col(y), col(f"{y}.YY"), col(f"{y}.YN"), col("NN")
    )
    if f"{y}.XY" in index:
        tri = S[..., index["XX"]]
        p = index["X"].stop - index["X"].start
        i, j = np.triu_indices(p)
        XX = np.empty(tri.shape[:-1] + (p, p))
        XX[..., i, j] = tri
        XX[..., j, i] = tri
        s.X, s.XX, s.XY, s.XN = (
            S[..., index["X"]],
            XX,
            S[..., index[f"{y}.XY"]],
            S[..., index["XN"]],
        )
    return s


def sums(y, n, w=None, x=None) -> Sums:
    """Sums over users of hits y and sessions n, weighted by w (1 when None), with
    covariates x (users, p) if given. w may hold one row of weights per replicate."""
    F, index = columns(n, {"y": y}, x, cross=("y",) if x is not None else ())
    return take(F.sum(0) if w is None else np.asarray(w, dtype=np.float64) @ F, index, "y")


def ratio(s: Sums) -> tuple[np.ndarray, np.ndarray]:
    """sum(Y) / sum(N) and its delta-method variance: sum((Y - R N)^2) / sum(N)^2 over
    users, times n / (n - 1) as for a sample variance. Users are the independent units;
    one user's sessions are not."""
    r = s.Y / s.N
    return r, (s.YY - 2 * r * s.YN + r * r * s.NN) / (s.N * s.N) * s.n / (s.n - 1)


def planned_se(s: Sums, share: float = 0.5) -> np.ndarray:
    """The standard error of the difference between two arms that split these users,
    `share` of them on the treatment, both with this group's variance per user: the view
    before a test, when only the control runs."""
    _, var = ratio(s)
    return np.sqrt(var / share + var / (1 - share))


def naive_variance(s: Sums) -> np.ndarray:
    """The binomial variance of the hit rate, as if every session were independent."""
    r = s.Y / s.N
    return r * (1 - r) / s.N


def difference(t: Sums, c: Sums) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Treatment minus control, its delta-method standard error and the naive one."""
    (rt, vt), (rc, vc) = ratio(t), ratio(c)
    return rt - rc, np.sqrt(vt + vc), np.sqrt(naive_variance(t) + naive_variance(c))


# -- CUPED -----------------------------------------------------------------------


def _linearised(s: Sums) -> tuple[np.ndarray, np.ndarray]:
    """sum(psi^2) and sum(x psi) for psi = (Y - R N) / (sessions per user), the ratio
    linearised per user. psi sums to zero over the users it is computed on."""
    r = s.Y / s.N
    nbar = s.N / s.n
    psi2 = (s.YY - 2 * r * s.YN + r * r * s.NN) / (nbar * nbar)
    return psi2, (s.XY - r[..., None] * s.XN) / nbar[..., None]


def _centred(n: np.ndarray, X: np.ndarray, XX: np.ndarray) -> np.ndarray:
    """sum((x - mean)(x - mean)') from sum(1), sum(x) and sum(x x')."""
    m = X / n[..., None]
    return XX - n[..., None, None] * m[..., :, None] * m[..., None, :]


def _ols(sxx: np.ndarray, sxy: np.ndarray) -> np.ndarray:
    # A pseudo-inverse, so a covariate that never varies (no user with a pre-period on
    # the first day, say) gets a coefficient of zero instead of a singular matrix.
    return (np.linalg.pinv(sxx, hermitian=True) @ sxy[..., None])[..., 0]


def _dot(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (a * b).sum(-1)


def cuped(t: Sums, c: Sums) -> tuple[np.ndarray, np.ndarray]:
    """The CUPED difference and its standard error. Each arm's ratio is linearised per
    user, psi = (hits - R_arm sessions) / (sessions per user in the arm); theta is the OLS
    of psi on the covariates, centred and pooled over both arms; the adjusted difference
    is the plain one minus theta times the difference in covariate means; its variance is
    that of psi - theta'x within each arm."""
    (pt, xt), (pc, xc) = _linearised(t), _linearised(c)
    theta = _ols(_centred(t.n + c.n, t.X + c.X, t.XX + c.XX), xt + xc)
    var = 0.0
    for s, psi2, xpsi in ((t, pt, xt), (c, pc, xc)):
        m = s.X / s.n[..., None]
        quad = np.einsum("...i,...ij,...j->...", theta, s.XX, theta)
        ss = psi2 - 2 * _dot(theta, xpsi) + quad - s.n * _dot(theta, m) ** 2
        var = var + ss / (s.n * (s.n - 1))
    shift = _dot(theta, t.X / t.n[..., None] - c.X / c.n[..., None])
    return t.Y / t.N - c.Y / c.N - shift, np.sqrt(var)


def reduction(s: Sums) -> np.ndarray:
    """1 - Var(psi - theta'x) / Var(psi) within one group of users: the share of the
    variance CUPED removes, which is the R squared of psi on the covariates."""
    psi2, xpsi = _linearised(s)
    return _dot(_ols(_centred(s.n, s.X, s.XX), xpsi), xpsi) / psi2


# -- analytic power --------------------------------------------------------------


def z_alpha(alpha: float = ALPHA) -> float:
    return _NORMAL.inv_cdf(1 - alpha / 2)


def mde(se, alpha: float = ALPHA, power: float = POWER):
    """The smallest true effect a two-sided z-test at alpha detects with this power."""
    return (z_alpha(alpha) + _NORMAL.inv_cdf(power)) * np.asarray(se, dtype=float)


def power(effect, se, alpha: float = ALPHA):
    """The probability that a two-sided z-test at alpha rejects at this true effect."""
    d = np.abs(np.asarray(effect, dtype=float)) / np.asarray(se, dtype=float)
    z = z_alpha(alpha)
    return _cdf(d - z) + _cdf(-d - z)


def users_needed(effect, variance_per_user, alpha: float = ALPHA, power: float = POWER):
    """Users for this power when the estimate's variance is variance_per_user / users."""
    k = z_alpha(alpha) + _NORMAL.inv_cdf(power)
    return k * k * np.asarray(variance_per_user, dtype=float) / np.asarray(effect, dtype=float) ** 2


def p_value(effect, se):
    """Two-sided, from the normal distribution."""
    return 2 * _cdf(-np.abs(np.asarray(effect, dtype=float) / np.asarray(se, dtype=float)))


# -- interleaving ----------------------------------------------------------------


def team_draft_one(a, b, coins, k: int = K) -> tuple[list[int], list[int]]:
    """Team draft for one session: the interleaved list and, per item, the team that
    contributed it, +1 for a and -1 for b. While the list is short, the team with fewer
    picks picks next, coins[position] decides when the teams are level, and the picking
    team adds its highest-ranked item not already in the list. -1 pads a list and is never
    an item; a team with nothing left to add passes its turn."""
    pools = {1: [p for p in a if p != -1], -1: [p for p in b if p != -1]}
    items: list[int] = []
    teams: list[int] = []
    picks = {1: 0, -1: 0}
    while len(items) < k:
        left = {t: [p for p in pools[t] if p not in items] for t in (1, -1)}
        if not left[1] and not left[-1]:
            break
        if picks[1] != picks[-1]:
            team = 1 if picks[1] < picks[-1] else -1
        else:
            team = 1 if coins[len(items)] else -1
        if not left[team]:
            team = -team
        items.append(left[team][0])
        teams.append(team)
        picks[team] += 1
    return items, teams


def team_draft(a: np.ndarray, b: np.ndarray, coins: np.ndarray, k: int = K):
    """team_draft_one for many sessions at once. a and b are (sessions, k) lists padded
    with -1, coins (sessions, k) booleans. Returns the (sessions, k) interleaved lists,
    padded with -1, and the team of each position: +1 for a, -1 for b, 0 for padding.

    Each step adds one position to every session: the picking team takes its first item
    not yet marked as taken, and the item is marked in both lists."""
    n = a.shape[0]
    rows = np.arange(n)
    taken_a, taken_b = a == -1, b == -1
    picks_a, picks_b = np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)
    out = np.full((n, k), -1, dtype=a.dtype)
    team = np.zeros((n, k), dtype=np.int8)
    for pos in range(k):
        left_a, left_b = ~taken_a.all(1), ~taken_b.all(1)
        wants_a = (picks_a < picks_b) | ((picks_a == picks_b) & coins[:, pos])
        pick_a = left_a & (wants_a | ~left_b)
        pick_b = left_b & ~pick_a
        item = np.where(
            pick_a,
            a[rows, np.argmax(~taken_a, axis=1)],
            np.where(pick_b, b[rows, np.argmax(~taken_b, axis=1)], -1),
        )
        out[:, pos] = item
        team[:, pos] = pick_a.astype(np.int8) - pick_b.astype(np.int8)
        picks_a += pick_a
        picks_b += pick_b
        taken_a |= a == item[:, None]
        taken_b |= b == item[:, None]
    return out, team


def credit(items: np.ndarray, team: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Per session, the team whose item was the target: +1, -1, or 0 for no click."""
    return (team * (items == target[:, None])).sum(1).astype(np.int8)


# -- the bootstrap -----------------------------------------------------------------


@dataclass
class Panel:
    """Per user and day, cumulated over the days so far: sessions, hits on the control
    list, hits on each treatment's list, and interleaving credits of each treatment and
    of the control against the control. Per user, the covariates. Users are sorted by the
    day they first appear, so the users active by day d are the first active[d], and
    order[i] is row i's index in the arrays the panel was built from."""

    sessions: np.ndarray  # (users, days)
    control: np.ndarray  # (users, days)
    treatment: np.ndarray  # (treatments, users, days)
    credit: np.ndarray  # (treatments, users, days)
    credit_aa: np.ndarray  # (users, days)
    x: np.ndarray  # (users, covariates)
    active: np.ndarray  # (days,)
    order: np.ndarray  # (users,)

    @classmethod
    def from_sessions(cls, user, day, days, control, treatment, credit, credit_aa, x) -> Panel:
        """user and day per session (user indexes the rows of x); control and credit_aa
        per session, treatment and credit (treatments, sessions)."""
        user, day = np.asarray(user), np.asarray(day)
        users = x.shape[0]
        first = np.full(users, days, dtype=np.int64)
        np.minimum.at(first, user, day)
        order = np.argsort(first, kind="stable")
        row = np.empty(users, dtype=np.int64)
        row[order] = np.arange(users)
        cell = row[user] * days + day

        def cumulate(v) -> np.ndarray:
            per_day = np.bincount(cell, weights=v, minlength=users * days)
            return per_day.reshape(users, days).cumsum(1).round().astype(np.int32)

        return cls(
            sessions=cumulate(np.ones(len(user))),
            control=cumulate(control),
            treatment=np.stack([cumulate(t) for t in treatment]),
            credit=np.stack([cumulate(c) for c in credit]),
            credit_aa=cumulate(credit_aa),
            x=np.asarray(x, dtype=np.float64)[order],
            active=np.searchsorted(first[order], np.arange(days), side="right"),
            order=order,
        )

    def columns(self, d: int, out=None) -> tuple[np.ndarray, dict[str, slice]]:
        """The columns every experiment on day d sums: control "c", treatments "t0", "t1",
        ..., their credits "i0", "i1", ..., and the control against itself, "a"."""
        u = self.active[d]
        outcomes = {"c": self.control[:u, d]}
        q = self.treatment.shape[0]
        outcomes.update({f"t{j}": self.treatment[j, :u, d] for j in range(q)})
        outcomes.update({f"i{j}": self.credit[j, :u, d] for j in range(q)})
        outcomes["a"] = self.credit_aa[:u, d]
        cross = ["c", *(f"t{j}" for j in range(q))]
        return columns(self.sessions[:u, d], outcomes, self.x[:u], cross, out)


def estimates(T: np.ndarray, C: np.ndarray, index: dict[str, slice], q: int) -> dict:
    """Every experiment's estimate and standard error, from the column sums of the
    treatment's weights T and the control's weights C (rows: replicates).

    ab, cuped    each treatment against the control, by arm
    il           each treatment's interleaving credit per session, both arms together
    aa_*         the same with both arms on the control; for interleaving, the credits
                 of the copies in T keep their sign and those in C flip it, since swapping
                 the teams of a list interleaved with itself is as likely as not"""
    out: dict[str, list] = {
        k: [] for k in ("ab", "ab_se", "ab_se_naive", "cuped", "cuped_se", "il", "il_se")
    }
    control = take(C, index, "c")
    W = T + C
    for j in range(q):
        treated = take(T, index, f"t{j}")
        effect, se, se_naive = difference(treated, control)
        adjusted, adjusted_se = cuped(treated, control)
        pref, pref_var = ratio(take(W, index, f"i{j}"))
        for key, v in zip(
            out, (effect, se, se_naive, adjusted, adjusted_se, pref, np.sqrt(pref_var)), strict=True
        ):
            out[key].append(v)
    res = {k: np.stack(v, axis=-1) for k, v in out.items()}
    effect, se, se_naive = difference(take(T, index, "c"), control)
    adjusted, adjusted_se = cuped(take(T, index, "c"), control)
    swapped = take(W, index, "a")
    swapped.Y = (T - C)[..., index["a"]][..., 0]
    swapped.YN = (T - C)[..., index["a.YN"]][..., 0]
    pref, pref_var = ratio(swapped)
    res.update(
        aa=effect,
        aa_se=se,
        aa_se_naive=se_naive,
        aa_cuped=adjusted,
        aa_cuped_se=adjusted_se,
        aa_il=pref,
        aa_il_se=np.sqrt(pref_var),
    )
    return res


def replicate_weights(users: int, seed: int, r: int) -> np.ndarray:
    """Replicate r's weights in each arm: independent Poisson(1/2) counts, drawn from the
    r-th child of numpy's SeedSequence(seed), so a replicate never depends on how many
    others run or how they are blocked."""
    rng = np.random.default_rng(np.random.SeedSequence(seed, spawn_key=(r,)))
    return rng.poisson(0.5, (2, users))


def simulate(panel: Panel, replicates: int, seed: int, block: int = 100) -> dict:
    """Every estimate of every experiment on every day, per bootstrap replicate: arrays
    of (replicates, treatments, days), or (replicates, days) for the A/A tests."""
    q, users, days = panel.treatment.shape
    out: dict[str, np.ndarray] = {}
    # The weights and the columns are written into the same memory for every block and
    # day: a fresh array of a gigabyte costs more to fault in than to fill.
    w = np.empty((2 * block, users))
    buffer, _ = panel.columns(days - 1)
    for lo in range(0, replicates, block):
        b = min(block, replicates - lo)
        for i in range(b):
            w[[i, block + i]] = replicate_weights(users, seed, lo + i)
        for d in range(days):
            F, index = panel.columns(d, buffer)
            S = w[:, : panel.active[d]] @ F
            for key, v in estimates(S[:b], S[block : block + b], index, q).items():
                if key not in out:
                    out[key] = np.empty((replicates, *v.shape[1:], days))
                out[key][lo : lo + b, ..., d] = v
    return out
