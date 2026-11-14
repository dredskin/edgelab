"""The tournament: every signal is paper-traded forward; only statistically surviving signals get capital.

Lifecycle:  candidate --(enough trades, BH q <= fdr_q, positive mean)--> promoted
            promoted  --(recent trades decay: t < retire_t)----------> retired
Lab signals need lab_min_trades instead of min_trades.

Every pick is recorded BEFORE its outcome is known, entered at the next open, and settled
after its horizon, net of trading costs and relative to the benchmark.
"""
import sqlite3

import numpy as np
import pandas as pd

from .stats import benjamini_hochberg, summarize

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks(
  id INTEGER PRIMARY KEY, signal TEXT, ticker TEXT, direction INTEGER, strength REAL,
  horizon INTEGER, asof TEXT, rationale TEXT,
  entry_date TEXT, entry_price REAL, bench_entry REAL,
  exit_date TEXT, exit_price REAL, ret REAL, excess REAL, state TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS signals(
  name TEXT PRIMARY KEY, lab INTEGER, status TEXT, n INTEGER, mean REAL, std REAL,
  t REAL, p REAL, q REAL, hit REAL, recent_t REAL, updated TEXT);
CREATE TABLE IF NOT EXISTS status_log(date TEXT, signal TEXT, old TEXT, new TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS daily(date TEXT PRIMARY KEY, equity REAL, fragility REAL, risk_mult REAL,
  gross REAL, picks INTEGER, orders INTEGER);
CREATE TABLE IF NOT EXISTS orders(date TEXT, ticker TEXT, signal TEXT, weight REAL, rationale TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class Tournament:
    def __init__(self, db_path: str, cfg: dict):
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        self.t = cfg["tournament"]
        self.cost = cfg.get("costs_bps_per_side", 30) / 1e4
        self.bench = cfg.get("universe", {}).get("benchmark")

    # ---------- registration ----------
    def register(self, signal):
        self.db.execute("INSERT OR IGNORE INTO signals(name, lab, status) VALUES(?,?,?)",
                        (signal.name, int(signal.lab), "candidate"))
        self.db.commit()

    def status(self) -> dict:
        return dict(self.db.execute("SELECT name, status FROM signals").fetchall())

    # ---------- recording & settling ----------
    def record(self, picks, asof):
        a = pd.Timestamp(asof).strftime("%Y-%m-%d")
        self.db.executemany(
            "INSERT INTO picks(signal,ticker,direction,strength,horizon,asof,rationale) VALUES(?,?,?,?,?,?,?)",
            [(p.signal, p.ticker, p.direction, p.strength, p.horizon, a, p.rationale) for p in picks])
        self.db.commit()

    def _bench_series(self, panel):
        if self.bench and self.bench in panel.close.columns:
            return panel.close[self.bench]
        ew = panel.close.pct_change(fill_method=None).mean(axis=1).fillna(0)
        return (1 + ew).cumprod()

    def settle(self, panel):
        """Fill entries at next open; close trades whose horizon has elapsed. panel = data up to today."""
        dates = panel.dates
        idx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(dates)}
        bench = self._bench_series(panel).values
        opn, cls = panel.open, panel.close
        cols = {c: j for j, c in enumerate(cls.columns)}
        O, C = opn.values, cls.values

        for pid, tk, asof in self.db.execute(
                "SELECT id, ticker, asof FROM picks WHERE state='pending'").fetchall():
            i = idx.get(asof)
            if i is None or i + 1 >= len(dates) or tk not in cols:
                continue
            px = O[i + 1, cols[tk]]
            if not np.isfinite(px):
                px = C[i + 1, cols[tk]]
            if not np.isfinite(px) or px <= 0:
                self.db.execute("UPDATE picks SET state='void' WHERE id=?", (pid,))
                continue
            self.db.execute("UPDATE picks SET entry_date=?, entry_price=?, bench_entry=?, state='open' WHERE id=?",
                            (dates[i + 1].strftime("%Y-%m-%d"), float(px), float(bench[i]), pid))

        for pid, tk, d, h, e_date, e_px, b0 in self.db.execute(
                "SELECT id, ticker, direction, horizon, entry_date, entry_price, bench_entry FROM picks WHERE state='open'").fetchall():
            i = idx.get(e_date)
            if i is None:
                continue
            j = i + h - 1
            if j >= len(dates):
                continue
            x = C[j, cols[tk]]
            if not np.isfinite(x):
                continue
            raw = x / e_px - 1
            braw = bench[j] / b0 - 1 if b0 else 0.0
            ret = d * raw - 2 * self.cost
            excess = d * (raw - braw) - 2 * self.cost
            self.db.execute("UPDATE picks SET exit_date=?, exit_price=?, ret=?, excess=?, state='closed' WHERE id=?",
                            (dates[j].strftime("%Y-%m-%d"), float(x), float(ret), float(excess), pid))
        self.db.commit()

    # ---------- judging ----------
    def evaluate(self, asof) -> pd.DataFrame:
        names = self.db.execute("SELECT name, lab, status FROM signals").fetchall()
        rows = []
        for name, lab, status in names:
            ex = [r[0] for r in self.db.execute(
                "SELECT excess FROM picks WHERE signal=? AND state='closed' ORDER BY exit_date", (name,))]
            s = summarize(ex)
            s["recent_t"] = summarize(ex[-self.t["retire_window"]:])["t"] if len(ex) >= self.t["retire_window"] else 0.0
            rows.append(dict(name=name, lab=lab, status=status, **s))
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["q"] = benjamini_hochberg(df["p"].values)
        a = pd.Timestamp(asof).strftime("%Y-%m-%d")
        for _, r in df.iterrows():
            need = self.t["lab_min_trades"] if r.lab else self.t["min_trades"]
            new, why = r.status, ""
            if r.status == "candidate" and r.n >= need and r.q <= self.t["fdr_q"] and r["mean"] > 0:
                new, why = "promoted", f"n={r.n}, mean={r['mean']:.4f}, q={r.q:.3f}"
            elif r.status == "promoted" and r.recent_t < self.t["retire_t"]:
                new, why = "retired", f"recent t={r.recent_t:.2f} (edge decayed)"
            if new != r.status:
                self.db.execute("INSERT INTO status_log VALUES(?,?,?,?,?)", (a, r["name"], r.status, new, why))
                df.loc[df.name == r["name"], "status"] = new
            self.db.execute("""UPDATE signals SET status=?, n=?, mean=?, std=?, t=?, p=?, q=?, hit=?, recent_t=?, updated=?
                               WHERE name=?""", (new, int(r.n), r["mean"], r["std"], r.t, r.p, float(r.q), r.hit,
                                                 r.recent_t, a, r["name"]))
        self.db.commit()
        return df

    def log(self) -> pd.DataFrame:
        return pd.read_sql("SELECT * FROM status_log", self.db)
