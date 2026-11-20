"""Automatic IB Gateway paper login through IBC. No Gateway, keyring or network is touched."""

import pytest

from edgelab import ibgateway


def test_settings_force_paper_mode_and_the_paper_port():
    text = ibgateway.settings_text("IbLoginId=edemo\nIbPassword=demouser\nTradingMode=live\nOverrideTwsApiPort=\n",
                                   "paperuser", "s3cret", "11:45 PM")
    assert "TradingMode=paper" in text and "TradingMode=live" not in text
    assert "OverrideTwsApiPort=4002" in text and "AcceptNonBrokerageAccountWarning=yes" in text
    assert "IbLoginId=paperuser" in text and "edemo" not in text and "demouser" not in text
    assert "AutoRestartTime=11:45 PM" in text and "ReadOnlyApi=no" in text


def _install(tmp_path, monkeypatch, opens_after=1):
    ibc, jts = tmp_path / "IBC", tmp_path / "Jts"
    (ibc / "scripts").mkdir(parents=True)
    (ibc / "scripts" / "StartIBC.bat").write_text("rem")
    (ibc / "config.ini").write_text("IbLoginId=edemo\nIbPassword=demouser\nTradingMode=live\n")
    for v in ("1040", "1045"):
        (jts / "ibgateway" / v).mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(ibgateway, "load_login", lambda: ("paperuser", "s3cret"))
    checks = {"n": 0}

    def port_open(*a, **k):
        checks["n"] += 1
        return checks["n"] > opens_after
    monkeypatch.setattr(ibgateway, "port_open", port_open)
    monkeypatch.setattr(ibgateway.time, "sleep", lambda s: None)
    cfg = {"broker": {"ibc": {"path": str(ibc), "gateway_path": str(jts), "auto_restart": "11:45 PM"}}}
    return cfg, tmp_path / "local" / "EdgeLab" / "ibc" / "config.ini"


def test_start_passes_no_password_on_the_command_line_and_deletes_the_settings(tmp_path, monkeypatch):
    cfg, temp = _install(tmp_path, monkeypatch)
    seen = {}

    def popen(args, **kw):
        seen["args"] = args
        seen["text"] = temp.read_text(encoding="latin-1")          # the file exists while IBC starts
    assert ibgateway.ensure_running(cfg, log=lambda m: None, popen=popen) == "started"
    assert "s3cret" not in " ".join(seen["args"]) and "/Mode:paper" in seen["args"] and "1045" in seen["args"]
    assert "IbPassword=s3cret" in seen["text"] and "TradingMode=paper" in seen["text"]
    assert not temp.exists()


def test_settings_are_deleted_even_when_gateway_never_answers(tmp_path, monkeypatch):
    cfg, temp = _install(tmp_path, monkeypatch, opens_after=10**9)
    with pytest.raises(ibgateway.GatewayError, match="did not answer"):
        ibgateway.ensure_running(cfg, log=lambda m: None, popen=lambda *a, **k: None, wait=0.01)
    assert not temp.exists()


def test_does_nothing_when_gateway_is_already_running(tmp_path, monkeypatch):
    cfg, temp = _install(tmp_path, monkeypatch, opens_after=0)
    called = []
    assert ibgateway.ensure_running(cfg, popen=lambda *a, **k: called.append(1)) == "already running"
    assert not called and not temp.exists()


def test_missing_login_is_a_clear_error(tmp_path, monkeypatch):
    cfg, _ = _install(tmp_path, monkeypatch)
    monkeypatch.setattr(ibgateway, "load_login",
                        lambda: (_ for _ in ()).throw(ibgateway.GatewayError("no IB paper login saved yet")))
    with pytest.raises(ibgateway.GatewayError, match="no IB paper login"):
        ibgateway.ensure_running(cfg, popen=lambda *a, **k: None)
