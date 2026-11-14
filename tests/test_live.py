"""Live paper trading runs as a fresh process every day. These tests prove that changes nothing:
state is rebuilt from the database, a day can be re-run, missed days are caught up, and the broker
is never sent the same orders twice."""
import copy
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from edgelab.cli import build_signals
from edgelab.config import load_config
from edgelab.live import run_session
from edgelab.llm.client import MockLLM
from edgelab.runner import Runner
from edgelab.synthetic import build_world

ROOT = Path(__file__).resolve().parents[1]
DAYS = 170


@pytest.fixture(scope="module")
def world():
    cfg = copy.deepcopy(load_config(ROOT / "config.yaml"))
    # A short market cannot satisfy the real promotion bar, and these tests are about bookkeeping, not statistics:
    # loosen it so signals are promoted early and capital is actually deployed.
    cfg["tournament"].update(min_trades=10, lab_min_trades=10, min_weeks=2, fdr_q=0.5)
    panel, events, _ = build_world(n_tickers=60, n_days=DAYS, seed=5)
    return cfg, panel, events, panel.dates[3]


def fresh_runner(cfg, db, cutoff):
    return Runner(cfg, build_signals(cfg, demo=True), MockLLM(cutoff), str(db), eval_every=1)


def one_process(world, db, last):
    cfg, panel, events, cutoff = world
    run = fresh_runner(cfg, db, cutoff)
    for i in range(1, last + 1):
        run.day(i, panel, events)
    run.db.close()


def process_per_day(world, db, days):
    cfg, panel, events, cutoff = world
    caught = 0
    for i in days:
        run = fresh_runner(cfg, db, cutoff)                 # nothing carried over in memory
        today = panel.upto(panel.dates[i])                  # and no prices beyond today
        caught = run.restore(today, i)
        run.day(i, today, events)
        run.db.close()
    return caught


def table(db, sql):
    return sqlite3.connect(db).execute(sql).fetchall()


@pytest.fixture(scope="module")
def both(world, tmp_path_factory):
    d = tmp_path_factory.mktemp("live")
    a, b = d / "one_process.db", d / "per_day.db"
    one_process(world, a, DAYS - 1)
    process_per_day(world, b, range(1, DAYS))
    return a, b


def test_a_fresh_process_each_day_matches_one_long_running_process(both):
    a, b = both
    daily = "SELECT date, equity, fragility, risk_mult, gross, picks, orders FROM daily ORDER BY date"
    rows_a, rows_b = table(a, daily), table(b, daily)
    assert len(rows_a) == len(rows_b) == DAYS - 1
    for ra, rb in zip(rows_a, rows_b):
        assert ra[0] == rb[0] and ra[5:] == rb[5:]
        assert ra[1:5] == pytest.approx(rb[1:5], abs=1e-12)
    orders = "SELECT date, ticker, signal, round(weight, 10) FROM orders ORDER BY date, ticker, signal"
    assert table(a, orders) == table(b, orders)
    assert len(table(a, orders)) > 20                        # the comparison is not vacuous: capital was deployed
    assert rows_a[-1][1] != 1.0
    status = "SELECT name, status, n, checkpoint FROM signals ORDER BY name"
    assert table(a, status) == table(b, status)


