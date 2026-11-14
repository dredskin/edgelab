"""Events are the common currency between data sources and signals.

Kinds used by the built-in signals:
  insider_buy     payload: cluster (int), first_time (bool), owners (list), value (float)
  earnings        payload: text (str)  -- press release / transcript text
  spinoff_listed  payload: parent (str)
  headline        payload: text (str), source (str)
"""
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd


@dataclass(frozen=True)
class Event:
    kind: str
    date: pd.Timestamp
    ticker: str
    payload: dict = field(default_factory=dict, hash=False, compare=False)


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

    def count(self, kind=None) -> int:
        return sum(len(v) for (k, _), v in self._by.items() if kind is None or k == kind)
