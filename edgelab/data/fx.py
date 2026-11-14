"""Latest FX rates (currency -> base currency) for sizing foreign orders."""
import math


def load_fx(currencies, base: str) -> dict:
    import yfinance as yf

    out = {}
    for ccy in sorted({c.upper() for c in currencies} - {base.upper()}):
        rate = None
        for pair, invert in ((f"{ccy}{base}=X", False), (f"{base}{ccy}=X", True)):
            try:
                px = yf.Ticker(pair).history(period="5d")["Close"].dropna()
                if len(px):
                    v = float(px.iloc[-1])
                    rate = 1 / v if invert else v
                    break
            except Exception:
                continue
        if rate and math.isfinite(rate) and rate > 0:
            out[ccy] = rate
    return out
