"""The daily loop: settle -> mark book -> measure risk -> generate picks -> judge -> size.

Two ways to drive it, with identical results:
  - in one process over many days (the synthetic demo): call day(i, ...) for each i;
  - a fresh process per day (live paper trading): call restore(panel, i) once, then day(i, ...).
All state that must outlive a process (book positions, equity curve, fragility readings, tournament
ledger) lives in SQLite, and a day can be re-run safely: it replaces its own rows instead of adding to them.
"""
import numpy as np
import pandas as pd

from .portfolio import PaperBook
from .risk.fragility import KEYS, FragilityThermometer
from .risk.sizing import DrawdownBudget
from .signals.base import Context
from .tournament.engine import Tournament


class Runner:
    def __init__(self, cfg, signals, llm, db_path, eval_every=5):
        self.cfg, self.signals, self.llm = cfg, signals, llm
        self.tour = Tournament(db_path, cfg)
        self.db = self.tour.db
        for s in signals:
            self.tour.register(s)
        f = cfg["risk"]["fragility"]
        self.thermo = FragilityThermometer(f["window"], f["calm_below"], f["floor"])
        self.warm_sessions = f.get("warm_sessions", 250)
        self.dd = DrawdownBudget(cfg["risk"]["monthly_loss_cap"], cfg["risk"]["drawdown_halve_at"])
        self.book = PaperBook(cfg)
        self.eval_every = eval_every
        self.sig_stats = {}
        self.notes = {}
        self.daily = []          # per-day diagnostics for the dashboard

    # ---------- helpers ----------
    @staticmethod
    def _returns(panel_full, i):
        """(close-to-close, open-to-close) returns for session i; holiday gaps are carried forward."""
        lo = max(0, i - 5)
        close = panel_full.close.iloc[lo:i + 1].ffill()
        if len(close) < 2:
            return {}, {}
        c2c = (close.iloc[-1] / close.iloc[-2] - 1).to_dict()
        o2c = (panel_full.close.iloc[i] / panel_full.open.iloc[i] - 1).to_dict()
        return c2c, o2c

    def _save_fragility(self, date_str, m):
        if m is not None:
            self.db.execute("INSERT OR REPLACE INTO fragility VALUES(?,?,?,?)", (date_str, *[m[k] for k in KEYS]))

    def _save_daily(self, row):
        self.db.execute("INSERT OR REPLACE INTO daily VALUES(:date,:equity,:fragility,:risk_mult,:gross,:picks,:orders)", row)

    def _promoted_stats(self):
        return {name: dict(mean=mean, std=std) for name, mean, std in self.db.execute(
            "SELECT name, mean, std FROM signals WHERE status='promoted'")}

    # ---------- live: rebuild yesterday's state, catch up missed sessions ----------
    def restore(self, panel_full, i_today):
        dates = panel_full.dates
        ds = [d.strftime("%Y-%m-%d") for d in dates]
        idx = {d: k for k, d in enumerate(ds)}
        today = ds[i_today]

        past = self.db.execute("SELECT date, equity FROM daily WHERE date < ? ORDER BY date", (today,)).fetchall()
        for d, eq in past:                                   # replays peak and month-start equity
            self.dd.multiplier(d, eq)
        last = past[-1][0] if past else None
        first_step = idx[last] + 1 if last in idx else i_today

        # thermometer: stored readings, then any sessions not yet measured (all of history on the first run)
        frag = self.db.execute("SELECT date, corr, vol, illiq FROM fragility WHERE date < ? ORDER BY date",
                               (today,)).fetchall()
        self.thermo.load([r[1:] for r in frag])
        j0 = idx[frag[-1][0]] + 1 if frag and frag[-1][0] in idx else max(1, i_today - self.warm_sessions)
        scores = {}
        for j in range(j0, i_today):
            m = self.thermo.metrics(panel_full.upto(dates[j]))
            scores[j] = self.thermo.push(m)
            self._save_fragility(ds[j], m)

        # book: positions opened on earlier days, then mark it through any sessions missed since the last run
        rows = self.db.execute("SELECT ticker, signal, asof, w, horizon FROM book_positions WHERE asof < ?",
                               (today,)).fetchall()
        self.book.load(rows, idx, past[-1][1] if past else 1.0, first_step)
        for j in range(first_step, i_today):
            self.book.step(j, dates[j], *self._returns(panel_full, j))
            mult = self.thermo.multiplier(scores.get(j, 0.0)) * self.dd.multiplier(dates[j], self.book.equity)
            self._save_daily(dict(date=ds[j], equity=self.book.equity, fragility=scores.get(j, 0.0), risk_mult=mult,
                                  gross=self.book.gross(j + 1), picks=0, orders=0))
        self.sig_stats = self._promoted_stats()
        self.db.commit()
        return i_today - first_step                          # sessions caught up

    # ---------- one session ----------
    def day(self, i, panel_full, events):
        date = panel_full.dates[i]
        today = date.strftime("%Y-%m-%d")
        panel = panel_full.upto(date)
        self.tour.settle(panel)
        self.book.step(i, date, *self._returns(panel_full, i))

        m = self.thermo.metrics(panel)
        frag = self.thermo.push(m)
        self._save_fragility(today, m)
        risk_mult = self.thermo.multiplier() * self.dd.multiplier(date, self.book.equity)

        ctx = Context(asof=date, prices=panel, events=events, llm=self.llm, notes=self.notes)
        all_picks, seen = [], set()
        for s in self.signals:
            for p in s.generate(ctx):
                if (p.signal, p.ticker) not in seen:         # one pick per signal and stock per day
                    seen.add((p.signal, p.ticker))
                    all_picks.append(p)
        self.tour.record(all_picks, date)

        if i % self.eval_every == 0:
            self.tour.evaluate(date)
            self.sig_stats = self._promoted_stats()

        live = [p for p in all_picks if p.signal in self.sig_stats]
        orders = self.book.size(live, self.sig_stats, i, today, risk_mult)
        row = dict(date=today, equity=self.book.equity, fragility=frag, risk_mult=risk_mult,
                   gross=self.book.gross(i + 1), picks=len(all_picks), orders=len(orders))
        self.daily.append(row)
        self._save_daily(row)
        for table, col in (("orders", "date"), ("book_positions", "asof")):   # re-running a day replaces its rows
            self.db.execute(f"DELETE FROM {table} WHERE {col}=?", (today,))
        self.db.executemany("INSERT INTO orders VALUES(?,?,?,?,?)",
                            [(today, p.ticker, p.signal, w, p.rationale) for p, w in orders])
        self.db.executemany("INSERT OR REPLACE INTO book_positions VALUES(?,?,?,?,?)",
                            [(p.ticker, p.signal, today, w, p.horizon) for p, w in orders])
        self.db.commit()
        return orders


def performance(equity: pd.Series) -> dict:
    r = equity.pct_change().dropna()
    if r.empty:
        return {}
    ann = (equity.iloc[-1] / equity.iloc[0]) ** (252 / len(r)) - 1
    vol = r.std() * np.sqrt(252)
    dd = (1 - equity / equity.cummax()).max()
    return dict(total=float(equity.iloc[-1] / equity.iloc[0] - 1), cagr=float(ann), vol=float(vol),
                sharpe=float(ann / vol) if vol else 0.0, max_dd=float(dd))
