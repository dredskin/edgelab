"""US defense contract awards (Department of War / Defense daily contract announcements).

Every business day at 5 p.m. Eastern the Pentagon publishes one page, "Contracts For <date>", listing each award of
$7.5 million or more: "<Company>, <City>, <State>, was awarded a $<amount> ... contract for ...". The page is the
moment the award becomes public, so events are dated by the page's publication date, never the award date written
inside it (some entries say "Awarded Oct. 09" weeks earlier; that information was not public until the page).

Awardees are matched to universe stocks by company name (SEC's ticker -> company title list). Name matching is
deliberately conservative and is a known source of false matches (subsidiaries, look-alike names). The source only
fetches and shapes data; whether awards predict returns is the tournament's job.
"""
import re

import pandas as pd

from ..data.events import Event
from .announcements import parse_feed
from .base import Source, SourceError, strip_markup

FEED = "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945&max=10"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
AGENCIES = ("ARMY", "NAVY", "AIR FORCE", "SPACE FORCE", "MARINE CORPS", "DEFENSE LOGISTICS AGENCY",
            "DEFENSE HEALTH AGENCY", "MISSILE DEFENSE AGENCY", "U.S. SPECIAL OPERATIONS COMMAND",
            "DEFENSE INFORMATION SYSTEMS AGENCY", "DEFENSE THREAT REDUCTION AGENCY", "WASHINGTON HEADQUARTERS SERVICES",
            "U.S. TRANSPORTATION COMMAND", "DEFENSE ADVANCED RESEARCH PROJECTS AGENCY", "U.S. CYBER COMMAND")
SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "llc", "lp", "llp", "ltd", "limited",
            "the", "plc", "holdings", "holding", "group", "sa", "nv", "ag", "de", "del"}
# "... was awarded a $1,234,567 ..." ; also "has been awarded", "were awarded", "a maximum $12,000,000", "$4.7 million"
AWARD = re.compile(
    r"\s(?:was|were|has been|have been|is being|are being)\s+awarded\s+(?:an?\s+)?"
    r"(?:(?:not-to-exceed|maximum|ceiling)\s+)?\$(?P<amt>[\d,]+(?:\.\d+)?)\s*(?P<unit>million|billion)?",
    re.IGNORECASE)
# Words whose trailing period does not end a sentence ("The Boeing Co., Mesa, Arizona, was awarded ...")
ABBR = {"co", "inc", "corp", "ltd", "llc", "jr", "sr", "st", "mt", "ft", "no", "bros", "assn", "intl", "mfg", "dept",
        "u", "s", "d", "c", "l", "p"}


def _entry_start(text: str, pos: int, floor: int) -> int:
    """Start of the award entry that ends at `pos`: just after the last real sentence end (or agency heading)."""
    for m in reversed(list(re.finditer(r"\.\s+(?=[A-Z])", text[floor:pos]))):
        word = re.findall(r"([A-Za-z]+)$", text[floor:floor + m.start()])
        if not word or word[0].lower() not in ABBR:
            return floor + m.end()
    return floor


def norm(name: str) -> list:
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("&", " and ")).split()
    return [w for w in words if w not in SUFFIXES]


def money(amt: str, unit: str) -> float:
    v = float(amt.replace(",", ""))
    return v * {"million": 1e6, "billion": 1e9}.get((unit or "").lower(), 1.0)


def parse_awards(text: str) -> list:
    """All awards on one daily page: [{awardee, amount, modification, agency, sentence}]."""
    text = re.sub(r"\s+", " ", text)
    marks = sorted((m.end(), a) for a in AGENCIES for m in re.finditer(rf"\b{re.escape(a)}\b", text))
    out = []
    for m in AWARD.finditer(text):
        floor, agency = next(((pos, a) for pos, a in reversed(marks) if pos <= m.start()), (0, ""))
        lead = text[_entry_start(text, m.start(), floor):m.start()].strip()
        parts = [p.strip() for p in lead.split(",") if p.strip()]
        if len(parts) < 3 or len(lead) > 250:
            continue                                  # not the "<Company>, <City>, <State>," shape
        end = text.find(". ", m.end())
        sentence = text[m.start(): end if end > 0 else m.end() + 300]
        out.append(dict(awardee=", ".join(parts[:-2]), amount=money(m.group("amt"), m.group("unit")),
                        modification="modification" in sentence.lower(), agency=agency.title(),
                        sentence=sentence[:400]))
    return out


def match(awardee: str, names: dict) -> str:
    """Return the universe ticker whose company name the awardee starts with, or ''. `names` maps ticker -> title.
    Requires the issuer's first two significant words (or its single word, if 5+ letters) to lead the awardee."""
    a = norm(awardee)
    best = ""
    for tk, title in names.items():
        t = norm(title)
        if not t:
            continue
        core = t[:2] if len(t) >= 2 else (t if len(t[0]) >= 5 else [])
        if core and a[:len(core)] == core and len(core) > len(norm(names.get(best, ""))[:2]):
            best = tk
    return best


class DefenseContractsSource(Source):
    name = "defense_contracts"

    def _names(self, universe: set) -> dict:
        data = self.http.json(self.scfg.get("tickers_url", SEC_TICKERS))
        return {v["ticker"].upper(): v["title"] for v in data.values() if v["ticker"].upper() in universe}

    def fetch(self, date, universe, panel=None, llm=None):
        day = pd.Timestamp(date).normalize()
        us = {t for t in universe if "." not in t}
        if not us:
            return []
        try:
            items = parse_feed(self.http.content(self.scfg.get("feed", FEED)))
        except Exception as e:
            raise SourceError(f"contract feed unreadable: {type(e).__name__}") from None
        names = None
        events = []
        for it in items:
            try:
                ts = pd.Timestamp(it["when"])
                ts = (ts.tz_convert("America/New_York") if ts.tzinfo else ts).tz_localize(None)
            except Exception:
                continue                              # undated: cannot be placed in time safely
            key = it["id"] or it["link"] or it["title"]
            if ts.normalize() != day or self.seen(key):
                continue
            awards = parse_awards(strip_markup(it["body"], limit=400000))
            if not awards and it["link"]:             # feed carried a summary only: read the page itself
                page = self.http.content(it["link"])
                page = page.decode("utf-8", "replace") if isinstance(page, bytes) else str(page)
                awards = parse_awards(strip_markup(page, limit=400000))
            if names is None:
                names = self._names(us)
            totals = {}
            for a in awards:
                tk = match(a["awardee"], names)
                if not tk:
                    continue
                t = totals.setdefault(tk, dict(amount=0.0, new_amount=0.0, awards=0, awardees=set(), link=it["link"]))
                t["amount"] += a["amount"]
                t["new_amount"] += 0.0 if a["modification"] else a["amount"]
                t["awards"] += 1
                t["awardees"].add(a["awardee"])
            for tk, t in totals.items():
                events.append(Event("gov_contract", day, tk, {**t, "awardees": sorted(t["awardees"])[:5],
                                                              "title": it["title"][:120]}))
            self.mark(key)
        return events
