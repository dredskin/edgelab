"""SEC EDGAR adapters (free, no API key; SEC requires a descriptive User-Agent with a real contact).

Produces Events for:
  - insider_buy     from Form 4 open-market purchases (transaction code P)
  - earnings        from 8-K filings reporting results (Item 2.02), using the attached press release
  - spinoff_listed  from Form 10-12B registrations once the new shares start trading

"First purchase in a long time" needs history. Purchases are remembered in SQLite, and the flag is only
raised for a stock once EdgeLab has watched it for min_history_days. `edgelab backfill --days 365`
collects that history in one go instead of waiting a year.
"""
import re
import time
import xml.etree.ElementTree as ET
from datetime import timedelta

import pandas as pd
import requests

from .. import store
from .events import Event

BASE = "https://www.sec.gov"
DOC_RE = re.compile(r"<DOCUMENT>\s*<TYPE>([^\s<]+).*?<TEXT>(.*?)</TEXT>", re.S | re.I)


def strip_filing(raw: str) -> str:
    txt = re.sub(r"<ix:header>.*?</ix:header>", " ", raw, flags=re.S | re.I)     # hidden XBRL metadata
    txt = re.sub(r"<(script|style)\b.*?</\1>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"&nbsp;|&#160;|&#xa0;", " ", txt, flags=re.I)
    txt = re.sub(r"&[a-zA-Z#0-9]+;", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


class EdgarClient:
    def __init__(self, user_agent: str, db_path: str, min_interval=0.12, session=None):
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.min_interval = min_interval          # SEC fair-use: <= 10 requests/second
        self._last = 0.0
        self.db = store.connect(db_path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS insider_history(owner TEXT, ticker TEXT, date TEXT, shares REAL, price REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_insider ON insider_history(owner, ticker, date, shares, price);
            CREATE TABLE IF NOT EXISTS insider_coverage(ticker TEXT PRIMARY KEY, first_date TEXT);
            CREATE TABLE IF NOT EXISTS spinoff_registrations(
                cik TEXT PRIMARY KEY, company TEXT, filed TEXT, listed INTEGER DEFAULT 0);""")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(spinoff_registrations)")}
        for col in ("ticker", "first_trade"):
            if col not in have:
                self.db.execute(f"ALTER TABLE spinoff_registrations ADD COLUMN {col} TEXT")
        self.db.commit()
        self._tickers = None
        self._index = {}

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
        """All filings for one day from the EDGAR master index (fetched once per day per process)."""
        d = pd.Timestamp(date).normalize()
        if d in self._index:
            return self._index[d]
        rows = []
        if d.weekday() < 5:                                 # no index on weekends
            q = (d.month - 1) // 3 + 1
            url = f"{BASE}/Archives/edgar/daily-index/{d.year}/QTR{q}/master.{d:%Y%m%d}.idx"
            try:
                text = self._get(url).text
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code != 404:
                    raise                                   # 403 usually means SEC rejected the User-Agent
                text = ""                                   # market holiday: no index that day
            for line in text.splitlines():
                parts = line.split("|")
                if len(parts) == 5 and parts[0].isdigit():
                    rows.append(dict(cik=parts[0].lstrip("0"), company=parts[1], form=parts[2],
                                     date=parts[3], filename=parts[4]))
        self._index[d] = rows
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

    def form4_buys(self, date, universe: set) -> list:
        """Open-market purchases filed on `date`. Every Form 4 is indexed under its issuer, so only filings
        whose issuer is in the universe are downloaded (an empty universe means every issuer)."""
        rows = [r for r in self.daily_index(date) if r["form"] == "4"]
        if universe:
            tick = self.cik_to_ticker()
            files = sorted({r["filename"] for r in rows if tick.get(r["cik"]) in universe})
        else:
            files = sorted({r["filename"] for r in rows})
        buys = []
        for fn in files:
            try:
                raw = self._get(f"{BASE}/Archives/{fn}").text
            except requests.RequestException:
                continue
            buys += [b for b in self.parse_form4(raw) if not universe or b["ticker"] in universe]
        return buys

    def _remember(self, date, universe: set, buys: list):
        d = pd.Timestamp(date).strftime("%Y-%m-%d")
        self.db.executemany("INSERT OR IGNORE INTO insider_history VALUES(?,?,?,?,?)",
                            [(b["owner"], b["ticker"], d, b["shares"], b["price"]) for b in buys])
        self.db.executemany(
            """INSERT INTO insider_coverage VALUES(?,?)
               ON CONFLICT(ticker) DO UPDATE SET first_date=min(first_date, excluded.first_date)""",
            [(t, d) for t in (universe or {"*"})])
        self.db.commit()

    def history_days(self, ticker: str, date) -> int:
        """How long EdgeLab has been recording insider purchases for this stock."""
        rows = self.db.execute("SELECT first_date FROM insider_coverage WHERE ticker IN (?, '*')", (ticker,)).fetchall()
        if not rows:
            return 0
        return max(0, (pd.Timestamp(date) - pd.Timestamp(min(r[0] for r in rows))).days)

    def insider_events(self, date, universe: set, first_time_years=5, min_history_days=365) -> list:
        d = pd.Timestamp(date).normalize()
        buys = self.form4_buys(d, universe)
        since = (d - timedelta(days=365 * first_time_years)).strftime("%Y-%m-%d")
        by_ticker = {}
        for b in buys:
            by_ticker.setdefault(b["ticker"], []).append(b)
        events = []
        for tk, bs in by_ticker.items():
            owners = sorted({b["owner"] for b in bs})
            watched = self.history_days(tk, d)
            first_time = False
            if watched >= min_history_days:                 # without history, "first time" would be meaningless
                for o in owners:
                    prior = self.db.execute(
                        "SELECT COUNT(*) FROM insider_history WHERE owner=? AND ticker=? AND date>=? AND date<?",
                        (o, tk, since, d.strftime("%Y-%m-%d"))).fetchone()[0]
                    first_time = first_time or prior == 0
            events.append(Event("insider_buy", d, tk, dict(
                cluster=len(owners), first_time=first_time, history_days=watched, owners=owners,
                roles=sorted({b["role"] for b in bs}), value=sum(b["shares"] * b["price"] for b in bs))))
        self._remember(d, universe, buys)
        return events

    def backfill_insiders(self, start, end, universe: set, log=print) -> int:
        """Record past insider purchases (no events, no trades) so 'first purchase' flags can be trusted."""
        total = 0
        for d in pd.bdate_range(start, end):
            try:
                buys = self.form4_buys(d, universe)
            except requests.RequestException as e:
                log(f"  {d.date()}: skipped ({e})")
                continue
            self._remember(d, universe, buys)
            self._index.pop(d.normalize(), None)            # keep memory flat over long backfills
            total += len(buys)
            log(f"  {d.date()}: {len(buys)} purchases")
        return total

    # ---------- 8-K earnings ----------
    @staticmethod
    def is_earnings_8k(raw: str) -> bool:
        if "Results of Operations and Financial Condition" in raw[:8000]:      # item list in the SEC header
            return True
        return re.search(r"Item(?:\s|&nbsp;|&#160;)*2\.02", raw, re.I) is not None

    @staticmethod
    def earnings_text(raw: str, limit=15000) -> str:
        """The press release (Exhibit 99) if the filing carries one, else the 8-K body from Item 2.02."""
        for typ, body in DOC_RE.findall(raw):
            if typ.upper().startswith("EX-99"):
                txt = strip_filing(body)
                if len(txt) > 500 and not txt.startswith("begin "):           # skip encoded PDFs/images
                    return txt[:limit]
        txt = strip_filing(raw)
        m = re.search(r"Item\s*2\.02", txt, re.I)
        return txt[m.start() if m else 0:][:limit]

    def earnings_events(self, date, universe: set) -> list:
        events = []
        for row in self.daily_index(date):
            if row["form"] != "8-K":
                continue
            tk = self.cik_to_ticker().get(row["cik"])
            if not tk or (universe and tk not in universe):
                continue
            try:
                raw = self._get(f"{BASE}/Archives/{row['filename']}").text
            except requests.RequestException:
                continue
            if not self.is_earnings_8k(raw):
                continue
            events.append(Event("earnings", pd.Timestamp(date).normalize(), tk,
                                dict(text=self.earnings_text(raw), source=row["filename"])))
        return events

    # ---------- spin-offs ----------
    def spinoff_watch(self, date, max_age_days=540) -> set:
        """Tickers of recently registered spin-offs. They are not in universe.txt, so the caller must add
        them to the price download for the listing to be noticed and the trade to be tracked."""
        since = (pd.Timestamp(date) - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
        ciks = [r[0] for r in self.db.execute("SELECT cik FROM spinoff_registrations WHERE filed >= ?", (since,))]
        if not ciks:
            return set()
        tick = self.cik_to_ticker()
        return {tick[c] for c in ciks if c in tick}

    def spinoff_events(self, date, price_panel) -> list:
        """Track Form 10-12B registrations; emit spinoff_listed once the shares have a first trading day."""
        for row in self.daily_index(date):
            if row["form"].startswith("10-12B"):
                self.db.execute("INSERT OR IGNORE INTO spinoff_registrations(cik, company, filed) VALUES(?,?,?)",
                                (row["cik"], row["company"], pd.Timestamp(row["date"]).strftime("%Y-%m-%d")))
        self.db.commit()
        events = []
        if price_panel is None:
            return events
        pending = self.db.execute("SELECT cik, company, filed FROM spinoff_registrations WHERE listed=0").fetchall()
        tick = self.cik_to_ticker() if pending else {}
        for cik, company, filed in pending:
            tk = tick.get(cik)
            if not tk or tk not in price_panel.close.columns:
                continue
            first = price_panel.close[tk].first_valid_index()
            # the first trade must come after the registration and inside the panel (not cut off at its left edge)
            if first is None or first <= pd.Timestamp(filed) or first == price_panel.dates[0]:
                continue
            events.append(Event("spinoff_listed", pd.Timestamp(first).normalize(), tk, dict(company=company)))
            self.db.execute("UPDATE spinoff_registrations SET listed=1, ticker=?, first_trade=? WHERE cik=?",
                            (tk, pd.Timestamp(first).strftime("%Y-%m-%d"), cik))
        self.db.commit()
        return events
