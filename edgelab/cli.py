"""EdgeLab command line.

  edgelab demo                 run the full system on a synthetic market (no keys, no internet)
  edgelab daily [--date D]     live paper-trading day: pull SEC filings + prices, record picks, print orders
  edgelab daily --execute      ...and send them to your broker's PAPER account (Alpaca or Interactive Brokers) (exits expired positions first)
  edgelab broker               broker paper account and EdgeLab-managed positions
  edgelab report               signal leaderboard and status changes
  edgelab serve                start the dashboard / API (http://localhost:8000)
"""
import argparse
import json
from pathlib import Path

import pandas as pd

from .config import load_config


def build_signals(cfg, demo=False):
    from .lab.global_signals import Buyback, LargeStake, SupplyChainRead
    from .lab.herding_fade import HerdingFade
    from .signals.earnings_drift import EarningsDrift
    from .signals.insider import InsiderCluster
    from .signals.spinoff import SpinoffOrphan
    from .sources.asia import load_supply_chain
    sig, lab = cfg["signals"], cfg["lab"]
    on = lambda block, k: block.get(k, {}).get("enabled")
    out = []
    # Core (US) signals. In the synthetic demo there is one market, so no scope filter.
    scope = {} if demo else {"scope": "us"}
    if on(sig, "insider_cluster"):
        out.append(InsiderCluster({**sig["insider_cluster"], **scope}))
    if on(sig, "earnings_drift"):
        out.append(EarningsDrift({**sig["earnings_drift"], **scope}))
    if on(sig, "spinoff_orphan"):
        out.append(SpinoffOrphan(sig["spinoff_orphan"]))
    # Lab signals: stricter promotion bar.
    if on(lab, "herding_fade"):
        out.append(HerdingFade(lab["herding_fade"]))
    if demo:
        from .synthetic import Decoy
        return out + [Decoy(k, 100 + k) for k in range(3)]
    # International versions are separate tournament entries, judged on their own record.
    if on(lab, "earnings_drift_intl"):
        out.append(EarningsDrift({**lab["earnings_drift_intl"], "name": "earnings_drift_intl", "lab": True, "scope": "intl"}))
    if on(lab, "insider_intl"):
        out.append(InsiderCluster({**lab["insider_intl"], "name": "insider_intl", "lab": True, "scope": "intl"}))
    if on(lab, "large_stake"):
        out.append(LargeStake(lab["large_stake"]))
    if on(lab, "buyback"):
        out.append(Buyback(lab["buyback"]))
    if on(lab, "supply_chain"):
        out.append(SupplyChainRead(lab["supply_chain"], load_supply_chain(cfg)))
    return out


def build_sources(cfg, db):
    from .sources.announcements import AnnouncementSource
    from .sources.asia import DartSource, TaiwanSalesSource
    from .sources.japan import EdinetSource, TdnetSource
    from .sources.us import EdgarSource
    table = {"edgar": EdgarSource, "edinet": EdinetSource, "tdnet": TdnetSource, "taiwan_sales": TaiwanSalesSource,
             "dart": DartSource, "announcements": AnnouncementSource}
    return [cls(cfg, cfg["sources"][k], db) for k, cls in table.items() if cfg.get("sources", {}).get(k, {}).get("enabled")]


def cmd_demo(cfg, args):
    from .llm.client import MockLLM
    from .runner import Runner, performance
    from .synthetic import build_world

    db = Path(args.db or "demo.db")
    db.unlink(missing_ok=True)
    panel, events, truth = build_world(seed=args.seed)
    cutoff = panel.dates[150]
    run = Runner(cfg, build_signals(cfg, demo=True), MockLLM(cutoff), str(db))
    print(f"Synthetic market: {panel.close.shape[1]} stocks x {len(panel.dates)} days; LLM cutoff {cutoff.date()}")
    for i in range(1, len(panel.dates)):
        run.day(i, panel, events)
        if i % 150 == 0:
            print(f"  day {i}: equity {run.book.equity:.3f}, fragility {run.thermo.score:.2f}")
    df = run.tour.evaluate(panel.dates[-1])
    daily = pd.DataFrame(run.daily).set_index("date")
    ew = (1 + panel.close.pct_change(fill_method=None).mean(axis=1).fillna(0)).cumprod()
    perf, bench = performance(daily["equity"]), performance(ew.iloc[1:])
    _print_report(df, run.tour.log())
    print("\nPaper book vs equal-weight market")
    for k in ["total", "cagr", "vol", "sharpe", "max_dd"]:
        print(f"  {k:7s} book {perf[k]:+8.2%}   market {bench[k]:+8.2%}" if k != "sharpe"
              else f"  {k:7s} book {perf[k]:8.2f}   market {bench[k]:8.2f}")
    print(f"\nLLM contamination guard refused {run.llm.refused} pre-cutoff documents")
    s = daily.loc[truth["stress"][0].strftime("%Y-%m-%d"):truth["stress"][1].strftime("%Y-%m-%d")]
    print(f"Stress window: mean fragility {s.fragility.mean():.2f} (rest {daily.fragility.mean():.2f}), "
          f"mean risk multiplier {s.risk_mult.mean():.2f}")
    meta = dict(kind="demo", stress=[str(truth["stress"][0].date()), str(truth["stress"][1].date())],
                decay_from=str(truth["decay_from"].date()), cutoff=str(cutoff.date()), performance=perf, benchmark=bench,
                bench_curve=[round(float(v), 5) for v in (ew.iloc[1:] / ew.iloc[1]).values])
    run.tour.db.execute("INSERT OR REPLACE INTO meta VALUES('run', ?)", (json.dumps(meta),))
    run.tour.db.commit()
    print(f"\nSaved {db}. Run `edgelab serve --db {db}` to explore it in the dashboard.")


