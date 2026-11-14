"""One live paper-trading session, with no network access of its own.

The command line gathers prices and source events, then hands them here. Keeping the session logic free of
I/O means the whole live path can be tested on the synthetic market exactly as it runs for real.
"""
import pandas as pd

from . import store
from .data.events import EventStore
from .risk.fragility import MIN_NAMES
from .runner import Runner

EVENT_LOOKBACK_DAYS = 120       # how far back signals may look (a spin-off listed weeks ago, for example)


def run_session(cfg, db, panel, fetched, llm, signals, broker=None, fx=None, force=False, log=print) -> dict:
    """Run the session that ends on the last date of `panel`.

    fetched: events gathered today. They are saved, and signals then read events back from the database,
    so a re-run of the same session sees exactly what the first run saw.
    """
    session = panel.dates[-1]
    con = store.connect(db)
    fresh = EventStore()
    fresh.extend(e.not_after(session) for e in fetched)
    new_events = fresh.save(con)
    events = EventStore.load(con, session - pd.Timedelta(days=EVENT_LOOKBACK_DAYS))
    con.close()

    run = Runner(cfg, signals, llm, db, eval_every=1)
    i = len(panel.dates) - 1
    caught_up = run.restore(panel, i)
    if caught_up:
        log(f"Caught up {caught_up} session(s) missed since the last run (book marked to market; no new picks for those days).")

    equity = None
    if broker:
        for tk, qty, side in broker.close_expired(session):
            log(f"  exit: {side.upper()} {qty} {tk} (holding period over)")
        equity = broker.equity()
        broker.record_equity(session, equity)

    orders = run.day(i, panel, events)
    today = run.daily[-1]
    log(f"Session {session.date()}: {today['picks']} picks recorded, paper equity {today['equity']:.4f}")
    if run.thermo.names < MIN_NAMES:
        log(f"  ! fragility thermometer is blind: only {run.thermo.names} stocks with enough price history "
            f"(needs {MIN_NAMES}). Exposure is NOT being reduced for market stress.")
    else:
        log(f"  fragility {run.thermo.score:.2f} -> new positions at x{today['risk_mult']:.2f} of normal size")
    if run.notes:
        log("  documents the LLM layer skipped: " + ", ".join(f"{n} {k}" for k, n in sorted(run.notes.items())))
    if not orders:
        log("  no paper orders (no promoted signal fired, or the risk layer closed the gate)")
    for p, w in orders:
        log(f"  {'BUY ' if w > 0 else 'SELL'} {p.ticker:10s} {abs(w):.2%} of equity  [{p.signal}] {p.rationale}")

    sent = []
    if broker and orders:
        cap = cfg["risk"]["monthly_loss_cap"]
        mtd = broker.month_to_date(session)
        if broker.already_ran(session) and not force:
            log(f"  orders for {session.date()} were already sent to {broker.name}; not sending again (--force overrides)")
        elif mtd <= -cap:
            log(f"  {broker.name} account is {mtd:.1%} this month, past the {cap:.0%} monthly loss cap: no new orders sent")
        else:
            prices = panel.close.ffill().iloc[-1].dropna().to_dict()
            sent = broker.execute(orders, prices, equity, session, fx or {})
            broker.mark_ran(session)
            for tk, qty, side in sent:
                log(f"  sent to {broker.name}: {side.upper()} {qty} {tk}")
    elif orders:
        log("  (paper only; add --execute to send these to your broker's paper account)")
    return dict(session=session, orders=orders, sent=sent, run=run, caught_up=caught_up, new_events=new_events)
