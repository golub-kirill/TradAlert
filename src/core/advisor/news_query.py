"""Company-aware news queries + relevance / novelty filtering.

The old fetcher keyed news off the raw symbol, which (a) missed thin symbols and
(b) leaked wrong-company stories (CNQ.TO returned Cenovus). This module builds
queries from the company NAME, filters fetched headlines to ones that actually
reference the ticker or company, and tags pure price-action recaps.

Price recaps ("X surges 12%", "shares hit all-time high") restate momentum the
quant engine already scored — the news layer must not re-count them as fresh
information, so they are separated from genuine catalysts (guidance, M&A,
downgrades, litigation, management changes) before the LLM ever sees them.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

__all__ = [
    "symbol_root", "clean_company_name", "company_aliases", "is_relevant",
    "is_price_recap", "is_noise", "split_headlines", "build_queries", "generate_queries",
]

# Exchange suffixes stripped so Finnhub / AlphaVantage see the base root symbol
# (CNQ.TO -> CNQ; the dotted form 403s / is rejected, the root works).
_EXCH_SUFFIX = re.compile(
    r"\.(TO|V|NE|CN|L|AX|HK|SW|PA|DE|MI|MC|AS|ST|OL|HE|TW|T|SI|KS|SS|SZ|NS|BO)$", re.I)

# Legal / fund boilerplate dropped from a name before it anchors relevance.
_NAME_NOISE = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|"
    r"sa|nv|ag|se|holdings|holding|group|trust|the|class|shares)\b", re.I)
# Leading words too generic to anchor on alone — keep the multi-word phrase instead.
_GENERIC_FIRST = frozenset((
    "canadian", "american", "national", "general", "united", "global", "first",
    "new", "north", "south", "east", "west", "international", "pacific",
    "atlantic", "western", "eastern", "central", "royal", "imperial", "standard",
    "universal", "continental", "us", "u.s.", "ishares", "spdr", "vanguard",
    "invesco", "core", "total",
))

_PRICE_MOVE = re.compile(
    r"\b(surg\w*|soar\w*|plung\w*|jump\w*|climb\w*|rall\w*|slump\w*|sli(d\w*|p\w*)|"
    r"tumbl\w*|drop\w*|fall\w*|fell|gain\w*|ris(e|es|ing)|rose|spike\w*|pop\w*|"
    r"sink\w*|slid|rebound\w*|outperform\w*|underperform\w*)\b"
    r"|hits?\s+\S*\s*(record|all[- ]?time|new|fresh|\d)"
    r"|(?:trading|stock|shares)\s+(up|down|higher|lower|flat)"
    r"|\d+(\.\d+)?%", re.I)
_CATALYST = re.compile(
    r"\b(earnings|guidance|revenue|profit|loss|downgrad\w*|upgrad\w*|rating|"
    r"price target|merger|acqui\w*|deal|buyout|takeover|lawsuit|sued|probe|"
    r"investigat\w*|sec\b|fda|approval|recall|layoff\w*|restructur\w*|dividend|"
    r"buyback|ceo|cfo|resign\w*|steps?\s+down|appoint\w*|contract|award|"
    r"partnership|launch\w*|unveil\w*|bankrupt\w*|default|warn\w*|beat|miss\w*|"
    r"outlook|forecast|halt\w*|delist\w*|offering|stake|activist)\b", re.I)


def symbol_root(ticker: str) -> str:
    """Base symbol for keyed APIs — strips the exchange suffix (CNQ.TO -> CNQ)."""
    t = (ticker or "").strip().upper()
    return _EXCH_SUFFIX.sub("", t) or t


def clean_company_name(name: str) -> str:
    """Name with legal/fund boilerplate removed, for anchoring + query variants."""
    s = _NAME_NOISE.sub(" ", name or "")
    s = re.sub(r"[.,()]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def company_aliases(ticker: str, company_name: str = "") -> tuple[list[str], list[str]]:
    """(symbol_aliases, name_aliases) used to test headline relevance.

    Symbol aliases match case-sensitively on word boundaries (tickers appear as
    ``(AAPL)``, ``TSX:CNQ``); name aliases match case-insensitively as substrings.
    """
    root = symbol_root(ticker)
    syms = {s for s in (ticker.upper(), root) if len(s) >= 2}
    names: list[str] = []
    cleaned = clean_company_name(company_name).lower()
    if len(cleaned) >= 4:
        names.append(cleaned)
    tokens = cleaned.split()
    if len(tokens) >= 2:
        names.append(" ".join(tokens[:2]))
    if tokens and len(tokens[0]) >= 4 and tokens[0] not in _GENERIC_FIRST:
        names.append(tokens[0])
    # dedupe, preserve order
    seen, out = set(), []
    for a in names:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return sorted(syms), out


def is_relevant(text: str, symbol_aliases: list[str], name_aliases: list[str]) -> bool:
    """True when a headline plausibly references this ticker or company."""
    t = text or ""
    low = t.lower()
    if any(a in low for a in name_aliases):
        return True
    return any(re.search(rf"\b{re.escape(s)}\b", t) for s in symbol_aliases)


def is_price_recap(headline: str) -> bool:
    """Pure price-action recap (momentum the engine already scored) with no
    orthogonal catalyst — the news layer should not count it as fresh signal."""
    h = headline or ""
    return bool(_PRICE_MOVE.search(h)) and not _CATALYST.search(h)


# High-precision clickbait / listicle / opinion-mill patterns — chosen so real
# breaking news (earnings, M&A, downgrades, litigation) does not match.
_CLICKBAIT = re.compile(
    r"\b\d+\s+(stocks?|reasons?|things?|ways?|dividend\s+stocks?|growth\s+stocks?)\b"
    r"|should\s+you\s+(buy|sell)"
    r"|\bis\s+[\w.\-]+(\s+stock)?\s+a\s+(buy|sell|good|great|strong|smart)\b"
    r"|better\s+buy|here'?s\s+why|reasons?\s+to\s+(buy|own|sell)"
    r"|stocks?\s+to\s+buy\b|promising\s+stocks?|stock\s+picks?\b"
    r"|among\s+the\b.{0,40}\bstocks?\b|one\s+of\s+the\b.{0,40}\bstocks?\b"
    r"|could\s+(soar|skyrocket|explode|double|triple|make\s+you)|millionaire"
    r"|if\s+you(\'?d)?\s+(had\s+)?invested|turn(ed|ing)?\s+\$?\d[\d,]*\s+into"
    r"|\$\d[\d,]*\s+invested|price\s+prediction|prediction:|forecast\s+20\d\d"
    r"|where\s+will\s+.*\bin\s+\d+\s+years|no[-\s]?brainer|magnificent\s+seven"
    r"|top\s+stock\b|hedge\s+funds?\b|billionaire|jim\s+cramer|cathie\s+wood"
    r"|warren\s+buffett|you\s+won'?t\s+believe|shocking|smart\s+money"
    r"|top\s+\d+\s+(pick|stock)",
    re.I)
_AD = re.compile(r"\b(sponsored|advertisement|promoted|partner\s+content|paid\s+post)\b", re.I)
# Sources that are predominantly clickbait / auto-generated listicles.
_NOISE_SOURCE = re.compile(
    r"motley\s*fool|zacks|gurufocus|simply\s*wall|insider\s*monkey|24/7\s*wall", re.I)


def is_noise(headline: str, source: str = "") -> bool:
    """Clickbait, ad, or opinion-mill headline the advisor should not treat as
    news — listicles ("3 Stocks to Buy"), hype ("could soar"), 13F fluff, and
    known clickbait publishers."""
    h = headline or ""
    return bool(_CLICKBAIT.search(h) or _AD.search(h) or _NOISE_SOURCE.search(source or ""))


def split_headlines(headlines: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition into (catalysts, price_recaps) by the recap heuristic."""
    catalysts, recaps = [], []
    for h in headlines:
        text = str(h.get("headline") or h.get("title") or "")
        (recaps if is_price_recap(text) else catalysts).append(h)
    return catalysts, recaps


