"""Hermetic tests for the TradAlert control API (api.main:app).

Covers read endpoints (fail-open shape), config validation + a surgical
single-line write, the job/SSE seam, the action launchers (stubbed — no real
process), the journal-only positions invariant, and the optional token guard.

Every test is deterministic and isolated: no real files are mutated (writes go
to a tmp_path), no subprocess is spawned (launch is stubbed), and no network or
live DB is required (reads fail open to []/{}).
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
import api  # noqa: F401,E402  (path + dotenv bootstrap on import)
from fastapi.testclient import TestClient  # noqa: E402
from api.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


# ── health ──────────────────────────────────────────────────────────────────

def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ── reads: status + documented shape (fail-open, no specific data) ────────────

def test_positions_read_shape(client):
    r = client.get("/api/positions")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_scanner_runs_read_shape(client):
    r = client.get("/api/scanner/runs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_scanner_latest_read_shape(client):
    r = client.get("/api/scanner/latest")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    for key in ("run", "fired", "stand_down"):
        assert key in body
    assert isinstance(body["fired"], list)


def test_backtests_read_shape(client):
    r = client.get("/api/backtests")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_equity_curve_shape(client):
    # Unknown run id fails open to an empty, cumulative-shaped curve (deterministic).
    r = client.get("/api/backtests/99999999/equity")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == 99999999
    assert isinstance(body["points"], list)
    for p in body["points"]:
        assert set(p) == {"date", "equity_r"}


def test_config_read_shape(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    for key in ("filters", "settings", "editable"):
        assert key in body
    assert isinstance(body["filters"], dict)
    assert isinstance(body["settings"], dict)
    assert isinstance(body["editable"], list)


# ── charts: graceful unknown ticker ───────────────────────────────────────────

def test_chart_unknown_ticker_404(client):
    r = client.get("/api/charts/__nope__")
    assert r.status_code == 404


# ── job stream: unknown id ends gracefully ────────────────────────────────────

def test_job_stream_unknown_id(client):
    r = client.get("/api/backtests/jobs/deadbeef/stream")
    assert r.status_code == 200
    assert "event: status" in r.text
    assert "unknown" in r.text


# ── config write: validation (these never reach the filesystem) ───────────────

def test_config_write_locked_key_400(client):
    # entry_slippage_pct is a real engine param deliberately NOT in the editable whitelist.
    r = client.post(
        "/api/config",
        json={"updates": {"filters.execution.entry_slippage_pct": 0.01}},
    )
    assert r.status_code == 400


def test_config_write_empty_updates_400(client):
    r = client.post("/api/config", json={"updates": {}})
    assert r.status_code == 400


def test_config_write_out_of_range_400(client):
    r = client.post(
        "/api/config",
        json={"updates": {"settings.risk.max_open_risk": 999}},
    )
    assert r.status_code == 400


# ── config write: hermetic success path (surgical single-line edit) ───────────

def test_config_write_success_surgical(client, tmp_path, monkeypatch):
    pytest.importorskip("ruamel.yaml")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    settings = cfg_dir / "settings.yaml"
    original = "risk:\n  max_open_risk: 5.0  # note\n"
    settings.write_text(original, encoding="utf-8")

    # Point the config router at the throwaway dir so the write is contained.
    monkeypatch.setattr("api.routers.config.CONFIG", cfg_dir)

    r = client.post(
        "/api/config",
        json={"updates": {"settings.risk.max_open_risk": 6.5}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "settings.risk.max_open_risk" in body["written"]

    written = settings.read_text(encoding="utf-8")
    # Only the value token changed: the inline comment and 'risk:' line survive.
    assert "# note" in written
    assert written.splitlines()[0] == "risk:"
    assert "6.5" in written
    assert "5.0" not in written

    # Re-parses cleanly to the new value.
    import yaml
    assert yaml.safe_load(written)["risk"]["max_open_risk"] == 6.5


# ── action launchers: stubbed, no real subprocess ─────────────────────────────

def test_scan_launch_stubbed(client, monkeypatch):
    captured = {}

    def fake_launch(cmd):
        captured["cmd"] = cmd
        return "testjob"

    monkeypatch.setattr("api.routers.scanner.launch", fake_launch)
    r = client.post("/api/scan", json={"morning": False, "force": False})
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "testjob"
    assert any("main.py" in part for part in captured["cmd"])


def test_backtest_run_launch_stubbed(client, monkeypatch):
    captured = {}

    def fake_launch(cmd):
        captured["cmd"] = cmd
        return "testjob"

    monkeypatch.setattr("api.routers.backtests.launch", fake_launch)
    r = client.post("/api/backtests/run", json={"mode": "baseline"})
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "testjob"
    assert any("run_backtest" in part for part in captured["cmd"])


# ── positions: journal-only invariant (mutations hit the adapter, never a broker)

class _RecordingAdapter:
    """Records calls; mimics the journal adapter surface with success returns."""

    def __init__(self):
        self.calls = []

    def open(self, ticker, entry_price, entry_date, side="long",
             stop_price=None, notes=""):
        self.calls.append(("open", ticker, entry_price, side))
        return 42

    def close(self, position_id, exit_price, exit_date):
        self.calls.append(("close", position_id, exit_price))
        return True

    def update_stop(self, position_id, stop_price):
        self.calls.append(("update_stop", position_id, stop_price))
        return True

    def scale_out(self, position_id, exit_price, exit_date, fraction):
        self.calls.append(("scale_out", position_id, exit_price, fraction))
        return 99

    def edit_position(self, position_id, *, entry_price=None, stop_price=None,
                      initial_stop=None, exit_price=None, notes=None):
        self.calls.append(("edit_position", position_id))
        return True


def _patch_adapter(monkeypatch):
    fake = _RecordingAdapter()
    monkeypatch.setattr("api.routers.positions._adapter", lambda: fake)
    return fake


def test_open_position_goes_through_adapter(client, monkeypatch):
    fake = _patch_adapter(monkeypatch)
    r = client.post(
        "/api/positions",
        json={"ticker": "TEST.1", "entry_price": 10.0, "side": "long",
              "stop_price": 9.0},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "id": 42}
    assert fake.calls[0][0] == "open"


def test_close_position_goes_through_adapter_close(client, monkeypatch):
    fake = _patch_adapter(monkeypatch)
    r = client.post("/api/positions/7/close", json={"exit_price": 12.5})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert ("close", 7, 12.5) in fake.calls
    # No other adapter surface was touched for a close.
    assert [c[0] for c in fake.calls] == ["close"]


def test_update_stop_goes_through_adapter(client, monkeypatch):
    fake = _patch_adapter(monkeypatch)
    r = client.patch("/api/positions/7/stop", json={"stop_price": 9.5})
    assert r.status_code == 200
    assert fake.calls[0][0] == "update_stop"


def test_scale_out_goes_through_adapter(client, monkeypatch):
    fake = _patch_adapter(monkeypatch)
    r = client.post(
        "/api/positions/7/scale-out",
        json={"exit_price": 13.0, "fraction": 0.5},
    )
    assert r.status_code == 200
    assert fake.calls[0][0] == "scale_out"


def test_edit_position_goes_through_adapter(client, monkeypatch):
    fake = _patch_adapter(monkeypatch)
    r = client.patch("/api/positions/7", json={"notes": "trim"})
    assert r.status_code == 200
    assert fake.calls[0][0] == "edit_position"


# ── auth: optional X-API-Token guard on mutating /api routes ──────────────────

def test_token_guard_blocks_unauth_post(monkeypatch):
    monkeypatch.setenv("TRADALERT_API_TOKEN", "tkn")
    with TestClient(app) as guarded:
        # GET stays open even with a token configured.
        assert guarded.get("/api/health").status_code == 200
        # Mutating POST without the header is rejected before the handler.
        blocked = guarded.post("/api/config", json={"updates": {}})
        assert blocked.status_code == 401
        # With the header the guard passes; empty-updates 400 proves we reached
        # the handler without writing anything.
        passed = guarded.post(
            "/api/config",
            json={"updates": {}},
            headers={"X-API-Token": "tkn"},
        )
        assert passed.status_code == 400


# ── input validation: ticker / side / mode guards (reject before any effect) ──

def test_open_position_rejects_bad_ticker(client, monkeypatch):
    fake = _patch_adapter(monkeypatch)
    r = client.post(
        "/api/positions",
        json={"ticker": "<img src=x>", "entry_price": 10.0, "side": "long"},
    )
    assert r.status_code == 400
    assert fake.calls == []  # rejected before the journal adapter is touched


def test_open_position_rejects_bad_side(client, monkeypatch):
    fake = _patch_adapter(monkeypatch)
    r = client.post(
        "/api/positions",
        json={"ticker": "TEST.1", "entry_price": 10.0, "side": "sideways"},
    )
    assert r.status_code == 400
    assert fake.calls == []


def test_open_position_accepts_caret_index(client, monkeypatch):
    # The journal path uses the canonical validator, which permits '^' index symbols
    # (^VIX) — the argv-hardened TICKER_RE must not be applied here.
    fake = _patch_adapter(monkeypatch)
    r = client.post(
        "/api/positions",
        json={"ticker": "^vix", "entry_price": 15.0, "side": "long"},
    )
    assert r.status_code == 200
    assert fake.calls and fake.calls[0][0] == "open"
    assert fake.calls[0][1] == "^VIX"  # validated + normalized to upper, passed through


def test_backtest_run_rejects_unknown_mode(client, monkeypatch):
    called = {"n": 0}

    def fake_launch(cmd):
        called["n"] += 1
        return "job"

    monkeypatch.setattr("api.routers.backtests.launch", fake_launch)
    r = client.post("/api/backtests/run", json={"mode": "bogus"})
    assert r.status_code == 400
    assert called["n"] == 0  # no subprocess enqueued for an invalid mode


# ── config write: multi-file two-phase commit (both files, no temp left) ──────

def test_config_write_multi_file(client, tmp_path, monkeypatch):
    pytest.importorskip("ruamel.yaml")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "settings.yaml").write_text("risk:\n  max_open_risk: 5.0\n", encoding="utf-8")
    (cfg_dir / "filters.yaml").write_text("price:\n  min_price: 1.0\n", encoding="utf-8")
    monkeypatch.setattr("api.routers.config.CONFIG", cfg_dir)

    r = client.post(
        "/api/config",
        json={"updates": {"settings.risk.max_open_risk": 7.5,
                          "filters.price.min_price": 3.0}},
    )
    assert r.status_code == 200
    assert set(r.json()["written"]) == {"settings.risk.max_open_risk", "filters.price.min_price"}

    import yaml
    assert yaml.safe_load((cfg_dir / "settings.yaml").read_text())["risk"]["max_open_risk"] == 7.5
    assert yaml.safe_load((cfg_dir / "filters.yaml").read_text())["price"]["min_price"] == 3.0
    assert not list(cfg_dir.glob("*.tmp"))  # two-phase commit leaves no temp files


# ── serve guard: refuse a non-loopback bind without a token ───────────────────

def test_serve_refuses_nonloopback_without_token(monkeypatch):
    import api.__main__ as entry

    monkeypatch.delenv("TRADALERT_API_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["python -m api", "--host", "0.0.0.0"])
    started = {"uvicorn": False}
    # If the guard regresses, this no-op stops the test from actually binding a port.
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: started.__setitem__("uvicorn", True))

    with pytest.raises(SystemExit):
        entry.main()
    assert started["uvicorn"] is False
