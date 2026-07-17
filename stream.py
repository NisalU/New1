"""Live stream manager.

Bridges Binance WebSocket market streams (trades + klines) to dashboard
clients, and runs the confluence engine on a background schedule, pushing
fresh analysis snapshots and new signals over the same socket.

Pure-Python: only `websockets` (plus the existing engine deps).
"""
import asyncio
import contextlib
import json
import time
import traceback

import config
from engine import engine

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

# Binance market-data WS endpoints, tried in order.
# data-stream.binance.vision usually works even where stream.binance.com
# is geo-restricted.
WS_ENDPOINTS = [
    "wss://data-stream.binance.vision/ws",
    "wss://stream.binance.com:9443/ws",
    "wss://stream.binance.com:443/ws",
]

TICK_FLUSH_MS = 50           # coalesce trade ticks to at most 20/sec per client
SNAPSHOT_SECONDS = config.REFRESH_SECONDS


class Client:
    """One connected dashboard browser."""

    def __init__(self, ws):
        self.ws = ws
        self.symbol = config.DEFAULT_SYMBOL
        self.interval = config.DEFAULT_INTERVAL
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=512)

    def market(self):
        return (self.symbol, self.interval)

    def send(self, msg: dict):
        """Non-blocking enqueue; drops oldest style (skip) when overloaded."""
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass


