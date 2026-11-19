"""E-mail the daily EdgeLab report (paper trading only, read-only over the database).

Configuration split, so no private data is ever committed:
  config.yaml   mail.host / mail.port / mail.security (public server details)
  environment   EDGELAB_MAIL_TO       recipient address
                EDGELAB_MAIL_FROM     sender address (the Gmail account)
                EDGELAB_SMTP_USER     SMTP login (same Gmail account)
                EDGELAB_SMTP_PASSWORD Gmail app password (never a normal password, never in a file)

Each e-mail has a plain-text part and an HTML part (cards, a trend chart, tables). Text from filings, signal
rationales and error messages is untrusted: control characters are stripped, and every value placed in the HTML
is escaped, so nothing in it can run or restyle the message. The chart is a PNG attached inline (Gmail drops SVG).
"""
import html
import json
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


def _collect(db_path: str, date: str = None) -> dict:
    """Everything the report shows, read once from the database (read-only)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = lambda sql, args=(): con.execute(sql, args).fetchall()
    try:
        row = con.execute("SELECT * FROM daily WHERE date=?", (date,)).fetchone() if date else None
        if row is None:
            row = con.execute("SELECT * FROM daily ORDER BY date DESC LIMIT 1").fetchone()
        day = row["date"] if row else (date or "unknown")
        prev = con.execute("SELECT * FROM daily WHERE date < ? ORDER BY date DESC LIMIT 1", (day,)).fetchone()
        history = q("SELECT date, equity FROM daily WHERE date <= ? ORDER BY date", (day,))
        # Sources are logged under the date the command ran, which can be after the market session (a weekend run
        # covers Friday). Show the latest run made on or after this session.
        src_day = con.execute("SELECT MIN(date) FROM source_log WHERE date >= ?", (day,)).fetchone()[0] \
            or con.execute("SELECT MAX(date) FROM source_log").fetchone()[0]
        sources = q("SELECT source, events, error FROM source_log WHERE date=? ORDER BY source", (src_day,))
        status = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM signals GROUP BY status")}
        signals = q("SELECT name, lab, status, n, mean, t FROM signals ORDER BY COALESCE(n, 0) DESC, name LIMIT 15")
        open_positions = con.execute("SELECT COUNT(*) FROM book_positions").fetchone()[0]
        orders = q("SELECT ticker, signal, weight, rationale FROM orders WHERE date=? ORDER BY weight DESC LIMIT 25", (day,))
        changes = q("SELECT signal, old, new, reason FROM status_log WHERE date=? ORDER BY signal", (day,))
        b = con.execute("SELECT value FROM meta WHERE key='broker_status'").fetchone()
        broker = json.loads(b[0]) if b else None
        if broker and broker.get("date") != day:
            broker = None                             # only report the status recorded for this session
    finally:
        con.close()
    return dict(day=day, row=row, prev=prev, history=[(h["date"], h["equity"]) for h in history], src_day=src_day,
                sources=sources, status=status, signals=signals, open_positions=open_positions, orders=orders,
                changes=changes, broker=broker)


def _subject(d: dict, run_exit) -> str:
    flag = "FAILED | " if run_exit not in (None, 0) else ""
    warn = "BROKER UNAVAILABLE | " if d["broker"] and not d["broker"].get("ok") else ""
    return _clean(f"EdgeLab | {flag}{warn}Daily paper report | {d['day']}")


def build_report(db_path: str, date: str = None, run_exit: int = None) -> tuple:
    """Return (subject, plain-text body). Read-only."""
    d = _collect(db_path, date)
    row, day, broker = d["row"], d["day"], d["broker"]
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
            f"Open paper positions in the book: {d['open_positions']}",
        ]
    if broker and not broker.get("ok"):
        lines += ["", f"! BROKER UNAVAILABLE: no paper orders were sent. {broker.get('note', '')}"]
    elif broker:
        lines += ["", f"Broker: {broker.get('note', 'connected')}"]
    if d["orders"]:
        lines += ["", "Orders sized today:"] + [f"  {o['ticker']:8s} {o['weight']:6.2%}  [{o['signal']}] {o['rationale'] or ''}"
                                                 for o in d["orders"]]
    for c in d["changes"]:
        lines.append(f"Status change: {c['signal']} {c['old']} -> {c['new']} ({c['reason']})")
    lines += ["", "Signal status: " + (", ".join(f"{k} {v}" for k, v in sorted(d["status"].items())) or "none")]
    lines += ["", f"Data sources (run of {d['src_day']}):" if d["src_day"] else "Data sources:"]
    if not d["sources"]:
        lines.append("  (no source activity logged for this date)")
    for s in d["sources"]:
        note = f"ERROR: {s['error']}" if s["error"] else "ok"
        lines.append(f"  {s['source']}: {s['events']} events, {note}")
    return _subject(d, run_exit), _clean("\n".join(lines)) + "\n"


# ---------- HTML ----------
NAVY, BLUE, PURPLE, GREEN, RED, AMBER = "#0f2350", "#2563eb", "#8b5cf6", "#16a34a", "#dc2626", "#d97706"
INK, MUTED, LINE, PAGE, CARD = "#0f172a", "#64748b", "#e2e8f0", "#f1f5f9", "#ffffff"
FONT = "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
CHART_CID = "edgelab-trend"


def e(text) -> str:
    """Escape untrusted text for HTML."""
    return html.escape(_clean("" if text is None else text), quote=True)


def _delta(value, fmt, good_if_up=True, neutral=False) -> str:
    if value is None or value != value:
        return f'<span style="color:{MUTED};font-size:13px;">no prior day</span>'
    if abs(value) < 1e-12:
        return f'<span style="color:{MUTED};font-size:13px;">&#9644; unchanged</span>'
    up = value > 0
    color = MUTED if neutral else (GREEN if up == good_if_up else RED)
    arrow = "&#9650;" if up else "&#9660;"
    return f'<span style="color:{color};font-size:13px;font-weight:600;">{arrow} {fmt.format(value)}</span>'


def _card(label, value, delta) -> str:
    return (f'<td width="33%" valign="top" style="padding:6px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CARD};'
            f'border:1px solid {LINE};border-radius:12px;"><tr><td style="padding:14px 16px;{FONT}">'
            f'<div style="color:{NAVY};font-size:14px;font-weight:700;">{label}</div>'
            f'<div style="color:{INK};font-size:28px;font-weight:700;line-height:36px;margin-top:4px;">{value}</div>'
            f'<div>{delta}</div></td></tr></table></td>')


def _section(title, inner) -> str:
    return (f'<tr><td style="padding:10px 6px 0;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{CARD};border:1px solid {LINE};border-radius:12px;"><tr><td style="padding:16px 18px;{FONT}">'
            f'<div style="color:{NAVY};font-size:17px;font-weight:700;margin-bottom:10px;">{title}</div>{inner}'
            f'</td></tr></table></td></tr>')


def _table(headers, rows, align=None) -> str:
    align = align or ["left"] * len(headers)
    th = "".join(f'<th align="{a}" style="color:{MUTED};font-size:12px;font-weight:600;padding:6px 8px;'
                 f'border-bottom:1px solid {LINE};">{h}</th>' for h, a in zip(headers, align))
    tr = "".join("<tr>" + "".join(f'<td align="{a}" style="color:{INK};font-size:13px;padding:7px 8px;'
                                  f'border-bottom:1px solid {LINE};">{c}</td>' for c, a in zip(r, align)) + "</tr>"
                 for r in rows)
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="{FONT}"><tr>{th}</tr>{tr}</table>'


def _badge(text, color) -> str:
    return (f'<span style="display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;'
            f'color:{color};background:{color}1a;">{text}</span>')


def trend_png(history, width=600, height=230) -> bytes:
    """The paper book's change since the first session, as a PNG. Returns b'' if matplotlib is missing or there
    are fewer than two sessions."""
    if len(history) < 2:
        return b""
    try:
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        import pandas as pd
    except Exception:
        return b""
    s = pd.Series([v for _, v in history], index=pd.to_datetime([d for d, _ in history])).iloc[-130:]
    pct = (s / s.iloc[0] - 1) * 100 if len(history) > 130 else (s - 1) * 100
    dd = (s / s.cummax() - 1) * 100
    fig, ax = plt.subplots(figsize=(width / 100, height / 100), dpi=200)
    ax.plot(pct.index, pct.values, color=BLUE, lw=2.2, marker="o", markersize=3 if len(pct) < 40 else 0, label="Paper book")
    ax.fill_between(pct.index, pct.values, 0, color=BLUE, alpha=0.08)
    ax.plot(dd.index, dd.values, color=PURPLE, lw=1.6, label="Drawdown from peak")
    ax.fill_between(dd.index, dd.values, 0, color=PURPLE, alpha=0.06)
    ax.axhline(0, color=LINE, lw=1)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(LINE)
    ax.grid(axis="y", color=LINE, lw=0.8)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:+.1f}%" if v else "0%"))
    span_days = (pct.index[-1] - pct.index[0]).days
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, span_days // 6)) if span_days < 60
                               else mdates.AutoDateLocator(maxticks=7))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.32), ncol=2, frameon=False, fontsize=8, labelcolor=INK)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def build_html(db_path: str, date: str = None, run_exit: int = None, dashboard_url: str = "") -> tuple:
    """Return (subject, html, chart_png). The HTML references the chart as cid:edgelab-trend when it exists."""
    d = _collect(db_path, date)
    row, prev, broker = d["row"], d["prev"], d["broker"]
    banners = []
    if run_exit not in (None, 0):
        banners.append((RED, f"The daily run FAILED (exit code {e(run_exit)}). Check the log on the computer."))
    if broker and not broker.get("ok"):
        banners.append((RED, f"Broker unavailable: no paper orders were sent. {e(broker.get('note', ''))}"))
    if row is None:
        banners.append((AMBER, "No session has been recorded in the database yet."))

    cards = ""
    if row is not None:
        eq, gross, mult = row["equity"], row["gross"], row["risk_mult"]
        d_eq = (eq / prev["equity"] - 1) if prev and prev["equity"] else None
        d_gross = (gross - prev["gross"]) * 100 if prev and gross is not None and prev["gross"] is not None else None
        d_mult = (mult - prev["risk_mult"]) if prev and mult is not None and prev["risk_mult"] is not None else None
        cards = ("<tr>" + _card("Paper book (since start)", f"{eq - 1:+.2%}", _delta(d_eq, "{:+.2%} today"))
                 + _card("Exposure", f"{(gross or 0):.0%}", _delta(d_gross, "{:+.1f} pp", neutral=True))
                 + _card("Risk multiplier", f"{(mult if mult is not None else 1):.2f}",
                         _delta(d_mult, "{:+.2f}", neutral=True)) + "</tr>")

    png = trend_png(d["history"])
    n = len(d["history"])
    chart = (f'<img src="cid:{CHART_CID}" width="100%" style="display:block;max-width:600px;height:auto;border:0;" '
             f'alt="Paper book trend">' if png else
             f'<div style="color:{MUTED};font-size:13px;">The chart appears once there are two sessions.</div>')
    span = "Since the first session" if n <= 130 else "Last 6 months"

    body = []
    for color, text in banners:
        body.append(f'<tr><td style="padding:10px 6px 0;"><div style="{FONT}background:{color}14;border:1px solid {color}55;'
                    f'border-radius:10px;padding:12px 14px;color:{color};font-size:14px;font-weight:600;">{text}</div></td></tr>')
    if cards:
        body.append(f'<tr><td style="padding:12px 0 0;"><table role="presentation" width="100%" cellpadding="0" '
                    f'cellspacing="0">{cards}</table></td></tr>')
    body.append(_section(f"Paper book trend ({span})", chart))

    if d["orders"]:
        rows = [(f"<b>{e(o['ticker'])}</b>", e(o["signal"]), f"{o['weight']:.2%}", e(o["rationale"])) for o in d["orders"]]
        body.append(_section("Orders sized today", _table(["Stock", "Signal", "Weight", "Why"], rows,
                                                         ["left", "left", "right", "left"])))
    else:
        body.append(_section("Orders sized today", f'<div style="color:{MUTED};font-size:13px;">None today. '
                                                    f'{row["picks"] if row is not None else 0} new pick(s), '
                                                    f'{d["open_positions"]} open paper position(s).</div>'))

    tone = {"promoted": GREEN, "candidate": BLUE, "retired": MUTED}
    pills = " ".join(_badge(f"{e(k)} {v}", tone.get(k, AMBER)) for k, v in sorted(d["status"].items())) or "none"
    sig_rows = [(e(s["name"]) + (" " + _badge("lab", PURPLE) if s["lab"] else ""), _badge(e(s["status"]), tone.get(s["status"], AMBER)),
                 f"{s['n'] or 0}", "&mdash;" if s["mean"] is None else f"{s['mean']:+.2%}",
                 "&mdash;" if s["t"] is None else f"{s['t']:+.2f}") for s in d["signals"]]
    changes = "".join(f'<div style="font-size:13px;color:{INK};margin-top:8px;">Status change: <b>{e(c["signal"])}</b> '
                      f'{e(c["old"])} &rarr; {e(c["new"])} <span style="color:{MUTED};">({e(c["reason"])})</span></div>'
                      for c in d["changes"])
    body.append(_section("Signal tournament", f'<div style="margin-bottom:10px;">{pills}</div>'
                         + _table(["Signal", "Status", "Trades", "Avg excess", "t"], sig_rows,
                                  ["left", "left", "right", "right", "right"]) + changes))

    src_rows = [(e(s["source"]), f"{s['events']}", _badge("error", RED) + f' <span style="color:{MUTED};">{e(s["error"])}</span>'
                 if s["error"] else _badge("ok", GREEN)) for s in d["sources"]]
    src = _table(["Source", "Events", "Status"], src_rows, ["left", "right", "left"]) if src_rows else \
        f'<div style="color:{MUTED};font-size:13px;">No source activity logged for this session.</div>'
    if broker and broker.get("ok"):
        src += f'<div style="margin-top:10px;font-size:13px;color:{INK};">Broker: {_badge("connected", GREEN)} {e(broker.get("note", ""))}</div>'
    body.append(_section("Data sources" + (f" (run of {e(d['src_day'])})" if d["src_day"] else ""), src))

    button = ""
    if dashboard_url.startswith("https://"):
        button = (f'<tr><td align="center" style="padding:18px 6px 4px;"><a href="{e(dashboard_url)}" '
                  f'style="{FONT}display:inline-block;background:{NAVY};color:#ffffff;text-decoration:none;font-size:15px;'
                  f'font-weight:700;padding:13px 34px;border-radius:10px;">View EdgeLab Dashboard</a></td></tr>')

    logo = (f'<td width="44" valign="middle"><div style="width:40px;height:40px;border-radius:20px;background:{NAVY};'
            f'color:#ffffff;{FONT}font-size:20px;font-weight:700;line-height:40px;text-align:center;">&#8599;</div></td>')
    day_label = e(d["day"])
    try:
        import pandas as pd
        ts = pd.Timestamp(d["day"])
        day_label = e(f"{ts:%A, %B} {ts.day}, {ts.year}")      # %-d does not exist on Windows
    except Exception:
        pass
    html_doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>EdgeLab daily paper report</title></head>
<body style="margin:0;padding:0;background:{PAGE};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE};"><tr><td align="center" style="padding:20px 10px;">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">
<tr><td style="padding:0 6px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CARD};border:1px solid {LINE};border-radius:12px;">
<tr><td style="padding:16px 18px;border-bottom:1px solid {LINE};"><table role="presentation" cellpadding="0" cellspacing="0"><tr>{logo}
<td valign="middle" style="{FONT}padding-left:10px;"><div style="color:{NAVY};font-size:20px;font-weight:700;">EdgeLab</div>
<div style="color:{MUTED};font-size:13px;">Paper Trading Research System</div></td></tr></table></td></tr>
<tr><td style="padding:16px 18px;{FONT}"><div style="color:{INK};font-size:26px;font-weight:700;">Daily Paper Report</div>
<div style="color:{MUTED};font-size:15px;margin-top:2px;">{day_label}</div></td></tr></table></td></tr>
{''.join(body)}
{button}
<tr><td style="padding:14px 6px;{FONT}color:{MUTED};font-size:12px;line-height:18px;" align="center">
PAPER TRADING ONLY. Research output, not a recommendation to trade real money.<br>
Paper book: 0% = the value at the first session. Exposure: share of the paper book invested.
Risk multiplier: 1.00 = full size; lower when markets look fragile or after losses.</td></tr>
</table></td></tr></table></body></html>"""
    return _subject(d, run_exit), html_doc, png


