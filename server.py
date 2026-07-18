"""AI Trading Signal Bot — aiohttp + WebSocket server.

Run on any local Linux/Mac machine:
    pip install -r requirements.txt
    python server.py
Then open http://<local-ip>:8000 from any device on the same network.

Keys can be supplied three ways (highest priority first):
  1. Environment variables: GROQ_API_KEY, BINANCE_API_KEY, BINANCE_API_SECRET
  2. A .env file in the project directory
  3. Interactive prompt at startup (only when running in a TTY)
"""
import asyncio
import contextlib
import json
import logging
import os
import socket
import traceback
from pathlib import Path
import time

# ── .env support ──────────────────────────────────────────────────────────────
# Load before anything else so env vars are available to all modules.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; fall back to plain env vars

from aiohttp import WSMsgType, web

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("server")

BASE_DIR = Path(__file__).parent

config     = None  # type: ignore[assignment]
ai_analyst = None  # type: ignore[assignment]
engine     = None  # type: ignore[assignment]
manager    = None  # type: ignore[assignment]
scanner    = None  # type: ignore[assignment]

# ── Single active symbol — only one coin is analysed at a time ───────────────
# Changed by POST /api/symbol (REST) or the WS "subscribe" message.
_active_symbol: str = ""          # set to DEFAULT_SYMBOL in _load_app_modules
_priority_event: "asyncio.Event | None" = None


# ---------------------------------------------------------------------------
# Terminal key prompting
# ---------------------------------------------------------------------------



def _load_app_modules() -> None:
    global config, ai_analyst, engine, manager, scanner

    import config as _config
    from ai_analyst import ai_analyst as _ai_analyst
    from engine import engine as _engine
    from stream import manager as _manager
    from scanner import scanner as _scanner

    _config.BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
    _config.BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
    _config.CMC_API_KEY        = os.environ.get("CMC_API_KEY", "")

    config     = _config
    ai_analyst = _ai_analyst
    engine     = _engine
    manager    = _manager
    scanner    = _scanner

    global _active_symbol
    _active_symbol = _config.DEFAULT_SYMBOL


# ---------------------------------------------------------------------------
# Static / index
# ---------------------------------------------------------------------------

async def index(_request: web.Request) -> web.StreamResponse:
    return web.FileResponse(BASE_DIR / "static" / "index.html")


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

async def api_config(_request: web.Request) -> web.Response:
    return web.json_response({
        "active_symbol":    _active_symbol or config.DEFAULT_SYMBOL,
        "intervals":        config.INTERVALS,
        "default_symbol":   config.DEFAULT_SYMBOL,
        "default_interval": config.DEFAULT_INTERVAL,
        "threshold":        config.SIGNAL_THRESHOLD,
        "refresh_seconds":  config.REFRESH_SECONDS,
        "ai_refresh_seconds": config.AI_REFRESH_SECONDS,
    })


def _valid_symbol(sym: str) -> bool:
    """Accept any USDT-margined futures pair (e.g. BTCUSDT, SOLUSDT)."""
    return bool(sym) and sym.upper().endswith("USDT") and len(sym) >= 5


async def api_get_symbol(_request: web.Request) -> web.Response:
    """Return the single coin currently being watched by the AI loop."""
    return web.json_response({"symbol": _active_symbol})


async def api_set_symbol(request: web.Request) -> web.Response:
    """Switch the single coin the AI loop watches.
    Body: { "symbol": "SOLUSDT" }
    The old symbol's AI cache is cleared so the new coin gets a fresh read.
    """
    global _active_symbol
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    sym = (body.get("symbol") or "").upper().strip()
    if not _valid_symbol(sym):
        return web.json_response({"error": "symbol must end with USDT (e.g. SOLUSDT)"}, status=400)
    old = _active_symbol
    _active_symbol = sym
    if old != sym:
        # Clear stale cache so the loop does a fresh call immediately
        with ai_analyst._lock:
            ai_analyst._cache.pop(sym, None)
        if _priority_event:
            _priority_event.set()
        # Tell all connected clients the active symbol changed
        for c in manager.clients:
            c.send({"type": "active_symbol", "symbol": sym})
    return web.json_response({"symbol": sym, "ok": True})


async def api_state(request: web.Request) -> web.Response:
    symbol   = request.query.get("symbol",   config.DEFAULT_SYMBOL)
    interval = request.query.get("interval", config.DEFAULT_INTERVAL)
    if not _valid_symbol(symbol) or interval not in config.INTERVALS:
        return web.json_response({"error": "invalid symbol or interval"}, status=400)
    try:
        data = await asyncio.to_thread(engine.get_state, symbol, interval)
        return web.json_response(data)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)


async def api_signals(_request: web.Request) -> web.Response:
    return web.json_response(list(reversed(engine.signals[-50:])))


