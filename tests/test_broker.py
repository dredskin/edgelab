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


# ---------------- Interactive Brokers ----------------
from types import SimpleNamespace as NS

from edgelab.broker import IBKRBroker, ib_contract_spec


class FakeIB:
    def __init__(self, accounts=("DU1234567",)):
        self.accounts, self.placed, self.connected = list(accounts), [], False

    def isConnected(self): return self.connected
    def connect(self, host, port, clientId, timeout): self.connected = True
    def disconnect(self): self.connected = False
    def managedAccounts(self): return self.accounts
    def sleep(self, s): pass
    def qualifyContracts(self, c): return [c]

    def accountSummary(self, acct):
        return [NS(tag="NetLiquidation", value="50000", currency="USD"),
                NS(tag="TotalCashValue", value="20000", currency="USD")]

    def positions(self, acct):
        return [NS(contract=NS(symbol="ABC", currency="USD", primaryExchange=""), position=300),
                NS(contract=NS(symbol="XYZ", currency="CAD", primaryExchange="VENTURE"), position=1000)]

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return NS(order=NS(orderId=len(self.placed)), orderStatus=NS(status="Submitted"))


def ibcfg(port=7497, **b):
    return {"broker": {"provider": "ibkr", "ibkr": {"port": port}, **b}}


def test_ibkr_refuses_live_port_and_live_account(tmp_path):
    with pytest.raises(BrokerSafetyError):
        IBKRBroker(ibcfg(7496), str(tmp_path / "a.db"), FakeIB())
    with pytest.raises(BrokerSafetyError):                   # paper port but a LIVE account id
        IBKRBroker(ibcfg(7497), str(tmp_path / "a.db"), FakeIB(accounts=["U9999999"]))


def test_ibkr_paper_orders_and_canadian_symbols(tmp_path):
    ib = FakeIB()
    b = IBKRBroker(ibcfg(), str(tmp_path / "a.db"), ib)
    assert b.account() == {"id": "DU1234567", "equity": 50000.0, "cash": 20000.0, "status": "paper"}
    assert b.positions() == {"ABC": {"qty": 300.0}, "XYZ.V": {"qty": 1000.0}}
    sent = b.execute([(Pick("insider_cluster", "XYZ.V", 1, 1, 20), 0.05)], {"XYZ.V": 0.50}, b.equity(), "2026-11-16", {"CAD": 0.72})
    assert sent == [("XYZ.V", 6944, "buy")]                 # $2,500 / (C$0.50 x 0.72)
    contract, order = ib.placed[0]
    assert (contract.symbol, contract.currency, contract.primaryExchange) == ("XYZ", "CAD", "VENTURE")
    assert (order.action, order.totalQuantity, order.account, order.orderRef) == ("BUY", 6944, "DU1234567", "edgelab")


def test_brokers_keep_separate_books(tmp_path):
    db = str(tmp_path / "s.db")
    a = AlpacaBroker(cfg(), db, FakeSession())
    a.execute([(Pick("insider_cluster", "ABC", 1, 1, 20), 0.05)], {"ABC": 10.0}, 100000, "2026-11-02")
    ib = IBKRBroker(ibcfg(), db, FakeIB())
    assert ib.managed() == [] and len(a.managed()) == 1


def test_contract_mapping():
    assert ib_contract_spec("SHOP.TO")["currency"] == "CAD"
    assert ib_contract_spec("BRK-B") == dict(symbol="BRK B", exchange="SMART", currency="USD")


# ---------------- worldwide ----------------
from edgelab import markets


def test_market_table():
    assert markets.split("7203.T") == ("7203", ".T")
    assert markets.split("6488.TWO") == ("6488", ".TWO")          # longest suffix wins over .TW
    assert markets.ib_symbol("0700.HK") == "700"
    assert markets.from_ib("700", "HKD", "SEHK") == "0700.HK"
    assert markets.market("005930.KS")["tradable"] is False


def test_ibkr_sizes_foreign_orders_in_base_currency_with_lots(tmp_path):
    ib = FakeIB()
    b = IBKRBroker(ibcfg(), str(tmp_path / "w.db"), ib)            # base USD, equity 50,000
    fx = {"JPY": 1 / 150, "GBP": 1.25}
    orders = [(Pick("x", "7203.T", 1, 1, 20), 0.05),                # $2,500 / (¥3,000 = $20) = 125 -> 100 (lot)
              (Pick("x", "VOD.L", 1, 1, 20), 0.05),                 # 80p = £0.80 = $1.00 -> 2,500 shares
              (Pick("x", "005930.KS", 1, 1, 20), 0.05),             # Korea: research-only, skipped
              (Pick("x", "SAP.DE", 1, 1, 20), 0.05)]                # no EUR rate -> skipped, not mis-sized
    sent = b.execute(orders, {"7203.T": 3000, "VOD.L": 80, "005930.KS": 70000, "SAP.DE": 200}, 50000, "2026-11-16", fx)
    assert sent == [("7203.T", 100, "buy"), ("VOD.L", 2500, "buy")]
    jp = ib.placed[0][0]
    assert (jp.symbol, jp.currency, jp.primaryExchange) == ("7203", "JPY", "TSEJ")


def test_alpaca_only_trades_us(tmp_path):
    b = AlpacaBroker(cfg(), str(tmp_path / "u.db"), FakeSession())
    assert b.supports("ABC") and not b.supports("7203.T")


def test_global_headline_detection():
    from edgelab.lab.headlines import detect_tickers
    assert detect_tickers("トヨタ（TYO: 7203）が発表; Tencent (HKEX: 700); BP (LSE: BP.)") == {"7203.T", "0700.HK", "BP.L"}
