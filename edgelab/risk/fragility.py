"""Fragility thermometer (physicist + geologist panelists).

Markets get fragile before they break: stocks start moving together (correlation up),
volatility rises, and liquidity drains. Each day we measure all three and rank today
against the thermometer's own history. Score 0 = calm, 1 = most fragile ever seen.
"""
import numpy as np


class FragilityThermometer:
    def __init__(self, window=60, calm_below=0.6, floor=0.25, max_names=150, seed=0):
        self.window, self.calm_below, self.floor = window, calm_below, floor
        self.max_names = max_names
        self.rng = np.random.default_rng(seed)
        self.hist = {"corr": [], "vol": [], "illiq": []}
        self._cols = None
        self.score = 0.0

    def _metrics(self, panel):
        close = panel.close.iloc[-(self.window + 1):]
        if self._cols is None or len(self._cols) == 0:
            good = close.columns[close.notna().all()]
            k = min(self.max_names, len(good))
            self._cols = list(self.rng.choice(good, k, replace=False)) if k else []
        cols = [c for c in self._cols if c in close.columns]
        r = close[cols].pct_change(fill_method=None).iloc[1:].dropna(axis=1)
        if r.shape[1] < 5:
            return None
        c = np.corrcoef(r.values.T)
        n = c.shape[0]
        avg_corr = (c.sum() - n) / (n * (n - 1))
        vol = r.mean(axis=1).std() * np.sqrt(252)
        dv = (panel.close * panel.volume)[cols]
        recent, base = dv.iloc[-20:].median().median(), dv.iloc[-120:].median().median()
        illiq = base / recent if recent and recent > 0 else 1.0     # >1 means liquidity drained
        return {"corr": avg_corr, "vol": vol, "illiq": illiq}

    def update(self, panel) -> float:
        if len(panel.close) < self.window + 1:
            return 0.0
        m = self._metrics(panel)
        if m is None:
            return 0.0
        ranks = []
        for k, v in m.items():
            h = self.hist[k]
            h.append(v)
            if len(h) >= 20:
                ranks.append((np.array(h) <= v).mean())
        self.score = float(np.mean(ranks)) if ranks else 0.0
        return self.score

    def multiplier(self, score=None) -> float:
        s = self.score if score is None else score
        if s < self.calm_below:
            return 1.0
        frac = (s - self.calm_below) / (1 - self.calm_below)
        return float(max(self.floor, 1 - frac * (1 - self.floor)))