async def api_ai(request: web.Request) -> web.Response:
    symbol = request.query.get("symbol", config.DEFAULT_SYMBOL)
    if not _valid_symbol(symbol):
        return web.json_response({"error": "invalid symbol"}, status=400)
    if not ai_analyst.enabled:
        return web.json_response({"error": "GROQ_API_KEY not set"}, status=503)
    cached = ai_analyst.get_cached(symbol)
    if cached:
        return web.json_response(cached)
    result = await asyncio.to_thread(ai_analyst.analyze_safe, symbol)
    return web.json_response(result)


async def api_engine_status(_request: web.Request) -> web.Response:
    return web.json_response(ai_analyst.get_status())


async def api_ai_signals(_request: web.Request) -> web.Response:
    return web.json_response(ai_analyst.get_recent_signals())


async def api_binance_key_status(_request: web.Request) -> web.Response:
    return web.json_response({
        "api_key_configured":    bool(config.BINANCE_API_KEY),
        "api_secret_configured": bool(config.BINANCE_API_SECRET),
    })


async def api_pipeline_events(_request: web.Request) -> web.Response:
    return web.json_response({
        "events":     ai_analyst.get_pipeline_log(),
        "active_run": ai_analyst.get_active_run(),
    })


async def api_scanner(_request: web.Request) -> web.Response:
    """Return the latest scanner results (or current scanning progress)."""
    return web.json_response(scanner.get_state())


async def api_scanner_trigger(_request: web.Request) -> web.Response:
    """Kick off an immediate rescan. Results arrive via WebSocket scanner_results."""
    if scanner.get_state()["scanning"]:
        return web.json_response({"status": "already_scanning"})
    asyncio.create_task(_run_scan_once())
    return web.json_response({"status": "scanning"})


async def api_pending_limits(request: web.Request) -> web.Response:
    """Return pending LIMIT order signals (waiting for price to reach entry)."""
    symbol = request.query.get("symbol")
    limits = ai_analyst.get_pending_limits(symbol)
    return web.json_response({"pending": limits})


async def api_signal_status(request: web.Request) -> web.Response:
    """Active-signal lock status for each symbol."""
    symbol = request.query.get("symbol")
    status = ai_analyst.get_status()
    if symbol:
        return web.json_response({
            "symbol":        symbol,
            "active_signal": status["active_signals"].get(symbol),
            "next_analysis": status["next_analysis_ts"].get(symbol),
        })
    return web.json_response({
        "active_signals":   status["active_signals"],
        "next_analysis_ts": status["next_analysis_ts"],
    })


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

async def ws_endpoint(request: web.Request) -> web.WebSocketResponse:
    global _active_symbol
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    from stream import Client
    client = Client(ws)

    async def sender():
        try:
            while True:
                msg = await client.queue.get()
                try:
                    await ws.send_str(json.dumps(msg, default=str))
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    send_task = asyncio.create_task(sender())
    try:
        # ── Hello burst ──────────────────────────────────────────────────
        client.send({
            "type":               "config",
            "active_symbol":      _active_symbol or config.DEFAULT_SYMBOL,
            "intervals":          config.INTERVALS,
            "default_symbol":     config.DEFAULT_SYMBOL,
            "default_interval":   config.DEFAULT_INTERVAL,
            "threshold":          config.SIGNAL_THRESHOLD,
            "ai_refresh_seconds": config.AI_REFRESH_SECONDS,
        })
        if config.ENGINE_SIGNAL_FEED:
            client.send({"type": "signals", "data": list(reversed(engine.signals[-50:]))})
        if ai_analyst.enabled:
            cached_ai = ai_analyst.get_cached(client.symbol)
            if cached_ai:
                client.send({"type": "ai", "data": cached_ai})
            client.send({"type": "engine_status",    "data": ai_analyst.get_status()})
            client.send({"type": "ai_signals_table", "data": ai_analyst.get_recent_signals()})
            client.send({"type": "pipeline_log",     "data": ai_analyst.get_pipeline_log()[:40]})
            client.send({"type": "pending_limits",   "data": ai_analyst.get_pending_limits(client.symbol)})
            # Send countdown for default symbol
            _push_countdown(client, client.symbol)
        # Push cached scanner results immediately if available
        scan_state = scanner.get_state()
        if scan_state["results"]:
            client.send({"type": "scanner_results", "data": scan_state})
        elif scan_state["scanning"]:
            client.send({"type": "scanner_progress", "progress": scan_state["progress"]})
        manager.add_client(client)

        async def push_snapshot(symbol, interval):
            try:
                data = await asyncio.to_thread(engine.get_state, symbol, interval)
                if client.market() == (symbol, interval):
                    client.send({"type": "snapshot", "data": data})
            except Exception as e:
                client.send({"type": "error", "message": str(e)})

        asyncio.create_task(push_snapshot(client.symbol, client.interval))

        async for frame in ws:
            if frame.type != WSMsgType.TEXT:
                if frame.type == WSMsgType.ERROR:
                    break
                continue
            try:
                msg = json.loads(frame.data)
            except (json.JSONDecodeError, TypeError):
                continue
            kind = msg.get("type")

            if kind == "subscribe":
                sym = msg.get("symbol", config.DEFAULT_SYMBOL)
                ivl = msg.get("interval", config.DEFAULT_INTERVAL)
                if _valid_symbol(sym) and ivl in config.INTERVALS:
                    old_sym = _active_symbol
                    # Switch the single watched coin
                    _active_symbol = sym
                    manager.retarget(client, sym, ivl)
                    asyncio.create_task(push_snapshot(sym, ivl))
                    if old_sym != sym:
                        # Clear stale cache, wake the AI loop immediately
                        with ai_analyst._lock:
                            ai_analyst._cache.pop(sym, None)
                        if _priority_event:
                            _priority_event.set()
                        # Tell all clients the active coin changed
                        for c in manager.clients:
                            c.send({"type": "active_symbol", "symbol": sym})
                    if ai_analyst.enabled:
                        cached_ai = ai_analyst.get_cached(sym)
                        if cached_ai:
                            client.send({"type": "ai", "data": cached_ai})
                        else:
                            client.send({"type": "ai_analyzing", "symbol": sym})
                        _push_countdown(client, sym)
                        client.send({"type": "engine_status",    "data": ai_analyst.get_status()})
                        client.send({"type": "ai_signals_table", "data": ai_analyst.get_recent_signals()})
                        client.send({"type": "pipeline_log",     "data": ai_analyst.get_pipeline_log()[:40]})
                        client.send({"type": "pending_limits",   "data": ai_analyst.get_pending_limits(sym)})

            elif kind == "ping":
                # Application-level ping → pong with echo timestamp
                client.send({"type": "pong", "t": msg.get("t", 0)})

    finally:
        send_task.cancel()
        manager.remove_client(client)
        with contextlib.suppress(asyncio.CancelledError):
            await send_task
        with contextlib.suppress(Exception):
            await ws.close()

    return ws


