"""United States: SEC EDGAR (Form 4 insider buys, 8-K earnings, Form 10 spin-offs)."""
from ..data.edgar import EdgarClient
from .base import Source


class EdgarSource(Source):
    name = "edgar"

    def __init__(self, cfg, scfg, db_path, http=None):
        super().__init__(cfg, scfg, db_path, http)
        self.client = EdgarClient(cfg["sec"]["user_agent"], db_path)

    def fetch(self, date, universe, panel=None, llm=None):
        us = {t for t in universe if "." not in t}
        if not us:
            return []
        ev = self.client.insider_events(date, us)
        ev += self.client.earnings_events(date, us)
        ev += self.client.spinoff_events(date, panel)
        return ev
