"""Lab signals added 2026-11-19: defense contract awards, forced-selling cascade rebound, tax-loss rebound.
Fixtures follow the shape of the real daily contract pages ("Contracts For <date>", agency headings, then
"<Company>, <City>, <State>, was awarded a $<amount> ... contract"). No network."""
import numpy as np
import pandas as pd

from edgelab.data.events import Event, EventStore
from edgelab.data.prices import PricePanel
from edgelab.lab.patterns import CascadeRebound, GovContract, TaxLossRebound
from edgelab.signals.base import Context
from edgelab.sources.defense import DefenseContractsSource, match, parse_awards

DAY = pd.Timestamp("2026-11-17")

PAGE = (
    "Contracts For Nov. 17, 2026 ARMY The Boeing Co., Mesa, Arizona, was awarded a $4,690,000,000 firm-fixed-price "
    "contract for new-build AH-64E helicopters. Work will be performed in Mesa, Arizona. Army Contracting Command, "
    "Redstone Arsenal, Alabama, is the contracting activity (W58RGZ-26-C-0002). "
    "Kadant Johnson LLC, Three Rivers, Michigan, was awarded a $31,500,000 firm-fixed-price contract for steam joints. "
    "NAVY Kadant Inc., Westford, Massachusetts, was awarded a $9,000,000 modification (P00012) to a previously "
    "awarded contract for spare parts. "
    "DEFENSE LOGISTICS AGENCY Royal Food Service Co., Atlanta, Georgia, has been awarded a maximum $12,000,000 "
    "contract for fresh fruit and vegetable items. (Awarded Oct. 09, 2026)"
)
TICKERS_JSON = {"0": {"cik_str": 1, "ticker": "BA", "title": "BOEING CO"},
                "1": {"cik_str": 2, "ticker": "KAI", "title": "KADANT INC"},
                "2": {"cik_str": 3, "ticker": "RFS", "title": "ROYAL FINANCIAL SERVICES CORP"}}


def feed(items):
    rows = "".join(f"<item><title>{t}</title><link>{l}</link><guid>{l}</guid><pubDate>{d}</pubDate>"
                   f"<description><![CDATA[{b}]]></description></item>" for t, l, d, b in items)
    return f"<?xml version='1.0'?><rss><channel>{rows}</channel></rss>".encode()


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes

    def _find(self, url):
        for k, v in self.routes.items():
            if k in url:
                return v
        raise RuntimeError(url)

    json = content = lambda self, url, params=None: self._find(url)


def test_parse_awards_reads_amounts_agencies_and_modifications():
    a = parse_awards(PAGE)
    by = {x["awardee"]: x for x in a}
    assert by["The Boeing Co."]["amount"] == 4.69e9 and by["The Boeing Co."]["agency"] == "Army"
    assert by["Kadant Inc."]["modification"] and by["Kadant Inc."]["agency"] == "Navy"
    assert not by["Kadant Johnson LLC"]["modification"]
    assert by["Royal Food Service Co."]["amount"] == 12e6


def test_name_matching_is_conservative():
    names = {t["ticker"]: t["title"] for t in TICKERS_JSON.values()}
    assert match("The Boeing Co.", names) == "BA"
    assert match("Kadant Johnson LLC", names) == "KAI"            # subsidiary led by the issuer's name
    assert match("Royal Food Service Co.", names) == ""           # shares one word with "Royal Financial": no match
    assert match("Acme Widgets Inc.", names) == ""


def test_source_dates_events_by_publication_and_emits_once(tmp_path):
    cfg = {"_root": str(tmp_path), "sec": {"user_agent": "t"}}
    http = FakeHttp({"war.gov": feed([
        ("Contracts For Nov. 17, 2026", "https://www.war.gov/a/1", "Tue, 17 Nov 2026 22:00:00 GMT", PAGE * 2),
        ("Contracts For Nov. 16, 2026", "https://www.war.gov/a/0", "Mon, 16 Nov 2026 22:00:00 GMT", PAGE * 2)]),
        "company_tickers": TICKERS_JSON})
    src = DefenseContractsSource(cfg, {}, str(tmp_path / "d.db"), http=http)
    ev = src.fetch(DAY, {"BA", "KAI", "RFS"})
    by = {e.ticker: e for e in ev}
    assert set(by) == {"BA", "KAI"} and all(e.date == DAY and e.kind == "gov_contract" for e in ev)
    # page doubled in the fixture: 2 x 31.5M new + 2 x 9M modification
    assert by["KAI"].payload["new_amount"] == 63e6 and by["KAI"].payload["amount"] == 81e6
    assert src.fetch(DAY, {"BA", "KAI", "RFS"}) == []             # same page is never emitted twice


