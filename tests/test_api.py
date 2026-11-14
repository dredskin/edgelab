"""The dashboard server must not leak files, must not be readable by other websites, and must demand a
token before it is exposed beyond this computer."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from edgelab import store
from edgelab.api import create_app
from edgelab.cli import main

CFG = {"_root": ".", "storage": "x.db", "risk": {}, "tournament": {}}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    con = store.connect(str(path))
    con.execute("INSERT INTO signals(name, lab, status) VALUES('sig', 0, 'candidate')")
    con.executemany("INSERT INTO orders VALUES('2027-01-04', ?, 'sig', 0.01, '<img src=x onerror=alert(1)>')",
                    [(f"T{k}",) for k in range(1500)])
    con.commit()
    con.close()
    return str(path)


def test_pages_cannot_be_pointed_at_other_files(db, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("api-key-12345")
    c = TestClient(create_app(CFG, db))
    for route in ("/", "/icon.svg", "/sw.js", "/manifest.webmanifest"):
        r = c.get(route, params={"p": str(secret), "path": str(secret), "name": "../config.yaml"})
        assert r.status_code == 200 and "api-key-12345" not in r.text
    assert c.get("/static/../config.yaml").status_code == 404
    assert c.get("/docs").status_code == 404                  # no auto-generated API explorer either


def test_other_websites_cannot_read_the_api_unless_listed(db):
    evil = {"Origin": "https://evil.example"}
    assert "access-control-allow-origin" not in TestClient(create_app(CFG, db)).get("/api/summary", headers=evil).headers
    app = create_app({**CFG, "api": {"cors_origins": ["tauri://localhost"]}}, db)
    r = TestClient(app).get("/api/summary", headers={"Origin": "tauri://localhost"})
    assert r.headers["access-control-allow-origin"] == "tauri://localhost"
    assert "access-control-allow-origin" not in TestClient(app).get("/api/summary", headers=evil).headers


def test_token_protects_data_but_not_the_page_shell(db):
    c = TestClient(create_app(CFG, db, token="s3cret"))
    assert c.get("/").status_code == 200
    assert c.get("/api/signals").status_code == 401
    assert c.get("/api/signals", headers={"X-EdgeLab-Token": "wrong"}).status_code == 401
    assert c.get("/api/signals", headers={"X-EdgeLab-Token": "s3cret"}).json()[0]["name"] == "sig"
    assert c.get("/api/signals?token=s3cret").status_code == 200


def test_serve_refuses_to_listen_on_the_network_without_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGELAB_TOKEN", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("storage: x.db\n")
    with pytest.raises(SystemExit) as e:
        main(["--config", str(cfg), "serve", "--host", "0.0.0.0"])
    assert "token" in str(e.value)


def test_api_is_read_only_capped_and_survives_a_missing_database(db, tmp_path):
    c = TestClient(create_app(CFG, db))
    assert len(c.get("/api/orders?limit=999999").json()) == 1000
    assert c.get("/api/signals/sig'; DROP TABLE signals;--/trades").json() == []
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
    empty = TestClient(create_app(CFG, str(tmp_path / "nope.db")))
    assert empty.get("/api/daily").json() == [] and empty.get("/api/summary").json()["latest"] is None
    assert not (tmp_path / "nope.db").exists()                # reading must never create a database


def test_dashboard_escapes_every_value_it_renders():
    """Filer names, headlines and error text come from outside. None may reach the page as HTML."""
    import re
    from edgelab.api import WEB
    page = (WEB / "index.html").read_text()
    script = page[page.index("<script>"):]
    fields = r"[rst]\.(?:name|date|ticker|signal|rationale|reason|error|source|old|new|asof|state|status)\b"
    safe = ("esc(", "muted(", "chipFor(", "chip(", "encodeURIComponent(", "LAB_NOTES[")
    # 1. a table cell must never be a bare data field
    assert re.findall(rf"=>\s*{fields}\s*[,\]]", script) == []
    # 2. every ${...} that mentions a data field must pass it through an escaping helper
    unsafe = [m for m in re.findall(r"\$\{([^{}]*)\}", script) if re.search(fields, m) and not any(h in m for h in safe)]
    assert unsafe == [], f"unescaped data in templates: {unsafe}"
