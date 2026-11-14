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


class Signal(ABC):
    name = "base"
    lab = False             # lab signals face a stricter promotion bar

    def __init__(self, params: dict):
        self.p = params

    @abstractmethod
    def generate(self, ctx: Context) -> list:
        ...
