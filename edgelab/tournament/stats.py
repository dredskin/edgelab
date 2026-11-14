import numpy as np
from scipy import stats as st


def summarize(x) -> dict:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 2:
        return dict(n=n, mean=float(x.mean()) if n else 0.0, std=0.0, t=0.0, p=1.0, hit=0.0)
    m, s = x.mean(), x.std(ddof=1)
    t = m / (s / np.sqrt(n)) if s > 0 else 0.0
    p = float(st.t.sf(t, n - 1))                 # one-sided: is mean excess return > 0?
    return dict(n=n, mean=float(m), std=float(s), t=float(t), p=p, hit=float((x > 0).mean()))


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
