"""SQLite schema and small migrations. Every module opens the database through connect()."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks(
  id INTEGER PRIMARY KEY, signal TEXT, ticker TEXT, direction INTEGER, strength REAL,
  horizon INTEGER, asof TEXT, rationale TEXT,
  entry_date TEXT, entry_price REAL, bench_entry REAL,
  exit_date TEXT, exit_price REAL, ret REAL, excess REAL, state TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS signals(
  name TEXT PRIMARY KEY, lab INTEGER, status TEXT, n INTEGER, mean REAL, std REAL,
  t REAL, p REAL, q REAL, hit REAL, recent_t REAL, updated TEXT);
CREATE TABLE IF NOT EXISTS status_log(date TEXT, signal TEXT, old TEXT, new TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS daily(date TEXT PRIMARY KEY, equity REAL, fragility REAL, risk_mult REAL,
  gross REAL, picks INTEGER, orders INTEGER);
CREATE TABLE IF NOT EXISTS orders(date TEXT, ticker TEXT, signal TEXT, weight REAL, rationale TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS book_positions(ticker TEXT, signal TEXT, asof TEXT, w REAL, horizon INTEGER,
  PRIMARY KEY(ticker, signal, asof));
CREATE TABLE IF NOT EXISTS fragility(date TEXT PRIMARY KEY, corr REAL, vol REAL, illiq REAL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, kind TEXT, date TEXT, ticker TEXT, payload TEXT,
  digest TEXT, UNIQUE(kind, date, ticker, digest));
CREATE TABLE IF NOT EXISTS source_log(date TEXT, source TEXT, events INTEGER, error TEXT,
  PRIMARY KEY(date, source));
CREATE TABLE IF NOT EXISTS source_seen(source TEXT, key TEXT, PRIMARY KEY(source, key));
"""

# Columns added after the first release: (table, column, declaration)
MIGRATIONS = [
    ("signals", "weeks", "INTEGER DEFAULT 0"),            # distinct entry weeks (independent clusters)
    ("signals", "checkpoint", "INTEGER DEFAULT -1"),      # last review passed: -1 none, 0 at need, 1 at 2x need...
    ("signals", "p_seq", "REAL DEFAULT 1.0"),             # p-value after alpha spending across reviews
    ("signals", "next_review", "INTEGER"),                # trade count at which it is judged next
    ("picks", "mark_ret", "REAL"),                        # open trades: return so far, as of mark_date...
    ("picks", "mark_bench", "REAL"),                      # ...and the benchmark's over the same span
    ("picks", "mark_date", "TEXT"),                       # last day the stock had a price
]


def connect(db_path: str) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    try:    # one pick per signal, stock and day: makes re-running a day harmless
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_picks ON picks(signal, ticker, asof)")
    except sqlite3.IntegrityError:
        print("warning: this database holds duplicate picks from an older version; re-runs are not idempotent")
    db.commit()
    return db
