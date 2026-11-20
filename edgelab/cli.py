"""EdgeLab command line.

  edgelab demo                 run the full system on a synthetic market (no keys, no internet)
  edgelab daily [--date D]     one live paper-trading session: fetch prices and filings, record picks, size orders
  edgelab daily --execute      ...and send the orders to your broker's PAPER account (Alpaca or Interactive Brokers)
  edgelab backfill --days N    collect past insider purchases so "first purchase" flags can be trusted
  edgelab broker               broker paper account and EdgeLab-managed positions
  edgelab report               signal leaderboard and status changes (read-only)
  edgelab email-report [--dry-run]  e-mail the daily report (mail settings: config.yaml + environment variables)
  edgelab snapshot             write site/: read-only JSON and dashboard for Cloudflare Pages (no database copied)
  edgelab verify-access URL    fail unless Cloudflare Access protects the hosted site
  edgelab serve                start the dashboard / API (http://localhost:8000)
"""
import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

from .config import load_config

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


# ---------- wiring ----------
def db_path(cfg, args) -> str:
    return getattr(args, "db", None) or str(Path(cfg["_root"]) / cfg["storage"])


def read_universe(cfg) -> list:
    path = Path(cfg["_root"]) / cfg["universe"]["tickers_file"]
    out = []
    for line in path.read_text().splitlines():
        t = line.split("#")[0].strip().upper()
        if t and t not in out:
            out.append(t)
    return out


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
    # US-only lab entries from the cluster matrices. Another market would be a separate entry (e.g. *_intl).
    from .lab.patterns import CascadeRebound, GovContract, TaxLossRebound
    for key, cls in (("gov_contract", GovContract), ("cascade_rebound", CascadeRebound),
                     ("tax_loss_rebound", TaxLossRebound)):
        if on(lab, key):
            out.append(cls({**lab[key], "scope": "us"}))
    return out


def build_sources(cfg, db):
    from .sources.announcements import AnnouncementSource
    from .sources.asia import DartSource, TaiwanSalesSource
    from .sources.defense import DefenseContractsSource
    from .sources.japan import EdinetSource, TdnetSource
    from .sources.us import EdgarSource
    table = {"edgar": EdgarSource, "edinet": EdinetSource, "tdnet": TdnetSource, "taiwan_sales": TaiwanSalesSource,
             "dart": DartSource, "announcements": AnnouncementSource, "defense_contracts": DefenseContractsSource}
    return [cls(cfg, cfg["sources"][k], db) for k, cls in table.items() if cfg.get("sources", {}).get(k, {}).get("enabled")]


LEADERBOARD = ["name", "lab", "status", "n", "weeks", "mean", "hit", "t", "q", "next_review"]


