"""International sources, tested against fixtures shaped like each provider's responses (no network)."""
import io
import json
import zipfile

import pandas as pd
import pytest

from edgelab.data.events import Event, EventStore
from edgelab.data.prices import PricePanel
from edgelab.lab.global_signals import Buyback, LargeStake, SupplyChainRead
from edgelab.signals.base import Context
from edgelab.signals.earnings_drift import EarningsDrift
from edgelab.sources.announcements import AnnouncementSource
from edgelab.sources.asia import DartSource, TaiwanSalesSource, classify_dart
from edgelab.sources.base import SourceError, run_sources
from edgelab.sources.japan import EdinetSource, TdnetSource, classify_tdnet, parse_large_holding_csv

DAY = pd.Timestamp("2026-11-10")


class FakeHttp:
    """Routes by URL substring to canned payloads."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _find(self, url):
        self.calls.append(url)
        for k, v in self.routes.items():
            if k in url:
                return v
        raise RuntimeError(f"no route for {url}")

    def json(self, url, params=None):
        return self._find(url)

    def content(self, url, params=None):
        return self._find(url)


def cfg(tmp_path, **extra):
    return {"_root": str(tmp_path), "sec": {"user_agent": "t"}, **extra}


def panel(tickers, n=80):
    idx = pd.bdate_range(end=DAY, periods=n)
    df = pd.DataFrame(10.0, idx, tickers)
    return PricePanel(df, df, df)


def edinet_zip(ratio="0.0712", prev="0.0500"):
    rows = [["要素ID", "項目名", "コンテキストID", "値"],
            ["jplvh_cor:SecurityCodeOfIssuer", "証券コード", "c", "65010"],
            ["jplvh_cor:HoldingRatioOfShareCertificatesEtc", "株券等保有割合", "c", ratio],
            ["jplvh_cor:HoldingRatioOfShareCertificatesEtcPerLastReport", "直前の報告書に記載された株券等保有割合", "c", prev],
            ["jplvh_cor:PurposeOfHolding", "保有目的", "c", "純投資及び重要提案行為等を行うこと"]]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL_TO_CSV/doc.csv", "\n".join("\t".join(r) for r in rows).encode("utf-16"))
    return buf.getvalue()


# ---------------- Japan ----------------
def test_edinet_large_holding_parse_and_event(tmp_path, monkeypatch):
    assert parse_large_holding_csv(edinet_zip()) == {
        "sec": "6501", "ratio": 7.12, "prev_ratio": 5.0, "purpose": "純投資及び重要提案行為等を行うこと"}
    monkeypatch.setenv("EDINET_API_KEY", "k")
    listing = {"results": [
        {"docID": "S1", "docTypeCode": "350", "filerName": "Oasis Fund", "issuerEdinetCode": "E01737",
         "secCode": None, "edinetCode": "E99999", "docDescription": "変更報告書", "withdrawalStatus": "0"},
        {"docID": "S2", "docTypeCode": "120", "filerName": "Hitachi", "edinetCode": "E01737", "secCode": "65010"}]}
    src = EdinetSource(cfg(tmp_path), {}, str(tmp_path / "s.db"), FakeHttp({"documents.json": listing, "documents/S1": edinet_zip()}))
    ev = src.fetch(DAY, {"6501.T"})
    assert len(ev) == 1 and ev[0].kind == "large_stake" and ev[0].ticker == "6501.T"
    assert ev[0].payload["ratio"] == 7.12 and ev[0].payload["holder"] == "Oasis Fund"
    assert src.fetch(DAY, {"9999.T"}) == []                 # outside the universe


def test_edinet_needs_key(tmp_path, monkeypatch):
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    with pytest.raises(SourceError):
        EdinetSource(cfg(tmp_path), {}, str(tmp_path / "s.db"), FakeHttp({})).fetch(DAY, set())


def test_tdnet_classification_and_dedupe(tmp_path):
    assert classify_tdnet("2026年3月期 第2四半期決算短信〔日本基準〕（連結）") == ("earnings", "results")
    assert classify_tdnet("業績予想の修正に関するお知らせ") == ("earnings", "forecast_revision")
    assert classify_tdnet("自己株式取得に係る事項の決定に関するお知らせ") == ("buyback", "decision")
    assert classify_tdnet("自己株式の取得状況に関するお知らせ") is None          # progress update, not a decision
    assert classify_tdnet("（訂正）決算短信の一部訂正について") is None
    items = {"items": [
        {"Tdnet": {"id": "1", "company_code": "65010", "title": "業績予想の修正に関するお知らせ", "document_url": "http://x/a.pdf"}},
        {"Tdnet": {"id": "2", "company_code": "72030", "title": "自己株式取得に係る事項の決定に関するお知らせ", "document_url": ""}},
        {"Tdnet": {"id": "3", "company_code": "99990", "title": "人事異動のお知らせ", "document_url": ""}}]}
    src = TdnetSource(cfg(tmp_path), {}, str(tmp_path / "s.db"), FakeHttp({"tdnet/list": items, "a.pdf": b"not a pdf"}))
    ev = src.fetch(DAY, set())
    assert [(e.kind, e.ticker) for e in ev] == [("earnings", "6501.T"), ("buyback", "7203.T")]
    assert src.fetch(DAY, set()) == []                      # same items are not emitted twice


# ---------------- Taiwan ----------------
def tw_rows(ym, cur, last):
    return [{"資料年月": ym, "公司代號": "3008", "公司名稱": "大立光", "營業收入-當月營收": str(cur),
             "營業收入-上月營收": "1", "營業收入-去年當月營收": str(last)}]


def test_taiwan_sales_dated_when_first_seen_with_acceleration(tmp_path):
    (tmp_path / "supply_chain.yaml").write_text("3008.TW: [AAPL]\n")
    http = FakeHttp({"twse": tw_rows("11509", 110, 100), "tpex": []})
    src = TaiwanSalesSource(cfg(tmp_path), {}, str(tmp_path / "s.db"), http)
    first = src.fetch("2026-10-09", set())
    assert first[0].ticker == "3008.TW" and first[0].payload["accel"] is None      # no history yet
    assert first[0].date == pd.Timestamp("2026-10-09")                              # publication day, not the month
    assert src.fetch("2026-10-12", set()) == []                                     # same month again: nothing new
    http.routes["twse"] = tw_rows("11510", 150, 100)
    nxt = src.fetch(DAY, set())
    assert nxt[0].payload["yoy"] == pytest.approx(0.5) and nxt[0].payload["accel"] == pytest.approx(0.4)


# ---------------- Korea ----------------
def test_dart(tmp_path, monkeypatch):
    assert classify_dart("연결재무제표기준영업(잠정)실적(공정공시)") == "earnings"
    assert classify_dart("주요사항보고서(자기주식취득결정)") == "buyback"
    assert classify_dart("[기재정정]영업(잠정)실적(공정공시)") is None
    monkeypatch.setenv("DART_API_KEY", "k")
    (tmp_path / "supply_chain.yaml").write_text("000660.KS: [NVDA]\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("d.xml", "<DOCUMENT><P>매출액 17조원</P></DOCUMENT>".encode("utf-8"))
    listing = {"status": "000", "total_page": 1, "list": [
        {"corp_cls": "Y", "stock_code": "000660", "corp_name": "SK하이닉스", "report_nm": "영업(잠정)실적(공정공시)", "rcept_no": "1"},
        {"corp_cls": "K", "stock_code": "111111", "corp_name": "Other", "report_nm": "영업(잠정)실적(공정공시)", "rcept_no": "2"}]}
    src = DartSource(cfg(tmp_path), {}, str(tmp_path / "s.db"), FakeHttp({"list.json": listing, "document.xml": buf.getvalue()}))
    ev = src.fetch(DAY, set())
    assert len(ev) == 1 and ev[0].ticker == "000660.KS" and "매출액 17조원" in ev[0].payload["text"]


# ---------------- universal announcements ----------------
class StubLLM:
    def __init__(self, answers):
        self.answers = answers

    def classify_announcement(self, text, date):
        return next(v for k, v in self.answers.items() if k in text)

    def buyback_size(self, event):
        return {"pct_of_shares": 4.0, "confidence": 0.8}

    def score_earnings(self, event):
        return {"surprise": 0.8, "evasion": 0.0, "confidence": 0.8}


RSS = """<rss><channel>
<item><title>Director/PDMR Shareholding - Acme plc (LSE: ACME)</title><description>Two directors purchased shares</description>
 <pubDate>Tue, 10 Nov 2026 07:00:00 GMT</pubDate><guid>a1</guid></item>
