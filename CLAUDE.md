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
   a new `prompt_version` (e.g. `earnings_v2`), not editing `v1` in place. Results stay cached in `.llm_cache/`.
4. **The LLM extracts features; statistics decide.** Don't let model confidence size positions directly.
5. **Every idea goes through the tournament.** New signals start as candidates (or `lab = True` for experimental
   ones, which need `lab_min_trades`). Never hard-promote a signal, edit status in the DB, or delete losing
   signals' history. The false-discovery correction depends on counting every idea tried.
6. **Costs and benchmark are always on.** Report excess return net of `costs_bps_per_side`, vs the benchmark.
7. **Treat great results as bugs first.** If a change makes performance jump, look for leakage before celebrating.

## Risk rules

- Survival before returns: keep the fragility thermometer, quarter-Kelly cap, monthly loss cap and drawdown
  halving in the sizing path. Loosening any of them needs an explicit request from the user.
- The broker adapter is **paper-only by default** (`broker.allow_live: false`). Never enable live trading,
  shorting, or larger sizes unless the user explicitly asks in that conversation.
- The broker only closes positions EdgeLab opened (`broker_positions` table, per broker). Keep it that way.
- Interactive Brokers: keep both paper checks (paper port 7497/4002 AND account id starting `DU`).

## Working conventions

- Run `pytest` before committing. The end-to-end test must keep showing decoys rejected.
- The synthetic world (`edgelab/synthetic.py`) has planted edges for testing machinery. Never quote its returns
  as evidence about real markets.
- Keep the dashboard talking only to `/api/*`, so web, desktop (Tauri) and mobile (Capacitor) wrappers keep working.
- Secrets live in environment variables (`ANTHROPIC_API_KEY`, `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`), never in files.

## Layout

`edgelab/signals/` core signals · `edgelab/lab/` experimental signals · `edgelab/tournament/` ledger and stats ·
`edgelab/risk/` fragility and sizing · `edgelab/broker.py` Alpaca + Interactive Brokers (paper) · `edgelab/api.py` + `web/` dashboard ·
`edgelab/cli.py` commands
