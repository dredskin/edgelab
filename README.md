# EdgeLab

A research platform that hunts for trading edges in small, under-covered stocks without fooling itself.

Every idea is a **signal**. Every signal is paper-traded forward in a **tournament**. Only signals that
survive a statistical test that corrects for luck get capital, and a **risk layer** decides how much.
An experimental **lab** (starting with the AI-herding fade) runs in the same tournament under a stricter bar.

**Status:** the machinery is built and tested; no real edge has been demonstrated. Everything below has been
exercised on a synthetic market and against stand-ins for the outside services, not yet against the live ones.
See [Known limitations](#known-limitations).

> This is a research tool, not financial advice. It paper-trades by default and places no real orders.
> The synthetic demo has planted edges so the machinery can be tested. Its returns say nothing about real markets.

## Architecture

```
 Sources: US EDGAR · Japan EDINET/TDnet · Taiwan sales · Korea DART · any-language announcement feeds
 SEC EDGAR (Form 4, 8-K, 10-12B) ─┐
 Prices (yfinance)               ├─► Events ─► Signals ─────────────► Tournament ──► promoted signals
 Headline feeds (RSS)            ┘    │        insider_cluster          paper ledger      │
                                      │        earnings_drift  ◄─LLM    scheduled reviews  │
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
| Signal tournament | `edgelab/tournament/` | Rigor enforcers: forward-only testing, scheduled reviews, week-clustered statistics, Benjamini–Hochberg FDR, retirement when edges decay |
| Insider cluster | `edgelab/signals/insider.py` | Behavior readers: first-time and clustered insider buys |
| LLM earnings drift | `edgelab/signals/earnings_drift.py` | Edge hunters + LLM trainer: LLM scores surprise and evasion; stats decide |
| Spin-off orphans | `edgelab/signals/spinoff.py` | Forced sellers |
| AI-herding fade (lab) | `edgelab/lab/herding_fade.py` | Fade big moves on headlines the LLM judges to be hype |
| Fragility thermometer | `edgelab/risk/fragility.py` | Physicist + geologist: correlation, volatility, liquidity drain |
| Sizing & drawdown budget | `edgelab/risk/sizing.py` | Mathematician + strategist + rabbi: quarter-Kelly, reserves, loss caps |
| LLM client | `edgelab/llm/client.py` | Contamination guard, frozen model and prompts, disk cache, documents treated as untrusted |
| Live session | `edgelab/live.py`, `edgelab/runner.py` | One session per run; all state in SQLite, so runs can be repeated and missed days caught up |

## Quick start (no keys, no internet)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
edgelab demo                 # synthetic small-cap market, ~15 seconds
edgelab serve --db demo.db   # open http://localhost:8000
pytest                       # about 40 seconds, including an end-to-end run
```

The demo's edges are planted, so the right answers are known. It shows the tournament promoting the two lasting
edges, promoting the earnings edge while it works and retiring it after it is made to reverse, never promoting
the three random decoys, the LLM contamination guard refusing pre-cutoff documents, and the fragility
thermometer cutting the size of new positions to about a quarter during the crash window.

The decoys are an easy test (they lose to trading costs). The hard test is in `tests/test_tournament.py`: signals
with exactly zero edge, looked at every week for over a year, must almost never be promoted.

## How a signal earns capital

- **Forward only.** A pick is written down before its outcome exists, bought at the next open, and scored net of
  costs against its own market's benchmark.
- **Scheduled reviews, not daily peeking.** A signal is judged when its settled trades reach 40, 80, 160, 320...
  (80, 160... for lab signals), and each later review has to clear a higher bar. Checking every day and promoting
  on the first good day is how a no-edge signal gets through: in a simulation with correlated trades, about
  half of the no-edge signals were promoted under that rule, and about 1 in 70 under this one.
- **Weeks, not trades, are the evidence.** Trades entered in the same week tend to win or lose together, so the
  statistics count independent weeks (`min_weeks`, default 8) rather than treating 40 trades as 40 experiments.
- **Every idea counts against the budget.** The false-discovery limit (`fdr_q`) is applied across every signal
  ever registered, including the ones that failed.
- **Failures stay on the record.** A stock that stops trading is closed at the last price seen, not dropped.

Expect this to be slow. A modest real edge can need a few hundred trades before it is promoted.

## Live paper trading

```bash
pip install -e ".[live]"
export ANTHROPIC_API_KEY=...                 # for the LLM reading layer
# edit config.yaml: sec.user_agent (SEC requires a real contact), llm.training_cutoff, lab feeds
# edit universe.txt: your small/micro-cap list
edgelab backfill --days 365                  # once: past insider purchases, so "first purchase" flags mean something
edgelab daily                                # run once per trading day after the US close
edgelab report
edgelab serve
```

Each run pulls Form 4 purchases, 8-K earnings releases (the attached press release) and spin-off registrations
from EDGAR, prices from yfinance, and headlines from your feeds. It records every signal's picks in the
tournament and prints sized paper orders for promoted signals only. On a fresh database nothing is promoted until
signals build a forward track record. Run it on a schedule (cron / Task Scheduler) and be patient. That wait is
the point.

- **Safe to repeat.** Everything lives in the database (`edgelab.db`). Running the same day twice changes nothing.
- **Missed days.** If the computer was off, the next run marks the paper book through the sessions it missed. It
  does not invent picks for them; `edgelab daily --date YYYY-MM-DD` processes a missed day's filings.
- **Failures are visible.** A data source that fails is listed in the dashboard and skipped. A document the LLM
  could not score is counted in the run's output; re-run that date once the problem is fixed.
- **Back up `edgelab.db`.** It is the forward track record, and it cannot be rebuilt.

**Contamination rule:** the LLM refuses any document dated on or before `llm.training_cutoff`. Never backtest LLM
signals on older text. Set the cutoff to your model's published training cutoff.

## Broker: Alpaca paper trading

1. Create a free Alpaca account and open the **paper trading** dashboard to generate API keys.
2. `export APCA_API_KEY_ID=...` and `export APCA_API_SECRET_KEY=...`
3. `edgelab broker` shows the paper account; `edgelab daily --execute` exits positions whose horizon has passed,
   then sends today's sized orders.

Safeguards: non-paper endpoints are refused unless `broker.allow_live: true`; long-only unless
`broker.allow_shorts: true`; whole-share market orders; EdgeLab only closes positions it opened; set
`broker.dry_run: true` to print orders without sending. A session's orders are sent once (a second run that day
sends nothing unless you pass `--force`), and no new orders go out while the broker account itself is down more
than `monthly_loss_cap` for the month.

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
- **Each market has its own benchmark** (`universe.benchmarks`) and can have its own trading cost
  (`costs_bps_by_market`). A Japanese stock is judged against Japan, never against a US index. The benchmark
  tickers in `config.yaml` are suggestions: check they load, or the market falls back to the average of its own
  stocks in your universe.
