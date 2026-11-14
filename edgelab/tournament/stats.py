"""Statistics for judging signals.

Three protections against fooling ourselves:
  1. clustered()            trades entered in the same week move together, so they are not independent
                            evidence. Standard errors are clustered by entry week.
  2. checkpoints + spend()  looking at results every day and promoting on the first good-looking day inflates
                            false positives badly. A signal is only judged when its trade count reaches
                            need, 2x need, 4x need, ... and each later look must clear a stricter bar
                            (alpha spending), so the overall error rate stays controlled however long it runs.
  3. benjamini_hochberg()   false-discovery control across every signal ever registered.
"""
import numpy as np
from scipy import stats as st


def summarize(x) -> dict:
    """Plain per-trade summary (used for the short 'recent' window that drives retirement)."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 2:
        return dict(n=n, mean=float(x.mean()) if n else 0.0, std=0.0, t=0.0, p=1.0, hit=0.0)
    m, s = x.mean(), x.std(ddof=1)
    t = m / (s / np.sqrt(n)) if s > 0 else 0.0
    p = float(st.t.sf(t, n - 1))                 # one-sided: is mean excess return > 0?
    return dict(n=n, mean=float(m), std=float(s), t=float(t), p=p, hit=float((x > 0).mean()))


def clustered(x, groups) -> dict:
    """Mean excess return with a one-sided test whose standard error is clustered by `groups`.

    With one trade per group this is the ordinary t-test. With many correlated trades per group the
    t-stat shrinks towards what the number of independent groups can actually support.
    """
    x, g = np.asarray(x, dtype=float), np.asarray(groups)
    ok = np.isfinite(x)
    x, g = x[ok], g[ok]
    n = len(x)
    out = dict(n=n, mean=float(x.mean()) if n else 0.0, std=float(x.std(ddof=1)) if n > 1 else 0.0,
               hit=float((x > 0).mean()) if n else 0.0, t=0.0, p=1.0, clusters=0)
    if n < 2:
        return out
    _, inv = np.unique(g, return_inverse=True)
    G = int(inv.max()) + 1
    out["clusters"] = G
    if G < 2:
        return out
    sums = np.bincount(inv, weights=x - x.mean())
    var = G / (G - 1) * float((sums ** 2).sum()) / n ** 2
    if var <= 0:
        return out
    t = x.mean() / np.sqrt(var)
    out["t"], out["p"] = float(t), float(st.t.sf(t, G - 1))
    return out


def checkpoint_index(n: int, need: int) -> int:
    """-1 until `need` trades; then 0 at need, 1 at 2x need, 2 at 4x need, ..."""
    return -1 if n < need or need <= 0 else int(np.floor(np.log2(n / need)))


def spend(p: float, k: int) -> float:
    """Alpha spending: the k-th look may use alpha / 2^(k+1), so all looks together use at most alpha."""
    return float(min(1.0, p * 2 ** (k + 1)))


def benjamini_hochberg(pvals) -> np.ndarray:
    """q-values controlling the false discovery rate across ALL signals ever tested."""
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(n) + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.minimum(q, 1.0)
    return out
