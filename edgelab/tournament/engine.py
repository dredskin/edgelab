"""The tournament: every signal is paper-traded forward; only statistically surviving signals get capital.

Lifecycle:  candidate --(passes a scheduled review)--> promoted --(recent trades decay)--> retired

A review happens when a signal's settled-trade count reaches need, 2x need, 4x need, ... (need = min_trades,
or lab_min_trades for lab signals). To be promoted a signal must, at a review:
  - have a positive mean excess return (net of costs, vs its market's benchmark),
  - have trades spread over at least min_weeks different entry weeks,
  - clear the false-discovery limit (Benjamini-Hochberg across ALL signals ever registered) using a p-value
    that is clustered by entry week and penalised for the number of reviews so far (see stats.py).

Every pick is recorded BEFORE its outcome is known, entered at the next open, and settled after its horizon.
Returns are always computed from the price panel in hand, so later dividend/split adjustments cannot distort them.
"""
from datetime import date as _date

import numpy as np
import pandas as pd

from .. import markets, store
from .stats import benjamini_hochberg, checkpoint_index, clustered, spend, summarize


def _week(d: str) -> int:
    return (_date.fromisoformat(d).toordinal() - 1) // 7        # Monday-aligned week number


class Tournament:
    MIN_PEERS = 8      # stocks needed before a market's own average can serve as its benchmark
    ABANDON_DAYS = 21  # calendar days without a price before an open trade is closed at the last price seen

    def __init__(self, db_path: str, cfg: dict):
        self.db = store.connect(db_path)
        self.t = cfg["tournament"]
        self.min_weeks = self.t.get("min_weeks", 8)
        self.cost = cfg.get("costs_bps_per_side", 30) / 1e4
        self.cost_by_market = {k.upper(): v / 1e4 for k, v in (cfg.get("costs_bps_by_market") or {}).items()}
        u = cfg.get("universe", {})
        self.bench = u.get("benchmark")
        self.bench_by_market = {k.upper(): v for k, v in (u.get("benchmarks") or {}).items()}

    # ---------- registration ----------
    def register(self, signal):
        self.db.execute("INSERT OR IGNORE INTO signals(name, lab, status) VALUES(?,?,?)",
                        (signal.name, int(signal.lab), "candidate"))
        self.db.commit()

    def status(self) -> dict:
        return dict(self.db.execute("SELECT name, status FROM signals").fetchall())

    def need(self, lab) -> int:
        return self.t["lab_min_trades"] if lab else self.t["min_trades"]

    def open_tickers(self) -> set:
        """Stocks with unsettled picks: their prices must stay in the panel even if they leave the universe."""
        return {r[0] for r in self.db.execute("SELECT DISTINCT ticker FROM picks WHERE state IN ('pending','open')")}

    def benchmark_tickers(self) -> set:
        return {t for t in [self.bench, *self.bench_by_market.values()] if t}

    # ---------- recording & settling ----------
    def record(self, picks, asof):
        """One pick per signal, stock and day (re-running a day adds nothing)."""
        a = pd.Timestamp(asof).strftime("%Y-%m-%d")
        self.db.executemany(
            "INSERT OR IGNORE INTO picks(signal,ticker,direction,strength,horizon,asof,rationale) VALUES(?,?,?,?,?,?,?)",
            [(p.signal, p.ticker, p.direction, p.strength, p.horizon, a, p.rationale) for p in picks])
        self.db.commit()

    def _bench(self, panel, suffix: str, cache: dict) -> np.ndarray:
        """Benchmark level series for one market: its configured index/ETF, else the average of that market's
        stocks in the panel, else the default benchmark, else the average of everything."""
        if suffix in cache:
            return cache[suffix]
        close = panel.close

        def usable(tk):
            return tk and tk in close.columns and close[tk].notna().sum() > 1

        def average(cols):
            ew = close[cols].pct_change(fill_method=None).mean(axis=1).fillna(0)
            return (1 + ew).cumprod()

        named = self.bench_by_market.get(suffix) or (self.bench if suffix == "" else None)
        peers = [c for c in close.columns if markets.suffix(c) == suffix]
        if usable(named):
            s = close[named].ffill().bfill()
        elif len(peers) >= self.MIN_PEERS:
            s = average(peers)
        elif usable(self.bench):
            s = close[self.bench].ffill().bfill()
        else:
            s = average(list(close.columns))
        cache[suffix] = s.values
        return cache[suffix]

    def settle(self, panel):
        """Fill entries at the next open; close trades whose horizon has elapsed. panel = data up to today."""
        dates = panel.dates
        ds = [d.strftime("%Y-%m-%d") for d in dates]
        idx = {d: i for i, d in enumerate(ds)}
        cols = {c: j for j, c in enumerate(panel.close.columns)}
        O, C = panel.open.values, panel.close.values
        benches = {}

        def entry_price(k, c):
            px = O[k, c]
            return px if np.isfinite(px) and px > 0 else C[k, c]

        stale = (dates[-1] - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
        for pid, tk, asof in self.db.execute(
                "SELECT id, ticker, asof FROM picks WHERE state='pending'").fetchall():
            i = idx.get(asof)
            if i is None or tk not in cols:
                if asof < stale:                      # no price data ever arrived for this stock
                    self.db.execute("UPDATE picks SET state='void' WHERE id=?", (pid,))
                continue
            if i + 1 >= len(dates):
                continue
            # First session after the signal on which this stock actually trades (markets have different
            # holidays, so a global panel has gaps). Give up after 5 sessions without a price.
            k, px = None, float("nan")
            for k in range(i + 1, min(i + 6, len(dates))):
                px = entry_price(k, cols[tk])
                if np.isfinite(px) and px > 0:
                    break
            if not (np.isfinite(px) and px > 0):
                if i + 6 <= len(dates):
                    self.db.execute("UPDATE picks SET state='void' WHERE id=?", (pid,))
                continue
            bench = self._bench(panel, markets.suffix(tk), benches)
            self.db.execute("UPDATE picks SET entry_date=?, entry_price=?, bench_entry=?, state='open' WHERE id=?",
                            (ds[k], float(px), float(bench[k - 1]), pid))

        today = ds[-1]
        gone_before = (dates[-1] - pd.Timedelta(days=self.ABANDON_DAYS)).strftime("%Y-%m-%d")
        for pid, tk, d, h, e_date, e_px, b0, m_ret, m_bench, m_date in self.db.execute(
                "SELECT id, ticker, direction, horizon, entry_date, entry_price, bench_entry, mark_ret, mark_bench, "
                "mark_date FROM picks WHERE state='open'").fetchall():
            suffix = markets.suffix(tk)
            cost = self.cost_by_market.get(suffix, self.cost)
            i, c = idx.get(e_date), cols.get(tk)
            if i is not None and c is not None:
                # Re-read the entry from THIS panel: stored prices may predate a dividend/split adjustment,
                # and an averaged benchmark is rebased whenever the panel window moves.
                bench = self._bench(panel, suffix, benches)
                px0 = entry_price(i, c)
                if np.isfinite(px0) and px0 > 0:
                    e_px, b0 = px0, bench[max(i - 1, 0)]
                j = i + h - 1
                if j < len(dates):
                    x = C[j, c]
                    while not np.isfinite(x) and j + 1 < len(dates) and j < i + h + 4:   # local holiday on exit day
                        j += 1
                        x = C[j, c]
                    if np.isfinite(x):
                        raw = x / e_px - 1
                        braw = bench[j] / b0 - 1 if b0 else 0.0
                        self.db.execute(
                            "UPDATE picks SET exit_date=?, exit_price=?, ret=?, excess=?, state='closed' WHERE id=?",
                            (ds[j], float(x), float(d * raw - 2 * cost), float(d * (raw - braw) - 2 * cost), pid))
                        continue
                last = C[-1, c]
                if np.isfinite(last):                      # still trading: remember where the trade stands
                    self.db.execute("UPDATE picks SET mark_ret=?, mark_bench=?, mark_date=? WHERE id=?",
                                    (float(last / e_px - 1), float(bench[-1] / b0 - 1) if b0 else 0.0, today, pid))
                    continue
            # No price today: halted, delisted, or dropped by the data feed. A trade that silently vanished
            # would flatter the record (bankruptcies disappear), so after ABANDON_DAYS it is closed at the
            # last price seen.
            if (m_date or e_date) >= gone_before:
                continue
            if m_date is None:
                self.db.execute("UPDATE picks SET state='void' WHERE id=?", (pid,))
            else:
                self.db.execute(
                    "UPDATE picks SET exit_date=?, ret=?, excess=?, state='closed', "
                    "rationale=COALESCE(rationale, '') || ? WHERE id=?",
                    (m_date, float(d * m_ret - 2 * cost), float(d * (m_ret - m_bench) - 2 * cost),
                     f" [no prices after {m_date}: closed at the last price seen]", pid))
        self.db.commit()

    # ---------- judging ----------
    def evaluate(self, asof) -> pd.DataFrame:
        rows = []
        for name, lab, status, ck, p_seq in self.db.execute(
                "SELECT name, lab, status, checkpoint, p_seq FROM signals").fetchall():
            tr = self.db.execute("SELECT excess, entry_date FROM picks WHERE signal=? AND state='closed' "
                                 "ORDER BY exit_date, id", (name,)).fetchall()
            ex = [r[0] for r in tr]
            s = clustered(ex, [_week(r[1]) for r in tr])
            w = self.t["retire_window"]
            s["recent_t"] = summarize(ex[-w:])["t"] if len(ex) >= w else 0.0
            need = self.need(lab)
            ck = -1 if ck is None else ck
            p_seq = 1.0 if p_seq is None else p_seq
            k = checkpoint_index(s["n"], need)
            reviewed = k > ck
            if reviewed:                                   # a scheduled review: spend some alpha
                p_seq, ck = min(p_seq, spend(s["p"], k)), k
            rows.append(dict(name=name, lab=lab, status=status, **s, weeks=s["clusters"], checkpoint=ck,
                             p_seq=p_seq, reviewed=reviewed, next_review=need * 2 ** (ck + 1)))
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["q"] = benjamini_hochberg(df["p_seq"].values)
        a = pd.Timestamp(asof).strftime("%Y-%m-%d")
        for _, r in df.iterrows():
            new, why = r.status, ""
            if (r.status == "candidate" and r.checkpoint >= 0 and r.q <= self.t["fdr_q"] and r["mean"] > 0
                    and r.weeks >= self.min_weeks):
                new, why = "promoted", f"n={r.n} over {r.weeks} weeks, mean={r['mean']:.4f}, q={r.q:.3f}"
            elif r.status == "promoted" and r.recent_t < self.t["retire_t"]:
                new, why = "retired", f"recent t={r.recent_t:.2f} (edge decayed)"
            if new != r.status:
                self.db.execute("INSERT INTO status_log VALUES(?,?,?,?,?)", (a, r["name"], r.status, new, why))
                df.loc[df.name == r["name"], "status"] = new
            self.db.execute(
                """UPDATE signals SET status=?, n=?, mean=?, std=?, t=?, p=?, q=?, hit=?, recent_t=?, updated=?,
                   weeks=?, checkpoint=?, p_seq=?, next_review=? WHERE name=?""",
                (new, int(r.n), r["mean"], r["std"], r.t, r.p, float(r.q), r.hit, r.recent_t, a,
                 int(r.weeks), int(r.checkpoint), float(r.p_seq), int(r.next_review), r["name"]))
        self.db.commit()
        return df

    def leaderboard(self) -> pd.DataFrame:
        """Read-only view of the last evaluation."""
        return pd.read_sql("SELECT * FROM signals", self.db)

    def log(self) -> pd.DataFrame:
        return pd.read_sql("SELECT * FROM status_log", self.db)
