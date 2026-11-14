"""The hosted snapshot: exports only read-only JSON and the dashboard files, never the database, and refuses to
call a site protected unless Cloudflare Access answers in front of it. No network is used."""
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from edgelab import snapshot, store

ROOT = Path(__file__).resolve().parents[1]
CFG = {"_root": str(ROOT), "storage": "x.db", "risk": {}, "tournament": {}}


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "s.db")
    con = store.connect(path)
    con.execute("INSERT INTO signals(name, lab, status) VALUES('insider', 0, 'candidate')")
    con.execute("INSERT INTO signals(name, lab, status) VALUES('../escape', 0, 'candidate')")
    con.execute("INSERT INTO daily VALUES('2027-01-04', 1000.0, 0.9, 1.0, 0.1, 2, 1)")
    con.commit()
    con.close()
    return path


def test_export_writes_api_json_and_dashboard_but_never_the_database(db, tmp_path):
    out = tmp_path / "site"
    files = snapshot.export(CFG, db, str(out), str(ROOT / "web"))
    assert (out / "api" / "summary").exists()
    assert (out / "api" / "trades" / "insider").exists()
    assert 'name="edgelab-static"' in (out / "index.html").read_text()
    assert json.loads((out / "api" / "daily").read_text())["data"][0]["date"] == "2027-01-04"
    assert (out / "index.html").exists() and (out / "sw.js").exists()
    assert not any(p.suffix == ".db" for p in out.rglob("*"))
    assert not any("escape" in f for f in files)              # unsafe names are skipped, never written


def test_export_rebuilds_the_api_folder_every_time(db, tmp_path):
    out = tmp_path / "site"
    snapshot.export(CFG, db, str(out), str(ROOT / "web"))
    stale = out / "api" / "trades" / "gone"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("old")
    snapshot.export(CFG, db, str(out), str(ROOT / "web"))
    assert not stale.exists()


def test_export_contains_no_token_or_secret_values(db, tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-secret-value")
    out = tmp_path / "site"
    snapshot.export(CFG, db, str(out), str(ROOT / "web"))
    for p in out.rglob("*"):
        if p.is_file():
            assert "cf-secret-value" not in p.read_text(errors="ignore")


class FakeOpener:
    def __init__(self, responses):
        self.responses = responses            # path suffix -> ("status", location) or ("error", reason)

    def open(self, req, timeout=None):
        from urllib.parse import urlsplit
        path = urlsplit(req.full_url).path.lstrip("/")
        kind, value = self.responses[path]
        if kind == "error":
            raise value
        status, location = value
        if 300 <= status < 400:
            raise urllib.error.HTTPError(req.full_url, status, "redirect", {"Location": location}, io.BytesIO())
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO())
        class R:
            pass
        r = R()
        r.status, r.headers = status, {}
        return r


ACCESS = "https://team.cloudflareaccess.com/cdn-cgi/access/login/site"


def test_access_redirect_counts_as_protected():
    op = FakeOpener({"": ("status", (302, ACCESS)), "api/summary": ("status", (302, ACCESS))})
    assert snapshot.check_access("https://edgelab.example.pages.dev", opener=op) == "protected"


def test_a_page_answering_200_is_reported_as_public():
    op = FakeOpener({"": ("status", (200, "")), "api/summary": ("status", (200, ""))})
    with pytest.raises(snapshot.PublicSiteError) as e:
        snapshot.check_access("https://edgelab.example.pages.dev", opener=op)
    assert "PUBLIC" in str(e.value)


def test_unreachable_site_is_not_treated_as_verified():
    op = FakeOpener({"": ("error", OSError("dns")), "api/summary": ("error", OSError("dns"))})
    with pytest.raises(snapshot.PublicSiteError) as e:
        snapshot.check_access("https://edgelab.example.pages.dev", opener=op)
    assert "not verified" in str(e.value)
