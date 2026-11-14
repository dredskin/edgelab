"""Taiwan monthly sales and Korea DART filings.

Both markets are research-only by default (not tradable through IB for most non-resident accounts), so
these sources mainly feed the supply-chain signal: a supplier's numbers, read early, say something about
its customers elsewhere.

Taiwan: every listed company must publish the previous month's revenue by the 10th. TWSE and TPEx expose
the latest month as open data. The feed carries no publication timestamp, so an item is dated the day
EdgeLab FIRST SEES it (never the month it describes).

Korea: DART OpenAPI (free key -> env DART_API_KEY): preliminary earnings, buyback decisions.
"""
import io
import os
import zipfile

import pandas as pd

from ..data.events import Event
from .base import Source, SourceError, strip_markup

TW_FEEDS = {".TW": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
            ".TWO": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"}
DART = "https://opendart.fss.or.kr/api"


def _field(row: dict, *needles):
    for k, v in row.items():
        if all(n in k for n in needles):
            return v
    return None


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def load_supply_chain(cfg) -> dict:
    """supplier ticker -> list of customer tickers (see supply_chain.yaml)."""
    import yaml
    from pathlib import Path
    p = Path(cfg.get("_root", ".")) / cfg.get("supply_chain_file", "supply_chain.yaml")
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return {str(k).upper(): [str(c).upper() for c in (v or [])] for k, v in data.items()}


class TaiwanSalesSource(Source):
    name = "taiwan_sales"

    def __init__(self, cfg, scfg, db_path, http=None):
        super().__init__(cfg, scfg, db_path, http)
        self.db.execute("""CREATE TABLE IF NOT EXISTS tw_sales(ticker TEXT, ym TEXT, revenue REAL, yoy REAL,
                           PRIMARY KEY(ticker, ym))""")
        self.watch = set(load_supply_chain(cfg))

    def fetch(self, date, universe, panel=None, llm=None):
        d = pd.Timestamp(date)
        watch = self.watch | {t for t in universe if t.endswith((".TW", ".TWO"))}
        events, errors = [], []
        for suffix, url in TW_FEEDS.items():
            try:
                rows = self.http.json(self.scfg.get(f"url{suffix.lower().replace('.', '_')}", url))
            except Exception as e:
                errors.append(str(e))
                continue
            for row in rows or []:
                code = str(_field(row, "公司代號") or "").strip()
                ym = str(_field(row, "資料年月") or "").strip()
                cur = _f(next((v for k, v in row.items() if "當月營收" in k and "去年" not in k), None))
                last = _f(_field(row, "去年當月營收"))
                tk = f"{code}{suffix}"
                if not code or not ym or cur is None or tk not in watch:
                    continue
                yoy = cur / last - 1 if last else None
                prior = [r[0] for r in self.db.execute(
                    "SELECT yoy FROM tw_sales WHERE ticker=? AND ym<? AND yoy IS NOT NULL ORDER BY ym DESC LIMIT 3",
                    (tk, ym))]
                self.db.execute("INSERT OR IGNORE INTO tw_sales VALUES(?,?,?,?)", (tk, ym, cur, yoy))
                if not self.first_time(f"{tk}|{ym}"):
                    continue
                accel = yoy - sum(prior) / len(prior) if yoy is not None and prior else None
                events.append(Event("monthly_sales", d, tk, dict(
                    ym=ym, revenue=cur, yoy=yoy, accel=accel, name=str(_field(row, "公司名稱") or ""),
                    source="taiwan_sales")))
        self.db.commit()
        if errors and len(errors) == len(TW_FEEDS):
            raise SourceError("; ".join(errors)[:200])
        return events


def classify_dart(report_nm: str):
    n = report_nm.replace(" ", "")
    if "정정" in n:
        return None                                       # corrections
    if "잠정" in n and "실적" in n:
        return "earnings"
    if "자기주식취득결정" in n:
        return "buyback"
    return None


class DartSource(Source):
    name = "dart"

    def __init__(self, cfg, scfg, db_path, http=None):
        super().__init__(cfg, scfg, db_path, http)
        self.watch = set(load_supply_chain(cfg))

    def _text(self, key, rcept_no) -> str:
        try:
            z = zipfile.ZipFile(io.BytesIO(self.http.content(f"{DART}/document.xml",
                                                             {"crtfc_key": key, "rcept_no": rcept_no})))
            raw = z.read(z.namelist()[0])
            for enc in ("utf-8", "euc-kr", "cp949"):
                try:
                    return strip_markup(raw.decode(enc))
                except UnicodeError:
                    continue
        except Exception:
            pass
        return ""

    def fetch(self, date, universe, panel=None, llm=None):
        key = os.environ.get("DART_API_KEY")
        if not key:
            raise SourceError("set DART_API_KEY (free at opendart.fss.or.kr)")
        d = pd.Timestamp(date)
        ymd = d.strftime("%Y%m%d")
        watch = self.watch | {t for t in universe if t.endswith((".KS", ".KQ"))}
        events, page = [], 1
        while True:
            j = self.http.json(f"{DART}/list.json", {"crtfc_key": key, "bgn_de": ymd, "end_de": ymd,
                                                     "page_no": page, "page_count": 100})
            if j.get("status") == "013":                  # no filings that day
                break
            if j.get("status") != "000":
                raise SourceError(f"DART status {j.get('status')}: {j.get('message')}")
            for it in j.get("list", []):
                suffix = {"Y": ".KS", "K": ".KQ"}.get(it.get("corp_cls"))
                kind = classify_dart(it.get("report_nm", ""))
                tk = f"{it.get('stock_code', '')}{suffix}" if suffix and it.get("stock_code") else None
                if not kind or not tk or tk not in watch or not self.first_time(it["rcept_no"]):
                    continue
                body = self._text(key, it["rcept_no"])
                events.append(Event(kind, d, tk, dict(text=f"{it['report_nm']}\n{body}", title=it["report_nm"],
                                                      company=it.get("corp_name", ""), source="dart")))
            if page >= int(j.get("total_page", 1)):
                break
            page += 1
        return events
