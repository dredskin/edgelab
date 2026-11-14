"""Paper book: turns promoted signals' picks into sized positions under the risk layer."""
from collections import defaultdict

import numpy as np

from .risk.sizing import kelly_weight


class PaperBook:
    def __init__(self, cfg):
        r = cfg["risk"]
        self.frac, self.cap, self.max_gross = r["kelly_fraction"], r["max_position"], r["max_gross"]
        self.cost = cfg.get("costs_bps_per_side", 30) / 1e4
        self.positions = []            # dicts: ticker, w, start, end, signal
        self.equity = 1.0
        self.curve = []

    def gross(self, i) -> float:
        return sum(abs(p["w"]) for p in self.positions if p["start"] <= i <= p["end"])

    def size(self, picks, sig_stats: dict, i, risk_mult) -> list:
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
            self.positions.append(dict(ticker=p.ticker, w=w, start=i + 1, end=i + p.horizon, signal=p.signal))
            orders.append((p, w))
        return orders

    def step(self, i, date, ret_row: dict):
        """Apply day i's returns to active positions, charge costs on entry/exit days."""
        pnl = 0.0
        for p in self.positions:
            if p["start"] <= i <= p["end"]:
                r = ret_row.get(p["ticker"], 0.0)
                pnl += p["w"] * (r if np.isfinite(r) else 0.0)
                if i == p["start"] or i == p["end"]:
                    pnl -= abs(p["w"]) * self.cost
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