<item><title>須予披露交易 回購股份</title><description>本公司決定回購股份</description>
 <pubDate>Tue, 10 Nov 2026 09:00:00 GMT</pubDate><guid>a2</guid></item>
<item><title>New office opening</title><description>We moved</description>
 <pubDate>Tue, 10 Nov 2026 09:00:00 GMT</pubDate><guid>a3</guid></item>
<item><title>Old news (LSE: ACME)</title><description>results</description>
 <pubDate>Mon, 02 Nov 2026 09:00:00 GMT</pubDate><guid>a4</guid></item>
</channel></rss>""".encode()


def test_announcement_reader_routes_any_language(tmp_path):
    llm = StubLLM({"PDMR": {"kind": "insider_buy", "n_insiders": 2, "first_purchase": False, "language": "en"},
                   "回購": {"kind": "buyback", "exchange": "HKEX", "symbol": "700", "language": "zh"},
                   "office": {"kind": "other"}})
    src = AnnouncementSource(cfg(tmp_path), {"feeds": [{"url": "http://feed"}]}, str(tmp_path / "s.db"), FakeHttp({"feed": RSS}))
    ev = src.fetch(DAY, set(), None, llm)
    got = {(e.kind, e.ticker) for e in ev}
    assert got == {("insider_buy", "ACME.L"), ("buyback", "0700.HK")}
    assert next(e for e in ev if e.kind == "insider_buy").payload["cluster"] == 2


# ---------------- lab signals ----------------
def ctx_for(events, tickers, llm=None):
    store = EventStore()
    store.extend(events)
    return Context(asof=DAY, prices=panel(tickers), events=store, llm=llm)


def test_large_stake_and_buyback_signals():
    up = Event("large_stake", DAY, "6501.T", dict(holder="Oasis", ratio=7.1, prev_ratio=5.0, purpose="重要提案行為等"))
    down = Event("large_stake", DAY, "7203.T", dict(holder="X", ratio=4.0, prev_ratio=6.0, purpose=""))
    picks = LargeStake({}).generate(ctx_for([up, down], ["6501.T", "7203.T"]))
    assert [(p.ticker, p.direction) for p in picks] == [("6501.T", 1)] and "activist" in picks[0].rationale
    bb = Buyback({"min_pct": 1.0}).generate(ctx_for([Event("buyback", DAY, "7203.T", {"text": "x"})], ["7203.T"], StubLLM({})))
    assert bb[0].ticker == "7203.T" and bb[0].strength == 0.5


def test_supply_chain_trades_customers_not_suppliers():
    sig = SupplyChainRead({"min_accel": 0.10}, {"3008.TW": ["AAPL", "NOPE"], "000660.KS": ["NVDA"]})
    ev = [Event("monthly_sales", DAY, "3008.TW", {"accel": -0.25}),
          Event("monthly_sales", DAY, "2330.TW", {"accel": 0.9}),            # not in the map
          Event("earnings", DAY, "000660.KS", {"text": "x"})]
    picks = sig.generate(ctx_for(ev, ["AAPL", "NVDA"], StubLLM({})))
    assert {(p.ticker, p.direction) for p in picks} == {("AAPL", -1), ("NVDA", 1)}


def test_scope_keeps_us_and_international_records_separate():
    ev = [Event("earnings", DAY, "ABC", {"text": "x"}), Event("earnings", DAY, "6501.T", {"text": "x"})]
    c = ctx_for(ev, ["ABC", "6501.T"], StubLLM({}))
    assert [p.ticker for p in EarningsDrift({"scope": "us"}).generate(c)] == ["ABC"]
    intl = EarningsDrift({"scope": "intl", "name": "earnings_drift_intl", "lab": True})
    assert [p.ticker for p in intl.generate(c)] == ["6501.T"] and intl.lab and intl.name == "earnings_drift_intl"


def test_failing_source_is_logged_not_fatal(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    db = str(tmp_path / "s.db")
    bad = EdinetSource(cfg(tmp_path), {}, db, FakeHttp({}))
    ok = TdnetSource(cfg(tmp_path), {}, db, FakeHttp({"tdnet/list": {"items": []}}))
    store = run_sources([bad, ok], DAY, set(), None, db)
    log = dict((r[0], r[1]) for r in sqlite3.connect(db).execute("SELECT source, error FROM source_log"))
    assert "EDINET_API_KEY" in log["edinet"] and log["tdnet"] == "" and store.count() == 0
