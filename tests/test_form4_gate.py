"""Unit tests for the Form-4 PIVOT milestone F4-1: the EDGAR ownership parser
(scripts/form4_fetch.parse_ownership) and the pre-registered predictive gate
(scripts/form4_gate). Pure-logic / pure-math — no network, no I/O — so they run in the
normal ``pytest tests/`` suite and guard the gate that decides whether to build the
insider signal into the engine. See docs/backtest_out/form4_gate_prereg.md.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "studies"))

from form4_fetch import parse_ownership  # noqa: E402
from form4_gate import (  # noqa: E402
    composite_score, evaluate_gate, month_end_decision_days, rank_ic, trailing_features,
)

_OWNERSHIP_XML = """
<SEC-DOCUMENT><XML>
<ownershipDocument>
  <issuer><issuerTradingSymbol>test</issuerTradingSymbol></issuer>
  <reportingOwner><reportingOwnerId><rptOwnerCik>0001234567</rptOwnerCik></reportingOwnerId></reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2020-05-01</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>10.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2020-05-02</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>11.0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
</XML></SEC-DOCUMENT>
"""


def test_parse_ownership_extracts_buy_and_sell():
    rows = parse_ownership(_OWNERSHIP_XML, "2020-05-04", "2020-05-04T18:00:00Z")
    assert len(rows) == 2
    buy = next(r for r in rows if r["code"] == "P")
    sell = next(r for r in rows if r["code"] == "S")
    assert buy["ad"] == "A" and buy["shares"] == 1000 and buy["price"] == 10.5
    assert abs(buy["value"] - 10500.0) < 1e-9
    assert sell["ad"] == "D" and abs(sell["value"] - 5500.0) < 1e-9
    assert buy["owner_cik"] == "0001234567" and buy["symbol"] == "TEST"
    assert buy["filing_date"] == "2020-05-04"


def test_parse_ownership_no_ownership_block_returns_empty():
    assert parse_ownership("<SEC-DOCUMENT>no xml here</SEC-DOCUMENT>", "2020-01-01", "") == []


def test_trailing_features_point_in_time_window():
    fdates = np.array(["2020-01-01", "2020-02-15", "2020-03-01", "2019-09-01"],
                      dtype="datetime64[D]").astype("datetime64[ns]")
    codes = np.array(["P", "P", "S", "P"])
    values = np.array([1000.0, 2000.0, 500.0, 9999.0])
    owners = np.array(["a", "b", "a", "c"])
    f = trailing_features(fdates, codes, values, owners, np.datetime64("2020-03-15"))
    assert f["net_buy_count_90d"] == 1.0          # 2 P − 1 S (2019-09 excluded by 90d window)
    assert f["net_buy_value_90d"] == 2500.0        # (1000+2000) − 500
    assert f["distinct_buyers_90d"] == 2.0         # owners a, b bought
    assert f["active"] is True


def test_trailing_features_strictly_before_decision_day():
    fdates = np.array(["2020-03-15"], dtype="datetime64[D]").astype("datetime64[ns]")
    f = trailing_features(fdates, np.array(["P"]), np.array([1.0]), np.array(["a"]),
                          np.datetime64("2020-03-15"))   # same day → excluded (strictly <)
    assert f["active"] is False and f["net_buy_count_90d"] == 0.0


def test_rank_ic_perfect_and_inverse():
    assert abs(rank_ic(np.arange(10.0), np.arange(10.0))[0] - 1.0) < 1e-9
    assert abs(rank_ic(np.arange(10.0), np.arange(10.0)[::-1])[0] + 1.0) < 1e-9


def test_composite_score_neutral_fill():
    df = pd.DataFrame({"a": [np.nan, np.nan], "b": [np.nan, np.nan]})
    assert (composite_score(df) == 0.5).all()


def test_month_end_decision_days_last_trading_day():
    idx = pd.bdate_range("2010-01-01", "2010-03-31")
    me = month_end_decision_days(idx)
    assert list(pd.Series(me).dt.strftime("%Y-%m-%d")) == ["2010-01-29", "2010-02-26", "2010-03-31"]


def _panel(rng, *, signal: bool):
    rows = []
    for tk in range(10):
        for yr in range(2005, 2021):
            for m in range(12):
                nb = int(rng.integers(-2, 4))
                fwd = (0.004 * nb + rng.normal(0, 0.002)) if signal else rng.normal(0, 0.05)
                rows.append(dict(
                    ticker=f"T{tk}", date=pd.Timestamp(yr, m + 1, 28), year=yr,
                    fwd=fwd, fwd2=fwd, net_buy_count_90d=float(nb),
                    net_buy_value_90d=float(nb * 1000), distinct_buyers_90d=float(max(nb, 0)),
                    active=True))
    return pd.DataFrame(rows)


def test_evaluate_gate_proceeds_on_strong_signal():
    g = evaluate_gate(_panel(np.random.default_rng(1), signal=True))
    assert g["pass_ic"] and g["pass_econ"] and g["verdict"] == "PROCEED"


def test_evaluate_gate_closes_on_noise():
    g = evaluate_gate(_panel(np.random.default_rng(2), signal=False))
    assert g["verdict"] == "CLOSED"
