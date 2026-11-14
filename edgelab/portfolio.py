"""Paper book: turns promoted signals' picks into sized positions under the risk layer.

Positions are kept as (signal date, horizon) in the database and mapped onto whatever price panel is in
hand, so the book survives being rebuilt in a fresh process every day.
"""
from collections import defaultdict

import numpy as np

from .markets import suffix
from .risk.sizing import kelly_weight


class PaperBook:
    def __init__(self, cfg):
        r = cfg["risk"]
        self.frac, self.cap, self.max_gross = r["kelly_fraction"], r["max_position"], r["max_gross"]
        self.cost = cfg.get("costs_bps_per_side", 30) / 1e4
        self.cost_by_market = {k.upper(): v / 1e4 for k, v in (cfg.get("costs_bps_by_market") or {}).items()}
        self.positions = []            # dicts: ticker, signal, asof, horizon, w, start, end (panel indices)
        self.equity = 1.0
        self.curve = []

    def load(self, rows, idx: dict, equity: float, first_index: int = 0):
        """rows: (ticker, signal, asof, w, horizon). idx maps a date string to its index in today's panel.
        A position is held from the session after its signal date for `horizon` sessions."""
        self.equity = equity
        for tk, sig, asof, w, h in rows:
            i0 = idx.get(asof)
            if i0 is None or i0 + h < first_index:
                continue
            self.positions.append(dict(ticker=tk, signal=sig, asof=asof, horizon=h, w=w, start=i0 + 1, end=i0 + h))

    def gross(self, i) -> float:
        return sum(abs(p["w"]) for p in self.positions if p["start"] <= i <= p["end"])

    def size(self, picks, sig_stats: dict, i, asof: str, risk_mult) -> list:
        raw = []
        for p in picks:
            s = sig_stats.get(p.signal)
            if not s:
                continue
            w = kelly_weight(s["mean"], s["std"], p.strength, self.frac, self.cap) * p.direction
            if w:
                raw.append((p, w))
        if not raw:
            return []
        room = max(0.0, self.max_gross * risk_mult - self.gross(i + 1))
        need = sum(abs(w) for _, w in raw)
        scale = min(1.0, room / need) if need else 0.0
        orders = []
        for p, w in raw:
            w *= scale
            if abs(w) < 1e-4:
                continue
            self.positions.append(dict(ticker=p.ticker, signal=p.signal, asof=asof, horizon=p.horizon, w=w,
                                       start=i + 1, end=i + p.horizon))
            orders.append((p, w))
        return orders

    def step(self, i, date, ret_row: dict, entry_ret_row: dict = None):
        """Apply day i's returns to active positions, charge costs on entry/exit days.

        ret_row is close-to-close. On a position's first day it earns entry_ret_row (open-to-close) instead,
        because it is bought at that day's open, not at the previous close the signal was computed on."""
        pnl = 0.0
        for p in self.positions:
            if p["start"] <= i <= p["end"]:
                row = entry_ret_row if (i == p["start"] and entry_ret_row is not None) else ret_row
                r = row.get(p["ticker"], 0.0)
                pnl += p["w"] * (r if r is not None and np.isfinite(r) else 0.0)
                cost = self.cost_by_market.get(suffix(p["ticker"]), self.cost)
                pnl -= abs(p["w"]) * cost * ((i == p["start"]) + (i == p["end"]))
        self.positions = [p for p in self.positions if p["end"] >= i]
        self.equity *= 1 + pnl
        self.curve.append((date, self.equity, self.gross(i)))
        return pnl

    def by_signal_exposure(self, i) -> dict:
        out = defaultdict(float)
        for p in self.positions:
            if p["start"] <= i <= p["end"]:
                out[p["signal"]] += abs(p["w"])
        return dict(out)
