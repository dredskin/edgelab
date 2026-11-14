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
    """Free daily data via yfinance (pip install yfinance). Fine for research, not for production."""
    import yfinance as yf

    df = yf.download(list(tickers), start=start, end=end, auto_adjust=True,
                     progress=False, group_by="column", threads=True)
    if not isinstance(df.columns, pd.MultiIndex):           # single ticker
        df.columns = pd.MultiIndex.from_product([df.columns, [list(tickers)[0]]])
    panel = PricePanel(df["Open"], df["Close"], df["Volume"])
    keep = panel.close.columns[panel.close.notna().sum() > 30]
    return PricePanel(panel.open[keep], panel.close[keep], panel.volume[keep])