def _push_countdown(client, symbol):
    """Push next-analysis countdown for `symbol` to a single client."""
    next_ts = ai_analyst.get_next_analysis_ts(symbol)
    if next_ts:
        client.send({
            "type":        "ai_countdown",
            "symbol":      symbol,
            "next_ts":     next_ts,
            "interval_s":  config.AI_REFRESH_SECONDS,
        })


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

async def _push_ai_charts(symbol: str, result: dict) -> None:
    if not (result or {}).get("signal"):
        return
    for ivl in (config.AI_INTERVAL, config.AI_HTF_INTERVAL):
        try:
            data    = await asyncio.to_thread(engine.get_state, symbol, ivl)
            payload = {"type": "snapshot", "data": data}
            for c in manager.clients:
                if c.symbol == symbol and c.interval == ivl:
                    c.send(payload)
        except Exception:
            pass


async def _status_loop() -> None:
    """Broadcast engine status + AI signals table every 10 s."""
    while True:
        try:
            status_payload  = {"type": "engine_status",    "data": ai_analyst.get_status()}
            signals_payload = {"type": "ai_signals_table", "data": ai_analyst.get_recent_signals()}
            for c in manager.clients:
                c.send(status_payload)
                c.send(signals_payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(10)


async def _ai_loop() -> None:
    """Run AI analyst for the single active symbol, every AI_REFRESH_SECONDS.

    Only one coin is analysed at a time.  Switch coins via POST /api/symbol
    or the WS "subscribe" message — the loop wakes immediately and runs a
    fresh call for the new coin.
    """
    _consecutive_failures = 0
    _BACKOFF = [300, 600, 1200, 1800]

    while True:
        try:
            symbol = _active_symbol or config.DEFAULT_SYMBOL

            # Set next-analysis timestamp and broadcast countdown BEFORE running
            next_ts = int(time.time() + config.AI_REFRESH_SECONDS)
            ai_analyst.set_next_analysis_ts(symbol, next_ts)
            countdown_payload = {
                "type":       "ai_countdown",
                "symbol":     symbol,
                "next_ts":    next_ts,
                "interval_s": config.AI_REFRESH_SECONDS,
            }
            for c in manager.clients:
                c.send(countdown_payload)

            result = await asyncio.to_thread(ai_analyst.analyze_safe, symbol)

            if result.get("error", "").startswith("RATE_LIMIT:"):
                _consecutive_failures += 1
                backoff = _BACKOFF[min(_consecutive_failures - 1, len(_BACKOFF) - 1)]
                log.warning(
                    "[ai] All models rate-limited (failure #%d). Backing off %d min.",
                    _consecutive_failures, backoff // 60,
                )
                # Update next-ts for extended backoff
                backoff_next = int(time.time() + backoff)
                ai_analyst.set_next_analysis_ts(symbol, backoff_next)
                backoff_payload = {
                    "type":       "ai_countdown",
                    "symbol":     symbol,
                    "next_ts":    backoff_next,
                    "interval_s": backoff,
                    "rate_limited": True,
                }
                for c in manager.clients:
                    c.send(backoff_payload)
                await asyncio.sleep(backoff)
                continue
            else:
                _consecutive_failures = 0

            # Broadcast results
            ai_payload       = {"type": "ai",               "data": result}
            status_payload   = {"type": "engine_status",    "data": ai_analyst.get_status()}
            signals_payload  = {"type": "ai_signals_table", "data": ai_analyst.get_recent_signals()}
            pipeline_payload = {"type": "pipeline_log",     "data": ai_analyst.get_pipeline_log()[:40]}
            limits_payload   = {"type": "pending_limits",   "data": ai_analyst.get_pending_limits(symbol)}

            for c in manager.clients:
                if c.symbol == symbol:
                    c.send(ai_payload)
                    c.send(pipeline_payload)
                    c.send(limits_payload)
                c.send(status_payload)
                c.send(signals_payload)

            asyncio.create_task(_push_ai_charts(symbol, result))

        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()

        # Interruptible sleep: wakes immediately when a priority symbol arrives
        # (e.g. user switched to a symbol with no cached result).
        # NOTE: do NOT wrap with asyncio.shield — that leaks coroutines on timeout.
        if _priority_event:
            _priority_event.clear()
            try:
                await asyncio.wait_for(
                    _priority_event.wait(),
                    timeout=float(config.AI_REFRESH_SECONDS),
                )
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(config.AI_REFRESH_SECONDS)


def _broadcast_all(msg: dict) -> None:
    """Send a message to every connected WebSocket client."""
    if manager is None:
        return
    for c in manager.clients:
        c.send(msg)


async def _run_scan_once() -> None:
    """Run one full scan in a thread, broadcast progress + results via WS."""
    def _on_progress(msg: str):
        _broadcast_all({"type": "scanner_progress", "progress": msg})

    _broadcast_all({"type": "scanner_progress", "progress": "Starting scan…"})
    try:
        result = await asyncio.to_thread(scanner.scan, _on_progress)
        _broadcast_all({"type": "scanner_results", "data": result})
    except Exception:
        traceback.print_exc()
        _broadcast_all({"type": "scanner_progress", "progress": "Scan failed — see server logs"})


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application) -> None:
    global _priority_event
    _priority_event = asyncio.Event()
    manager.start()
    if ai_analyst.enabled:
        app["ai_task"]     = asyncio.create_task(_ai_loop())
        app["status_task"] = asyncio.create_task(_status_loop())
        log.info("[ai] Groq AI analyst enabled — 1-min refresh cycle active")
    else:
        log.info("[ai] No Groq key — AI analysis disabled")
    if config.BINANCE_API_KEY:
        log.info("[binance] API key configured")
    else:
        log.info("[binance] No API key — public endpoints only")


