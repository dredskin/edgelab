"""JSON API + dashboard. Any front end (web, desktop wrapper, mobile app) talks to these endpoints.

Security posture:
  - The API only reads; the database is opened read-only.
  - Same-origin by default. Other origins (a Tauri/Capacitor wrapper) must be listed in api.cors_origins.
  - With a token set, every /api/ call must present it. `edgelab serve` insists on a token before it
    will listen on anything other than this computer.
"""
import json
import secrets
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

WEB = Path(__file__).resolve().parent.parent / "web"
PAGES = {"/": "index.html", "/manifest.webmanifest": "manifest.webmanifest", "/sw.js": "sw.js", "/icon.svg": "icon.svg"}
MAX_ROWS = 1000


def create_app(cfg: dict, db_path=None, token: str = None) -> FastAPI:
    db_file = Path(db_path or Path(cfg["_root"]) / cfg["storage"]).resolve()
    app = FastAPI(title="EdgeLab", docs_url=None, redoc_url=None, openapi_url=None)

    if token:
        @app.middleware("http")
        async def require_token(request: Request, call_next):
            if request.url.path.startswith("/api/") and request.method != "OPTIONS":
                given = request.headers.get("x-edgelab-token") or request.query_params.get("token") or ""
                if not secrets.compare_digest(given.encode(), token.encode()):
                    return JSONResponse({"error": "missing or wrong token"}, status_code=401)
            return await call_next(request)

    origins = cfg.get("api", {}).get("cors_origins") or []
    if origins:      # added last so it wraps the token check and can answer browser preflights
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET"],
                           allow_headers=["X-EdgeLab-Token"])

    def q(sql, args=()):
        try:
            con = sqlite3.connect(f"{db_file.as_uri()}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            return []                                   # no database yet
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        except sqlite3.OperationalError:
            return []                                   # table not created yet
        finally:
            con.close()

    def cap(limit: int) -> int:
        return max(1, min(int(limit), MAX_ROWS))

    @app.get("/api/summary")
    def summary():
        meta = q("SELECT value FROM meta WHERE key='run'")
        last = q("SELECT * FROM daily ORDER BY date DESC LIMIT 1")
        counts = q("SELECT status, COUNT(*) n FROM signals GROUP BY status")
        return {"meta": json.loads(meta[0]["value"]) if meta else {"kind": "live"},
                "latest": last[0] if last else None,
                "status_counts": {r["status"]: r["n"] for r in counts},
                "risk": cfg["risk"], "tournament": cfg["tournament"]}

    @app.get("/api/daily")
    def daily():
        return q("SELECT * FROM daily ORDER BY date")

    @app.get("/api/signals")
    def signals():
        return q("SELECT * FROM signals ORDER BY (status='promoted') DESC, t DESC")

    @app.get("/api/signals/{name}/trades")
    def trades(name: str, limit: int = 200):
        return q("""SELECT ticker, direction, strength, asof, entry_date, exit_date, ret, excess, rationale, state
                    FROM picks WHERE signal=? ORDER BY id DESC LIMIT ?""", (name, cap(limit)))

    @app.get("/api/log")
    def log():
        return q("SELECT * FROM status_log ORDER BY date DESC")

    @app.get("/api/orders")
    def orders(limit: int = 100):
        return q("SELECT * FROM orders ORDER BY date DESC LIMIT ?", (cap(limit),))

    @app.get("/api/sources")
    def sources(limit: int = 60):
        return q("SELECT * FROM source_log ORDER BY date DESC, source LIMIT ?", (cap(limit),))

    def page(name):
        path = WEB / name                               # fixed at start-up: nothing in a request can change it

        def serve():
            return FileResponse(path)
        return serve

    if WEB.exists():
        for route, name in PAGES.items():
            app.add_api_route(route, page(name), methods=["GET"])
    return app
