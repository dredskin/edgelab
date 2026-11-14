"""Position sizing and the drawdown budget (mathematician + strategist + rabbi panelists)."""
import pandas as pd


def kelly_weight(mean, std, strength, fraction=0.25, cap=0.05) -> float:
    """Fractional Kelly from a signal's forward (paper) track record, scaled by pick strength."""
    if not std or std <= 0 or mean <= 0:
        return 0.0
    return float(min(cap, fraction * mean / std ** 2 * strength))


class DrawdownBudget:
    """Stops new risk after a bad month; halves size after a deep drawdown. Reserves stay untouched."""

    def __init__(self, monthly_loss_cap=0.04, halve_at=0.15):
        self.cap, self.halve_at = monthly_loss_cap, halve_at
        self.peak = None
        self.month_start = None
        self.month = None

    def multiplier(self, date, equity) -> float:
        d = pd.Timestamp(date)
        if self.month != (d.year, d.month):
            self.month, self.month_start = (d.year, d.month), equity
        self.peak = equity if self.peak is None else max(self.peak, equity)
        mtd = equity / self.month_start - 1
        dd = 1 - equity / self.peak
        if mtd <= -self.cap:
            return 0.0
        if dd >= self.halve_at:
            return 0.5
        return 1.0
