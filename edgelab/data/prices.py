from dataclasses import dataclass

import pandas as pd


@dataclass
class PricePanel:
    """Daily OHLCV-style panel: index = dates, columns = tickers."""
    open: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame

    @property
    def dates(self):
        return self.close.index

    def upto(self, asof) -> "PricePanel":
        """Everything known as of the close on `asof` -- the only view signals get."""
        return PricePanel(self.open.loc[:asof], self.close.loc[:asof], self.volume.loc[:asof])

    def returns(self) -> pd.DataFrame:
        return self.close.pct_change(fill_method=None)


def load_yfinance(tickers, start, end=None) -> PricePanel:
    """Free daily data via yfinance (pip install yfinance). Fine for research, not for production.
    Prices are adjusted for splits and dividends, so levels change over time: never compare a stored price
    with a freshly downloaded one (the tournament re-reads both ends of a trade from the same panel)."""
    import yfinance as yf

    tickers = list(tickers)
    empty = pd.DataFrame()
    if not tickers:
        return PricePanel(empty, empty, empty)
    df = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False, group_by="column", threads=True)
    if df is None or df.empty:
        return PricePanel(empty, empty, empty)
    if not isinstance(df.columns, pd.MultiIndex):           # single ticker
        df.columns = pd.MultiIndex.from_product([df.columns, [tickers[0]]])
    df = df[df["Close"].notna().any(axis=1)]                 # drop days on which nothing traded
    keep = df["Close"].columns[df["Close"].notna().sum() > 1]   # newly listed stocks have little history: keep them
    return PricePanel(df["Open"][keep], df["Close"][keep], df["Volume"][keep])
