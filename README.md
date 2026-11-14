# EdgeLab

A research platform that hunts for trading edges in small, under-covered stocks without fooling itself.

Every idea is a **signal**. Every signal is paper-traded forward in a **tournament**. Only signals that
survive a statistical test that corrects for luck get capital, and a **risk layer** decides how much.
An experimental **lab** (starting with the AI-herding fade) runs in the same tournament under a stricter bar.

> This is a research tool, not financial advice. It paper-trades by default and places no real orders.
> The synthetic demo has planted edges so the machinery can be tested. Its returns say nothing about real markets.

## Architecture

```
 SEC EDGAR (Form 4, 8-K, 10-12B) ─┐
 Prices (yfinance)               ├─► Events ─► Signals ─────────────► Tournament ──► promoted signals
 Headline feeds (RSS)            ┘    │        insider_cluster          paper ledger      │
                                      │        earnings_drift  ◄─LLM    BH false-discovery │
                                      │        spinoff_orphan           promote / retire   ▼
                                      │        lab/herding_fade ◄─LLM                  Risk layer
                                      │                                  fragility thermometer
                                      └──────────────────────────────►  quarter-Kelly sizing
                                                                         monthly loss cap / drawdown halving
                                                                                 │
                                                       SQLite ◄─────────── Paper book & orders
                                                         │
                                               FastAPI  /api/*  ──►  Dashboard (installable web app)
```

| Piece | File | Panel idea it implements |
|---|---|---|
| Signal tournament | `edgelab/tournament/` | Rigor enforcers: forward-only testing, Benjamini–Hochberg FDR, automatic retirement when edges decay |
| Insider cluster | `edgelab/signals/insider.py` | Behavior readers: first-time and clustered insider buys |
| LLM earnings drift | `edgelab/signals/earnings_drift.py` | Edge hunters + LLM trainer: LLM scores surprise and evasion; stats decide |
| Spin-off orphans | `edgelab/signals/spinoff.py` | Forced sellers |
| AI-herding fade (lab) | `edgelab/lab/herding_fade.py` | Fade big moves on headlines the LLM judges to be hype |
| Fragility thermometer | `edgelab/risk/fragility.py` | Physicist + geologist: correlation, volatility, liquidity drain |
| Sizing & drawdown budget | `edgelab/risk/sizing.py` | Mathematician + strategist + rabbi: quarter-Kelly, reserves, loss caps |
| LLM client | `edgelab/llm/client.py` | Contamination guard, frozen model and prompts, disk cache |

