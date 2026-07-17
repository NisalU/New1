"""Binance market data fetcher with endpoint fallback and API key support.

Pure-Python (only `requests`) so it installs cleanly on Termux.
Thread-safe: one session per worker thread and TTL caches so concurrent
analyses don't hammer the API.

Binance API key usage:
  - Public endpoints (klines, ticker, futures stats): API key added as header
    for higher rate limits (optional — works without it too).
  - Private endpoints (account, orders): require API key + HMAC-SHA256 signed
    query string. Use get_account_info() or place_order() for those.
"""
import hashlib
import hmac
import threading
import time
import urllib.parse

import requests

import config

_tls = threading.local()   # one requests.Session per worker thread

_spot_base = None          # cached working spot endpoint
_fut_base = None           # cached working futures endpoint
_fut_disabled_until = 0

_cache_lock = threading.Lock()
_ticker_cache = {}         # symbol -> (expires_at, data)
_futures_cache = {}        # symbol -> (expires_at, data)
TICKER_TTL = 10            # s — 24h ticker doesn't need per-snapshot fetches
FUTURES_TTL = 120          # s — funding/OI/LS move slowly; saves 3 HTTP calls per snapshot


class DataError(Exception):
    pass


def _session():
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        headers = {"User-Agent": "signal-bot/1.0"}
        if config.BINANCE_API_KEY:
            headers["X-MBX-APIKEY"] = config.BINANCE_API_KEY
        s.headers.update(headers)
        _tls.session = s
    return s


