import json

import pytest

from edgelab.broker import AlpacaBroker, BrokerSafetyError
from edgelab.signals.base import Pick


class FakeResp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data
        self.text = json.dumps(data)

    def json(self):
        return self._d


class FakeSession:
    def __init__(self):
        self.headers, self.sent = {}, []

    def request(self, method, url, **kw):
        if url.endswith("/v2/account"):
            return FakeResp(200, {"equity": "100000", "cash": "100000", "status": "ACTIVE"})
        if url.endswith("/v2/positions"):
            return FakeResp(200, [{"symbol": "ABC", "qty": "100"}])
        if url.endswith("/v2/orders"):
            self.sent.append(kw["json"])
            return FakeResp(200, {"id": f"o{len(self.sent)}"})
        return FakeResp(404, {})


def cfg(**b):
    return {"broker": {"base_url": "https://paper-api.alpaca.markets", **b}}


def test_refuses_live_endpoint():
    with pytest.raises(BrokerSafetyError):
        AlpacaBroker({"broker": {"base_url": "https://api.alpaca.markets"}}, ":memory:", FakeSession())


def test_sizes_whole_shares_and_skips_shorts(tmp_path):
    s = FakeSession()
    b = AlpacaBroker(cfg(), str(tmp_path / "b.db"), s)
    orders = [(Pick("insider_cluster", "ABC", 1, 1, 20), 0.05), (Pick("herding_fade", "XYZ", -1, 1, 3), -0.05)]
    sent = b.execute(orders, {"ABC": 9.99, "XYZ": 5.0}, b.equity(), "2026-11-16")
    assert sent == [("ABC", 500, "buy")]                    # $5,000 / $9.99 -> 500 shares; short skipped
    assert s.sent[0]["symbol"] == "ABC" and s.sent[0]["qty"] == "500"


def test_closes_only_its_own_positions_after_horizon(tmp_path):
    s = FakeSession()
    b = AlpacaBroker(cfg(), str(tmp_path / "b.db"), s)
    b.execute([(Pick("insider_cluster", "ABC", 1, 1, 20), 0.05)], {"ABC": 10.0}, 100000, "2026-11-02")
    assert b.close_expired("2026-11-20") == []              # horizon not reached
    assert b.close_expired("2026-12-01") == [("ABC", 100, "sell")]   # capped at shares actually held
    assert b.managed() == []
