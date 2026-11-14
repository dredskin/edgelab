"""Events are the common currency between data sources and signals.

Kinds used by the built-in signals:
  insider_buy     payload: cluster (int), first_time (bool), owners (list), value (float)
  earnings        payload: text (str)  -- press release / filing text
  spinoff_listed  payload: company (str)
  headline        payload: text (str), source (str)
  large_stake     payload: holder, ratio, prev_ratio, purpose
  buyback         payload: text
  monthly_sales   payload: ym, revenue, yoy, accel

Events are saved to the database as they arrive, so a signal can look back at earlier days (a spin-off
listed two weeks ago) and a re-run of the same day sees exactly what the first run saw.
"""
import dataclasses
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd


@dataclass(frozen=True)
class Event:
    kind: str
    date: pd.Timestamp
    ticker: str
    payload: dict = field(default_factory=dict, hash=False, compare=False)

    def not_after(self, session) -> "Event":
        """Events fetched on a non-trading day belong to the most recent session."""
        session = pd.Timestamp(session).normalize()
        return dataclasses.replace(self, date=session) if pd.Timestamp(self.date).normalize() > session else self


class EventStore:
    def __init__(self):
        self._by = defaultdict(list)

    def add(self, e: Event):
        self._by[(e.kind, pd.Timestamp(e.date).normalize())].append(e)

    def extend(self, events):
        for e in events:
            self.add(e)

    def on(self, kind: str, date) -> list:
        return list(self._by.get((kind, pd.Timestamp(date).normalize()), []))

    def all(self) -> list:
        return [e for v in self._by.values() for e in v]

    def count(self, kind=None, date=None) -> int:
        d = None if date is None else pd.Timestamp(date).normalize()
        return sum(len(v) for (k, when), v in self._by.items()
                   if (kind is None or k == kind) and (d is None or when == d))

    # ---------- persistence ----------
    def save(self, db) -> int:
        """Insert events not already stored (same kind, date, ticker and content). Returns how many were new."""
        new = 0
        for e in self.all():
            payload = json.dumps(e.payload, sort_keys=True, default=str, ensure_ascii=False)
            digest = hashlib.sha1(payload.encode()).hexdigest()[:16]
            cur = db.execute("INSERT OR IGNORE INTO events(kind, date, ticker, payload, digest) VALUES(?,?,?,?,?)",
                             (e.kind, pd.Timestamp(e.date).strftime("%Y-%m-%d"), e.ticker, payload, digest))
            new += cur.rowcount
        db.commit()
        return new

    @classmethod
    def load(cls, db, since) -> "EventStore":
        store = cls()
        for kind, date, ticker, payload in db.execute(
                "SELECT kind, date, ticker, payload FROM events WHERE date >= ? ORDER BY id",
                (pd.Timestamp(since).strftime("%Y-%m-%d"),)):
            store.add(Event(kind, pd.Timestamp(date), ticker, json.loads(payload)))
        return store