class StreamManager:
    def __init__(self):
        self.clients: set[Client] = set()
        self._latest_tick: dict[str, dict] = {}       # symbol -> tick msg
        self._dirty_ticks: set[str] = set()
        self._resub = asyncio.Event()
        self._known_signals = 0
        self._refreshing: set[tuple[str, str]] = set()
        self._tasks: list[asyncio.Task] = []
        self._started = False

    # ---------------- lifecycle ----------------
    def start(self):
        if self._started:
            return
        self._started = True
        self._known_signals = len(engine.signals)
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._upstream_loop()),
            loop.create_task(self._tick_flusher()),
            loop.create_task(self._analysis_loop()),
        ]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks = []
        self._started = False

    # ---------------- client registry ----------------
    def add_client(self, client: Client):
        self.clients.add(client)
        self._resub.set()

    def remove_client(self, client: Client):
        self.clients.discard(client)
        self._resub.set()

    def retarget(self, client: Client, symbol: str, interval: str):
        client.symbol, client.interval = symbol, interval
        self._resub.set()

    def _desired_streams(self) -> set[str]:
        streams = set()
        for c in self.clients:
            s = c.symbol.lower()
            streams.add(f"{s}@aggTrade")
            streams.add(f"{s}@kline_{c.interval}")
        # Always keep the default market warm so signals keep firing
        d = config.DEFAULT_SYMBOL.lower()
        streams.add(f"{d}@aggTrade")
        streams.add(f"{d}@kline_{config.DEFAULT_INTERVAL}")
        return streams

    def active_markets(self) -> set[tuple[str, str]]:
        markets = {c.market() for c in self.clients}
        markets.add((config.DEFAULT_SYMBOL, config.DEFAULT_INTERVAL))
        return markets

    # ---------------- upstream: Binance WS ----------------
    async def _upstream_loop(self):
        if websockets is None:
            print("[stream] `websockets` not installed — live ticks disabled")
            return
        backoff = 1
        while True:
            subscribed: set[str] = set()
            try:
                url = None
                conn = None
                for endpoint in WS_ENDPOINTS:
                    try:
                        conn = await asyncio.wait_for(
                            websockets.connect(endpoint, ping_interval=20, ping_timeout=20),
                            timeout=10,
                        )
                        url = endpoint
                        break
                    except Exception:  # noqa: BLE001
                        continue
                if conn is None:
                    raise ConnectionError("all Binance WS endpoints failed")
                print(f"[stream] connected to {url}")
                backoff = 1
                async with conn:
                    self._resub.set()  # force initial subscribe
                    recv_task = asyncio.create_task(self._recv_loop(conn))
                    try:
                        while not recv_task.done():
                            # (re)subscribe whenever the desired set changes
                            with contextlib.suppress(asyncio.TimeoutError):
                                await asyncio.wait_for(self._resub.wait(), timeout=5)
                            if self._resub.is_set():
                                self._resub.clear()
                                desired = self._desired_streams()
                                to_add = sorted(desired - subscribed)
                                to_del = sorted(subscribed - desired)
                                if to_add:
                                    await conn.send(json.dumps(
                                        {"method": "SUBSCRIBE", "params": to_add, "id": int(time.time() * 1000)}))
                                if to_del:
                                    await conn.send(json.dumps(
                                        {"method": "UNSUBSCRIBE", "params": to_del, "id": int(time.time() * 1000) + 1}))
                                subscribed = desired
                    finally:
                        recv_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await recv_task
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[stream] upstream error: {e} — reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _recv_loop(self, conn):
        async for raw in conn:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            etype = msg.get("e")
            if etype == "aggTrade":
                symbol = msg["s"]
                self._latest_tick[symbol] = {
                    "type": "tick",
                    "symbol": symbol,
                    "price": float(msg["p"]),
                    "qty": float(msg["q"]),
                    "sell": bool(msg["m"]),  # buyer is maker => taker sold
                    "time": msg["T"],
                }
                self._dirty_ticks.add(symbol)
            elif etype == "kline":
                k = msg["k"]
                symbol, interval = msg["s"], k["i"]
                vol = float(k["v"])
                taker_buy = float(k["V"])
                out = {
                    "type": "kline",
                    "symbol": symbol,
                    "interval": interval,
                    "closed": bool(k["x"]),
                    "candle": {
                        "time": k["t"] // 1000,
                        "open": float(k["o"]),
                        "high": float(k["h"]),
                        "low": float(k["l"]),
                        "close": float(k["c"]),
                        "volume": vol,
                        "taker_buy": taker_buy,
                        "delta": 2 * taker_buy - vol,
                    },
                }
                for c in self.clients:
                    if c.symbol == symbol and c.interval == interval:
                        c.send(out)
                # Candle just closed: refresh analysis immediately instead of
                # waiting up to REFRESH_SECONDS — score/overlays update live.
                if out["closed"] and (symbol, interval) in self.active_markets():
                    asyncio.create_task(self._refresh_market(symbol, interval))

    # ---------------- tick fanout (coalesced) ----------------
    async def _tick_flusher(self):
        while True:
            await asyncio.sleep(TICK_FLUSH_MS / 1000)
            if not self._dirty_ticks:
                continue
            dirty, self._dirty_ticks = self._dirty_ticks, set()
            for symbol in dirty:
                tick = self._latest_tick.get(symbol)
                if not tick:
                    continue
                for c in self.clients:
                    if c.symbol == symbol:
                        c.send(tick)

    # ---------------- background analysis ----------------
    def _broadcast_new_signals(self):
        if not config.ENGINE_SIGNAL_FEED:
            self._known_signals = len(engine.signals)
            return
        if len(engine.signals) > self._known_signals:
            fresh = engine.signals[self._known_signals:]
            self._known_signals = len(engine.signals)
            for sig in fresh:
                for c in self.clients:
                    c.send({"type": "signal", "data": sig})

    async def _refresh_market(self, symbol, interval):
        """Analyze one market and push the snapshot; deduped per market."""
        key = (symbol, interval)
        if key in self._refreshing:
            return
        self._refreshing.add(key)
        try:
            analysis = await asyncio.to_thread(engine.analyze, symbol, interval)
            payload = {"type": "snapshot", "data": analysis}
            for c in self.clients:
                if c.market() == key:
                    c.send(payload)
            self._broadcast_new_signals()
            # Check pending LIMIT order triggers on each analysis cycle
            try:
                from ai_analyst import ai_analyst as _ai
                if getattr(config, "LIMIT_SIGNALS_ENABLED", True):
                    price     = analysis["price"]
                    triggered = _ai.check_and_trigger_limits(symbol, price)
                    for order in triggered:
                        trig_payload = {"type": "limit_triggered", "data": order}
                        for c in self.clients:
                            c.send(trig_payload)
                    if triggered:
                        updated     = _ai.get_pending_limits(symbol)
                        upd_payload = {"type": "pending_limits", "data": updated}
                        for c in self.clients:
                            if c.symbol == symbol:
                                c.send(upd_payload)
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            self._refreshing.discard(key)

    async def _analysis_loop(self):
        while True:
            try:
                # analyze all active markets concurrently, not one by one
                await asyncio.gather(
                    *(self._refresh_market(s, i) for s, i in self.active_markets())
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            await asyncio.sleep(SNAPSHOT_SECONDS)


manager = StreamManager()
