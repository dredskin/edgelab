"""Publish a read-only snapshot of the dashboard to Cloudflare Pages behind Cloudflare Access.

Nothing listens on your computer. The daily run writes the same JSON the local API serves into `site/`,
which is uploaded outbound with Wrangler. The hosted copy never sees the database file and needs no token.

Safety rules:
  * `export` reads the database through the real API handlers (read-only), so the hosted data matches the local dashboard.
  * `verify-access` must see Cloudflare Access in front of the site (a redirect to cloudflareaccess.com, or 401/403).
    A page that answers 200 is reported as PUBLIC; the publish script then stops and tells the owner to fix it.
  * Secrets never go in the snapshot. Wrangler reads CLOUDFLARE_API_TOKEN from the environment.
"""
import json
import re
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WEB_FILES = ["index.html", "sw.js", "manifest.webmanifest", "icon.svg"]
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
STATIC_ENDPOINTS = ["/api/summary", "/api/daily", "/api/signals", "/api/log", "/api/orders", "/api/sources"]
ACCESS_MARKERS = ("cloudflareaccess.com", "/cdn-cgi/access")
# Access answers browsers with a redirect to its login page; plain scripts may just get 403.
BROWSER_HEADERS = {"Accept": "text/html,application/xhtml+xml", "User-Agent": "Mozilla/5.0 (EdgeLab access check)"}


class PublicSiteError(Exception):
    pass


def export(cfg: dict, db_path: str, out_dir: str, web_dir: str) -> list:
    """Write site/ from the database. Rebuilds the api/ folder every time; never copies the database."""
    from fastapi.testclient import TestClient      # needs httpx (the `live` extra)
    from .api import create_app

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    api_dir = out / "api"
    if api_dir.exists():
        shutil.rmtree(api_dir)
    written = []

    client = TestClient(create_app(cfg, db_path=db_path, token=None))
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def save(rel: str, payload):
        target = api_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"exported_at": stamp, "data": payload}, ensure_ascii=False, indent=1),
                          encoding="utf-8")
        written.append(str(target.relative_to(out)))

    for path in STATIC_ENDPOINTS:
        r = client.get(path)
        r.raise_for_status()
        save(path[len("/api/"):], r.json())

    for sig in client.get("/api/signals").json():
        name = sig.get("name", "")
        if not SAFE_NAME.match(name):               # never write a file path taken from the database
            continue
        r = client.get(f"/api/signals/{name}/trades", params={"limit": 40})
        r.raise_for_status()
        save(f"trades/{name}", r.json())             # flat name: a file cannot also be the folder for /signals

    web = Path(web_dir)
    for name in WEB_FILES:
        shutil.copyfile(web / name, out / name)
        written.append(name)
    index = out / "index.html"                       # tell the dashboard it is the hosted copy
    index.write_text(index.read_text(encoding="utf-8").replace(
        "<!--EDGELAB_STATIC-->", '<meta name="edgelab-static" content="1">'), encoding="utf-8")
    return written


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                                  # look at the redirect itself, do not follow it


def check_access(url: str, opener=None) -> str:
    """Return 'protected' or raise PublicSiteError. Checks the front page and one data file."""
    opener = opener or urllib.request.build_opener(_NoRedirect)
    for path in ("/", "/api/summary"):
        target = url.rstrip("/") + path
        try:
            resp = opener.open(urllib.request.Request(target, method="GET", headers=BROWSER_HEADERS), timeout=20)
            status, location = resp.status, resp.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            status, location = e.code, e.headers.get("Location", "") if e.headers else ""
        except Exception as e:
            raise PublicSiteError(f"{target} could not be reached ({type(e).__name__}); not verified") from None
        if 300 <= status < 400 and any(m in location for m in ACCESS_MARKERS):
            continue                                  # the only proof: a redirect to the Access login
        if status in (401, 403):                      # Cloudflare also sends 403 to scripts for bot reasons
            raise PublicSiteError(f"{target} answered {status} but not with an Access login redirect; "
                                  "not verified, check it in a private browser window")
        raise PublicSiteError(f"{target} answered {status} without Cloudflare Access: the site is PUBLIC")
    return "protected"


def check_empty(url: str, opener=None) -> str:
    """The bare project address cannot be put behind Access, so it must serve only the placeholder.
    Raise PublicSiteError if any snapshot data (an `exported_at` stamp) is served there."""
    opener = opener or urllib.request.build_opener(_NoRedirect)
    target = url.rstrip("/") + "/api/summary"
    try:
        resp = opener.open(urllib.request.Request(target, method="GET", headers=BROWSER_HEADERS), timeout=20)
        body = resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError:
        return "empty"                                # an error page holds no data
    except Exception as e:
        raise PublicSiteError(f"{target} could not be reached ({type(e).__name__}); not verified") from None
    if "exported_at" in body:
        raise PublicSiteError(f"{target} serves snapshot data: the bare address is PUBLIC")
    return "empty"
