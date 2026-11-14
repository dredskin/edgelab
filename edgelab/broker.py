"""Alpaca paper-trading adapter.

Safety rules (enforced in code, not just docs):
  - Refuses any endpoint that is not Alpaca's PAPER API unless config broker.allow_live is true.
  - Only ever closes positions EdgeLab itself opened (tracked in SQLite), never your other holdings.
  - Whole-share market orders, day time-in-force; shorts only if broker.allow_shorts is true.
  - dry_run prints what it would send without sending anything.

Keys: set APCA_API_KEY_ID and APCA_API_SECRET_KEY (from your Alpaca paper account) in the environment.
"""
import math
import os
import sqlite3
import uuid

import pandas as pd
import requests

PAPER_URL = "https://paper-api.alpaca.markets"


class BrokerSafetyError(Exception):
    pass


class AlpacaBroker:
    def __init__(self, cfg: dict, db_path: str, session=None):
        b = cfg.get("broker", {})
        self.base = b.get("base_url", PAPER_URL).rstrip("/")
        if "paper" not in self.base and not b.get("allow_live", False):
            raise BrokerSafetyError(f"{self.base} is not a paper endpoint; set broker.allow_live to override")
        self.allow_shorts = b.get("allow_shorts", False)
        self.min_order = b.get("min_order_dollars", 100)
        self.dry_run = b.get("dry_run", False)
        self.s = session or requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": os.environ.get("APCA_API_KEY_ID", ""),
                               "APCA-API-SECRET-KEY": os.environ.get("APCA_API_SECRET_KEY", "")})
        self.db = sqlite3.connect(db_path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS broker_positions(
            ticker TEXT, qty INTEGER, side TEXT, signal TEXT, opened TEXT, exit_on TEXT,
            order_id TEXT, state TEXT DEFAULT 'open')""")
        self.db.commit()

    # ---------- REST ----------
    def _req(self, method, path, **kw):
        r = self.s.request(method, f"{self.base}{path}", timeout=20, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {path} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.text else {}

    def account(self) -> dict:
        return self._req("GET", "/v2/account")

    def equity(self) -> float:
        return float(self.account()["equity"])

    def positions(self) -> dict:
        return {p["symbol"]: p for p in self._req("GET", "/v2/positions")}

    def managed(self) -> list:
        cur = self.db.execute("SELECT rowid, ticker, qty, side, signal, opened, exit_on FROM broker_positions WHERE state='open'")
        cols = ["rowid", "ticker", "qty", "side", "signal", "opened", "exit_on"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def managed_gross(self, prices: dict, equity: float) -> float:
        if equity <= 0:
            return 0.0
        return sum(p["qty"] * prices.get(p["ticker"], 0) for p in self.managed()) / equity

    # ---------- actions ----------
    def _submit(self, symbol, qty, side):
        body = {"symbol": symbol, "qty": str(qty), "side": side, "type": "market", "time_in_force": "day",
                "client_order_id": f"edgelab-{uuid.uuid4().hex[:16]}"}
        if self.dry_run:
            print(f"  [dry run] {side.upper()} {qty} {symbol}")
            return {"id": "dry-run", **body}
        return self._req("POST", "/v2/orders", json=body)

    def close_expired(self, today) -> list:
        """Exit EdgeLab positions whose holding horizon has elapsed."""
        today = pd.Timestamp(today).strftime("%Y-%m-%d")
        held = self.positions() if not self.dry_run else {}
        done = []
        for p in self.managed():
            if p["exit_on"] > today:
                continue
            side = "sell" if p["side"] == "buy" else "buy"
            live_qty = abs(int(float(held.get(p["ticker"], {}).get("qty", p["qty"])))) if held else p["qty"]
            qty = min(p["qty"], live_qty)
            if qty > 0:
                self._submit(p["ticker"], qty, side)
            self.db.execute("UPDATE broker_positions SET state='closed' WHERE rowid=?", (p["rowid"],))
            done.append((p["ticker"], qty, side))
        self.db.commit()
        return done

    def execute(self, orders, prices: dict, equity: float, today) -> list:
        """orders: [(Pick, weight)] from the paper book. Converts weights to whole shares."""
        sent = []
        for pick, w in orders:
            px = prices.get(pick.ticker)
            if not px or px <= 0 or not math.isfinite(px):
                continue
            side = "buy" if w > 0 else "sell"
            if side == "sell" and not self.allow_shorts:
                print(f"  skip short {pick.ticker} (broker.allow_shorts is false)")
                continue
            dollars = abs(w) * equity
            qty = int(dollars // px)
            if dollars < self.min_order or qty < 1:
                continue
            o = self._submit(pick.ticker, qty, side)
            exit_on = (pd.Timestamp(today) + pd.offsets.BDay(pick.horizon)).strftime("%Y-%m-%d")
            self.db.execute("INSERT INTO broker_positions(ticker,qty,side,signal,opened,exit_on,order_id) VALUES(?,?,?,?,?,?,?)",
                            (pick.ticker, qty, side, pick.signal, pd.Timestamp(today).strftime("%Y-%m-%d"),
                             exit_on, o.get("id", "")))
            sent.append((pick.ticker, qty, side))
        self.db.commit()
        return sent
