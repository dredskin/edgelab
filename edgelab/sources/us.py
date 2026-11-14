"""United States: SEC EDGAR (Form 4 insider buys, 8-K earnings, Form 10 spin-offs)."""
from .. import markets
from ..data.edgar import EdgarClient
from .base import Source, SourceError


class EdgarSource(Source):
    name = "edgar"

    def __init__(self, cfg, scfg, db_path, http=None, client=None):
        super().__init__(cfg, scfg, db_path, http)
        self.user_agent = cfg["sec"]["user_agent"]
        self.client = client or EdgarClient(self.user_agent, db_path)
        self.min_history = cfg.get("signals", {}).get("insider_cluster", {}).get("min_history_days", 365)

    def _check_contact(self):
        if "example.com" in self.user_agent:
            raise SourceError("edit sec.user_agent in config.yaml: SEC requires a real contact e-mail")

    def extra_tickers(self, date) -> set:
        try:
            self._check_contact()
            return self.client.spinoff_watch(date)
        except Exception:
            return set()

    def fetch(self, date, universe, panel=None, llm=None):
        self._check_contact()
        us = {t for t in universe if markets.suffix(t) == ""}
        if not us:
            return []
        ev = self.client.insider_events(date, us, min_history_days=self.min_history)
        ev += self.client.earnings_events(date, us)
        ev += self.client.spinoff_events(date, panel)
        return ev
