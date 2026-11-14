"""Japan.

EDINET (official FSA API, free key -> env EDINET_API_KEY): large shareholding reports (大量保有報告書),
i.e. someone building a 5%+ stake. Japan's closest analogue to US insider/activist filings.

TDnet (timely disclosure): earnings releases (決算短信), forecast revisions (業績予想の修正) and buyback
decisions (自己株式取得). The exchange offers no free official API, so the list endpoint is configurable;
the default is a widely used community mirror. Swap in a licensed feed for production use.
"""
import csv
import io
import os
import zipfile

import pandas as pd

from ..data.events import Event
from .base import Source, SourceError, pdf_text

EDINET = "https://api.edinet-fsa.go.jp/api/v2"
TDNET_LIST = "https://webapi.yanoshin.jp/webapi/tdnet/list/{date}.json?limit=2000"


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def parse_large_holding_csv(zip_bytes: bytes) -> dict:
    """Pull issuer code, holding ratio, previous ratio and purpose out of EDINET's CSV package.
    Matches on element ids and Japanese labels, because taxonomy versions differ."""
    out = {}
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return out
    for name in z.namelist():
        if not name.lower().endswith(".csv"):
            continue
        raw = z.read(name)
        for enc in ("utf-16", "utf-8-sig", "cp932"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeError:
                continue
        else:
            continue
        for row in csv.reader(io.StringIO(text), delimiter="\t"):
            if len(row) < 3:
                continue
            key, val = f"{row[0]} {row[1]}", row[-1].strip()
            if ("SecurityCodeOfIssuer" in key or "証券コード" in key) and "sec" not in out and val[:4].isalnum():
                out["sec"] = val[:4]
            elif "PerLastReport" in key or "直前の報告書" in key:
                out.setdefault("prev_ratio", _num(val))
            elif "HoldingRatioOfShareCertificatesEtc" in key or "株券等保有割合" in key:
                out.setdefault("ratio", _num(val))
            elif "PurposeOfHolding" in key or "保有目的" in key:
                out.setdefault("purpose", val[:400])
    for k in ("ratio", "prev_ratio"):                     # EDINET reports ratios as fractions (0.0712)
        if out.get(k) is not None and out[k] <= 1:
            out[k] = round(out[k] * 100, 4)
    return out


class EdinetSource(Source):
    name = "edinet"

    def __init__(self, cfg, scfg, db_path, http=None):
        super().__init__(cfg, scfg, db_path, http)
        self.db.execute("CREATE TABLE IF NOT EXISTS edinet_codes(edinet TEXT PRIMARY KEY, sec TEXT)")
        self.max_docs = self.scfg.get("max_docs_per_day", 80)

    def _sec(self, edinet_code):
        r = self.db.execute("SELECT sec FROM edinet_codes WHERE edinet=?", (edinet_code or "",)).fetchone()
        return r[0] if r else None

    def fetch(self, date, universe, panel=None, llm=None):
        key = os.environ.get("EDINET_API_KEY")
        if not key:
            raise SourceError("set EDINET_API_KEY (free at api.edinet-fsa.go.jp)")
        d = pd.Timestamp(date)
        j = self.http.json(f"{EDINET}/documents.json", {"date": d.strftime("%Y-%m-%d"), "type": 2,
                                                        "Subscription-Key": key})
        rows = j.get("results") or []
        for r in rows:                                    # learn EDINET code -> securities code as we go
            if r.get("edinetCode") and r.get("secCode"):
                self.db.execute("INSERT OR REPLACE INTO edinet_codes VALUES(?,?)", (r["edinetCode"], r["secCode"][:4]))
        self.db.commit()
        events, fetched = [], 0
        for r in rows:
            if r.get("docTypeCode") != "350" or r.get("withdrawalStatus") not in (None, "0"):
                continue
            sec = self._sec(r.get("issuerEdinetCode"))
            if sec and universe and f"{sec}.T" not in universe:
                continue
            if fetched >= self.max_docs:
                break
            fetched += 1
            try:
                info = parse_large_holding_csv(self.http.content(
                    f"{EDINET}/documents/{r['docID']}", {"type": 5, "Subscription-Key": key}))
            except Exception:
                info = {}
            sec = sec or info.get("sec")
            if not sec or info.get("ratio") is None:
                continue
            tk = f"{sec}.T"
            if universe and tk not in universe:
                continue
            events.append(Event("large_stake", d, tk, dict(
                holder=r.get("filerName", ""), ratio=info["ratio"], prev_ratio=info.get("prev_ratio"),
                purpose=info.get("purpose", ""), description=r.get("docDescription", ""), doc_id=r["docID"],
                source="edinet")))
        return events


def classify_tdnet(title: str):
    """Map a TDnet headline to (event kind, subtype); None = ignore."""
    if "訂正" in title:
        return None                                       # corrections
    if "自己株式" in title and "取得" in title and not any(w in title for w in ("結果", "状況", "終了", "消却")):
        return ("buyback", "decision")
    if "決算短信" in title:
        return ("earnings", "results")
    if "業績予想" in title and "修正" in title:
        return ("earnings", "forecast_revision")
    return None


class TdnetSource(Source):
    name = "tdnet"

    def fetch(self, date, universe, panel=None, llm=None):
        d = pd.Timestamp(date)
        url = self.scfg.get("list_url", TDNET_LIST).format(date=d.strftime("%Y%m%d"))
        j = self.http.json(url)
        items = j.get("items", j) if isinstance(j, dict) else j
        events = []
        for it in items or []:
            t = it.get("Tdnet", it)
            code, title = str(t.get("company_code", ""))[:4], t.get("title", "")
            kind = classify_tdnet(title)
            tk = f"{code}.T"
            if not kind or not code or (universe and tk not in universe):
                continue
            if not self.first_time(str(t.get("id") or f"{code}|{title}|{d.date()}")):
                continue
            body = ""
            if t.get("document_url"):
                try:
                    body = pdf_text(self.http.content(t["document_url"]))
                except Exception:
                    body = ""
            events.append(Event(kind[0], d, tk, dict(text=f"{title}\n{body}", title=title, subtype=kind[1],
                                                     company=t.get("company_name", ""), source="tdnet",
                                                     url=t.get("document_url", ""))))
        return events
