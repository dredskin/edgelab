"""Start IB Gateway with an automatic PAPER login through IBC (Windows).

IBC (github.com/IbcAlpha/IBC, retired 1 Sept 2026, last release 3.24.2 for Gateway 10.48+) fills in the Gateway login
dialog. The owner approved storing the IB paper login on this PC, on these terms:

  * The username and password live in Windows Credential Manager (via the `keyring` package), never in a file
    in the repository and never on a command line (command lines are visible in the process list).
  * At start, a temporary IBC settings file is written in %LOCALAPPDATA%\\EdgeLab\\ibc, readable only by the
    current Windows account, and deleted as soon as Gateway answers on its API port (or after 3 minutes).
  * Trading mode is always PAPER and the API port always 4002; the broker adapter still refuses any account whose
    id does not start with DU.

`ensure_running()` is safe to call repeatedly (a scheduled task does so at logon, every evening, and on Sunday
mornings after IB's weekly reset): if Gateway already answers on 4002 it does nothing.
"""
import getpass
import os
import re
import socket
import subprocess
import time
from pathlib import Path

SERVICE = "edgelab-ibkr-paper"
PORT = 4002
WAIT_SECONDS = 180


class GatewayError(Exception):
    pass


def _keyring():
    try:
        import keyring
    except ImportError:
        raise GatewayError('the keyring package is missing: pip install -e ".[live,ibkr]"') from None
    return keyring


def save_login(username: str = None, password: str = None) -> str:
    """Store the IB paper login in Windows Credential Manager (prompts if not given)."""
    kr = _keyring()
    username = username or input("IB PAPER trading username: ").strip()
    password = password or getpass.getpass("IB PAPER trading password (not shown): ")
    if not username or not password:
        raise GatewayError("username and password are both required")
    kr.set_password(SERVICE, "username", username)
    kr.set_password(SERVICE, "password", password)
    return username


def load_login() -> tuple:
    kr = _keyring()
    user, pw = kr.get_password(SERVICE, "username"), kr.get_password(SERVICE, "password")
    if not user or not pw:
        raise GatewayError("no IB paper login saved yet: run `edgelab ib-gateway save-login`")
    return user, pw


def port_open(port: int = PORT, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def gateway_version(jts: Path) -> str:
    """Major version of the newest installed Gateway, e.g. 'C:\\Jts\\ibgateway\\1045' -> '1045'."""
    versions = sorted((p.name for p in (jts / "ibgateway").glob("*") if p.is_dir() and p.name.isdigit()), key=int)
    if not versions:
        raise GatewayError(f"no IB Gateway found under {jts / 'ibgateway'}; install the offline (stable) Gateway")
    return versions[-1]


def settings_text(template: str, user: str, pw: str, restart_at: str) -> str:
    """IBC config.ini with the login and EdgeLab's fixed safety settings filled in."""
    values = {
        "IbLoginId": user, "IbPassword": pw,
        "TradingMode": "paper",                       # never live
        "OverrideTwsApiPort": str(PORT),
        "ReadOnlyApi": "no",                          # orders need it off
        "AcceptNonBrokerageAccountWarning": "yes",    # the paper-account notice blocks the API until accepted
        "AcceptIncomingConnectionAction": "reject",   # only addresses trusted in Gateway's own API settings
        "ExistingSessionDetectedAction": "primary",
        "AutoRestartTime": restart_at,                # daily restart without a new login
        "ColdRestartTime": "",                        # the weekly full login is done by our scheduled task
        "CommandServerPort": "0",
        "AllowBlindTrading": "no",
    }
    out, seen = [], set()
    for line in template.splitlines():
        m = re.match(r"^([A-Za-z][A-Za-z0-9]*)=", line)
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={values[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    return "\n".join(out) + "\n"


def _private_file(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="latin-1", errors="strict")
    if os.name == "nt":                               # current Windows account only, no inherited access
        user = os.environ.get("USERNAME", "")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                       check=True, capture_output=True)


def ensure_running(cfg: dict, log=print, popen=subprocess.Popen, wait=WAIT_SECONDS) -> str:
    """Start Gateway through IBC unless it already answers on the paper port. Returns a short status."""
    if port_open():
        return "already running"
    c = (cfg.get("broker") or {}).get("ibc") or {}
    ibc = Path(c.get("path", r"C:\IBC"))
    jts = Path(c.get("gateway_path", r"C:\Jts"))
    start = ibc / "scripts" / "StartIBC.bat"
    template = ibc / "config.ini"
    for p in (start, template):
        if not p.exists():
            raise GatewayError(f"{p} not found: install IBC to {ibc} (see deploy\\windows\\README.md)")
    version = str(c.get("gateway_version") or gateway_version(jts))
    user, pw = load_login()
    temp = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "EdgeLab" / "ibc" / "config.ini"
    try:
        _private_file(temp, settings_text(template.read_text(encoding="latin-1"), user, pw,
                                          c.get("auto_restart", "11:45 PM")))
        args = ["cmd", "/c", str(start), version, "/Gateway", f"/TwsPath:{jts}", f"/IbcPath:{ibc}",
                f"/Config:{temp}", "/Mode:paper", "/On2FATimeout:exit"]
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        popen(args, creationflags=flags, cwd=str(ibc))
        log(f"started IBC (Gateway {version}, paper, port {PORT}); waiting for the API")
        deadline = time.time() + wait
        while time.time() < deadline:
            if port_open():
                return "started"
            time.sleep(3)
        raise GatewayError(f"Gateway did not answer on port {PORT} within {wait} s; see {ibc / 'Logs'}")
    finally:
        try:
            temp.unlink()                             # the password never stays on disk
        except FileNotFoundError:
            pass