def print_report(df, log):
    if df.empty or df["n"].isna().all():
        print("No signals evaluated yet.")
        return
    print("\nSignal leaderboard (excess return per trade, net of costs; t clustered by entry week)")
    print(df[LEADERBOARD].sort_values("t", ascending=False).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    if not log.empty:
        print("\nStatus changes")
        print(log.to_string(index=False))


# ---------- commands ----------
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
    print_report(df, run.tour.log())
    print("\nPaper book vs equal-weight market (synthetic: shows the machinery works, says nothing about real markets)")
    for k in ["total", "cagr", "vol", "sharpe", "max_dd"]:
        print(f"  {k:7s} book {perf[k]:+8.2%}   market {bench[k]:+8.2%}" if k != "sharpe"
              else f"  {k:7s} book {perf[k]:8.2f}   market {bench[k]:8.2f}")
    print(f"\nLLM contamination guard refused {run.llm.refused} pre-cutoff documents")
    s = daily.loc[truth["stress"][0].strftime("%Y-%m-%d"):truth["stress"][1].strftime("%Y-%m-%d")]
    print(f"Stress window: mean fragility {s.fragility.mean():.2f} (whole run {daily.fragility.mean():.2f}), "
          f"mean size multiplier for new positions {s.risk_mult.mean():.2f}")
    meta = dict(kind="demo", stress=[str(truth["stress"][0].date()), str(truth["stress"][1].date())],
                decay_from=str(truth["decay_from"].date()), cutoff=str(cutoff.date()), performance=perf, benchmark=bench,
                bench_curve=[round(float(v), 5) for v in (ew.iloc[1:] / ew.iloc[1]).values])
    run.db.execute("INSERT OR REPLACE INTO meta VALUES('run', ?)", (json.dumps(meta),))
    run.db.commit()
    print(f"\nSaved {db}. Run `edgelab serve --db {db}` to explore it in the dashboard.")


def cmd_report(cfg, args):
    from .tournament.engine import Tournament
    t = Tournament(db_path(cfg, args), cfg)
    print_report(t.leaderboard(), t.log())


def cmd_daily(cfg, args):
    from . import markets
    from .data.prices import load_yfinance
    from .lab.headlines import fetch_headlines
    from .live import run_session
    from .llm.client import LLMClient
    from .sources.base import run_sources
    from .tournament.engine import Tournament

    db = db_path(cfg, args)
    date = pd.Timestamp(args.date or pd.Timestamp.today()).normalize()
    universe = read_universe(cfg)
    if not universe:
        sys.exit(f"{cfg['universe']['tickers_file']} is empty: list the stocks you want EdgeLab to cover")
    sources = build_sources(cfg, db)

    # Prices are needed for the universe, for each market's benchmark, for stocks with trades still open
    # (even if they have left the universe), and for newly listed spin-offs the sources are watching.
    tour = Tournament(db, cfg)
    in_play = {markets.suffix(t) for t in universe}
    benches = {b for b in [cfg["universe"].get("benchmark")] if b and "" in in_play}
    benches |= {b for suf, b in tour.bench_by_market.items() if suf in in_play}
    extra = tour.open_tickers()
    tour.db.close()
    for s in sources:
        extra |= s.extra_tickers(date)
    panel = load_yfinance(sorted(set(universe) | benches | extra),
                          start=date - pd.Timedelta(days=int(cfg["universe"]["history_days"] * 1.5)),
                          end=date + pd.Timedelta(days=1))
    if panel.close.empty:
        sys.exit("no prices could be downloaded: check your internet connection and the tickers in the universe file")
    session = panel.dates[-1]
    print(f"{date.date()}: {len(universe)} stocks in universe, {panel.close.shape[1]} with prices, "
          f"latest session {session.date()}")
    if (date - session).days > 4:
        print(f"  ! latest prices are from {session.date()}: is the date right, and is the price feed working?")

    llm = LLMClient(cfg)
    fetched = run_sources(sources, date, set(universe), panel, db, llm).all()
    fetched += fetch_headlines(cfg["lab"]["herding_fade"].get("feeds", []), date, set(universe))
    kinds = {}
    for e in fetched:
        kinds[e.kind] = kinds.get(e.kind, 0) + 1
    print("  events: " + (", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) or "none"))

    broker, fx = None, {}
    if args.execute:
        from .broker import BrokerSafetyError, make_broker
        from .data.fx import load_fx
        try:
            broker = make_broker(cfg, db)
            if not broker.equity() > 0:               # sizing from a zero balance would be meaningless
                raise RuntimeError("the paper account's value reads 0; check `edgelab broker --debug`")
            fx = load_fx({markets.currency(t) for t in panel.close.columns}, broker.base_ccy)
            note_broker(db, session, ok=True, note=f"connected to {broker.name} paper account")
        except BrokerSafetyError:
            raise                                     # a paper-safety refusal must stop the run, never be skipped
        except Exception as e:                        # e.g. TWS / IB Gateway not running: keep the research session
            if broker and hasattr(broker, "close"):
                broker.close()
            broker = None
            msg = f"{type(e).__name__}: {e}"[:300]
            note_broker(db, session, ok=False, note=msg)
            print(f"  ! BROKER UNAVAILABLE ({msg}). Research session continues; NO orders were sent.")
    try:
        run_session(cfg, db, panel, fetched, llm, build_signals(cfg), broker=broker, fx=fx, force=args.force)
    finally:
        if broker and hasattr(broker, "close"):
            broker.close()
    if llm.failed:
        print(f"  ! {llm.failed} LLM call(s) failed. Those documents were skipped; re-run `edgelab daily --date "
              f"{date.date()}` once the problem is fixed to score them.")


def note_broker(db: str, session, ok: bool, note: str):
    """Remember whether the broker could be reached for this session, so the e-mail report can say so."""
    import sqlite3
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT OR REPLACE INTO meta VALUES('broker_status', ?)",
                    (json.dumps({"date": pd.Timestamp(session).strftime("%Y-%m-%d"), "ok": ok, "note": note}),))
        con.commit()
    finally:
        con.close()


def cmd_ib_gateway(cfg, args):
    from . import ibgateway
    try:
        if args.action == "save-login":
            user = ibgateway.save_login()
            print(f"Saved the IB paper login for {user} in Windows Credential Manager ({ibgateway.SERVICE}).")
        elif args.action == "status":
            print("running" if ibgateway.port_open() else "not running", f"(port {ibgateway.PORT})")
        else:
            print(ibgateway.ensure_running(cfg))
    except ibgateway.GatewayError as e:
        sys.exit(f"IB Gateway: {e}")


def cmd_email_report(cfg, args):
    from . import notify
    try:
        print(notify.send_report(cfg, db_path(cfg, args), args.date, args.run_exit, dry_run=args.dry_run,
                                 html_out=args.html_out))
    except notify.MailError as e:
        sys.exit(f"e-mail not sent: {e}")


def cmd_snapshot(cfg, args):
    from . import snapshot
    root = Path(cfg["_root"])
    files = snapshot.export(cfg, db_path(cfg, args), str(root / "site"), str(root / "web"))
    print(f"wrote {len(files)} files to site/ (database not included)")


def cmd_verify_access(cfg, args):
    from . import snapshot
    try:
        print(snapshot.check_access(args.url))
    except snapshot.PublicSiteError as e:
        sys.exit(f"STOP: {e}. Add the Cloudflare Access application for this hostname before publishing.")


def cmd_verify_empty(cfg, args):
    from . import snapshot
    try:
        print(snapshot.check_empty(args.url))
    except snapshot.PublicSiteError as e:
        sys.exit(f"STOP: {e}.")


def cmd_backfill(cfg, args):
    from . import markets
    from .data.edgar import EdgarClient

    if "example.com" in cfg["sec"]["user_agent"]:
        sys.exit("edit sec.user_agent in config.yaml first: SEC requires a real contact e-mail")
    universe = {t for t in read_universe(cfg) if markets.suffix(t) == ""}
    if not universe:
        sys.exit("no US stocks in the universe file: nothing to backfill")
    end = pd.Timestamp(args.end or pd.Timestamp.today()).normalize() - pd.Timedelta(days=1)
    start = end - pd.Timedelta(days=args.days)
    print(f"Collecting insider purchases for {len(universe)} US stocks, {start.date()} to {end.date()} "
          f"(no trades are made; this only builds history)")
    n = EdgarClient(cfg["sec"]["user_agent"], db_path(cfg, args)).backfill_insiders(start, end, universe)
    print(f"Done: {n} purchases recorded.")


def cmd_broker(cfg, args):
    from .broker import make_broker
    b = make_broker(cfg, db_path(cfg, args))
    try:
        a = b.account()
        if getattr(args, "debug", False) and hasattr(b, "debug_values"):
            for tag, value, ccy in b.debug_values():
                print(f"  {tag:16s} {value:>18s} {ccy}")
        print(f"{b.name} account {a['id']}: equity {a['equity']:,.2f}, cash {a['cash']:,.2f}, status {a['status']}")
        for p in b.managed():
            print(f"  {p['side'].upper():4s} {p['qty']:>6} {p['ticker']:10s} [{p['signal']}] opened {p['opened']}, exits {p['exit_on']}")
    finally:
        if hasattr(b, "close"):
            b.close()


def cmd_serve(cfg, args):
    import uvicorn
    from .api import create_app
    token = args.token or os.environ.get("EDGELAB_TOKEN")
    if args.host not in LOOPBACK and not token:
        sys.exit("Refusing to listen beyond this computer without a token: anyone on the network could read your "
                 "positions.\nSet one with --token <secret> (or the EDGELAB_TOKEN environment variable), then open "
                 "the dashboard once as http://<address>:<port>/#token=<secret>")
    uvicorn.run(create_app(cfg, db_path(cfg, args), token), host=args.host, port=args.port)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="edgelab")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo")
    p.add_argument("--db")
    p.add_argument("--seed", type=int, default=42)
    p = sub.add_parser("daily")
    p.add_argument("--date")
    p.add_argument("--db")
    p.add_argument("--execute", action="store_true", help="send orders to the configured broker's PAPER account")
    p.add_argument("--force", action="store_true", help="send orders even if this session was already executed")
    p = sub.add_parser("backfill")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--end")
    p.add_argument("--db")
    sub.add_parser("report").add_argument("--db")
    p = sub.add_parser("ib-gateway", help="start IB Gateway with the saved PAPER login (IBC), or save that login")
    p.add_argument("action", choices=["start", "status", "save-login"])
    p = sub.add_parser("broker", help="show the paper account and EdgeLab's positions")
    p.add_argument("--db")
    p.add_argument("--debug", action="store_true", help="also print the raw account values IB reports")
    p = sub.add_parser("email-report", help="e-mail the daily paper report (settings in config.yaml + environment)")
    p.add_argument("--date")
    p.add_argument("--db")
    p.add_argument("--dry-run", action="store_true", help="print the report instead of sending it")
    p.add_argument("--run-exit", type=int, help="exit code of the daily run, shown in the report")
    p.add_argument("--html-out", help="also save the HTML version to this file (for a look in a browser)")
    p = sub.add_parser("snapshot", help="write site/ (read-only JSON + dashboard) for Cloudflare Pages")
    p.add_argument("--db")
    p = sub.add_parser("verify-access", help="check that Cloudflare Access protects a hosted URL")
    p.add_argument("url")
    p = sub.add_parser("verify-empty", help="check that a public address serves no snapshot data")
    p.add_argument("url")
    p = sub.add_parser("serve")
    p.add_argument("--db")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--token", help="require this secret on every API call (needed to listen beyond localhost)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    {"demo": cmd_demo, "daily": cmd_daily, "backfill": cmd_backfill, "report": cmd_report, "serve": cmd_serve,
     "broker": cmd_broker,
     "email-report": cmd_email_report, "snapshot": cmd_snapshot, "verify-access": cmd_verify_access,
     "verify-empty": cmd_verify_empty, "ib-gateway": cmd_ib_gateway}[a.cmd](cfg, a)


if __name__ == "__main__":
    main()
