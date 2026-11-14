"""Minimal RSS/Atom headline collector for the herding-fade lab.

Ticker detection uses common press-release patterns: "(NASDAQ: ABCD)", "(TSXV: XYZ)", "$ABCD".
Swap in a paid timestamped news API when you move to intraday.
"""
import re
import xml.etree.ElementTree as ET

import pandas as pd
import requests

from ..data.events import Event

PAT = re.compile(r"\((?:NASDAQ|NYSE|NYSE American|TSX|TSXV|CSE|OTCQB|OTCQX|AMEX)\s*:\s*([A-Z.]{1,6})\)"
                 r"|\$([A-Z]{1,5})\b")


def detect_tickers(text: str) -> set:
    return {a or b for a, b in PAT.findall(text)}


def fetch_headlines(feeds, date, universe: set, user_agent="EdgeLab") -> list:
    day = pd.Timestamp(date).normalize()
    events = []
    for url in feeds:
        try:
            root = ET.fromstring(requests.get(url, timeout=20, headers={"User-Agent": user_agent}).content)
        except Exception:
            continue
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for it in items:
            title = it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or ""
            desc = it.findtext("description") or it.findtext("{http://www.w3.org/2005/Atom}summary") or ""
            when = it.findtext("pubDate") or it.findtext("{http://www.w3.org/2005/Atom}updated") or ""
            try:
                ts = pd.Timestamp(when).tz_localize(None) if when else day
            except Exception:
                ts = day
            if ts.normalize() != day:
                continue
            for tk in detect_tickers(f"{title} {desc}"):
                if not universe or tk in universe:
                    events.append(Event("headline", day, tk, dict(text=f"{title}\n{desc[:800]}", source=url)))
    return events
