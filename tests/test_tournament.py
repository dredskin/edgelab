"""The tournament's promises: honest statistics, honest prices, the right benchmark, and safe re-runs."""
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

from edgelab.data.prices import PricePanel
from edgelab.signals.base import Pick
from edgelab.tournament.engine import Tournament
from edgelab.tournament.stats import checkpoint_index, clustered, spend, summarize

CFG = {"tournament": {"min_trades": 40, "lab_min_trades": 80, "fdr_q": 0.10, "retire_window": 30, "retire_t": -1.0,
                      "min_weeks": 8}, "costs_bps_per_side": 30, "universe": {"benchmark": "IWM"}}


# ---------------- statistics ----------------
def test_clustering_shrinks_evidence_from_correlated_trades():
    rng = np.random.default_rng(1)
    weeks = np.repeat(np.arange(20), 10)
    shared = np.repeat(rng.normal(0.01, 0.03, 20), 10)          # 10 trades a week that move together
    x = shared + rng.normal(0, 0.005, 200)
    assert abs(clustered(x, weeks)["t"]) < abs(summarize(x)["t"]) / 2
    solo = rng.normal(0.01, 0.03, 50)                           # one trade per week: the ordinary t-test
    assert clustered(solo, np.arange(50))["t"] == pytest.approx(summarize(solo)["t"])
    assert clustered(solo, np.zeros(50))["p"] == 1.0            # a single week proves nothing


def test_review_schedule_and_alpha_spending():
    assert [checkpoint_index(n, 40) for n in (0, 39, 40, 79, 80, 159, 160, 320)] == [-1, -1, 0, 0, 1, 1, 2, 3]
    assert spend(0.01, 0) == 0.02 and spend(0.01, 2) == 0.08 and spend(0.4, 3) == 1.0
    assert sum(1 / 2 ** (k + 1) for k in range(60)) <= 1.0      # all reviews together spend at most alpha


