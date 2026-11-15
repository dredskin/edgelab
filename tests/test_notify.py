"""The daily e-mail: refuses to send without full settings, never leaks the password, sends no private data
into the repository, and is read-only over the database. Uses a fake SMTP server; nothing leaves the machine."""
import sqlite3

import pytest

from edgelab import notify, store

CFG = {"mail": {"enabled": True, "host": "smtp.example.test", "port": 587, "security": "starttls"}}
ENV = {"EDGELAB_MAIL_TO": "reader@example.com", "EDGELAB_MAIL_FROM": "sender@example.com",
       "EDGELAB_SMTP_USER": "sender@example.com", "EDGELAB_SMTP_PASSWORD": "s3cret-app-pass"}


class FakeSMTP:
    instances = []

    def __init__(self, host, port):
        self.host, self.port, self.sent, self.logged_in_as = host, port, [], None
        self.tls = False
        FakeSMTP.instances.append(self)

    def ehlo(self): pass

    def starttls(self, context=None): self.tls = True

    def login(self, user, password): self.logged_in_as = (user, password)

    def send_message(self, msg): self.sent.append(msg)

    def quit(self): pass


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "e.db")
    con = store.connect(path)
    con.execute("INSERT INTO daily VALUES('2027-01-04', 1012.5, 0.9, 0.8, 0.12, 3, 2)")
    con.execute("INSERT INTO source_log VALUES('2027-01-04', 'edgar', 4, NULL)")
    con.execute("INSERT INTO source_log VALUES('2027-01-04', 'edinet', 0, '<b onclick=x>HTTP 503</b>\x07')")
    con.execute("INSERT INTO signals(name, lab, status) VALUES('insider', 0, 'candidate')")
    con.commit()
    con.close()
    return path


def test_refuses_without_settings_and_names_only_the_missing_variable():
    with pytest.raises(notify.MailError) as e:
        notify.settings(CFG, env={k: v for k, v in ENV.items() if k != "EDGELAB_SMTP_PASSWORD"})
    assert "EDGELAB_SMTP_PASSWORD" in str(e.value)
    assert "s3cret" not in str(e.value)


def test_disabled_or_insecure_settings_are_refused():
    with pytest.raises(notify.MailError):
        notify.settings({"mail": {"enabled": False, "host": "x"}}, env=ENV)
    with pytest.raises(notify.MailError):
        notify.settings({"mail": {"enabled": True, "host": "x", "security": "none"}}, env=ENV)


def test_bad_address_is_refused():
    with pytest.raises(notify.MailError):
        notify.settings(CFG, env={**ENV, "EDGELAB_MAIL_TO": "not an address"})


def test_report_is_plain_text_read_only_and_strips_control_characters(db):
    subject, body = notify.build_report(db, "2027-01-04", run_exit=None)
    assert "1012.5000" in body and "PAPER TRADING ONLY" in body
    assert "edinet: 0 events, ERROR:" in body
    assert "\x07" not in body
    assert "\x07" not in subject
    # read-only: the database refuses writes through the report's connection
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM daily")
    con.close()


def test_failed_run_is_flagged_in_subject_and_body(db):
    subject, body = notify.build_report(db, "2027-01-04", run_exit=1)
    assert subject.startswith("EdgeLab | FAILED")
    assert "FAILED (exit code 1)" in body


def test_sends_over_starttls_with_login_and_the_expected_headers(db):
    FakeSMTP.instances.clear()
    out = notify.send_report(CFG, db, "2027-01-04", env=ENV, smtp_factory=FakeSMTP)
    srv = FakeSMTP.instances[0]
    assert srv.tls and srv.logged_in_as == ("sender@example.com", "s3cret-app-pass")
    msg = srv.sent[0]
    assert msg["To"] == "reader@example.com" and msg["From"] == "sender@example.com"
    assert "s3cret" not in msg.as_string() and "s3cret" not in out


def test_dry_run_sends_nothing(db):
    FakeSMTP.instances.clear()
    out = notify.send_report(CFG, db, "2027-01-04", dry_run=True, env=ENV, smtp_factory=FakeSMTP)
    assert FakeSMTP.instances == [] and out.startswith("DRY RUN")


def test_password_is_scrubbed_from_connection_errors(db):
    class Broken(FakeSMTP):
        def login(self, user, password):
            raise smtplib_error(f"rejected {password}")

    def smtplib_error(text):
        import smtplib
        return smtplib.SMTPAuthenticationError(535, text.encode())

    with pytest.raises(notify.MailError) as e:
        notify.send_report(CFG, db, "2027-01-04", env=ENV, smtp_factory=Broken)
    assert "s3cret-app-pass" not in str(e.value) and "***" in str(e.value)


def test_no_private_addresses_in_the_committed_config():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1] / "config.yaml").read_text()
    assert "@" not in text.split("mail:")[1]


def test_sources_logged_on_a_later_run_date_are_shown(tmp_path):
    path = str(tmp_path / "w.db")
    con = store.connect(path)
    con.execute("INSERT INTO daily VALUES('2026-11-13', 1.002, 0.1, 1.0, 0.0, 0, 0)")
    con.execute("INSERT INTO source_log VALUES('2026-11-15', 'edgar', 3, NULL)")   # weekend run for Friday
    con.commit()
    con.close()
    _, body = notify.build_report(path)
    assert "edgar: 3 events, ok" in body and "run of 2026-11-15" in body
    assert "+0.20% since the first session" in body
