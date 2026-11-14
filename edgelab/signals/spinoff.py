from .base import Pick, Signal


class SpinoffOrphan(Signal):
    """Buy a new spin-off after institutions dump it, once the forced selling has pushed it down."""
    name = "spinoff_orphan"

    def generate(self, ctx):
        wait = self.p.get("wait_days", 10)
        dates = ctx.prices.dates
        if len(dates) <= wait:
            return []
        listed_on = dates[-1 - wait]
        picks = []
        for e in ctx.events.on("spinoff_listed", listed_on):
            if e.ticker not in ctx.prices.close.columns:
                continue
            px = ctx.prices.close[e.ticker]
            p0, p1 = px.loc[listed_on], px.iloc[-1]
            if p0 != p0 or p1 != p1:          # NaN
                continue
            drop = p1 / p0 - 1
            if drop <= -self.p.get("min_drop", 0.05):
                picks.append(Pick(self.name, e.ticker, +1, min(1.0, -drop * 5),
                                  self.p.get("horizon", 30), f"spin-off down {drop:.1%} since listing"))
        return picks