- The LLM prompts read filings and headlines in their original language and return English scores. Released
  prompt versions are frozen (a test pins them); `v3` is current, `v1` and `v2` are kept for reproducibility.

**Live trading is deliberately not switched on.** When signals have a real forward track record, it should be
an explicit decision with small size and extra limits (daily loss cap, maximum order size).

## International data sources

Turn sources on in `config.yaml` under `sources:`. Each one runs every day; a source that fails is logged
(dashboard → Activity → Data sources) and skipped, never fatal.

| Source | Market | What it reads | Needs |
|---|---|---|---|
| `edgar` | US | Form 4 insider buys, 8-K earnings, spin-off registrations | nothing |
| `edinet` | Japan | Large shareholding reports (大量保有報告書): who is building a 5%+ stake, and why | `EDINET_API_KEY` (free) |
| `tdnet` | Japan | Earnings (決算短信), forecast revisions (業績予想の修正), buyback decisions | nothing (community mirror; see note) |
| `taiwan_sales` | Taiwan | Monthly revenue for suppliers in `supply_chain.yaml` | nothing |
| `dart` | Korea | Preliminary earnings, buyback decisions | `DART_API_KEY` (free) |
| `announcements` | anywhere | Any RSS/Atom feed in any language: Claude sorts each item into earnings / insider buy / buyback / large stake | feed URLs |

New lab signals fed by these (all experimental, judged separately from the US ones):

- `earnings_drift_intl`, `insider_intl`: the core ideas outside the US, each with its own track record.
- `large_stake`: a holder discloses a new or increased stake; stronger when the stated purpose is activist.
- `buyback`: a company decides to repurchase shares; Claude extracts the size.
- `supply_chain`: a supplier's sales accelerate or its earnings surprise → trade its **customers**.
  Edit `supply_chain.yaml`; that map is where the edge lives.