def compose(cfg: dict, db_path: str, date: str = None, run_exit: int = None, sender="", to="") -> EmailMessage:
    subject, text = build_report(db_path, date, run_exit)
    _, html_doc, png = build_html(db_path, date, run_exit, (cfg.get("mail") or {}).get("dashboard_url", ""))
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.set_content(text)
    msg.add_alternative(html_doc, subtype="html")
    if png:
        msg.get_payload()[1].add_related(png, maintype="image", subtype="png", cid=f"<{CHART_CID}>",
                                         filename="edgelab-trend.png")
    return msg


def send(cfg: dict, msg: EmailMessage, env=os.environ, smtp_factory=None) -> None:
    s = settings(cfg, env)
    del msg["From"], msg["To"]
    msg["From"], msg["To"] = s["from"], s["to"]
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
    except Exception as ex:  # scrub the password in case a library echoes it
        raise MailError(_clean(str(ex)).replace(s["password"], "***")) from None


def send_report(cfg: dict, db_path: str, date: str = None, run_exit: int = None, dry_run=False,
                env=os.environ, smtp_factory=None, html_out: str = None) -> str:
    msg = compose(cfg, db_path, date, run_exit)
    if html_out:
        _, html_doc, png = build_html(db_path, date, run_exit, (cfg.get("mail") or {}).get("dashboard_url", ""))
        from pathlib import Path
        out = Path(html_out)
        out.write_text(html_doc.replace(f"cid:{CHART_CID}", f"{out.stem}-trend.png"), encoding="utf-8")
        if png:
            (out.parent / f"{out.stem}-trend.png").write_bytes(png)
    if dry_run:
        return f"DRY RUN (nothing sent)\nSubject: {msg['Subject']}\n\n{msg.get_body(('plain',)).get_content()}"
    send(cfg, msg, env=env, smtp_factory=smtp_factory)
    return f"sent: {msg['Subject']}"
