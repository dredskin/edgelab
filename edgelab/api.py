"""JSON API + dashboard. Any front end (web, desktop wrapper, mobile app) talks to these endpoints."""
import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

WEB = Path(__file__).resolve().parent.parent / "web"


def create_app(cfg: dict, db_path=None) -> FastAPI:
    db_file = db_path or str(Path(cfg["_root"]) / cfg["storage"])
    app = FastAPI(title="EdgeLab")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

    def q(sql, args=()):
        con = sqlite3.connect(db_file)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        except sqlite3.OperationalError:
            return []
        finally:
            con.close()

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
                    FROM picks WHERE signal=? ORDER BY id DESC LIMIT ?""", (name, limit))

    @app.get("/api/log")
    def log():
        return q("SELECT * FROM status_log ORDER BY date DESC")

    @app.get("/api/orders")
    def orders(limit: int = 100):
        return q("SELECT * FROM orders ORDER BY date DESC LIMIT ?", (limit,))

    if WEB.exists():
        app.mount("/static", StaticFiles(directory=WEB), name="static")

        @app.get("/")
        def index():
            return FileResponse(WEB / "index.html")

        for f in ("manifest.webmanifest", "sw.js", "icon.svg"):
            app.add_api_route(f"/{f}", (lambda p=WEB / f: FileResponse(p)), methods=["GET"])
    return app
