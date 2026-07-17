"""AI Trading Signal Bot — aiohttp + WebSocket server.

Run on any local Linux/Mac machine:
    pip install -r requirements.txt
    python server.py
Then open http://<local-ip>:8000 from any device on the same network.
"""
import asyncio
import contextlib
import getpass
import json
import os
import socket
import sys
import traceback
from pathlib import Path

from aiohttp import WSMsgType, web

BASE_DIR = Path(__file__).parent

config     = None  # type: ignore[assignment]
ai_analyst = None  # type: ignore[assignment]
engine     = None  # type: ignore[assignment]
manager    = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Terminal key prompting
# ---------------------------------------------------------------------------

def _prompt_for_keys() -> None:
    print()
    print("=" * 56)
    print("  AI Trading Signal Bot — API Key Setup")
    print("  Input is hidden; keys will not be displayed.")
    print("=" * 56)
    print()

    print("  Groq API key is required for the AI analyst.")
    print("  Get a free key at https://console.groq.com/keys")
    print("  Press Enter to skip (AI analyst will be disabled).")
    groq_key = getpass.getpass("  Groq API key: ").strip()
    if groq_key:
        os.environ["GROQ_API_KEY"] = groq_key
        print("  [ok] Groq API key set.")
    else:
        print("  [skip] No Groq key — AI analyst disabled.")

    print()

    print("  Binance API credentials (optional — for authenticated endpoints).")
    binance_key = getpass.getpass("  Binance API key (Enter to skip): ").strip()
    if binance_key:
        os.environ["BINANCE_API_KEY"] = binance_key
        binance_secret = getpass.getpass("  Binance API secret: ").strip()
        if binance_secret:
            os.environ["BINANCE_API_SECRET"] = binance_secret
            print("  [ok] Binance credentials set.")
        else:
            print("  [skip] No secret — skipping Binance auth.")
    else:
        print("  [skip] No Binance key — public market data only.")

    print()
    print("=" * 56)
    print()


def _load_app_modules() -> None:
    global config, ai_analyst, engine, manager

    import config as _config
    from ai_analyst import ai_analyst as _ai_analyst
    from engine import engine as _engine
    from stream import manager as _manager

    _config.BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
    _config.BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

    config     = _config
    ai_analyst = _ai_analyst
    engine     = _engine
    manager    = _manager


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
        "symbols":          config.SYMBOLS,
        "intervals":        config.INTERVALS,
        "default_symbol":   config.DEFAULT_SYMBOL,
        "default_interval": config.DEFAULT_INTERVAL,
        "threshold":        config.SIGNAL_THRESHOLD,
        "refresh_seconds":  config.REFRESH_SECONDS,
        "ai_refresh_seconds": config.AI_REFRESH_SECONDS,
    })


async def api_state(request: web.Request) -> web.Response:
    symbol   = request.query.get("symbol",   config.DEFAULT_SYMBOL)
    interval = request.query.get("interval", config.DEFAULT_INTERVAL)
    if symbol not in config.SYMBOLS or interval not in config.INTERVALS:
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
    if symbol not in config.SYMBOLS:
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
            "symbols":            config.SYMBOLS,
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
                if sym in config.SYMBOLS and ivl in config.INTERVALS:
                    old_sym = client.symbol
                    client.symbol   = sym
                    client.interval = ivl
                    asyncio.create_task(push_snapshot(sym, ivl))
                    # Push cached AI immediately so dashboard updates without wait
                    if ai_analyst.enabled:
                        cached_ai = ai_analyst.get_cached(sym)
                        if cached_ai:
                            client.send({"type": "ai", "data": cached_ai})
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
    """Run AI analyst for each active symbol, every AI_REFRESH_SECONDS.

    - Skips analysis when an active signal is live (price hasn't hit stop/target).
    - Broadcasts countdown so the dashboard can show a live timer.
    - Uses exponential backoff when all models are rate-limited.
    """
    _symbol_queue: list[str] = []
    _consecutive_failures = 0
    _BACKOFF = [300, 600, 1200, 1800]

    while True:
        try:
            active = list({c.symbol for c in manager.clients} | {config.DEFAULT_SYMBOL})
            _symbol_queue[:] = [s for s in _symbol_queue if s in active]
            for s in active:
                if s not in _symbol_queue:
                    _symbol_queue.append(s)

            symbol = _symbol_queue.pop(0) if _symbol_queue else config.DEFAULT_SYMBOL
            if symbol not in _symbol_queue:
                _symbol_queue.append(symbol)

            # Set next-analysis timestamp and broadcast countdown BEFORE running
            import time as _t; next_ts = int(_t.time() + config.AI_REFRESH_SECONDS)
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
                print(
                    f"[ai] All models rate-limited (failure #{_consecutive_failures}). "
                    f"Backing off {backoff // 60} min."
                )
                # Update next-ts for extended backoff
                backoff_next = int(__import__("time").time() + backoff)
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

        await asyncio.sleep(config.AI_REFRESH_SECONDS)


import time as _time_mod  # noqa: E402 (used in _ai_loop)


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application) -> None:
    manager.start()
    if ai_analyst.enabled:
        app["ai_task"]     = asyncio.create_task(_ai_loop())
        app["status_task"] = asyncio.create_task(_status_loop())
        print("[ai] Groq AI analyst enabled — 1-min refresh cycle active")
    else:
        print("[ai] No Groq key — AI analysis disabled")
    if config.BINANCE_API_KEY:
        print("[binance] API key configured")
    else:
        print("[binance] No API key — public endpoints only")


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
    _prompt_for_keys()
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
