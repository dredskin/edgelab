from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Pick:
    signal: str
    ticker: str
    direction: int          # +1 long, -1 short
    strength: float         # 0..1
    horizon: int            # trading days to hold
    rationale: str = ""


@dataclass
class Context:
    asof: object            # pd.Timestamp of today's close
    prices: object          # PricePanel sliced to asof (no look-ahead possible)
    events: object          # EventStore
    llm: object
    notes: dict = field(default_factory=dict)

    def skipped(self, error):
        """Count a document the LLM layer could not score (pre-cutoff, API failure, ...)."""
        k = type(error).__name__
        self.notes[k] = self.notes.get(k, 0) + 1


class Signal(ABC):
    name = "base"
    lab = False             # lab signals face a stricter promotion bar

    def __init__(self, params: dict):
        self.p = params
        # One class can run as several tournament entries (e.g. US vs international), each judged separately.
        self.name = params.get("name", self.name)
        self.lab = params.get("lab", self.lab)
        self.scope = params.get("scope", "all")        # all | us | intl

    def in_scope(self, ticker: str) -> bool:
        from ..markets import market
        us = market(ticker)["suffix"] == ""
        return self.scope == "all" or (self.scope == "us") == us

    @abstractmethod
    def generate(self, ctx: Context) -> list:
        ...
