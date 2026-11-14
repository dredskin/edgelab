"""A synthetic small-cap market with known, planted edges -- for testing the machinery, not for profit claims.

Planted truths (so we can check the system finds real edges and rejects fake ones):
  insider_buy     informative clusters / first-time buys drift up ~4% over 20 days (65% of the time)
  earnings        price jumps on the day, then drifts further in the same direction (post-earnings drift);
                  the drift DECAYS and reverses late in the sample -> the tournament should retire it
  spinoff_listed  forced selling for 10 days, then a rebound
  headline        hype headlines (low substance) spike the price, then 60% reverses over 3 days
  stress regime   a crash window with high correlation, volatility and drained liquidity
"""
import numpy as np
import pandas as pd

from .data.events import Event, EventStore
from .data.prices import PricePanel
from .signals.base import Pick, Signal


def build_world(n_tickers=300, n_days=750, seed=42, start="2024-01-02"):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    tickers = [f"T{i:03d}" for i in range(n_tickers)]
    T, N = n_days, n_tickers

    stress = np.zeros(T, bool)
    stress[430:470] = True
    mkt = rng.normal(0.0003, 0.009, T)
    mkt[stress] = rng.normal(-0.009, 0.025, stress.sum())
    beta = rng.uniform(0.6, 1.4, N)
    beta_t = np.where(stress[:, None], beta * 1.6, beta)
    idio = rng.normal(0, 0.022, (T, N))
    R = beta_t * mkt[:, None] + idio
    drift = np.zeros((T, N))
    store = EventStore()

    def add_drift(t0, j, per_day, days):
        t1 = min(T, t0 + days)
        if t0 < T:
            drift[t0:t1, j] += per_day

    # insider buys
    for t in range(T):
        for _ in range(rng.poisson(0.45)):
            j = rng.integers(N)
            cluster = int(rng.choice([1, 1, 1, 2, 2, 3]))
            first = bool(rng.random() < 0.3)
            if (cluster >= 2 or first) and rng.random() < 0.65:
                add_drift(t + 1, j, 0.002, 20)
            store.add(Event("insider_buy", dates[t], tickers[j], dict(cluster=cluster, first_time=first)))

    # quarterly earnings with post-earnings drift that decays then reverses after day 520
    phase = rng.integers(0, 63, N)
    for j in range(N):
        for t in range(phase[j], T, 63):
            s = rng.uniform(-1, 1)
            R[t, j] += 0.03 * s
            regime = 1.0 if t < 520 else -0.6
            add_drift(t + 1, j, 0.0015 * s * regime, 20)
            store.add(Event("earnings", dates[t], tickers[j], dict(text="(synthetic)", _true_surprise=s)))

    # spin-offs: forced selling, then rebound
    for t in range(0, T - 45, 10):
        j = rng.integers(N)
        add_drift(t, j, -0.005, 10)
        add_drift(t + 10, j, 0.003, 30)
        store.add(Event("spinoff_listed", dates[t], tickers[j], dict(parent="(synthetic)")))

    # headlines: hype spikes that partially reverse; substantive news that sticks
    for t in range(T - 4):
        for _ in range(rng.poisson(3)):
            j = rng.integers(N)
            sub = rng.uniform(0, 1)
            gap = rng.choice([-1, 1]) * rng.uniform(0.04, 0.09)
            R[t, j] += gap
            if sub < 0.4:
                add_drift(t + 1, j, -0.6 * gap / 3, 3)
            store.add(Event("headline", dates[t], tickers[j], dict(text="(synthetic)", _true_substance=sub)))

    R = R + drift
    close = pd.DataFrame(20 * np.exp(np.cumsum(np.log1p(np.clip(R, -0.9, None)), axis=0)), dates, tickers)
    opn = close.shift(1).fillna(close.iloc[0])
    base_vol = rng.lognormal(11, 1, N)
    vol_mult = np.where(stress[:, None], 0.55, 1.0) * rng.lognormal(0, 0.3, (T, N))
    volume = pd.DataFrame(base_vol * vol_mult, dates, tickers)
    return PricePanel(opn, close, volume), store, dict(stress=(dates[430], dates[469]), decay_from=dates[520])


class Decoy(Signal):
    """No edge by construction. The tournament should never promote these."""
    lab = False

    def __init__(self, k, seed):
        super().__init__({})
        self.name = f"decoy_{k}"
        self.rng = np.random.default_rng(seed)

    def generate(self, ctx):
        cols = ctx.prices.close.columns
        return [Pick(self.name, cols[self.rng.integers(len(cols))], int(self.rng.choice([-1, 1])), 0.5, 10, "random")
                for _ in range(2)]
