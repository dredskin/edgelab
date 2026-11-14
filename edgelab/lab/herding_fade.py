"""AI-herding fade (LAB / experimental).

Thesis: headline-reading algorithms react to keywords ("AI", "partnership", "breakthrough")
before anyone checks whether the news has substance. When a stock jumps hard on a headline
the LLM judges to be mostly hype, fade the move over the next few days.

Lives in the lab: it is paper-traded by the same tournament but must clear a stricter bar
(lab_min_trades) before it can receive capital.

Upgrade path (see README): intraday bars + timestamped headlines let you fade within minutes
instead of at the next open, which is where most of this edge likely sits.
"""
import numpy as np

from ..llm.client import LLMError
from ..signals.base import Pick, Signal


class HerdingFade(Signal):
    name = "herding_fade"
    lab = True

    def generate(self, ctx):
        close = ctx.prices.close
        if len(close) < 62:
            return []
        rets = close.iloc[-61:].pct_change(fill_method=None).iloc[1:]
        picks, seen = [], set()
        for e in ctx.events.on("headline", ctx.asof):
            tk = e.ticker
            if tk in seen or tk not in close.columns:
                continue
            r_today = rets[tk].iloc[-1]
            sigma = rets[tk].iloc[:-1].std()
            if not np.isfinite(r_today) or not sigma or not np.isfinite(sigma):
                continue
            z = r_today / sigma
            if abs(z) < self.p.get("gap_sigma", 2.0):
                continue
            try:
                s = ctx.llm.score_headline(e)
            except LLMError as e:
                ctx.skipped(e)
                continue
            if s["substance"] > self.p.get("max_substance", 0.35):
                continue                      # real news: don't fade it
            seen.add(tk)
            picks.append(Pick(self.name, tk, -int(np.sign(r_today)), min(1.0, abs(z) / 6),
                              self.p.get("horizon", 3),
                              f"{r_today:+.1%} move ({z:+.1f} sigma) on low-substance headline ({s['substance']:.2f})"))
        return picks