def prices(n=120, tickers=("A", "B", "C", "D", "E"), seed=0, start="2026-06-01"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    close = pd.DataFrame(100 * np.cumprod(1 + rng.normal(0, 0.01, (n, len(tickers))), axis=0), idx, list(tickers))
    vol = pd.DataFrame(1e5, idx, list(tickers))
    return close, vol


def ctx(close, vol, events=()):
    store = EventStore()
    store.extend(events)
    return Context(asof=close.index[-1], prices=PricePanel(close, close, vol), events=store, llm=None)


def test_gov_contract_needs_an_award_large_for_the_company():
    close, vol = prices()                                          # ~$100 x 100k shares = $10M a day
    day = close.index[-1]
    big = Event("gov_contract", day, "A", {"amount": 80e6, "new_amount": 80e6})   # 3% of ~$2.5B a year
    small = Event("gov_contract", day, "B", {"amount": 10e6, "new_amount": 10e6})
    mod = Event("gov_contract", day, "C", {"amount": 90e6, "new_amount": 0.0})    # modification only
    picks = GovContract({"scope": "us"}).generate(ctx(close, vol, [big, small, mod]))
    assert [p.ticker for p in picks] == ["A"] and picks[0].direction == +1


def test_cascade_rebound_wants_a_stock_specific_heavy_volume_drop():
    close, vol = prices()
    close.iloc[-3:, 0] = close.iloc[-4, 0] * np.array([0.92, 0.85, 0.78])   # A: -22% in 3 days
    vol.iloc[-3:, 0] = 4e5
    picks = CascadeRebound({"scope": "us"}).generate(ctx(close, vol))
    assert [p.ticker for p in picks] == ["A"]
    quiet = vol.copy()
    quiet.iloc[-3:, 0] = 1e5                                       # same drop on normal volume: not forced selling
    assert CascadeRebound({"scope": "us"}).generate(ctx(close, quiet)) == []
    crash = close.copy()
    crash.iloc[-3:, :] = crash.iloc[-4, :].values * np.array([[0.92], [0.85], [0.78]])
    heavy = vol.copy()
    heavy.iloc[-3:, :] = 4e5
    assert CascadeRebound({"scope": "us"}).generate(ctx(crash, heavy)) == []   # whole market fell


def test_tax_loss_rebound_fires_once_in_mid_december_on_deep_losers():
    close, vol = prices(n=300, start="2025-11-03")
    year = close.index >= "2026-01-01"
    close.loc[year, "A"] = np.linspace(close.loc[~year, "A"].iloc[-1], close.loc[~year, "A"].iloc[-1] * 0.5, year.sum())
    first = close.index[close.index >= "2026-12-15"][0]
    on_day = close.loc[:first]
    picks = TaxLossRebound({"scope": "us"}).generate(ctx(on_day, vol.loc[:first]))
    assert [p.ticker for p in picks] == ["A"]
    nxt = close.index[close.index > first][0]
    assert TaxLossRebound({"scope": "us"}).generate(ctx(close.loc[:nxt], vol.loc[:nxt])) == []
    before = close.index[close.index < "2026-12-15"][-1]
    assert TaxLossRebound({"scope": "us"}).generate(ctx(close.loc[:before], vol.loc[:before])) == []


def test_new_signals_are_registered_as_lab_entries():
    import yaml
    from pathlib import Path
    from edgelab.cli import build_signals
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())
    cfg["_root"] = str(Path(__file__).resolve().parents[1])
    sigs = {s.name: s for s in build_signals(cfg)}
    for name in ("gov_contract", "cascade_rebound", "tax_loss_rebound"):
        assert sigs[name].lab and sigs[name].scope == "us"


def test_source_reads_the_page_when_the_feed_has_only_a_summary(tmp_path):
    cfg = {"_root": str(tmp_path), "sec": {"user_agent": "t"}}
    http = FakeHttp({"RSS.ashx": feed([("Contracts For Nov. 17, 2026", "https://www.war.gov/News/Contracts/Contract/Article/9",
                                        "Tue, 17 Nov 2026 22:00:00 GMT", "Contracts valued at $7.5 million or more...")]),
                     "Article/9": f"<html><body><p>{PAGE}</p></body></html>".encode(),
                     "company_tickers": TICKERS_JSON})
    ev = DefenseContractsSource(cfg, {}, str(tmp_path / "d.db"), http=http).fetch(DAY, {"BA", "KAI"})
    assert {e.ticker for e in ev} == {"BA", "KAI"}
