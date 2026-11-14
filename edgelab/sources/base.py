"""Data-source framework. A Source turns one day of some feed into Events; nothing else.

Rules every source follows:
  - An event is dated when the information became PUBLIC (or when we first saw it), never the period it
    describes. Taiwan's October sales published on Nov 10 are a Nov 10 event. This is the look-ahead guard.
  - API keys come from environment variables only.
  - A failing source is logged and skipped; it never stops the daily run.
"""
import io
import re
import time

import pandas as pd
import requests

from .. import store
from ..data.events import EventStore


class SourceError(Exception):
    pass


class Http:
    def __init__(self, user_agent="EdgeLab research", min_interval=0.25, session=None):
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": user_agent})
        self.min_interval, self._last = min_interval, 0.0

    def get(self, url, params=None):
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        r = self.s.get(url, params=params, timeout=40)
        self._last = time.time()
        r.raise_for_status()
        return r

    def json(self, url, params=None):
        return self.get(url, params).json()

    def content(self, url, params=None) -> bytes:
        return self.get(url, params).content


class Source:
    name = "base"

    def __init__(self, cfg: dict, scfg: dict, db_path: str, http=None):
        self.cfg, self.scfg = cfg, scfg or {}
        self.http = http or Http(cfg.get("sec", {}).get("user_agent", "EdgeLab research"))
        self.db = store.connect(db_path)

    def first_time(self, key: str) -> bool:
        """True exactly once per key: lets a source emit only genuinely new items."""
        cur = self.db.execute("INSERT OR IGNORE INTO source_seen VALUES(?,?)", (self.name, key))
        self.db.commit()
        return cur.rowcount == 1

    def seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM source_seen WHERE source=? AND key=?", (self.name, key)).fetchone() is not None

    def mark(self, key: str):
        self.db.execute("INSERT OR IGNORE INTO source_seen VALUES(?,?)", (self.name, key))
        self.db.commit()

    def extra_tickers(self, date) -> set:
        """Stocks outside universe.txt whose prices this source needs (e.g. newly listed spin-offs)."""
        return set()

    def fetch(self, date, universe: set, panel=None, llm=None) -> list:
        raise NotImplementedError


def pdf_text(data: bytes, limit=15000) -> str:
    try:
        from pypdf import PdfReader
        out = []
        for page in PdfReader(io.BytesIO(data)).pages[:12]:
            out.append(page.extract_text() or "")
            if sum(map(len, out)) > limit:
                break
        return re.sub(r"[ \t]+", " ", "\n".join(out))[:limit]
    except Exception:
        return ""


def strip_markup(raw: str, limit=15000) -> str:
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = re.sub(r"&[a-zA-Z#0-9]+;", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()[:limit]


def run_sources(sources, date, universe: set, panel, db_path: str, llm=None) -> EventStore:
    """Run every source, log health per source, and return one EventStore."""
    db = store.connect(db_path)
    events = EventStore()
    d = pd.Timestamp(date).strftime("%Y-%m-%d")
    for s in sources:
        try:
            ev = s.fetch(date, universe, panel, llm)
            events.extend(ev)
            n, err = len(ev), ""
        except Exception as e:
            n, err = 0, f"{type(e).__name__}: {e}"[:300]
        db.execute("INSERT OR REPLACE INTO source_log VALUES(?,?,?,?)", (d, s.name, n, err))
        print(f"  source {s.name:14s} {n:4d} events" + (f"   ! {err}" if err else ""))
    db.commit()
    db.close()
    return events