def build_queries(ticker: str, company_name: str = "") -> list[str]:
    """Deterministic news queries — company name preferred, symbol as backstop."""
    root = symbol_root(ticker)
    name = (company_name or "").strip()
    qs: list[str] = []
    if name:
        qs.append(f'"{name}" stock')
        qs.append(f'"{name}"')
    qs.append(f"{root} stock news")
    seen, out = set(), []
    for q in qs:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def generate_queries(
        ticker: str,
        company_name: str,
        *,
        endpoint: str,
        model: str,
        timeout: int = 20,
        session=None,
) -> list[str] | None:
    """Optional: ask the local model for 1–3 focused news queries. Cached by the
    caller (queries are stable per ticker). Fail-open → None, caller uses
    ``build_queries``. Imported lazily so this module has no hard requests dep."""
    from core.advisor.ollama_client import ask_json

    schema = {
        "type": "object",
        "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
        "required": ["queries"],
    }
    prompt = (
        "You build web-news search queries for one publicly traded company. Given "
        "the ticker and name, return 1-3 short queries that would surface recent, "
        "company-specific news (earnings, guidance, M&A, management, litigation) — "
        "NOT generic market commentary. Prefer the full company name.\n\n"
        f"Ticker: {ticker}\nCompany: {company_name or '(unknown)'}\n\n"
        'Return JSON: {"queries": ["...", "..."]}.'
    )
    try:
        data = ask_json(prompt, schema, endpoint=endpoint, model=model,
                        timeout=timeout, session=session)
    except Exception as exc:
        logger.warning("news query generation failed for %s — %s", ticker, exc)
        return None
    if not data:
        return None
    qs = [str(q).strip() for q in (data.get("queries") or []) if str(q).strip()]
    return qs[:3] or None