def _print_report(df, log):
    if df.empty:
        print("No signals evaluated yet.")
        return
    cols = ["name", "lab", "status", "n", "mean", "hit", "t", "q", "recent_t"]
    print("\nSignal leaderboard (excess return per trade, net of costs)")
    print(df[cols].sort_values("t", ascending=False).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    if not log.empty:
        print("\nStatus changes")
        print(log.to_string(index=False))


def cmd_report(cfg, args):
    from .tournament.engine import Tournament
    t = Tournament(args.db or cfg["storage"], cfg)
    _print_report(t.evaluate(pd.Timestamp.today()), t.log())


def cmd_daily(cfg, args):
    from .data.prices import load_yfinance
    from .sources.base import run_sources
    from .lab.headlines import fetch_headlines
    from .llm.client import LLMClient
    from .runner import Runner

    root = Path(cfg["_root"])
    db = args.db or str(root / cfg["storage"])
    date = pd.Timestamp(args.date or pd.Timestamp.today().normalize())
    uni = [l.split("#")[0].strip().upper() for l in (root / cfg["universe"]["tickers_file"]).read_text().splitlines()
           if l.split("#")[0].strip()]
    bench = cfg["universe"].get("benchmark")
    panel = load_yfinance(uni + ([bench] if bench else []),
                          start=date - pd.Timedelta(days=int(cfg["universe"]["history_days"] * 1.5)),
                          end=date + pd.Timedelta(days=1))
    llm = LLMClient(cfg)
    universe = set(uni)
    print(f"{date.date()}: {len(universe)} stocks in universe, {panel.close.shape[1]} with prices")
    events = run_sources(build_sources(cfg, db), date, universe, panel, db, llm)
    events.extend(fetch_headlines(cfg["lab"]["herding_fade"].get("feeds", []), date, universe))
    kinds = ["insider_buy", "earnings", "spinoff_listed", "large_stake", "buyback", "monthly_sales", "headline"]
    print("  events: " + ", ".join(f"{events.count(k)} {k}" for k in kinds if events.count(k)) or "  events: none")

    run = Runner(cfg, build_signals(cfg), llm, db, eval_every=1)
    # warm the risk layer on history, then run today
    i_today = len(panel.dates) - 1
    for i in range(max(1, i_today - 120), i_today):
        run.thermo.update(panel.upto(panel.dates[i]))

    broker, prices, equity, fx = None, panel.close.ffill().iloc[-1].dropna().to_dict(), None, {}
    if args.execute:
        from .broker import make_broker
        broker = make_broker(cfg, db)
        for tk, q, side in broker.close_expired(date):
            print(f"  exit: {side.upper()} {q} {tk} (horizon reached)")
        equity = broker.equity()
        from . import markets
        from .data.fx import load_fx
        fx = load_fx({markets.currency(t) for t in prices} | {markets.currency(p["ticker"]) for p in broker.managed()},
                     broker.base_ccy)
        held = broker.managed_gross(prices, equity, fx)
        if held:   # count positions already at the broker against today's gross limit
            run.book.positions.append(dict(ticker="_held", w=held, start=i_today + 1, end=i_today + 1, signal="_held"))
        print(f"{broker.name} account equity {equity:,.0f} {broker.base_ccy}; EdgeLab positions use {held:.0%} of it")

    orders = run.day(i_today, panel, events)
    print(f"Fragility {run.thermo.score:.2f} -> exposure x{run.daily[-1]['risk_mult']:.2f}")
    if not orders:
        print("No paper orders today (no promoted signal fired, or risk layer closed the gate).")
    for p, w in orders:
        print(f"  {'BUY ' if w > 0 else 'SELL'} {p.ticker:6s} {abs(w):.2%} of equity  [{p.signal}] {p.rationale}")
    if broker and orders:
        for tk, q, side in broker.execute(orders, prices, equity, date, fx):
            print(f"  sent to {broker.name}: {side.upper()} {q} {tk}")
    elif orders:
        print("(paper orders only; add --execute to send them to your broker's paper account)")
    if broker and hasattr(broker, "close"):
        broker.close()


def cmd_broker(cfg, args):
    from .broker import make_broker
    b = make_broker(cfg, args.db or str(Path(cfg["_root"]) / cfg["storage"]))
    a = b.account()
    print(f"{b.name} account {a['id']}: equity {a['equity']:,.2f}, cash {a['cash']:,.2f}, status {a['status']}")
    for p in b.managed():
        print(f"  {p['side'].upper():4s} {p['qty']:>6} {p['ticker']:6s} [{p['signal']}] opened {p['opened']}, exits {p['exit_on']}")
    if hasattr(b, "close"):
        b.close()


def cmd_serve(cfg, args):
    import uvicorn
    from .api import create_app
    uvicorn.run(create_app(cfg, args.db), host=args.host, port=args.port)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="edgelab")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo"); d.add_argument("--db"); d.add_argument("--seed", type=int, default=42)
    y = sub.add_parser("daily"); y.add_argument("--date"); y.add_argument("--db")
    y.add_argument("--execute", action="store_true", help="send orders to the configured broker's PAPER account")
    b = sub.add_parser("broker"); b.add_argument("--db")
    r = sub.add_parser("report"); r.add_argument("--db")
    s = sub.add_parser("serve"); s.add_argument("--db"); s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    {"demo": cmd_demo, "daily": cmd_daily, "report": cmd_report, "serve": cmd_serve,
     "broker": cmd_broker}[a.cmd](cfg, a)


if __name__ == "__main__":
    main()
