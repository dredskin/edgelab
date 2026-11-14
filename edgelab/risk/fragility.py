"""Fragility thermometer (physicist + geologist panelists).

Markets get fragile before they break: stocks start moving together (correlation up),
volatility rises, and liquidity drains. Each day we measure all three and rank today
against the thermometer's own history. Score 0 = calm, 1 = most fragile ever seen.

The readings are stored in the database, so a fresh process each day ranks against the same history.
Works on a global panel: markets with different holidays leave gaps, which are carried forward.
"""
import zlib

import numpy as np

KEYS = ("corr", "vol", "illiq")
MIN_NAMES = 5


class FragilityThermometer:
    def __init__(self, window=60, calm_below=0.6, floor=0.25, max_names=150):
        self.window, self.calm_below, self.floor = window, calm_below, floor
        self.max_names = max_names
        self.hist = {k: [] for k in KEYS}
        self.score = 0.0
        self.names = 0            # stocks behind the latest reading; below MIN_NAMES the gauge is blind

    def metrics(self, panel):
        """Today's raw readings, or None when there is not enough data to measure."""
        if len(panel.close) < self.window + 1:
            return None
        tail = panel.close.iloc[-(self.window + 1):]
        cover = tail.notna().mean()
        # a stable pseudo-random subset: the same stocks every day, whatever order the data arrives in
        cols = sorted((c for c in tail.columns if cover[c] >= 0.8), key=lambda c: zlib.crc32(str(c).encode()))
        cols = cols[: self.max_names]
        self.names = len(cols)
        if len(cols) < MIN_NAMES:
            return None
        r = tail[cols].ffill().pct_change(fill_method=None).iloc[1:].fillna(0.0)
        c = np.corrcoef(r.values.T)
        off = c[~np.eye(len(cols), dtype=bool)]
        dv = panel.close[cols].iloc[-120:] * panel.volume[cols].iloc[-120:]
        recent, base = dv.iloc[-20:].median().median(), dv.median().median()
        return {"corr": float(np.nanmean(off)) if np.isfinite(off).any() else 0.0,
                "vol": float(r.mean(axis=1).std() * np.sqrt(252)),
                "illiq": float(base / recent) if recent and recent > 0 else 1.0}   # >1: liquidity drained

    def load(self, rows):
        """Restore earlier readings: rows of (corr, vol, illiq), oldest first."""
        for row in rows:
            for k, v in zip(KEYS, row):
                self.hist[k].append(v)

    def push(self, m) -> float:
        """Add a reading and score it against history."""
        if m is None:
            self.score = 0.0
            return self.score
        ranks = []
        for k in KEYS:
            h = self.hist[k]
            h.append(m[k])
            if len(h) >= 20:
                ranks.append((np.array(h) <= m[k]).mean())
        self.score = float(np.mean(ranks)) if ranks else 0.0
        return self.score

    def update(self, panel) -> float:
        return self.push(self.metrics(panel))

    def multiplier(self, score=None) -> float:
        s = self.score if score is None else score
        if s < self.calm_below:
            return 1.0
        frac = (s - self.calm_below) / (1 - self.calm_below)
        return float(max(self.floor, 1 - frac * (1 - self.floor)))
