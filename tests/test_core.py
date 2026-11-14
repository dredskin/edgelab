from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]

from edgelab.data.edgar import EdgarClient
from edgelab.data.events import Event
from edgelab.lab.headlines import detect_tickers
from edgelab.llm.client import ContaminationError, MockLLM
from edgelab.risk.sizing import DrawdownBudget, kelly_weight
from edgelab.tournament.stats import benjamini_hochberg, summarize

FORM4 = """<SEC-DOCUMENT><XML>
<ownershipDocument>
 <issuer><issuerTradingSymbol>abcd</issuerTradingSymbol></issuer>
 <reportingOwner><reportingOwnerId><rptOwnerName>Jane Doe</rptOwnerName></reportingOwnerId>
  <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle></reportingOwnerRelationship></reportingOwner>
 <nonDerivativeTable>
  <nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
   <transactionAmounts><transactionShares><value>1000</value></transactionShares>
   <transactionPricePerShare><value>4.50</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
  <nonDerivativeTransaction><transactionCoding><transactionCode>S</transactionCode></transactionCoding>
   <transactionAmounts><transactionShares><value>50</value></transactionShares>
   <transactionPricePerShare><value>5</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
 </nonDerivativeTable>
</ownershipDocument></XML></SEC-DOCUMENT>"""


def test_form4_parses_only_open_market_purchases():
    out = EdgarClient.parse_form4(FORM4)
    assert out == [dict(ticker="ABCD", owner="Jane Doe", role="CEO", shares=1000.0, price=4.5)]


def test_contamination_guard_refuses_pre_cutoff_documents():
    llm = MockLLM("2026-06-30")
    old = Event("earnings", pd.Timestamp("2026-05-01"), "X", {"_true_surprise": 0.5})
    new = Event("earnings", pd.Timestamp("2026-08-01"), "X", {"_true_surprise": 0.5})
    with pytest.raises(ContaminationError):
        llm.score_earnings(old)
    assert -1 <= llm.score_earnings(new)["surprise"] <= 1
    assert llm.refused == 1


def test_bh_controls_false_discoveries():
    p = np.r_[np.full(95, 0.5), [1e-6] * 5]
    q = benjamini_hochberg(p)
    assert (q[-5:] < 0.01).all() and (q[:95] > 0.1).all()


def test_summary_one_sided():
    rng = np.random.default_rng(0)
    assert summarize(rng.normal(0.01, 0.02, 400))["p"] < 0.001
    assert summarize(rng.normal(-0.01, 0.02, 400))["p"] > 0.99


def test_kelly_and_drawdown_budget():
    assert kelly_weight(-0.01, 0.05, 1) == 0
    assert kelly_weight(0.02, 0.05, 1, cap=0.05) == 0.05
    dd = DrawdownBudget(0.04, 0.15)
    assert dd.multiplier("2026-01-02", 1.00) == 1.0
    assert dd.multiplier("2026-01-20", 0.95) == 0.0       # -5% month-to-date: gate closed
    assert dd.multiplier("2026-02-02", 0.95) == 1.0       # new month, gate reopens
    assert dd.multiplier("2026-02-10", 0.84) == 0.0


def test_headline_ticker_detection():
    assert detect_tickers("Acme (NASDAQ: ACME) and Foo (TSXV: FOO.V) partner; $BAR soars") == {"ACME", "FOO.V", "BAR"}


def test_end_to_end_demo_promotes_real_edges_retires_decayed_and_rejects_decoys(tmp_path):
    """The whole lifecycle on the synthetic market, whose edges are planted and therefore known."""
    from edgelab.cli import main
    import json
    import sqlite3
    db = tmp_path / "t.db"
    main(["--config", str(ROOT / "config.yaml"), "demo", "--db", str(db)])
    con = sqlite3.connect(db)
    st = dict(con.execute("SELECT name, status FROM signals").fetchall())
    assert all(st[f"decoy_{k}"] == "candidate" for k in range(3))            # no edge: never promoted
    assert st["herding_fade"] == "promoted" and st["insider_cluster"] == "promoted"   # lasting edges
    # the earnings edge is planted to reverse part-way through: promoted while it worked, retired after
    decay = json.loads(con.execute("SELECT value FROM meta WHERE key='run'").fetchone()[0])["decay_from"]
    changes = con.execute("SELECT new, date FROM status_log WHERE signal='earnings_drift' ORDER BY date").fetchall()
    assert [c[0] for c in changes] == ["promoted", "retired"]
    assert changes[0][1] < decay < changes[1][1]