Notes:
- **Dating:** every event is dated when it became public (or when EdgeLab first saw it), never the period it
  describes. Taiwan's October sales seen on Nov 10 are a Nov 10 event.
- **TDnet** has no free official API. The default list URL is a community mirror; set `sources.tdnet.list_url`
  to a licensed feed for anything serious.
- For the UK (RNS), Hong Kong (HKEXnews), Canada (newswires), Europe and Australia, add announcement feeds with a
  `default_suffix` (e.g. `{url: "...", default_suffix: ".L"}`) so bare stock codes resolve to the right market.
- These adapters are tested against sample responses shaped like each provider's format, not against the live
  services. Expect to adjust field names on first contact; the source-health table shows what broke.

## The app (desktop and mobile)

The dashboard (`web/`) is an installable Progressive Web App that talks only to the JSON API:

- **Today:** `edgelab serve`, open it in Chrome/Edge/Safari, then "Install app" / "Add to Home Screen".
- **From your phone:** `edgelab serve --host 0.0.0.0 --token <a long secret>`, then open
  `http://<computer>:8000/#token=<the secret>` once on the phone. Without a token EdgeLab refuses to listen beyond
  the computer it runs on, because the API shows your positions. Over the internet, put it behind HTTPS.
- **Desktop app:** wrap `web/` with [Tauri](https://tauri.app) (small, native) and point it at the API.
- **Mobile app stores:** wrap `web/` with [Capacitor](https://capacitorjs.com). In the app, set
  `localStorage.edgelab_api = "https://your-server"` and `localStorage.edgelab_token`, and list the wrapper's
  origin under `api.cors_origins` (other websites cannot read the API by default).

The API is read-only: `/api/summary`, `/api/daily`, `/api/signals`, `/api/signals/{name}/trades`, `/api/log`,
`/api/orders`, `/api/sources`.

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

## Known limitations

- **Untested against the real services.** SEC, Yahoo, EDINET, TDnet, DART, Alpaca, Interactive Brokers and the
  Claude API are all exercised through stand-ins. Treat the first live runs as a shakedown.
- **Free price data.** yfinance is unofficial: prices can be late, wrong or missing, and delisted stocks may
  vanish. Daily bars only, so every trade enters at the next open.
- **One run per day, one calendar.** Asian filings are acted on a session later than a local trader could.
- **The thermometer is a heuristic.** It ranks today against its own history, so it reads "elevated" fairly
  often even in calm markets; it only shrinks new positions; and with fewer than 5 stocks it cannot measure
  anything (the run says so). Its thresholds have not been validated on real crashes.
- **Retirement is trigger-happy.** It looks at the last 30 trades, so a real but modest edge will sooner or later
  be retired by bad luck, and retirement is permanent.
- **The statistics are careful, not perfect.** Clustering by week does not fully cover month-long holds that
  overlap, or two signals that are really the same idea.
- **Paper is not live.** Market orders in thin stocks will fill worse than the flat 30 bp cost assumed here, and
  the paper book differs from a broker account (whole shares, skipped shorts, real fills).
- **The LLM layer can be gamed or wrong.** Documents are fenced off and answers range-checked, which reduces the
  risk of a press release steering its own score; it does not remove it. `llm.training_cutoff` must match the
  model you configure.
- **Simple definitions.** An insider "cluster" means several buyers on the same day; `supply_chain.yaml` ships
  empty; TDnet comes from a community mirror.

## Roadmap

1. **Shakedown on live data:** run `edgelab daily` for two weeks on a small universe and fix what the real
   services return differently from the stand-ins.
2. **Herding fade, intraday:** timestamped news API + minute bars; fade within minutes instead of at the next open.
3. **Live-trading gate:** limit orders, per-order and daily loss limits, and a confirmation step before
   `allow_live` can be used.
4. **More signals from the matrices:** government contracts, post-cascade rebound, tax-loss rebound,
   attention exhaustion. **More sources:** Canada SEDI insider reports, HKEXnews and UK RNS direct adapters.
5. **Synthetic-history stress lab:** run every promoted signal through thousands of generated market histories.
6. **Policy-risk layer:** flag tariff, sanction and regulatory exposure (political scientist's ask).