def test_rerunning_a_day_changes_nothing(world, both, tmp_path):
    _, b = both
    db = tmp_path / "rerun.db"
    db.write_bytes(Path(b).read_bytes())
    counts = lambda: [table(db, f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("picks", "orders", "book_positions", "daily")]
    last = lambda: table(db, "SELECT * FROM daily ORDER BY date DESC LIMIT 1")[0]
    before, row = counts(), last()
    process_per_day(world, db, [DAYS - 1, DAYS - 1])        # the same session, twice more
    assert counts() == before
    assert last() == pytest.approx(row)


def test_missed_sessions_are_caught_up(world, tmp_path):
    cfg, panel, events, cutoff = world
    db = tmp_path / "gap.db"
    process_per_day(world, db, range(1, 141))
    held = table(db, "SELECT gross FROM daily ORDER BY date DESC LIMIT 1")[0][0]
    assert held > 0                                          # positions are open going into the gap
    assert process_per_day(world, db, [144]) == 3            # computer was off for three sessions
    rows = table(db, "SELECT date, equity, picks FROM daily ORDER BY date")
    assert len(rows) == 144
    gap = rows[140:143]
    assert [r[0] for r in gap] == [d.strftime("%Y-%m-%d") for d in panel.dates[141:144]]
    assert all(r[2] == 0 for r in gap)                       # no picks invented for days nobody ran
    assert len({r[1] for r in rows[139:144]}) > 1            # but open positions were marked to market


def test_live_session_sends_broker_orders_once_and_respects_the_loss_cap(world, both, tmp_path):
    from test_broker import FakeSession
    from edgelab.broker import AlpacaBroker

    cfg, panel, events, cutoff = world
    a, _ = both
    day = table(a, "SELECT date FROM orders WHERE weight > 0 ORDER BY date DESC LIMIT 1")[0][0]
    k = list(panel.dates.strftime("%Y-%m-%d")).index(day)
    db = tmp_path / "broker.db"
    one_process(world, db, k - 1)

    today = panel.upto(panel.dates[k])
    fetched = [e for e in events.all() if e.date == panel.dates[k]]
    bcfg = {**cfg, "broker": {"base_url": "https://paper-api.alpaca.markets"}}
    http = FakeSession()
    go = lambda **kw: run_session(cfg, str(db), today, fetched, MockLLM(cutoff), build_signals(cfg, demo=True),
                                  broker=AlpacaBroker(bcfg, str(db), http), log=lambda *_: None, **kw)
    first = go()
    assert first["sent"] and len(http.sent) == len(first["sent"])
    assert all(side == "buy" for _, _, side in first["sent"])           # shorts are off by default
    again = go()                                                        # e.g. the scheduled job fires twice
    assert again["sent"] == [] and len(http.sent) == len(first["sent"])
    assert [(p.ticker, round(w, 10)) for p, w in again["orders"]] == [(p.ticker, round(w, 10)) for p, w in first["orders"]]

    # the real account is down 17% this month: even --force must not open new positions
    con = sqlite3.connect(db)
    month_before = (panel.dates[k] - pd.offsets.MonthBegin(1) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    con.execute("INSERT INTO broker_equity VALUES(?, 'alpaca', 120000)", (month_before,))
    con.commit()
    con.close()
    assert go(force=True)["sent"] == [] and len(http.sent) == len(first["sent"])


def test_daily_command_runs_end_to_end_and_is_repeatable(tmp_path, monkeypatch, capsys):
    """The real `edgelab daily` command, with only the price download replaced."""
    import yaml
    from edgelab import cli
    from edgelab.data import prices

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    for s in cfg["sources"].values():
        s["enabled"] = False                                   # no network in tests
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    panel, _, _ = build_world(n_tickers=30, n_days=140, seed=2)
    (tmp_path / "universe.txt").write_text("\n".join(panel.close.columns) + "\n# a comment\n")
    asked = {}

    def fake_download(tickers, start, end=None):
        asked["tickers"] = set(tickers)
        return panel.upto(pd.Timestamp(end) - pd.Timedelta(days=1))
    monkeypatch.setattr(prices, "load_yfinance", fake_download)

    args = ["--config", str(tmp_path / "config.yaml"), "daily", "--date"]
    cli.main(args + [panel.dates[-3].strftime("%Y-%m-%d")])
    cli.main(args + [panel.dates[-1].strftime("%Y-%m-%d")])    # two sessions later: one is caught up
    out = capsys.readouterr().out
    assert "Caught up 1 session" in out and "events: none" in out
    assert "IWM" in asked["tickers"] and "1306.T" not in asked["tickers"]   # only benchmarks for markets in play
    db = tmp_path / "edgelab.db"
    assert [r[0] for r in table(db, "SELECT date FROM daily ORDER BY date")] == [
        d.strftime("%Y-%m-%d") for d in panel.dates[-3:]]
    readings = table(db, "SELECT COUNT(*) FROM fragility")[0][0]
    assert readings > 50                                       # the gauge was warmed on history at first run
    cli.main(args + [panel.dates[-1].strftime("%Y-%m-%d")])    # same day again
    assert table(db, "SELECT COUNT(*) FROM daily")[0][0] == 3 and table(db, "SELECT COUNT(*) FROM fragility")[0][0] == readings