def _simulate(seed, mean, n_signals=6, weeks=60, per_week=8):
    """Signals whose trades share a weekly shock. Returns how many get promoted."""
    rng = np.random.default_rng(seed)
    t = Tournament(":memory:", CFG)
    for s in range(n_signals):
        t.register(NS(name=f"s{s}", lab=False))
    start = pd.Timestamp("2027-01-04")
    for w in range(weeks):
        day = start + pd.Timedelta(weeks=w)
        rows = []
        for s in range(n_signals):
            shock = rng.normal(0, 0.02)
            for k in range(per_week):
                d = (day + pd.Timedelta(days=k % 5)).strftime("%Y-%m-%d")
                x = float(mean + shock + rng.normal(0, 0.04))
                rows.append((f"s{s}", f"T{w}_{k}", 1, .5, 10, d, d, d, x, x, "closed"))
        t.db.executemany("INSERT INTO picks(signal,ticker,direction,strength,horizon,asof,entry_date,exit_date,"
                         "ret,excess,state) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        t.evaluate(day + pd.Timedelta(days=4))                  # looked at every single week
    log = t.log()
    return int((log["new"] == "promoted").sum()) if len(log) else 0


def test_signals_with_no_edge_are_rarely_promoted_despite_weekly_looks():
    """The rule this replaced promoted a no-edge signal in most simulated tournaments."""
    false_promotions = [_simulate(seed, mean=0.0) for seed in range(12)]
    assert sum(n > 0 for n in false_promotions) <= 2            # nominal false-discovery limit is 10%


def test_signals_with_a_real_edge_are_promoted():
    assert _simulate(seed=3, mean=0.02) == 6


# ---------------- settlement ----------------
def panel(cols: dict, start="2027-01-04"):
    n = len(next(iter(cols.values())))
    idx = pd.bdate_range(start, periods=n)
    close = pd.DataFrame(cols, index=idx, dtype=float)
    return PricePanel(close.shift(1).fillna(close.iloc[0]), close, close * 0 + 1e6)


def run_trade(t, p, ticker="ABC", horizon=3, direction=1):
    t.record([Pick("sig", ticker, direction, 1.0, horizon)], p.dates[0])
    t.settle(p)
    return t.db.execute("SELECT ret, excess, state, entry_date, exit_date FROM picks").fetchone()


def test_trade_is_entered_next_open_and_scored_net_of_costs_against_benchmark():
    t = Tournament(":memory:", CFG)
    p = panel({"ABC": [10, 10, 11, 12, 12, 12], "IWM": [100, 100, 101, 102, 103, 104]})
    ret, excess, state, entry, exit_ = run_trade(t, p)
    # signal on day 0 -> bought at day 1's open (= day 0 close, 10) -> held 3 sessions -> sold at day 3 close (12)
    assert (state, entry, exit_) == ("closed", "2027-01-05", "2027-01-07")
    assert ret == pytest.approx(0.20 - 0.006)
    assert excess == pytest.approx(0.20 - 0.02 - 0.006)        # benchmark went 100 -> 102


def test_later_price_adjustments_do_not_change_a_trades_return():
    """Free price data is re-adjusted after every dividend and split. Returns must not move with it."""
    t = Tournament(":memory:", CFG)
    cols = {"ABC": [10, 10, 11, 12, 12, 12], "IWM": [100] * 6}
    first = panel({k: v[:2] for k, v in cols.items()})
    t.record([Pick("sig", "ABC", 1, 1.0, 3)], first.dates[0])
    t.settle(first)                                            # filled at 10 with the prices of the day
    halved = panel({"ABC": [v / 2 for v in cols["ABC"]], "IWM": cols["IWM"]})   # 2-for-1 split, history rewritten
    t.settle(halved)
    assert t.db.execute("SELECT ret FROM picks").fetchone()[0] == pytest.approx(0.20 - 0.006)


def test_each_market_is_judged_against_its_own_benchmark_and_costs():
    cfg = {**CFG, "universe": {"benchmark": "IWM", "benchmarks": {".T": "1306.T"}}, "costs_bps_by_market": {".T": 50}}
    t = Tournament(":memory:", cfg)
    flat, up = [100] * 6, [100, 100, 105, 110, 110, 110]
    p = panel({"7203.T": up, "1306.T": up, "IWM": flat})
    _, excess, *_ = run_trade(t, p, ticker="7203.T")
    assert excess == pytest.approx(-0.01)                      # matched Japan exactly; paid 2 x 50bp. Not +10% vs the US.
    assert t.benchmark_tickers() == {"IWM", "1306.T"}


def test_market_without_a_named_benchmark_uses_its_own_average():
    t = Tournament(":memory:", CFG)
    cols = {f"{1000 + k}.T": [100, 100, 105, 110, 110, 110] for k in range(9)}
    cols["IWM"] = [100] * 6
    _, excess, *_ = run_trade(t, panel(cols), ticker="1000.T")
    assert excess == pytest.approx(-0.006)


def test_recording_the_same_day_twice_adds_nothing():
    t = Tournament(":memory:", CFG)
    picks = [Pick("sig", "ABC", 1, 1.0, 3), Pick("sig", "XYZ", -1, 1.0, 3)]
    t.record(picks, "2027-01-04")
    t.record(picks, "2027-01-04")
    assert t.db.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 2
    assert t.open_tickers() == {"ABC", "XYZ"}


def test_pick_without_any_price_data_is_voided_not_left_hanging():
    t = Tournament(":memory:", CFG)
    t.record([Pick("sig", "GHOST", 1, 1.0, 3)], "2027-01-04")
    t.settle(panel({"IWM": [100] * 15}))
    assert t.db.execute("SELECT state FROM picks").fetchone()[0] == "void"


def test_delisted_stock_is_closed_at_its_last_price_not_dropped_from_the_record():
    """Survivorship: if failures vanished from the ledger, every long signal would look better than it is."""
    t = Tournament(":memory:", CFG)
    nan = float("nan")
    crash = [10, 10, 9, 4] + [nan] * 30                        # falls 60%, then stops trading for good
    p = panel({"BUST": crash, "IWM": [100] * 34})
    t.record([Pick("sig", "BUST", 1, 1.0, 20)], p.dates[0])
    for k in range(2, 35):                                     # day by day, as in live running
        t.settle(p.upto(p.dates[k - 1]))
    ret, state, why = t.db.execute("SELECT ret, state, rationale FROM picks").fetchone()
    assert state == "closed" and ret == pytest.approx(-0.60 - 0.006) and "last price seen" in why
    # the data feed may drop the ticker altogether: same outcome
    t2 = Tournament(":memory:", CFG)
    t2.record([Pick("sig", "BUST", 1, 1.0, 20)], p.dates[0])
    for k in range(2, 35):
        day = p.upto(p.dates[k - 1])
        t2.settle(day if k < 6 else PricePanel(day.open[["IWM"]], day.close[["IWM"]], day.volume[["IWM"]]))
    assert t2.db.execute("SELECT ret, state FROM picks").fetchone() == (pytest.approx(-0.60 - 0.006), "closed")


# ---------------- lifecycle ----------------
def test_promoted_signal_is_retired_when_recent_trades_turn_bad():
    t = Tournament(":memory:", CFG)
    t.register(NS(name="sig", lab=False))
    t.db.execute("UPDATE signals SET status='promoted'")
    rng = np.random.default_rng(0)
    rows = []
    for k in range(60):
        d = (pd.Timestamp("2027-01-04") + pd.Timedelta(days=k)).strftime("%Y-%m-%d")
        x = float(rng.normal(0.03 if k < 30 else -0.03, 0.02))  # the edge reverses half-way
        rows.append(("sig", f"T{k}", 1, 1.0, 5, d, d, d, x, x, "closed"))
    t.db.executemany("INSERT INTO picks(signal,ticker,direction,strength,horizon,asof,entry_date,exit_date,"
                     "ret,excess,state) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    t.evaluate("2027-04-01")
    assert t.status()["sig"] == "retired"
    assert t.leaderboard().loc[0, "next_review"] == 80
