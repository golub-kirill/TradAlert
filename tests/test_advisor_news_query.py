"""Company-aware query building, relevance filtering, and price-recap tagging."""

from __future__ import annotations

from core.advisor import news_query as nq


def test_symbol_root_strips_exchange_suffix():
    assert nq.symbol_root("CNQ.TO") == "CNQ"
    assert nq.symbol_root("RCI-B.TO") == "RCI-B"
    assert nq.symbol_root("AAPL") == "AAPL"


def test_clean_company_name_drops_boilerplate():
    assert nq.clean_company_name("Canadian Natural Resources Limited") == "Canadian Natural Resources"
    assert nq.clean_company_name("Apple Inc.") == "Apple"


def test_relevance_keeps_right_company_drops_wrong():
    # The exact production bug: CNQ.TO cache was full of Cenovus stories.
    syms, names = nq.company_aliases("CNQ.TO", "Canadian Natural Resources Limited")
    assert nq.is_relevant("Canadian Natural Resources: A Dividend Machine", syms, names)
    assert nq.is_relevant("Record Production at TSX:CNQ", syms, names)
    assert not nq.is_relevant("Cenovus Surges 85% in a Year", syms, names)
    assert not nq.is_relevant("HighPeak Energy shares trade lower", syms, names)


def test_relevance_distinctive_first_token():
    # A single distinctive first token anchors (Cenovus), a generic one does not.
    syms, names = nq.company_aliases("CVE.TO", "Cenovus Energy Inc.")
    assert nq.is_relevant("Cenovus climbs after buyback", syms, names)


def test_price_recap_tagging():
    assert nq.is_price_recap("Apple stock hits all-time high at 318.79")
    assert nq.is_price_recap("Shares rise 6% Monday, outperform market")
    # Catalysts survive even when they mention a price move.
    assert not nq.is_price_recap("Test Industries slashes guidance, shares plunge 20%")
    assert not nq.is_price_recap("Analysts downgrade to Sell, cut target 30%")


def test_split_headlines_partitions_catalysts_and_recaps():
    heads = [{"headline": "Stock surges 8% to record high"},
             {"headline": "Company wins $2B defense contract"}]
    catalysts, recaps = nq.split_headlines(heads)
    assert len(catalysts) == 1 and len(recaps) == 1
    assert "contract" in catalysts[0]["headline"]


def test_is_noise_drops_clickbait_and_ads_keeps_real_news():
    assert nq.is_noise("3 Dividend Stocks to Buy Right Now")
    assert nq.is_noise("Should You Buy Apple Stock Before Earnings?")
    assert nq.is_noise("Is Nvidia a Buy?")
    assert nq.is_noise("This Stock Could Soar 300%")
    assert nq.is_noise("Apple Inc. (AAPL) Is A Top Stock In D. E. Shaw's Holdings")
    assert nq.is_noise("Great analysis", source="The Motley Fool")
    # Real catalysts must survive.
    assert not nq.is_noise("Apple slashes guidance, warns on iPhone demand")
    assert not nq.is_noise("Analysts downgrade Apple to Sell, cut target 30%")
    assert not nq.is_noise("Apple to acquire chipmaker for $4B")


def test_build_queries_prefers_company_name():
    qs = nq.build_queries("CNQ.TO", "Canadian Natural Resources Limited")
    assert any("Canadian Natural Resources" in q for q in qs)
    assert qs[0].startswith('"')  # quoted name query first
