"""TradAlert control API entrypoint.

    uvicorn api.main:app --reload --port 8000

Read endpoints back the dashboard; the backtest/scan routes enqueue the real
scripts as background jobs (streamable over SSE). The built Vite SPA is served
at "/"; the single-file page remains at "/legacy". CORS allows a local dev SPA.

Mutating routes (POST/PATCH/PUT/DELETE under /api) require the ``X-API-Token``
header IFF ``TRADALERT_API_TOKEN`` is set — otherwise open, for single-operator
localhost use.
"""

from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import api  # noqa: F401  (path + dotenv bootstrap on import)
from api.routers import backtests, charts, config, positions, scanner

app = FastAPI(title="TradAlert control API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_MUTATING = {"POST", "PATCH", "PUT", "DELETE"}

# Local socket addresses a mutating request may arrive on WITHOUT a token.
# "testserver" is starlette's in-process ASGI test client — it never corresponds
# to a network socket, so allowing it cannot open a remote path.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testserver", None}


@app.middleware("http")
async def _token_guard(request: Request, call_next):
    """Gate mutating /api routes behind X-API-Token when a token is configured.

    With NO token configured, mutations stay open ONLY over loopback: a request
    arriving on a non-loopback server socket (a direct ``uvicorn --host 0.0.0.0``
    that bypassed ``python -m api``'s bind refusal) is refused, so the
    unauthenticated control surface (subprocess launch, journal writes, config
    writes) can never face the LAN.
    """
    expected = os.environ.get("TRADALERT_API_TOKEN", "")
    if request.method in _MUTATING and request.url.path.startswith("/api/"):
        if expected:
            provided = request.headers.get("x-api-token", "")
            if not hmac.compare_digest(provided, expected):  # constant-time
                return JSONResponse({"detail": "invalid or missing API token"}, status_code=401)
        else:
            server = request.scope.get("server")
            host = server[0] if server else None
            if host not in _LOOPBACK_HOSTS:
                return JSONResponse(
                    {"detail": "mutating API refused on a non-loopback interface "
                               "without auth — set TRADALERT_API_TOKEN or bind 127.0.0.1"},
                    status_code=403)
    return await call_next(request)


for _r in (scanner.router, positions.router, backtests.router, charts.router, config.router):
    app.include_router(_r, prefix="/api")


@app.get("/api/health")
def health():
    return {"ok": True}


_DIST = api.ROOT / "web" / "dist"
_LEGACY = api.ROOT / "web" / "index.html"


@app.get("/legacy", include_in_schema=False)
def legacy():
    """The no-build single-file control panel (fallback / reference)."""
    return FileResponse(_LEGACY)


if (_DIST / "index.html").exists():
    # Serve the built SPA at "/". Registered last so /api/* + /legacy win.
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="spa")
else:
    @app.get("/", include_in_schema=False)
    def index():
        """SPA not built yet — serve the single-file page until `npm run build`."""
        return FileResponse(_LEGACY)
