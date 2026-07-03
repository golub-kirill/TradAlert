"""Ollama client — fail-open on every error, valid parse on success."""

from __future__ import annotations

import json

import requests

from core.advisor import client as advisor_client
from core.advisor.schemas import AdvisorInput, AdvisorVerdict


class _Resp:
    def __init__(self, payload, *, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return self._payload


class _Session:
    """Fake requests.Session capturing the last POST body."""

    def __init__(self, resp=None, *, exc=None):
        self._resp = resp
        self._exc = exc
        self.last_json = None
        self.last_url = None

    def post(self, url, json=None, timeout=None):  # noqa: A002
        self.last_url = url
        self.last_json = json
        if self._exc:
            raise self._exc
        return self._resp


def _input() -> AdvisorInput:
    return AdvisorInput(
        ticker="AAPL", direction="long", signal_type="momentum",
        stop_price=182.5, target_price=205.0, min_rr=2.5,
        market_regime="BULL", ticker_trend="UPTREND", reason="setup",
    )


def _content(obj) -> _Resp:
    return _Resp({"message": {"content": json.dumps(obj)}})


def test_connection_error_returns_none():
    s = _Session(exc=requests.exceptions.ConnectionError())
    assert advisor_client.ask_llm(_input(), session=s) is None


def test_timeout_returns_none():
    s = _Session(exc=requests.exceptions.Timeout())
    assert advisor_client.ask_llm(_input(), session=s) is None


def test_http_error_returns_none():
    s = _Session(_Resp({}, raise_exc=requests.exceptions.HTTPError("404")))
    assert advisor_client.ask_llm(_input(), session=s) is None


def test_empty_content_returns_none():
    # The Qwen3 thinking-mode trap: 200 OK but empty content.
    s = _Session(_Resp({"message": {"content": ""}}))
    assert advisor_client.ask_llm(_input(), session=s) is None


def test_non_json_content_returns_none():
    s = _Session(_Resp({"message": {"content": "sorry, I cannot help"}}))
    assert advisor_client.ask_llm(_input(), session=s) is None


def test_missing_verdict_field_returns_none():
    s = _Session(_content({"confidence": 0.5, "reasoning": "x", "risks": ""}))
    assert advisor_client.ask_llm(_input(), session=s) is None


def test_valid_response_parsed_to_verdict():
    s = _Session(_content({"verdict": "flag", "confidence": 0.7,
                           "reasoning": "earnings soon", "risks": "gap"}))
    v = advisor_client.ask_llm(_input(), session=s)
    assert isinstance(v, AdvisorVerdict)
    assert v.verdict == "flag" and v.confidence == 0.7 and v.risks == "gap"


def test_verdict_uppercase_is_normalized():
    s = _Session(_content({"verdict": "AGREE", "confidence": 0.8,
                           "reasoning": "ok", "risks": ""}))
    v = advisor_client.ask_llm(_input(), session=s)
    assert v is not None and v.verdict == "agree"


def test_request_body_disables_thinking_and_sets_schema():
    s = _Session(_content({"verdict": "agree", "confidence": 0.8,
                           "reasoning": "ok", "risks": ""}))
    advisor_client.ask_llm(_input(), session=s, model="qwen3:8b")
    assert s.last_json["think"] is False  # avoids the empty-content trap
    assert s.last_json["stream"] is False
    assert s.last_json["model"] == "qwen3:8b"
    assert s.last_json["format"]["properties"]["verdict"]["enum"] == [
        "agree", "disagree", "flag"]
    assert s.last_url.endswith("/api/chat")


def test_ollama_chat_without_schema_returns_text():
    s = _Session(_Resp({"message": {"content": "  Markets steady.  "}}))
    out = advisor_client.ollama_chat("summarize", session=s)
    assert out == "  Markets steady.  "
    assert "format" not in s.last_json  # no schema passed → free-form text
