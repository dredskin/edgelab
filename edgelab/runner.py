"""Shared daily loop: settle -> mark book -> measure risk -> generate picks -> judge -> size."""
import numpy as np
import pandas as pd

from .portfolio import PaperBook
from .risk.fragility import FragilityThermometer
from .risk.sizing import DrawdownBudget
from .signals.base import Context
from .tournament.engine import Tournament


class Runner:
    def __init__(self, cfg, signals, llm, db_path, eval_every=5):
        self.cfg, self.signals, self.llm = cfg, signals, llm
        self.tour = Tournament(db_path, cfg)
        for s in signals:
            self.tour.register(s)
        f = cfg["risk"]["fragility"]
        self.thermo = FragilityThermometer(f["window"], f["calm_below"], f["floor"])
        self.dd = DrawdownBudget(cfg["risk"]["monthly_loss_cap"], cfg["risk"]["drawdown_halve_at"])
        self.book = PaperBook(cfg)
        self.eval_every = eval_every
        self.sig_stats = {}
        self.notes = {}
        self.daily = []          # per-day diagnostics for the dashboard

    def day(self, i, panel_full, events):
        date = panel_full.dates[i]
        panel = panel_full.upto(date)
        self.tour.settle(panel)
        rets = panel.close.iloc[-1] / panel.close.iloc[-2] - 1 if i > 0 else panel.close.iloc[-1] * 0
        self.book.step(i, date, rets.to_dict())

        frag = self.thermo.update(panel)
        risk_mult = self.thermo.multiplier() * self.dd.multiplier(date, self.book.equity)

        ctx = Context(asof=date, prices=panel, events=events, llm=self.llm, notes=self.notes)
        all_picks = []
        for s in self.signals:
            all_picks += s.generate(ctx)
        self.tour.record(all_picks, date)

        if i % self.eval_every == 0:
            df = self.tour.evaluate(date)
            self.sig_stats = {r["name"]: dict(mean=r["mean"], std=r["std"])
                              for _, r in df.iterrows() if r["status"] == "promoted"} if not df.empty else {}

        live = [p for p in all_picks if p.signal in self.sig_stats]
        orders = self.book.size(live, self.sig_stats, i, risk_mult)
        row = dict(date=date.strftime("%Y-%m-%d"), equity=self.book.equity, fragility=frag,
                   risk_mult=risk_mult, gross=self.book.gross(i + 1), picks=len(all_picks), orders=len(orders))
        self.daily.append(row)
        db = self.tour.db
        db.execute("INSERT OR REPLACE INTO daily VALUES(:date,:equity,:fragility,:risk_mult,:gross,:picks,:orders)", row)
        db.executemany("INSERT INTO orders VALUES(?,?,?,?,?)",
                       [(row["date"], p.ticker, p.signal, w, p.rationale) for p, w in orders])
        db.commit()
        return orders


def performance(equity: pd.Series) -> dict:
    r = equity.pct_change().dropna()
    if r.empty:
        return {}
    ann = (equity.iloc[-1] / equity.iloc[0]) ** (252 / len(r)) - 1
    vol = r.std() * np.sqrt(252)
    dd = (1 - equity / equity.cummax()).max()
    return dict(total=float(equity.iloc[-1] / equity.iloc[0] - 1), cagr=float(ann), vol=float(vol),
                sharpe=float(ann / vol) if vol else 0.0, max_dd=float(dd))
