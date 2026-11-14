"""Broker adapters: Alpaca (REST) and Interactive Brokers (TWS / IB Gateway). Paper trading by default.

Safety rules (enforced in code, not just docs), shared by every broker:
  - Live trading is refused unless broker.allow_live is true (see each adapter's paper check).
  - Only ever closes positions EdgeLab itself opened (tracked per broker in SQLite), never your other holdings.
  - Whole-share market orders, day time-in-force; shorts only if broker.allow_shorts is true.
  - dry_run prints what it would send without sending anything.

Pick one with broker.provider: alpaca | ibkr.
"""
import math
import os
import sqlite3
import uuid

import pandas as pd
import requests

from . import markets

PAPER_URL = "https://paper-api.alpaca.markets"


class BrokerSafetyError(Exception):
    pass


class BrokerBase:
    name = "base"

    def __init__(self, cfg: dict, db_path: str):
        b = cfg.get("broker", {})
        self.allow_live = b.get("allow_live", False)
        self.allow_shorts = b.get("allow_shorts", False)
        self.min_order = b.get("min_order_dollars", 100)
        self.dry_run = b.get("dry_run", False)
        self.base_ccy = "USD"
        self.extra_tradable = set(b.get("extra_tradable", []))
        self.lot_overrides = {k.upper(): int(v) for k, v in (b.get("lot_sizes") or {}).items()}
        self.db = sqlite3.connect(db_path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS broker_positions(
            ticker TEXT, qty INTEGER, side TEXT, signal TEXT, opened TEXT, exit_on TEXT,
            order_id TEXT, state TEXT DEFAULT 'open', broker TEXT DEFAULT 'alpaca')""")
        self.db.execute("CREATE TABLE IF NOT EXISTS broker_runs(date TEXT, broker TEXT, PRIMARY KEY(date, broker))")
        self.db.execute("""CREATE TABLE IF NOT EXISTS broker_equity(date TEXT, broker TEXT, equity REAL,
                           PRIMARY KEY(date, broker))""")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(broker_positions)")}
        if "broker" not in cols:
            self.db.execute("ALTER TABLE broker_positions ADD COLUMN broker TEXT DEFAULT 'alpaca'")
        self.db.commit()

    # ---- implemented by each broker ----
    def account(self) -> dict: ...
    def equity(self) -> float: ...
    def positions(self) -> dict: ...                      # {symbol: {"qty": float}}
    def _submit(self, symbol, qty, side) -> dict: ...     # returns {"id": ...}

    # ---- guards against doing the same thing twice, and against trading through a bad month ----
    def already_ran(self, session) -> bool:
        d = pd.Timestamp(session).strftime("%Y-%m-%d")
        return self.db.execute("SELECT 1 FROM broker_runs WHERE date=? AND broker=?", (d, self.name)).fetchone() is not None

    def mark_ran(self, session):
        self.db.execute("INSERT OR IGNORE INTO broker_runs VALUES(?,?)", (pd.Timestamp(session).strftime("%Y-%m-%d"), self.name))
        self.db.commit()

    def record_equity(self, session, equity: float):
        self.db.execute("INSERT OR REPLACE INTO broker_equity VALUES(?,?,?)",
                        (pd.Timestamp(session).strftime("%Y-%m-%d"), self.name, float(equity)))
        self.db.commit()

    def month_to_date(self, session) -> float:
        """Return of the real broker account this month, from the snapshots record_equity() has taken."""
        d = pd.Timestamp(session)
        rows = self.db.execute("SELECT date, equity FROM broker_equity WHERE broker=? AND date<=? ORDER BY date",
                               (self.name, d.strftime("%Y-%m-%d"))).fetchall()
        if not rows:
            return 0.0
        month_start = d.strftime("%Y-%m-01")
        before = [e for dt, e in rows if dt < month_start]
        base = before[-1] if before else rows[0][1]
        return rows[-1][1] / base - 1 if base else 0.0

    # ---- shared bookkeeping ----
    def managed(self) -> list:
        cur = self.db.execute("""SELECT rowid, ticker, qty, side, signal, opened, exit_on FROM broker_positions
                                 WHERE state='open' AND broker=?""", (self.name,))
        cols = ["rowid", "ticker", "qty", "side", "signal", "opened", "exit_on"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def supports(self, ticker: str) -> bool:
        m = markets.market(ticker)
        return m["tradable"] or m["suffix"] in self.extra_tradable

    def lot(self, ticker: str) -> int:
        return self.lot_overrides.get(ticker.upper(), markets.market(ticker)["lot"])

    def price_in_base(self, ticker, px_local, fx: dict):
        """Convert a quoted local price (e.g. pence, yen) to the account's base currency."""
        m = markets.market(ticker)
        rate = 1.0 if m["ccy"] == self.base_ccy else (fx or {}).get(m["ccy"])
        if rate is None or not px_local or not math.isfinite(px_local):
            return None
        return px_local / m["divisor"] * rate

    def managed_gross(self, prices: dict, equity: float, fx: dict = None) -> float:
        if equity <= 0:
            return 0.0
        tot = 0.0
        for p in self.managed():
            pb = self.price_in_base(p["ticker"], prices.get(p["ticker"]), fx)
            tot += p["qty"] * (pb or 0)
        return tot / equity

    def close_expired(self, today) -> list:
        """Exit EdgeLab positions whose holding horizon has elapsed (never more shares than actually held)."""
        today = pd.Timestamp(today).strftime("%Y-%m-%d")
        held = None if self.dry_run else self.positions()
        done = []
        for p in self.managed():
            if p["exit_on"] > today:
                continue
            side = "sell" if p["side"] == "buy" else "buy"
            live_qty = p["qty"] if held is None else abs(int(float(held.get(p["ticker"], {}).get("qty", 0))))
            qty = min(p["qty"], live_qty)
            if qty > 0:
                self._submit(p["ticker"], qty, side)
            self.db.execute("UPDATE broker_positions SET state='closed' WHERE rowid=?", (p["rowid"],))
            done.append((p["ticker"], qty, side))
        self.db.commit()
        return done

    def execute(self, orders, prices: dict, equity: float, today, fx: dict = None) -> list:
        """orders: [(Pick, weight)]. prices are LOCAL quotes; fx maps currency -> base-currency rate.
        Sizes in the account's base currency, rounds down to whole board lots."""
        sent = []
        for pick, w in orders:
            tk = pick.ticker
            if not self.supports(tk):
                print(f"  skip {tk}: {markets.market(tk)['name']} is research-only for {self.name}")
                continue
            px = self.price_in_base(tk, prices.get(tk), fx)
            if not px or px <= 0:
                print(f"  skip {tk}: no price or no {markets.currency(tk)}->{self.base_ccy} rate")
                continue
            side = "buy" if w > 0 else "sell"
            if side == "sell" and not self.allow_shorts:
                print(f"  skip short {tk} (broker.allow_shorts is false)")
                continue
            amount = abs(w) * equity
            lot = self.lot(tk)
            qty = int(amount // (px * lot)) * lot
            if amount < self.min_order or qty < 1:
                continue
            try:
                o = self._submit(tk, qty, side)
            except Exception as e:                       # one bad contract must not stop the rest
                print(f"  order failed {tk}: {e}")
                continue
            exit_on = (pd.Timestamp(today) + pd.offsets.BDay(pick.horizon)).strftime("%Y-%m-%d")
            self.db.execute("""INSERT INTO broker_positions(ticker,qty,side,signal,opened,exit_on,order_id,broker)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (pick.ticker, qty, side, pick.signal, pd.Timestamp(today).strftime("%Y-%m-%d"),
                             exit_on, str(o.get("id", "")), self.name))
            sent.append((pick.ticker, qty, side))
        self.db.commit()
        return sent


# ============================== Alpaca ==============================
class AlpacaBroker(BrokerBase):
    """Keys: APCA_API_KEY_ID and APCA_API_SECRET_KEY (from your Alpaca paper account)."""
    name = "alpaca"

    def __init__(self, cfg: dict, db_path: str, session=None):
        super().__init__(cfg, db_path)
        b = cfg.get("broker", {})
        self.base = b.get("base_url", PAPER_URL).rstrip("/")
        if "paper" not in self.base and not self.allow_live:
            raise BrokerSafetyError(f"{self.base} is not a paper endpoint; set broker.allow_live to override")
        self.s = session or requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": os.environ.get("APCA_API_KEY_ID", ""),
                               "APCA-API-SECRET-KEY": os.environ.get("APCA_API_SECRET_KEY", "")})

    def supports(self, ticker: str) -> bool:
        return markets.market(ticker)["suffix"] == ""      # Alpaca trades US listings only

    def _req(self, method, path, **kw):
        r = self.s.request(method, f"{self.base}{path}", timeout=20, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {path} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.text else {}

    def account(self) -> dict:
        a = self._req("GET", "/v2/account")
        return {"id": a.get("account_number", ""), "equity": float(a["equity"]), "cash": float(a["cash"]),
                "status": a.get("status", "")}

    def equity(self) -> float:
        return self.account()["equity"]

    def positions(self) -> dict:
        return {p["symbol"]: {"qty": float(p["qty"])} for p in self._req("GET", "/v2/positions")}

    def _submit(self, symbol, qty, side):
        body = {"symbol": symbol, "qty": str(qty), "side": side, "type": "market", "time_in_force": "day",
                "client_order_id": f"edgelab-{uuid.uuid4().hex[:16]}"}
        if self.dry_run:
            print(f"  [dry run] {side.upper()} {qty} {symbol}")
            return {"id": "dry-run"}
        return self._req("POST", "/v2/orders", json=body)


# ========================= Interactive Brokers =========================
IB_PAPER_PORTS = {7497, 4002}        # TWS paper, IB Gateway paper
IB_LIVE_PORTS = {7496, 4001}


def ib_contract_spec(ticker: str) -> dict:
    m = markets.market(ticker)
    spec = dict(symbol=markets.ib_symbol(ticker), exchange="SMART", currency=m["ccy"])
    if m["exch"]:
        spec["primaryExchange"] = m["exch"]
    return spec


class IBKRBroker(BrokerBase):
    """Talks to TWS or IB Gateway running on YOUR machine (pip install ib_async).

    Two independent paper checks, both required unless broker.allow_live is true:
      1. the API port is a paper port (7497 TWS / 4002 Gateway), and
      2. the connected account id starts with "DU" (IB's paper-account prefix).
    """
    name = "ibkr"

    def __init__(self, cfg: dict, db_path: str, ib=None):
        super().__init__(cfg, db_path)
        c = cfg.get("broker", {}).get("ibkr", {})
        self.host, self.port = c.get("host", "127.0.0.1"), int(c.get("port", 7497))
        self.client_id = int(c.get("client_id", 17))
        self.base_ccy = c.get("base_currency", "USD").upper()
        if not self.allow_live and self.port not in IB_PAPER_PORTS:
            raise BrokerSafetyError(f"port {self.port} is not an IB paper port {sorted(IB_PAPER_PORTS)}; "
                                    "set broker.allow_live to override")
        self.ib = ib
        if self.ib is None:
            from ib_async import IB
            self.ib = IB()
        if not self.ib.isConnected():
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=15)
        accts = list(self.ib.managedAccounts())
        if not accts:
            raise BrokerSafetyError("IB returned no accounts")
        self.acct = c.get("account") or accts[0]
        if self.acct not in accts:
            raise BrokerSafetyError(f"account {self.acct} not available on this connection")
        if not self.allow_live and not self.acct.startswith("DU"):
            raise BrokerSafetyError(f"account {self.acct} is not a paper account (paper ids start with DU)")

    def _summary(self) -> dict:
        out = {}
        for v in self.ib.accountSummary(self.acct):
            if v.currency in (self.base_ccy, "BASE", ""):
                try:
                    out[v.tag] = float(v.value)
                except ValueError:
                    out[v.tag] = v.value
        return out

    def account(self) -> dict:
        s = self._summary()
        return {"id": self.acct, "equity": float(s.get("NetLiquidation", 0)), "cash": float(s.get("TotalCashValue", 0)),
                "status": "paper" if self.acct.startswith("DU") else "LIVE"}

    def equity(self) -> float:
        return self.account()["equity"]

    def positions(self) -> dict:
        out = {}
        for p in self.ib.positions(self.acct):
            c = p.contract
            out[markets.from_ib(c.symbol, c.currency, c.primaryExchange)] = {"qty": float(p.position)}
        return out

    def _submit(self, symbol, qty, side):
        if self.dry_run:
            print(f"  [dry run] {side.upper()} {qty} {symbol}")
            return {"id": "dry-run"}
        from ib_async import MarketOrder, Stock
        spec = ib_contract_spec(symbol)
        contract = Stock(spec["symbol"], spec["exchange"], spec["currency"],
                         primaryExchange=spec.get("primaryExchange", ""))
        if not self.ib.qualifyContracts(contract):
            raise RuntimeError(f"IB could not identify contract for {symbol}")
        order = MarketOrder(side.upper(), qty, tif="DAY", account=self.acct, orderRef="edgelab")
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)
        return {"id": trade.order.orderId, "status": trade.orderStatus.status}

    def close(self):
        if self.ib.isConnected():
            self.ib.disconnect()


def make_broker(cfg: dict, db_path: str):
    provider = cfg.get("broker", {}).get("provider", "alpaca").lower()
    if provider == "alpaca":
        return AlpacaBroker(cfg, db_path)
    if provider in ("ibkr", "ib", "interactive_brokers"):
        return IBKRBroker(cfg, db_path)
    raise ValueError(f"unknown broker.provider {provider!r}")
