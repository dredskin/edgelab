"""Universal announcement reader: any RSS/Atom feed, any language.

For markets without a clean free API (UK RNS, Hong Kong HKEXnews, Canadian newswires, Euronext, ASX...),
point EdgeLab at announcement feeds. Claude reads each item in its original language and classifies it into
an event the signals already understand: earnings, insider_buy, buyback, large_stake (or ignores it).

The LLM only extracts WHAT the announcement is. Whether that kind of event predicts returns is decided by
the tournament, like everything else.
"""
import xml.etree.ElementTree as ET

import pandas as pd

from ..data.events import Event
from ..lab.headlines import EXCH_SUFFIX, detect_tickers
from ..llm.client import ContaminationError
from .base import Source

ATOM = "{http://www.w3.org/2005/Atom}"
KINDS = {"earnings", "insider_buy", "buyback", "large_stake"}


def parse_feed(xml_bytes: bytes) -> list:
    root = ET.fromstring(xml_bytes)
    out = []
    for it in root.findall(".//item") or root.findall(f".//{ATOM}entry"):
        g = lambda *names: next((it.findtext(n) for n in names if it.findtext(n)), "")
        link = g("link") or (it.find(f"{ATOM}link").get("href", "") if it.find(f"{ATOM}link") is not None else "")
        out.append(dict(title=g("title", f"{ATOM}title"), body=g("description", f"{ATOM}summary", f"{ATOM}content"),
                        when=g("pubDate", f"{ATOM}updated", f"{ATOM}published"), link=link,
                        id=g("guid", f"{ATOM}id") or link))
    return out


class AnnouncementSource(Source):
    name = "announcements"

    def fetch(self, date, universe, panel=None, llm=None):
        if llm is None:
            return []
        day = pd.Timestamp(date).normalize()
        events = []
        for feed in self.scfg.get("feeds", []):
            feed = {"url": feed} if isinstance(feed, str) else feed
            try:
                items = parse_feed(self.http.content(feed["url"]))
            except Exception:
                continue
            for it in items:
                try:
                    ts = pd.Timestamp(it["when"]).tz_localize(None) if it["when"] else day
                except Exception:
                    ts = day
                if ts.normalize() != day or not self.first_time(it["id"] or it["title"]):
                    continue
                text = f"{it['title']}\n{it['body'][:3000]}"
                try:
                    c = llm.classify_announcement(text, day)
                except ContaminationError:
                    continue
                if c.get("kind") not in KINDS:
                    continue
                tickers = detect_tickers(text)
                if not tickers and c.get("symbol"):
                    suf = EXCH_SUFFIX.get(str(c.get("exchange", "")).upper(), feed.get("default_suffix"))
                    if suf is not None:
                        sym = str(c["symbol"]).upper().rstrip(".")
                        tickers = {(sym.zfill(4) if suf == ".HK" else sym) + suf}
                for tk in tickers:
                    if universe and tk not in universe:
                        continue
                    payload = dict(text=text, title=it["title"], source=feed["url"], language=c.get("language", ""))
                    if c["kind"] == "insider_buy":
                        payload.update(cluster=int(c.get("n_insiders") or 1), first_time=bool(c.get("first_purchase")))
                    events.append(Event(c["kind"], day, tk, payload))
        return events
