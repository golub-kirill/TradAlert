"""Ollama client — fail-open on every error, valid parse on success."""

from __future__ import annotations

import json

import requests

from core.advisor import ollama_client
from core.advisor.schemas import NewsRead

_HEADS = [{"headline": "Acme wins $2B contract, raises outlook"}]


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
        self.calls = 0

    def post(self, url, json=None, timeout=None):  # noqa: A002
        self.calls += 1
        self.last_url = url
        self.last_json = json
        if self._exc:
            raise self._exc
        return self._resp


def _content(obj) -> _Resp:
    return _Resp({"message": {"content": json.dumps(obj)}})


def _classify(session):
    return ollama_client.classify_news("AAPL", "Apple Inc.", "long", _HEADS, session=session)


# ── classify_news fail-open ──────────────────────────────────────────────────

def test_connection_error_returns_none():
    assert _classify(_Session(exc=requests.exceptions.ConnectionError())) is None


def test_timeout_returns_none():
    assert _classify(_Session(exc=requests.exceptions.Timeout())) is None


def test_http_error_returns_none():
    assert _classify(_Session(_Resp({}, raise_exc=requests.exceptions.HTTPError("404")))) is None


def test_empty_content_returns_none():
    # The Qwen3 thinking-mode trap: 200 OK but empty content.
    assert _classify(_Session(_Resp({"message": {"content": ""}}))) is None


def test_non_json_content_returns_none():
    assert _classify(_Session(_Resp({"message": {"content": "sorry"}}))) is None


def test_no_headlines_skips_the_call():
    s = _Session(_content({"news_stance": "neutral", "severity": "none", "material_news": ""}))
    assert ollama_client.classify_news("AAPL", "Apple Inc.", "long", [], session=s) is None
    assert s.calls == 0  # no model call when there is nothing to read


# ── classify_news success ────────────────────────────────────────────────────

def test_valid_response_parsed_to_newsread():
    s = _Session(_content(
        {"news_stance": "adverse", "severity": "major", "material_news": "guidance cut"}))
    nr = _classify(s)
    assert isinstance(nr, NewsRead)
    assert nr.stance == "adverse" and nr.severity == "major" and nr.material_news == "guidance cut"


def test_bad_stance_normalized():
    s = _Session(_content({"news_stance": "BANANA", "severity": "x", "material_news": ""}))
    nr = _classify(s)
    assert nr.stance == "unknown" and nr.severity == "none"


def test_request_body_disables_thinking_and_sets_news_schema():
    s = _Session(_content({"news_stance": "neutral", "severity": "none", "material_news": ""}))
    ollama_client.classify_news("AAPL", "Apple Inc.", "long", _HEADS, session=s, model="qwen3:8b")
    assert s.last_json["think"] is False  # avoids the empty-content trap
    assert s.last_json["stream"] is False
    assert s.last_json["model"] == "qwen3:8b"
    assert s.last_json["format"]["properties"]["news_stance"]["enum"] == [
        "supportive", "adverse", "neutral", "none"]
    assert s.last_url.endswith("/api/chat")


# ── transport primitives ─────────────────────────────────────────────────────

def test_ollama_chat_without_schema_returns_text():
    s = _Session(_Resp({"message": {"content": "  Markets steady.  "}}))
    out = ollama_client.ollama_chat("summarize", session=s)
    assert out == "  Markets steady.  "
    assert "format" not in s.last_json  # no schema passed → free-form text


def test_ask_json_parses_object():
    s = _Session(_content({"a": 1}))
    assert ollama_client.ask_json("p", {"type": "object"}, session=s) == {"a": 1}


def test_ask_json_non_object_returns_none():
    s = _Session(_Resp({"message": {"content": "[1,2,3]"}}))
    assert ollama_client.ask_json("p", {"type": "object"}, session=s) is None
