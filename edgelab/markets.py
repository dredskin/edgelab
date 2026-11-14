"""World markets: one table mapping Yahoo-style ticker suffixes (used for prices and in universe.txt)
to Interactive Brokers contracts, currencies, quote units and lot sizes.

tradable=False marks markets that Interactive Brokers generally does not offer to non-resident accounts
(Taiwan, Korea, India). EdgeLab still reads their filings and news as RESEARCH sources -- e.g. Taiwanese
supplier monthly sales predicting a North American or Japanese customer -- but will not send orders there
unless you set broker.extra_tradable to include them (only if your IB account actually has the permission).

If IB cannot identify a contract, the order is skipped and logged; nothing is guessed.
"""
from functools import lru_cache

MARKETS = {
    # suffix: ib primaryExchange, currency, quote divisor (pence -> pounds), board lot, tradable
    "":     dict(name="United States",          exch="",         ccy="USD"),
    ".TO":  dict(name="Toronto (TSX)",          exch="TSE",      ccy="CAD"),
    ".V":   dict(name="TSX Venture",            exch="VENTURE",  ccy="CAD"),
    ".CN":  dict(name="Canadian Securities Exch", exch="",       ccy="CAD"),
    ".L":   dict(name="London",                 exch="LSE",      ccy="GBP", divisor=100),
    ".DE":  dict(name="Germany (Xetra)",        exch="IBIS",     ccy="EUR"),
    ".PA":  dict(name="Paris (Euronext)",       exch="SBF",      ccy="EUR"),
    ".AS":  dict(name="Amsterdam (Euronext)",   exch="AEB",      ccy="EUR"),
    ".BR":  dict(name="Brussels (Euronext)",    exch="ENEXT.BE", ccy="EUR"),
    ".MI":  dict(name="Milan",                  exch="BVME",     ccy="EUR"),
    ".MC":  dict(name="Madrid",                 exch="BM",       ccy="EUR"),
    ".SW":  dict(name="Switzerland (SIX)",      exch="EBS",      ccy="CHF"),
    ".ST":  dict(name="Stockholm",              exch="SFB",      ccy="SEK"),
    ".OL":  dict(name="Oslo",                   exch="OSE",      ccy="NOK"),
    ".CO":  dict(name="Copenhagen",             exch="CPH",      ccy="DKK"),
    ".T":   dict(name="Tokyo",                  exch="TSEJ",     ccy="JPY", lot=100),
    ".HK":  dict(name="Hong Kong",              exch="SEHK",     ccy="HKD"),          # lots vary by stock
    ".SS":  dict(name="Shanghai via Stock Connect", exch="SEHKNTL", ccy="CNH", lot=100),
    ".SZ":  dict(name="Shenzhen via Stock Connect", exch="SEHKSZSE", ccy="CNH", lot=100),
    ".AX":  dict(name="Australia (ASX)",        exch="ASX",      ccy="AUD"),
    ".SI":  dict(name="Singapore (SGX)",        exch="SGX",      ccy="SGD", lot=100),
    ".TW":  dict(name="Taiwan (TWSE)",          exch="",         ccy="TWD", tradable=False),
    ".TWO": dict(name="Taiwan (TPEx)",          exch="",         ccy="TWD", tradable=False),
    ".KS":  dict(name="Korea (KOSPI)",          exch="",         ccy="KRW", tradable=False),
    ".KQ":  dict(name="Korea (KOSDAQ)",         exch="",         ccy="KRW", tradable=False),
    ".NS":  dict(name="India (NSE)",            exch="",         ccy="INR", tradable=False),
    ".BO":  dict(name="India (BSE)",            exch="",         ccy="INR", tradable=False),
}


_SUFFIXES = sorted((s for s in MARKETS if s), key=len, reverse=True)


@lru_cache(maxsize=None)
def split(ticker: str):
    """'7203.T' -> ('7203', '.T'). Longest matching suffix wins; no match -> US."""
    t = ticker.upper()
    for suf in _SUFFIXES:
        if t.endswith(suf):
            return t[: -len(suf)], suf
    return t, ""


def suffix(ticker: str) -> str:
    return split(ticker)[1]


def market(ticker: str) -> dict:
    base, suf = split(ticker)
    m = dict(MARKETS[suf])
    m.setdefault("divisor", 1)
    m.setdefault("lot", 1)
    m.setdefault("tradable", True)
    m["suffix"], m["base"] = suf, base
    return m


def currency(ticker: str) -> str:
    return market(ticker)["ccy"]


def ib_symbol(ticker: str) -> str:
    m = market(ticker)
    s = m["base"]
    if m["suffix"] == ".HK":
        return s.lstrip("0") or "0"             # 0700.HK -> 700
    if m["suffix"] == "":
        return s.replace("-", " ")              # BRK-B -> BRK B
    if m["suffix"] in (".TO", ".V"):
        return s.replace("-", ".")              # BBD-B.TO -> BBD.B
    return s


def from_ib(symbol: str, ccy: str, primary: str) -> str:
    """Inverse mapping for positions reported by IB."""
    for suf, m in MARKETS.items():
        if suf and m["ccy"] == ccy and m["exch"] and m["exch"] == primary:
            sym = symbol.zfill(4) if suf == ".HK" else symbol.replace(" ", "-").replace(".", "-") if suf in (".TO", ".V") else symbol
            return sym + suf
    if ccy == "CAD":
        return symbol + ".TO"
    return symbol.replace(" ", "-")
