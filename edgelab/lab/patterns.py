"""Lab signals from the cluster matrices that need no new paid data.

  gov_contract       buy a small company after a defense contract that is large relative to its trading size
  cascade_rebound    buy a stock after a forced-selling cascade: a sharp, heavy-volume, stock-specific drop
  tax_loss_rebound   buy deep year-to-date losers once a year in mid-December, hold into January

All three are LAB entries: they face the stricter promotion bar (lab_min_trades), and their thresholds are fixed
here before any results exist. Changing a threshold later means a new entry name, not an edit (see CLAUDE.md).
Each only sees ctx.prices (sliced to today) and events dated today.
"""
import numpy as np
import pandas as pd

from ..signals.base import Pick, Signal


def _dollar_volume(prices, tk, window=60) -> float:
    c, v = prices.close[tk].iloc[-window:], prices.volume[tk].iloc[-window:]
    dv = (c * v).dropna()
    return float(dv.mean()) if len(dv) >= window // 2 else float("nan")


class GovContract(Signal):
    """Thesis: a large award is material news for a small contractor but gets little coverage."""
    name = "gov_contract"
    lab = True

    def generate(self, ctx):
        picks = []
        for e in ctx.events.on("gov_contract", ctx.asof):
            tk = e.ticker
            if tk not in ctx.prices.close.columns or not self.in_scope(tk):
                continue
            amount = e.payload.get("amount" if self.p.get("count_modifications", False) else "new_amount", 0.0)
            yearly = _dollar_volume(ctx.prices, tk) * 252          # a year of trading value: a size yardstick
            if not amount or not np.isfinite(yearly) or yearly <= 0:
                continue
            ratio = amount / yearly
            if ratio < self.p.get("min_ratio", 0.02):
                continue
            picks.append(Pick(self.name, tk, +1, min(1.0, ratio / 0.2), self.p.get("horizon", 20),
                              f"${amount / 1e6:,.1f}M defense award(s) = {ratio:.1%} of a year's trading value"))
        return picks


class CascadeRebound(Signal):
    """Thesis: stock-specific forced selling (margin calls, fund redemptions, index exits) overshoots, then
    partly reverses once the sellers are done."""
    name = "cascade_rebound"
    lab = True

    def generate(self, ctx):
        close, vol = ctx.prices.close, ctx.prices.volume
        days, look = self.p.get("days", 3), self.p.get("lookback", 60)
        if len(close) < look + days + 1:
            return []
        rets = close.pct_change(fill_method=None)
        drop = close.iloc[-1] / close.iloc[-1 - days] - 1
        market = drop.median()                                   # the typical stock over the same days
        picks = []
        for tk in close.columns:
            if not self.in_scope(tk):
                continue
            d = drop.get(tk)
            sigma = rets[tk].iloc[-1 - days - look:-1 - days].std()
            if not np.isfinite(d) or not np.isfinite(sigma) or sigma <= 0:
                continue
            z = d / (sigma * np.sqrt(days))
            if d > -self.p.get("min_drop", 0.15) or z > -self.p.get("min_sigma", 3.0):
                continue
            if d - market > -self.p.get("min_drop", 0.15) * 0.75:
                continue                                         # the whole market fell: not stock-specific
            base_vol = vol[tk].iloc[-1 - days - look:-1 - days].median()
            heavy = vol[tk].iloc[-days:].mean()
            if not np.isfinite(base_vol) or base_vol <= 0 or heavy < self.p.get("min_volume_x", 2.0) * base_vol:
                continue
            picks.append(Pick(self.name, tk, +1, min(1.0, -z / 8), self.p.get("horizon", 5),
                              f"{d:+.1%} in {days} days ({z:+.1f} sigma) on {heavy / base_vol:.1f}x normal volume"))
        return picks


class TaxLossRebound(Signal):
    """Thesis: investors sell year-to-date losers in December to book tax losses; the pressure lifts in January.
    Fires once a year, on the first session on or after `start` (MM-DD, default 12-15)."""
    name = "tax_loss_rebound"
    lab = True

    def generate(self, ctx):
        close = ctx.prices.close
        if len(close) < 60:
            return []
        today, prev = close.index[-1], close.index[-2]
        mm, dd = (int(x) for x in self.p.get("start", "12-15").split("-"))
        start = pd.Timestamp(today.year, mm, dd)
        if not (prev < start <= today):
            return []                                            # only on the first session of the window
        before = close[close.index < pd.Timestamp(today.year, 1, 1)]
        if before.empty:
            return []
        year_open = before.iloc[-1]                              # last close of the previous year
        ytd = close.iloc[-1] / year_open - 1
        low = close.iloc[-252:].min()
        picks = []
        for tk, r in ytd.items():
            if not self.in_scope(tk) or not np.isfinite(r) or r > -self.p.get("min_loss", 0.30):
                continue
            if close[tk].iloc[-1] > low[tk] * (1 + self.p.get("near_low", 0.15)):
                continue                                         # already bounced well off the low
            picks.append(Pick(self.name, tk, +1, min(1.0, -r / 0.6), self.p.get("horizon", 20),
                              f"{r:+.0%} year to date, near its 52-week low going into tax-loss season"))
        return picks
