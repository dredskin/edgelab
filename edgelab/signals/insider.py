from .base import Pick, Signal


class InsiderCluster(Signal):
    """Buy when several insiders buy at once, or an insider buys for the first time in years."""
    name = "insider_cluster"

    def generate(self, ctx):
        picks = []
        for e in ctx.events.on("insider_buy", ctx.asof):
            c, first = e.payload.get("cluster", 1), e.payload.get("first_time", False)
            if c < self.p.get("min_cluster", 2) and not first:
                continue
            if e.ticker not in ctx.prices.close.columns or not self.in_scope(e.ticker):
                continue
            strength = min(1.0, 0.3 * c + (0.4 if first else 0))
            why = f"{c} insider(s) bought" + (", first purchase in years" if first else "")
            picks.append(Pick(self.name, e.ticker, +1, strength, self.p.get("horizon", 20), why))
        return picks