def _sign(params: dict) -> str:
    """Create HMAC-SHA256 signature for Binance private endpoints."""
    if not config.BINANCE_API_SECRET:
        raise DataError("BINANCE_API_SECRET is not set — cannot sign request")
    query = urllib.parse.urlencode(params)
    sig = hmac.new(
        config.BINANCE_API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return sig


def _signed_params(params: dict) -> dict:
    """Add timestamp and signature to params dict (modifies a copy)."""
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    return p


def _get(base_candidates, cached, path, params, signed=False):
    """Try each base URL until one responds. Returns (json, working_base).

    If signed=True the request uses a server timestamp + HMAC signature and
    requires BINANCE_API_KEY / BINANCE_API_SECRET to be configured.
    """
    if signed:
        params = _signed_params(params)
    bases = ([cached] if cached else []) + [b for b in base_candidates if b != cached]
    last_err = None
    for base in bases:
        try:
            r = _session().get(base + path, params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                # Binance geo-block returns 200 with {"code":0,"msg":...}
                if isinstance(data, dict) and "msg" in data and "code" in data:
                    last_err = data.get("msg")
                    continue
                return data, base
            last_err = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    raise DataError(f"All endpoints failed for {path}: {last_err}")


def get_klines(symbol, interval, limit=None):
    """Return list of candle dicts (oldest -> newest)."""
    global _spot_base
    limit = limit or config.KLINE_LIMIT
    raw, _spot_base = _get(
        config.SPOT_ENDPOINTS, _spot_base, "/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    candles = []
    for k in raw:
        vol = float(k[5])
        taker_buy = float(k[9])
        candles.append({
            "time": k[0] // 1000,          # unix seconds (lightweight-charts format)
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": vol,
            "taker_buy": taker_buy,
            "delta": 2 * taker_buy - vol,  # taker buy - taker sell volume
        })
    return candles


def get_ticker(symbol):
    global _spot_base
    now = time.time()
    with _cache_lock:
        hit = _ticker_cache.get(symbol)
        if hit and hit[0] > now:
            return hit[1]
    data, _spot_base = _get(
        config.SPOT_ENDPOINTS, _spot_base, "/api/v3/ticker/24hr", {"symbol": symbol}
    )
    out = {
        "last": float(data["lastPrice"]),
        "change_pct": float(data["priceChangePercent"]),
        "high": float(data["highPrice"]),
        "low": float(data["lowPrice"]),
        "volume": float(data["quoteVolume"]),
    }
    with _cache_lock:
        _ticker_cache[symbol] = (now + TICKER_TTL, out)
    return out


def get_futures_stats(symbol):
    """Funding rate, open interest and long/short ratio from Binance futures.

    Returns None if the futures API is unreachable (e.g. geo-restricted);
    the fundamentals strategy degrades gracefully. Results are cached for
    FUTURES_TTL seconds.
    """
    global _fut_base, _fut_disabled_until
    now = time.time()
    with _cache_lock:
        hit = _futures_cache.get(symbol)
        if hit and hit[0] > now:
            return hit[1]
    if now < _fut_disabled_until:
        return None
    try:
        premium, _fut_base = _get(
            config.FUTURES_ENDPOINTS, _fut_base, "/fapi/v1/premiumIndex", {"symbol": symbol}
        )
        oi_hist, _fut_base = _get(
            config.FUTURES_ENDPOINTS, _fut_base, "/futures/data/openInterestHist",
            {"symbol": symbol, "period": "1h", "limit": 25},
        )
        ls_ratio, _fut_base = _get(
            config.FUTURES_ENDPOINTS, _fut_base, "/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol, "period": "1h", "limit": 2},
        )
        oi_now = float(oi_hist[-1]["sumOpenInterest"]) if oi_hist else 0.0
        oi_prev = float(oi_hist[0]["sumOpenInterest"]) if oi_hist else 0.0
        out = {
            "funding_rate": float(premium.get("lastFundingRate", 0)),
            "mark_price": float(premium.get("markPrice", 0)),
            "open_interest": oi_now,
            "oi_change_pct": ((oi_now - oi_prev) / oi_prev * 100) if oi_prev else 0.0,
            "long_short_ratio": float(ls_ratio[-1]["longShortRatio"]) if ls_ratio else 1.0,
        }
        with _cache_lock:
            _futures_cache[symbol] = (now + FUTURES_TTL, out)
        return out
    except DataError:
        # Don't hammer a blocked endpoint; retry every 10 minutes.
        _fut_disabled_until = now + 600
        return None


# ---- Authenticated / private endpoints ----

def get_account_info():
    """Fetch spot account balances (requires BINANCE_API_KEY + SECRET).

    Returns a dict with non-zero balances:
        { "BTC": {"free": 0.001, "locked": 0.0}, ... }
    Raises DataError if credentials are not configured or the request fails.
    """
    global _spot_base
    if not config.BINANCE_API_KEY:
        raise DataError("BINANCE_API_KEY is not configured")
    data, _spot_base = _get(
        config.SPOT_ENDPOINTS, _spot_base,
        "/api/v3/account", {}, signed=True,
    )
    balances = {
        b["asset"]: {"free": float(b["free"]), "locked": float(b["locked"])}
        for b in data.get("balances", [])
        if float(b["free"]) > 0 or float(b["locked"]) > 0
    }
    return balances


def get_open_orders(symbol=None):
    """Fetch open orders (requires BINANCE_API_KEY + SECRET).

    If symbol is None, returns all open orders.
    Returns a list of order dicts from the Binance API.
    """
    global _spot_base
    if not config.BINANCE_API_KEY:
        raise DataError("BINANCE_API_KEY is not configured")
    params = {}
    if symbol:
        params["symbol"] = symbol
    data, _spot_base = _get(
        config.SPOT_ENDPOINTS, _spot_base,
        "/api/v3/openOrders", params, signed=True,
    )
    return data


def place_order(symbol, side, order_type, quantity, price=None, time_in_force="GTC"):
    """Place a spot order (requires BINANCE_API_KEY + SECRET).

    side: "BUY" or "SELL"
    order_type: "LIMIT" or "MARKET"
    quantity: base asset quantity (str or float)
    price: required for LIMIT orders
    time_in_force: "GTC", "IOC", "FOK" — used for LIMIT orders

    Returns the Binance API response dict.
    Raises DataError on failure or missing credentials.
    """
    global _spot_base
    if not config.BINANCE_API_KEY:
        raise DataError("BINANCE_API_KEY is not configured")
    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": order_type.upper(),
        "quantity": str(quantity),
    }
    if order_type.upper() == "LIMIT":
        if price is None:
            raise DataError("price is required for LIMIT orders")
        params["price"] = str(price)
        params["timeInForce"] = time_in_force
    signed = _signed_params(params)
    r = _session().post(
        (config.SPOT_ENDPOINTS[0]) + "/api/v3/order",
        params=signed, timeout=10,
    )
    if r.status_code != 200:
        raise DataError(f"Order placement failed: HTTP {r.status_code} — {r.text[:200]}")
    data = r.json()
    if isinstance(data, dict) and data.get("code", 0) < 0:
        raise DataError(f"Binance error {data['code']}: {data.get('msg', '')}")
    return data
