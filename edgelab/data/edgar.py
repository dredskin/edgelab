"""SEC EDGAR adapters (free, no API key; SEC requires a descriptive User-Agent).

Produces Events for:
  - insider_buy     from Form 4 open-market purchases (transaction code P)
  - earnings        from 8-K filings containing Item 2.02 (results of operations)
  - spinoff_listed  from Form 10-12B registrations once the new ticker starts trading

History needed for "first purchase in N years" is kept in a local SQLite table and
grows as you run daily. Backfill with `edgelab backfill-insiders --days 365` for a head start.
"""
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import timedelta

import pandas as pd
import requests

from .events import Event

BASE = "https://www.sec.gov"


class EdgarClient:
    def __init__(self, user_agent: str, db_path: str, min_interval=0.12):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.min_interval = min_interval          # SEC fair-use: <= 10 requests/second
        self._last = 0.0
        self.db = sqlite3.connect(db_path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS insider_history(
            owner TEXT, ticker TEXT, date TEXT, shares REAL, price REAL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS spinoff_registrations(
            cik TEXT PRIMARY KEY, company TEXT, filed TEXT, listed INTEGER DEFAULT 0)""")
        self._tickers = None

    # ---------- plumbing ----------
    def _get(self, url):
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        r = self.s.get(url, timeout=30)
        self._last = time.time()
        r.raise_for_status()
        return r

    def cik_to_ticker(self) -> dict:
        if self._tickers is None:
            data = self._get(f"{BASE}/files/company_tickers.json").json()
            self._tickers = {str(v["cik_str"]): v["ticker"].upper() for v in data.values()}
        return self._tickers

    def daily_index(self, date) -> list:
        """All filings for one day from the EDGAR master index."""
        d = pd.Timestamp(date)
        q = (d.month - 1) // 3 + 1
        url = f"{BASE}/Archives/edgar/daily-index/{d.year}/QTR{q}/master.{d:%Y%m%d}.idx"
        try:
            text = self._get(url).text
        except requests.HTTPError:
            return []                               # weekends/holidays have no index
        rows = []
        for line in text.splitlines():
            parts = line.split("|")
            if len(parts) == 5 and parts[0].isdigit():
                rows.append(dict(cik=parts[0], company=parts[1], form=parts[2],
                                 date=parts[3], filename=parts[4]))
        return rows

    # ---------- Form 4 ----------
    @staticmethod
    def parse_form4(raw: str) -> list:
        m = re.search(r"<XML>(.*?)</XML>", raw, re.S | re.I)
        if not m:
            return []
        try:
            root = ET.fromstring(m.group(1).strip())
        except ET.ParseError:
            return []
        g = lambda node, path: (node.findtext(path) or "").strip()
        ticker = g(root, "issuer/issuerTradingSymbol").upper()
        owners = [g(o, "reportingOwnerId/rptOwnerName") for o in root.findall("reportingOwner")]
        rel = root.find("reportingOwner/reportingOwnerRelationship")
        role = ""
        if rel is not None:
            if g(rel, "isOfficer") in ("1", "true"):
                role = g(rel, "officerTitle") or "Officer"
            elif g(rel, "isDirector") in ("1", "true"):
                role = "Director"
            elif g(rel, "isTenPercentOwner") in ("1", "true"):
                role = "10% owner"
        out = []
        for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
            if g(t, "transactionCoding/transactionCode") != "P":
                continue
            try:
                shares = float(g(t, "transactionAmounts/transactionShares/value") or 0)
                price = float(g(t, "transactionAmounts/transactionPricePerShare/value") or 0)
            except ValueError:
                continue
            for o in owners:
                out.append(dict(ticker=ticker, owner=o, role=role, shares=shares, price=price))
        return out

    def insider_events(self, date, universe: set, first_time_years=5) -> list:
        buys, seen = [], set()
        for row in self.daily_index(date):
            # each Form 4 is indexed under both issuer and owner CIK -> dedupe by filename
            if row["form"] != "4" or row["filename"] in seen:
                continue
            seen.add(row["filename"])
            tk = self.cik_to_ticker().get(row["cik"].lstrip("0"))
            if universe and tk and tk not in universe:
                continue          # issuer row for a company outside the universe
            try:
                raw = self._get(f"{BASE}/Archives/{row['filename']}").text
            except requests.HTTPError:
                continue
            for b in self.parse_form4(raw):
                if not universe or b["ticker"] in universe:
                    buys.append(b)
        d = pd.Timestamp(date)
        since = (d - timedelta(days=365 * first_time_years)).strftime("%Y-%m-%d")
        events = []
        by_ticker = {}
        for b in buys:
            by_ticker.setdefault(b["ticker"], []).append(b)
        for tk, bs in by_ticker.items():
            owners = sorted({b["owner"] for b in bs})
            first_time = False
            for o in owners:
                prior = self.db.execute(
                    "SELECT COUNT(*) FROM insider_history WHERE owner=? AND ticker=? AND date>=? AND date<?",
                    (o, tk, since, d.strftime("%Y-%m-%d"))).fetchone()[0]
                if prior == 0:
                    first_time = True
            value = sum(b["shares"] * b["price"] for b in bs)
            events.append(Event("insider_buy", d, tk, dict(
                cluster=len(owners), first_time=first_time, owners=owners,
                roles=sorted({b["role"] for b in bs}), value=value)))
            for b in bs:
                self.db.execute("INSERT INTO insider_history VALUES(?,?,?,?,?)",
                                (b["owner"], tk, d.strftime("%Y-%m-%d"), b["shares"], b["price"]))
        self.db.commit()
        return events

    # ---------- 8-K earnings ----------
    @staticmethod
    def clean_text(raw: str, limit=15000) -> str:
        txt = re.sub(r"<[^>]+>", " ", raw)
        txt = re.sub(r"&[a-z#0-9]+;", " ", txt)
        txt = re.sub(r"\s+", " ", txt)
        i = txt.find("Item 2.02")
        return txt[i if i >= 0 else 0:][:limit]

    def earnings_events(self, date, universe: set) -> list:
        events = []
        for row in self.daily_index(date):
            if row["form"] != "8-K":
                continue
            tk = self.cik_to_ticker().get(row["cik"].lstrip("0"))
            if not tk or (universe and tk not in universe):
                continue
            try:
                raw = self._get(f"{BASE}/Archives/{row['filename']}").text
            except requests.HTTPError:
                continue
            if "Item 2.02" not in raw:
                continue
            events.append(Event("earnings", pd.Timestamp(date), tk,
                                dict(text=self.clean_text(raw), source=row["filename"])))
        return events

    # ---------- spin-offs ----------
    def spinoff_events(self, date, price_panel) -> list:
        """Track Form 10-12B registrations; emit spinoff_listed when the ticker first trades."""
        for row in self.daily_index(date):
            if row["form"].startswith("10-12B"):
                self.db.execute("INSERT OR IGNORE INTO spinoff_registrations(cik,company,filed) VALUES(?,?,?)",
                                (row["cik"].lstrip("0"), row["company"], row["date"]))
        self.db.commit()
        events = []
        pending = self.db.execute("SELECT cik, company FROM spinoff_registrations WHERE listed=0").fetchall()
        for cik, company in pending:
            tk = self.cik_to_ticker().get(cik)
            if not tk or price_panel is None or tk not in price_panel.close.columns:
                continue
            first = price_panel.close[tk].first_valid_index()
            if first is not None:
                events.append(Event("spinoff_listed", pd.Timestamp(first), tk, dict(company=company)))
                self.db.execute("UPDATE spinoff_registrations SET listed=1 WHERE cik=?", (cik,))
        self.db.commit()
        return events
