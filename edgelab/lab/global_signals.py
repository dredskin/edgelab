"""Lab signals fed by the international sources. All start experimental (lab = True)."""
import numpy as np

from ..llm.client import LLMError
from ..signals.base import Pick, Signal

ACTIVIST_WORDS = ("重要提案", "経営への", "株主提案", "activis", "engage", "strategic")


class LargeStake(Signal):
    """Someone discloses a new or increased 5%+ stake (Japan's 大量保有報告書, UK TR-1, etc.)."""
    name = "large_stake"
    lab = True

    def generate(self, ctx):
        picks = []
        for e in ctx.events.on("large_stake", ctx.asof):
            if e.ticker not in ctx.prices.close.columns or not self.in_scope(e.ticker):
                continue
            p = e.payload
            ratio, prev = p.get("ratio"), p.get("prev_ratio")
            if ratio is not None and prev is not None and ratio <= prev:
                continue                                   # stake reduced: not a buy signal
            step = (ratio - (prev or 0)) if ratio is not None else 1.0
            if ratio is not None and step < self.p.get("min_increase", 1.0):
                continue
            activist = any(w in str(p.get("purpose", "")).lower() for w in ACTIVIST_WORDS)
            strength = min(1.0, 0.3 + step / 10 + (0.3 if activist else 0))
            why = f"{p.get('holder', 'holder')} at {ratio}% (was {prev})" if ratio is not None else "new large stake disclosed"
            picks.append(Pick(self.name, e.ticker, +1, strength, self.p.get("horizon", 30),
                              why + (", activist purpose" if activist else "")))
        return picks


class Buyback(Signal):
    """Company decides to repurchase its own shares; the LLM extracts how big the programme is."""
    name = "buyback"
    lab = True

    def generate(self, ctx):
        picks = []
        for e in ctx.events.on("buyback", ctx.asof):
            if e.ticker not in ctx.prices.close.columns or not self.in_scope(e.ticker):
                continue
            try:
                size = ctx.llm.buyback_size(e)["pct_of_shares"]
            except LLMError as e:
                ctx.skipped(e)
                continue
            if size < self.p.get("min_pct", 1.0):
                continue
            picks.append(Pick(self.name, e.ticker, +1, min(1.0, size / 8), self.p.get("horizon", 20),
                              f"buyback of up to {size:.1f}% of shares"))
        return picks


class SupplyChainRead(Signal):
    """Read a supplier's numbers early; trade its customers.

    A supplier's monthly sales accelerating (Taiwan) or its earnings surprising (any market) says something
    about demand at the companies it sells to. The mapping lives in supply_chain.yaml.
    """
    name = "supply_chain"
    lab = True

    def __init__(self, params, chain: dict):
        super().__init__(params)
        self.chain = chain

    def _emit(self, supplier, score, why, ctx, out, seen):
        if abs(score) < 1e-9:
            return
        for cust in self.chain.get(supplier, []):
            if cust in ctx.prices.close.columns and (supplier, cust) not in seen:
                seen.add((supplier, cust))
                out.append(Pick(self.name, cust, int(np.sign(score)), min(1.0, abs(score)),
                                self.p.get("horizon", 20), f"supplier {supplier}: {why}"))

    def generate(self, ctx):
        out, seen = [], set()
        for e in ctx.events.on("monthly_sales", ctx.asof):
            a = e.payload.get("accel")
            if e.ticker in self.chain and a is not None and abs(a) >= self.p.get("min_accel", 0.10):
                self._emit(e.ticker, a / 0.3, f"sales growth {'accelerated' if a > 0 else 'slowed'} by {a:+.0%} vs trend",
                           ctx, out, seen)
        for e in ctx.events.on("earnings", ctx.asof):
            if e.ticker not in self.chain:
                continue
            try:
                s = ctx.llm.score_earnings(e)
            except LLMError as e:
                ctx.skipped(e)
                continue
            if abs(s["surprise"]) >= self.p.get("min_abs_score", 0.5):
                self._emit(e.ticker, s["surprise"], f"earnings surprise {s['surprise']:+.2f}", ctx, out, seen)
        return out
