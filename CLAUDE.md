# EdgeLab: rules for working in this repo

EdgeLab hunts for trading edges in small, under-covered stocks without fooling itself. These rules came out
of a 13-expert design review. They are the point of the project, so follow them even when a shortcut looks harmless.

## Non-negotiable research rules

1. **No look-ahead.** Signals only see `ctx.prices` (already sliced to `asof`) and events dated `asof`.
   Never pass the full price panel, future events or settled outcomes into a signal.
2. **LLM contamination guard stays on.** `LLMClient.guard()` refuses documents dated on/before
   `llm.training_cutoff`. Never bypass it, never backtest LLM-scored signals on older text, and when the model
   changes, update the cutoff in `config.yaml`.
3. **Frozen LLM setup.** Model, prompt version and temperature are fixed per experiment. Changing a prompt means
   a new `prompt_version` (e.g. `earnings_v4`), not editing a released one in place; then update
   `tests/prompt_digests.json`, which pins every released prompt. Results stay cached in `.llm_cache/`.
4. **The LLM extracts features; statistics decide.** Don't let model confidence size positions directly.
5. **Every idea goes through the tournament.** New signals start as candidates (or `lab = True` for experimental
   ones, which need `lab_min_trades`). Never hard-promote a signal, edit status in the DB, or delete losing
   signals' history. The false-discovery correction depends on counting every idea tried.
6. **Costs and benchmark are always on.** Report excess return net of `costs_bps_per_side`, vs the benchmark.
7. **Treat great results as bugs first.** If a change makes performance jump, look for leakage before celebrating.
8. **Do not weaken the promotion test.** Reviews happen at need, 2x, 4x... trades with alpha spending; standard
   errors are clustered by entry week; `min_weeks` applies. `tests/test_tournament.py` simulates no-edge signals
   and must keep passing. Promoting on a daily look, or on a per-trade t-test, brings back a ~50% false-promotion rate.
9. **Trades never vanish.** A pick is closed, voided for lack of any price, or closed at the last price seen
   after a delisting. Never delete or skip one because the data is awkward.

## State and safety rules

- Everything that must outlive a process is in SQLite (`edgelab/store.py`). A session must be safe to run twice:
  write a day's rows by replacing them, never by appending. `tests/test_live.py` proves a fresh process per day
  equals one long-running process; keep it passing.
- Returns are computed from one price panel at a time. Never mix a stored price with a freshly downloaded one.
- Text from filings, feeds and error messages is untrusted: escape it in the dashboard (`esc()`), fence it in
  prompts, range-check what the model returns.
- The API stays read-only, same-origin by default, and token-protected whenever it listens beyond localhost.

## Data-source rules

- Date every event by when it became public (or when EdgeLab first saw it), never the period it describes.
- Sources only fetch and shape data. Whether an event type predicts returns is the tournament's job.
- A new market for an existing idea is a NEW tournament entry (e.g. `earnings_drift_intl`), `lab = True`.
  Never widen an existing signal's scope in place: it would mix track records.
- Source adapters are tested with fixtures in `tests/test_sources.py`. When a live format differs, fix the
  parser and add the real shape to the fixture.

## Risk rules

- Survival before returns: keep the fragility thermometer, quarter-Kelly cap, monthly loss cap and drawdown
  halving in the sizing path. Loosening any of them needs an explicit request from the user.
- The broker adapter is **paper-only by default** (`broker.allow_live: false`). Never enable live trading,
  shorting, or larger sizes unless the user explicitly asks in that conversation.
- The broker only closes positions EdgeLab opened (`broker_positions` table, per broker). Keep it that way.
- Interactive Brokers: keep both paper checks (paper port 7497/4002 AND account id starting `DU`).
- Foreign orders are sized in the account's base currency via FX; if a rate or price is missing, skip, never guess.
  Markets marked `tradable=False` in `edgelab/markets.py` stay research-only unless the user adds them to
  `broker.extra_tradable`.

## Working conventions

- Run `pytest` before committing. The end-to-end test must keep showing decoys rejected.
- The synthetic world (`edgelab/synthetic.py`) has planted edges for testing machinery. Never quote its returns
  as evidence about real markets.
- Keep the dashboard talking only to `/api/*`, so web, desktop (Tauri) and mobile (Capacitor) wrappers keep working.
- Secrets live in environment variables (`ANTHROPIC_API_KEY`, `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `EDINET_API_KEY`, `DART_API_KEY`), never in files.
  Exception approved by the owner (2026-11-19): the IB **paper** login for IBC lives in Windows Credential Manager
  (`edgelab-ibkr-paper`); it is written to disk only as a temporary owner-only file deleted once Gateway answers.

## Layout

`edgelab/signals/` core signals · `edgelab/lab/` experimental signals · `edgelab/sources/` data sources by region ·
`edgelab/markets.py` world market table · `edgelab/tournament/` ledger and stats ·
`edgelab/risk/` fragility and sizing · `edgelab/runner.py` daily loop · `edgelab/live.py` one live session ·
`edgelab/store.py` schema · `edgelab/broker.py` Alpaca + Interactive Brokers (paper) · `edgelab/api.py` + `web/` dashboard ·
`edgelab/cli.py` commands