async def on_cleanup(app: web.Application) -> None:
    for key in ("ai_task", "status_task"):
        task = app.get(key)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    await manager.stop()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/",                       index)
    app.router.add_get("/api/config",             api_config)
    app.router.add_get("/api/state",              api_state)
    app.router.add_get("/api/signals",            api_signals)
    app.router.add_get("/api/ai",                 api_ai)
    app.router.add_get("/api/engine-status",      api_engine_status)
    app.router.add_get("/api/ai-signals",         api_ai_signals)
    app.router.add_get("/api/binance-key-status", api_binance_key_status)
    app.router.add_get("/api/pipeline-events",    api_pipeline_events)
    app.router.add_get("/api/signal-status",      api_signal_status)
    app.router.add_get("/api/pending-limits",     api_pending_limits)
    app.router.add_get("/api/symbol",             api_get_symbol)
    app.router.add_post("/api/symbol",            api_set_symbol)
    app.router.add_get("/api/scanner",            api_scanner)
    app.router.add_post("/api/scanner/trigger",   api_scanner_trigger)
    app.router.add_get("/ws",                     ws_endpoint)
    app.router.add_static("/static", BASE_DIR / "static", name="static")
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    _load_app_modules()

    print("=" * 56)
    print("  AI Trading Signal Bot  (aiohttp + WebSocket)")
    print(f"  Local:   http://127.0.0.1:{config.PORT}")
    print(f"  Network: http://{_local_ip()}:{config.PORT}")
    print(f"  AI refresh: every {config.AI_REFRESH_SECONDS}s")
    print("=" * 56)
    web.run_app(
        create_app(),
        host=config.HOST,
        port=config.PORT,
        access_log=None,
        print=None,
    )
