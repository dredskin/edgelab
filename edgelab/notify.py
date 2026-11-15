"""E-mail the daily EdgeLab report (paper trading only, read-only over the database).

Configuration split, so no private data is ever committed:
  config.yaml   mail.host / mail.port / mail.security (public server details)
  environment   EDGELAB_MAIL_TO       recipient address
                EDGELAB_MAIL_FROM     sender address (the Gmail account)
                EDGELAB_SMTP_USER     SMTP login (same Gmail account)
                EDGELAB_SMTP_PASSWORD Gmail app password (never a normal password, never in a file)

The report is plain text. Text from filings and error messages is untrusted: control characters are stripped
and it is never rendered as HTML, so nothing in it can run as code in a mail client.
"""
import os
import re
import smtplib
import ssl
import sqlite3
from email.message import EmailMessage

ENV_TO = "EDGELAB_MAIL_TO"
ENV_FROM = "EDGELAB_MAIL_FROM"
ENV_USER = "EDGELAB_SMTP_USER"
ENV_PASSWORD = "EDGELAB_SMTP_PASSWORD"
ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class MailError(Exception):
    pass


def _clean(text) -> str:
    return CONTROL.sub("", str(text))


def settings(cfg: dict, env=os.environ) -> dict:
    """Validate everything needed to send. Raises MailError with a message that names the missing setting
    (never its value), so the password cannot leak into logs."""
    m = cfg.get("mail") or {}
    if not m.get("enabled"):
        raise MailError("mail.enabled is false in config.yaml")
    host = (m.get("host") or "").strip()
    if not host:
        raise MailError("mail.host is not set in config.yaml")
    if m.get("security", "starttls") != "starttls":
        raise MailError("only mail.security: starttls is supported")
    missing = [name for name in (ENV_TO, ENV_FROM, ENV_USER, ENV_PASSWORD) if not env.get(name)]
    if missing:
        raise MailError("missing environment variable(s): " + ", ".join(missing))
    for name in (ENV_TO, ENV_FROM):
        if not ADDRESS.match(env[name]):
            raise MailError(f"{name} is not an e-mail address")
    return {"host": host, "port": int(m.get("port", 587)), "to": env[ENV_TO], "from": env[ENV_FROM],
            "user": env[ENV_USER], "password": env[ENV_PASSWORD]}


def build_report(db_path: str, date: str = None, run_exit: int = None) -> tuple:
    """Return (subject, body) from the database. Read-only."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM daily WHERE date=?", (date,)).fetchone() if date else None
        if row is None:
            row = con.execute("SELECT * FROM daily ORDER BY date DESC LIMIT 1").fetchone()
        day = row["date"] if row else (date or "unknown")
        # Sources are logged under the date the command ran, which can be after the market session (a weekend run
        # covers Friday). Show the latest run made on or after this session.
        src_day = con.execute("SELECT MIN(date) FROM source_log WHERE date >= ?", (day,)).fetchone()[0] \
            or con.execute("SELECT MAX(date) FROM source_log").fetchone()[0]
        sources = con.execute("SELECT source, events, error FROM source_log WHERE date=?", (src_day,)).fetchall()
        status = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM signals GROUP BY status")}
        open_positions = con.execute("SELECT COUNT(*) FROM book_positions").fetchone()[0]
    finally:
        con.close()

    lines = [f"EdgeLab daily report for {day} (PAPER TRADING ONLY, research; no real-money recommendation).", ""]
    if run_exit not in (None, 0):
        lines.append(f"! The daily run FAILED (exit code {run_exit}). Check the log on the computer.")
    if row is None:
        lines.append("! No session has been recorded in the database yet.")
    else:
        lines += [
            f"Paper equity: {row['equity']:.4f} (starts at 1.0000; {row['equity'] - 1:+.2%} since the first session)",
            f"Gross exposure: {row['gross']:.1%}" if row["gross"] is not None else "Gross exposure: n/a",
            f"New picks today: {row['picks']}    Orders sized today: {row['orders']}",
            f"Fragility multiplier: {row['risk_mult']:.2f}" if row["risk_mult"] is not None else "Fragility: n/a",
            f"Open paper positions in the book: {open_positions}",
        ]
    lines += ["", "Signal status: " + (", ".join(f"{k} {v}" for k, v in sorted(status.items())) or "none")]
    lines += ["", f"Data sources (run of {src_day}):" if src_day else "Data sources:"]
    if not sources:
        lines.append("  (no source activity logged for this date)")
    for s in sources:
        note = f"ERROR: {s['error']}" if s["error"] else "ok"
        lines.append(f"  {s['source']}: {s['events']} events, {note}")
    body = _clean("\n".join(lines)) + "\n"
    flag = "FAILED | " if run_exit not in (None, 0) else ""
    subject = _clean(f"EdgeLab | {flag}Daily paper report | {day}")
    return subject, body


def send(cfg: dict, subject: str, body: str, env=os.environ, smtp_factory=None) -> None:
    s = settings(cfg, env)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, s["from"], s["to"]
    msg.set_content(body)
    factory = smtp_factory or (lambda host, port: smtplib.SMTP(host, port, timeout=30))
    try:
        srv = factory(s["host"], s["port"])
        try:
            srv.ehlo()
            srv.starttls(context=ssl.create_default_context())
            srv.ehlo()
            srv.login(s["user"], s["password"])
            srv.send_message(msg)
        finally:
            try:
                srv.quit()
            except Exception:
                pass
    except Exception as e:  # scrub the password in case a library echoes it
        raise MailError(_clean(str(e)).replace(s["password"], "***")) from None


def send_report(cfg: dict, db_path: str, date: str = None, run_exit: int = None, dry_run=False,
                env=os.environ, smtp_factory=None) -> str:
    subject, body = build_report(db_path, date, run_exit)
    if dry_run:
        return f"DRY RUN (nothing sent)\nSubject: {subject}\n\n{body}"
    send(cfg, subject, body, env=env, smtp_factory=smtp_factory)
    return f"sent: {subject}"