## Quick start (no keys, no internet)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
edgelab demo                 # synthetic small-cap market, ~15 seconds
edgelab serve --db demo.db   # open http://localhost:8000
pytest                       # 7 tests, including an end-to-end run
```

The demo shows the tournament promoting the planted edges, **rejecting all three random decoy signals**, retiring a
signal whose edge decays, the LLM contamination guard refusing pre-cutoff documents, and the fragility
thermometer cutting exposure to about a quarter during the crash window.

Known demo quirk worth noticing: `earnings_drift` gets retired *before* its planted decay, because a noisy stretch
pushed its recent t-stat below the threshold. Retirement rules trade off false alarms against slow reactions. Tune
`retire_window` / `retire_t` in `config.yaml` and watch the effect.

## Live paper trading

```bash
pip install -e ".[live]"
export ANTHROPIC_API_KEY=...                 # for the LLM reading layer
# edit config.yaml: sec.user_agent (SEC requires a real contact), llm.training_cutoff, lab feeds
# edit universe.txt: your small/micro-cap list
edgelab daily                                # run once per trading day after the close
edgelab report
edgelab serve
```

Each day it pulls Form 4 purchases, 8-K earnings releases (Item 2.02) and spin-off registrations from EDGAR,
prices from yfinance, and headlines from your feeds. It records every signal's picks in the tournament and
prints sized paper orders for promoted signals only. On a fresh database nothing is promoted until signals build
a forward track record (`min_trades`, default 40; lab signals 80). Run it on a schedule (cron / Task Scheduler)
and be patient. That wait is the point.

**Contamination rule:** the LLM refuses any document dated on or before `llm.training_cutoff`. Never backtest LLM
signals on older text. Set the cutoff to your model's published training cutoff.

## Broker: Alpaca paper trading

1. Create a free Alpaca account and open the **paper trading** dashboard to generate API keys.
2. `export APCA_API_KEY_ID=...` and `export APCA_API_SECRET_KEY=...`
3. `edgelab broker` shows the paper account; `edgelab daily --execute` exits positions whose horizon has passed,
   then sends today's sized orders.

Safeguards: non-paper endpoints are refused unless `broker.allow_live: true`; long-only unless
`broker.allow_shorts: true`; whole-share market orders; EdgeLab only closes positions it opened; set
`broker.dry_run: true` to print orders without sending. Existing EdgeLab positions count against the gross limit.

## Broker: Interactive Brokers paper trading

EdgeLab connects to **TWS or IB Gateway running on your own computer**, so run these commands there, not in the cloud.

1. `pip install -e ".[ibkr]"`
2. Log in to TWS or IB Gateway with your **paper trading** username.
3. In TWS: *File → Global Configuration → API → Settings*: tick **Enable ActiveX and Socket Clients**,
   keep the port at **7497** (paper), and add `127.0.0.1` to trusted IPs. (IB Gateway paper uses **4002**.)
4. In `config.yaml` set `broker.provider: ibkr` (and `broker.ibkr.base_currency: CAD` if your account is in CAD).
5. `edgelab broker` to check the connection, then `edgelab daily --execute`.

Two independent paper checks must both pass or EdgeLab refuses to connect: the port must be a paper port
(7497 / 4002), and the account id must start with `DU` (IB's paper prefix). Canadian listings use Yahoo-style
suffixes in `universe.txt`: `.TO` (TSX), `.V` (TSXV), `.CN` (CSE). Orders are tagged `edgelab` in TWS.
Alpaca and IB positions are tracked separately, so EdgeLab never closes one broker's positions through the other.

### Trading worldwide

Put any Yahoo-style ticker in `universe.txt`: `7203.T` (Tokyo), `0700.HK` (Hong Kong), `VOD.L` (London),
`SAP.DE` (Xetra), `ASML.AS`, `NESN.SW`, `BHP.AX`, `D05.SI`, `600519.SS` (Shanghai via Stock Connect), `SHOP.TO`,
`XYZ.V`... The full table lives in `edgelab/markets.py`.

- **Sizing is done in your account's base currency.** Local prices are converted with live FX rates (London's
  pence are converted to pounds first). If a rate is missing, the order is skipped rather than mis-sized.
- **Board lots** are respected (Tokyo, Shanghai/Shenzhen, Singapore trade in 100s). Hong Kong lots vary by stock:
  add them under `broker.lot_sizes`.
- **Research-only markets:** Taiwan, Korea and India are generally not tradable through IB for non-resident
  accounts. EdgeLab still reads their filings and news (for example, Taiwanese supplier sales predicting a
  customer's results) but won't send orders there unless you list them in `broker.extra_tradable`, and only do
  that if your IB account really has the permission.
- You'll need IB **trading permissions** for each region (Account Settings → Trading Permissions) and market data
  subscriptions for the exchanges you trade.
- Alpaca trades US listings only; foreign picks are skipped there.
- The LLM prompts (`prompt_version: v2`) read filings and headlines in their original language and return English
  scores. `v1` is kept unchanged so earlier results stay reproducible.

**Live trading is deliberately not switched on.** When signals have a real forward track record, it should be
an explicit decision with small size and extra limits (daily loss cap, maximum order size).

## The app (desktop and mobile)

The dashboard (`web/`) is an installable Progressive Web App that talks only to the JSON API:

- **Today:** `edgelab serve`, open it in Chrome/Edge/Safari, then "Install app" / "Add to Home Screen".
  To reach it from your phone, run `edgelab serve --host 0.0.0.0` on your computer or a small cloud server.
- **Desktop app:** wrap `web/` with [Tauri](https://tauri.app) (small, native) and point it at the API.
- **Mobile app stores:** wrap `web/` with [Capacitor](https://capacitorjs.com). In the app, set
  `localStorage.edgelab_api = "https://your-server"` so it calls your hosted API.

API: `/api/summary`, `/api/daily`, `/api/signals`, `/api/signals/{name}/trades`, `/api/log`, `/api/orders`.

## Adding a signal

```python
from edgelab.signals.base import Signal, Pick

class MyIdea(Signal):
    name = "my_idea"
    lab = True                     # start in the lab
    def generate(self, ctx):       # ctx.prices is sliced to today: look-ahead is impossible
        return [Pick(self.name, "ABCD", +1, 0.8, 10, "why")]
```

Register it in `build_signals()` in `edgelab/cli.py`. The tournament handles the rest. Every idea you try counts
against the false-discovery budget, which is intended.

## Roadmap

1. **Herding fade, intraday:** timestamped news API + minute bars; fade within minutes instead of at the next open.
2. **Live-trading gate:** per-order and daily loss limits plus a confirmation step before `allow_live` can be used.
3. **More signals from the matrices:** cross-border supply chain, government contracts, post-cascade rebound,
   tax-loss rebound, attention exhaustion.
4. **Synthetic-history stress lab:** run every promoted signal through thousands of generated market histories.
5. **Policy-risk layer:** flag tariff, sanction and regulatory exposure (political scientist's ask).
