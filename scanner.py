"""Coin Scanner — ranks Binance USDT futures by ATR-based grid suitability.

Scoring formula (identical to Grid Bot v9 MarketScanner):
    score = atr_pct × vol_score × trend_mult × cmc_mult

  atr_pct    ATR(14) on 15m futures klines / price × 100
  vol_score  min(1.0, log10(max(1, vol_24h_M)) / 3.0)
  trend_mult 0.5 if |4h_move| > SCANNER_TREND_PCT (grid bots need sideways)
  cmc_mult   sideways_mult × quality_mult  (default 0.60 when no CMC key)

Usage (called from server.py via asyncio.to_thread):
    from scanner import scanner
    result = scanner.scan()           # blocking, returns state dict
    state  = scanner.get_state()      # non-blocking, returns last cached state
"""
import json
import math
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

import config
import data_feed


class CoinScanner:
    def __init__(self):
        self._lock        = threading.Lock()
        self._results:  list  = []
        self._best:     str   = ""
        self._scanned_at:float= 0.0
        self._scanning: bool  = False
        self._progress: str   = ""
        self._cmc_cache: dict = {}
        self._cmc_ts:   float = 0.0

    # ── public API ─────────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        """Non-blocking snapshot of current scanner state."""
        with self._lock:
            return {
                "results":     list(self._results),
                "best":        self._best,
                "scanned_at":  self._scanned_at,
                "scanning":    self._scanning,
                "progress":    self._progress,
                "cmc_enabled": bool(getattr(config, "CMC_API_KEY", "")),
            }

    def should_scan(self) -> bool:
        interval_h = float(getattr(config, "SCANNER_INTERVAL_HOURS", 2))
        with self._lock:
            return (not self._scanning) and (time.time() - self._scanned_at > interval_h * 3600)

    def scan(self, on_progress=None) -> dict:
        """Run a full scan; blocks until complete. Thread-safe (re-entrant guard).

        on_progress: optional callable(str) called with progress messages.
        Returns the final state dict.
        """
        with self._lock:
            if self._scanning:
                return self.get_state()
            self._scanning = True
            self._progress = "Starting…"

        def _prog(msg: str):
            with self._lock:
                self._progress = msg
            if on_progress:
                try:
                    on_progress(msg)
                except Exception:
                    pass

        try:
            return self._do_scan(_prog)
        except Exception:
            traceback.print_exc()
            return self.get_state()
        finally:
            with self._lock:
                self._scanning = False
                self._progress = ""

    # ── internals ──────────────────────────────────────────────────────────────

    def _do_scan(self, progress) -> dict:
        min_vol_m       = float(getattr(config, "SCANNER_MIN_VOL_M",      50))
        blacklist       = set(getattr(config,   "SCANNER_BLACKLIST",       ["BTCUSDT", "ETHUSDT"]))
        show_top        = int(getattr(config,   "SCANNER_SHOW_TOP",        10))
        trend_penalty   = bool(getattr(config,  "SCANNER_TREND_PENALTY",   True))
        trend_hours     = int(getattr(config,   "SCANNER_TREND_HOURS",     4))
        trend_pct       = float(getattr(config, "SCANNER_TREND_PCT",       3.0))
        trend_mult_val  = float(getattr(config, "SCANNER_TREND_MULT",      0.5))
        min_vol         = min_vol_m * 1_000_000

        # ── Step 1: all futures 24h tickers ──────────────────────────────────
        progress("Fetching futures tickers…")
        try:
            tickers = data_feed.get_all_futures_tickers()
        except Exception as e:
            progress(f"Ticker fetch failed: {e}")
            return self.get_state()

        candidates = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            if sym in blacklist:
                continue
            vol = float(t.get("quoteVolume", 0) or 0)
            if vol < min_vol:
                continue
            cp = float(t.get("lastPrice", 0) or 0)
            if cp <= 0:
                continue
            candidates.append({
                "symbol": sym,
                "price":  cp,
                "vol24":  vol,
                "chg24":  float(t.get("priceChangePercent", 0) or 0),
            })

        if not candidates:
            progress("No candidates above volume threshold")
            return self.get_state()

        progress(f"Found {len(candidates)} candidates — enriching with CMC…")

        # ── Step 2: optional CoinMarketCap enrichment ────────────────────────
        cmc_map: dict = {}
        cmc_api_key = getattr(config, "CMC_API_KEY", "") or ""
        if cmc_api_key:
            try:
                cmc_map = self._fetch_cmc(cmc_api_key, {c["symbol"] for c in candidates})
                progress(f"CMC matched {len(cmc_map)} coins — scoring…")
            except Exception as e:
                progress(f"CMC failed ({e}) — Binance-only scoring")
        else:
            progress(f"No CMC key — Binance-only scoring for {len(candidates)} coins…")

        # ── Step 3: score each candidate ─────────────────────────────────────
        scored = []
        total  = len(candidates)
        for i, cd in enumerate(candidates):
            if i % 15 == 0:
                progress(f"Scoring {i}/{total}…")
            sym = cd["symbol"]
            try:
                # ATR(14) on 15m futures klines
                raw = data_feed.get_futures_klines(sym, "15m", limit=50)
                if len(raw) < 15:
                    continue
                closes = [float(k[4]) for k in raw]
                highs  = [float(k[2]) for k in raw]
                lows   = [float(k[3]) for k in raw]
                trs = [
                    max(highs[j] - lows[j],
                        abs(highs[j] - closes[j - 1]),
                        abs(lows[j]  - closes[j - 1]))
                    for j in range(1, len(closes))
                ]
                atr14   = sum(trs[-14:]) / 14
                atr_pct = atr14 / cd["price"] * 100 if cd["price"] > 0 else 0

                # Volume score: log-scaled, capped at 1.0
                vol_m  = cd["vol24"] / 1_000_000
                vol_sc = min(1.0, math.log10(max(1.0, vol_m)) / 3.0)

                # Trend penalty: discount strongly trending coins
                trend_m = 1.0
                if trend_penalty:
                    try:
                        kl = data_feed.get_futures_klines(sym, "1h", limit=trend_hours + 1)
                        if len(kl) >= 2:
                            move_pct = abs(
                                (float(kl[-1][4]) - float(kl[0][4])) / float(kl[0][4]) * 100
                            )
                            if move_pct > trend_pct:
                                trend_m = trend_mult_val
                    except Exception:
                        pass

                # CMC multiplier
                cmc_d    = cmc_map.get(sym, {})
                cmc_mult = cmc_d.get("cmc_mult", 0.60)
                chg7d    = cmc_d.get("change_7d", 0.0)
                mcap_m   = cmc_d.get("market_cap", 0.0) / 1_000_000

                score = atr_pct * vol_sc * trend_m * cmc_mult

                scored.append({
                    "symbol":   sym,
                    "atr_pct":  round(atr_pct,  3),
                    "vol_m":    round(vol_m,     1),
                    "chg24":    round(cd["chg24"], 2),
                    "chg7d":    round(chg7d,     2),
                    "mcap_m":   int(mcap_m),
                    "cmc_mult": round(cmc_mult,  2),
                    "trend_m":  round(trend_m,   2),
                    "score":    round(score,      4),
                })
            except Exception:
                pass
            time.sleep(0.03)  # ~33 req/s max — stay within Binance rate limits

        if not scored:
            progress("No coins survived scoring filters")
            return self.get_state()

        scored.sort(key=lambda x: x["score"], reverse=True)
        best = scored[0]["symbol"]
        ts   = time.time()

        with self._lock:
            self._results    = scored[:show_top]
            self._best       = best
            self._scanned_at = ts

        progress(f"Done — best: {best}  score: {scored[0]['score']:.4f}")
        return self.get_state()

    # ── CoinMarketCap ──────────────────────────────────────────────────────────

    def _fetch_cmc(self, api_key: str, binance_syms: set) -> dict:
        """Fetch CMC listings and build symbol → multipliers map (1 h TTL)."""
        if self._cmc_cache and time.time() - self._cmc_ts < 3600:
            return self._cmc_cache

        url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest?" + \
              urllib.parse.urlencode({
                  "start": 1, "limit": 200,
                  "sort": "volume_24h", "sort_dir": "desc",
                  "convert": "USD",
              })
        req = urllib.request.Request(
            url, headers={"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            listings = json.loads(resp.read().decode()).get("data", [])

        min_cap_m = float(getattr(config, "CMC_MIN_MARKET_CAP_M", 100)) * 1_000_000
        mid_cap_m = float(getattr(config, "CMC_QUALITY_MIDCAP_M", 10000)) * 1_000_000

        coin_map: dict = {}
        for coin in listings:
            sym = coin.get("symbol", "").upper() + "USDT"
            if sym not in binance_syms:
                continue
            try:
                q    = coin["quote"]["USD"]
                chg7 = float(q.get("percent_change_7d", 0) or 0)
                mcap = float(q.get("market_cap",        0) or 0)
                if mcap < min_cap_m:
                    continue
                qm  = 1.0 if mcap <= mid_cap_m else 0.5
                abs7 = abs(chg7)
                sm  = 1.0 if abs7 <= 5 else (0.65 if abs7 <= 12 else 0.20)
                coin_map[sym] = {
                    "change_7d":  chg7,
                    "market_cap": mcap,
                    "cmc_mult":   round(sm * qm, 4),
                }
            except Exception:
                continue

        self._cmc_cache = coin_map
        self._cmc_ts    = time.time()
        return coin_map


# Module-level singleton
scanner = CoinScanner()
