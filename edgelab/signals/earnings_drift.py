from ..llm.client import ContaminationError
from .base import Pick, Signal


class EarningsDrift(Signal):
    """LLM scores the real surprise in an uncovered company's results; ride the slow post-earnings drift."""
    name = "earnings_drift"

    def generate(self, ctx):
        picks = []
        for e in ctx.events.on("earnings", ctx.asof):
            if e.ticker not in ctx.prices.close.columns:
                continue
            try:
                s = ctx.llm.score_earnings(e)
            except ContaminationError:
                ctx.notes["contamination_refusals"] = ctx.notes.get("contamination_refusals", 0) + 1
                continue
            score = s["surprise"] - 0.3 * s["evasion"]          # evasion is a penalty
            if abs(score) < self.p.get("min_abs_score", 0.4):
                continue
            d = 1 if score > 0 else -1
            picks.append(Pick(self.name, e.ticker, d, min(1.0, abs(score)),
                              self.p.get("horizon", 20), f"LLM surprise {s['surprise']:+.2f}, evasion {s['evasion']:.2f}"))
        return picks
