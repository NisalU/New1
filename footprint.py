"""Footprint chart aggregator.

    Builds per-candle, per-price-level bid/ask volume from live aggTrade ticks.
    Exposes compact summaries consumed by the AI analyst prompt.

    Bin size is auto-scaled to the instrument price, or set via
    config.FOOTPRINT_BIN_SIZE. Each aggTrade is classified:
    m=True  → taker sold  → aggressive seller hits the bid
    m=False → taker bought → aggressive buyer lifts the ask
    """
    import threading
    from collections import defaultdict

    import config

    _VALUE_AREA_PCT    = 0.70
    _IMBALANCE_RATIO   = getattr(config, "FOOTPRINT_IMBALANCE_RATIO", 3.0)
    _MAX_CANDLES       = getattr(config, "FOOTPRINT_MAX_CANDLES",    20)
    _MAX_IMBALANCES    = 5
    _MAX_HVN           = 3
    _MAX_LVN           = 3


    def _bin_size(price: float) -> float:
      """Auto-scale bin width to the instrument price."""
      override = getattr(config, "FOOTPRINT_BIN_SIZE", None)
      if override:
          return float(override)
      if price >= 50_000:  return 10.0
      if price >= 10_000:  return  5.0
      if price >=  1_000:  return  1.0
      if price >=    100:  return  0.1
      return 0.01


    def _snap(price: float, bsz: float) -> float:
      """Snap price down to nearest bin boundary."""
      return round(int(price / bsz) * bsz, 8)


    class FootprintAggregator:
      """Thread-safe footprint chart aggregator.

      Called from asyncio (stream.py aggTrade handler) and from threads
      (AI analyst).  The lock is held for only microseconds — safe in both.
      """

      def __init__(self):
          self._lock    = threading.Lock()
          # symbol -> {price_bin -> {bid, ask}}   (accumulates until candle closes)
          self._current: dict[str, dict] = defaultdict(dict)
          # symbol -> [footprint_dict, ...]       (newest last, capped at _MAX_CANDLES)
          self._history: dict[str, list] = defaultdict(list)
          # symbol -> last known price (for bin-size scaling)
          self._last_price: dict[str, float] = {}

      # ── Public ingest ───────────────────────────────────────────────────────

      def on_trade(self, symbol: str, price: float, qty: float, is_sell: bool):
          """Record one aggTrade tick into the forming candle."""
          bsz = _bin_size(price)
          b   = _snap(price, bsz)
          with self._lock:
              self._last_price[symbol] = price
              levels = self._current[symbol]
              if b not in levels:
                  levels[b] = {"bid": 0.0, "ask": 0.0}
              if is_sell:
                  levels[b]["bid"] += qty   # taker sold → aggressive sell
              else:
                  levels[b]["ask"] += qty   # taker bought → aggressive buy

      def on_candle_close(self, symbol: str, candle_time: int,
                          candle_high: float, candle_low: float):
          """Finalise the forming candle and store its footprint."""
          with self._lock:
              raw  = dict(self._current.get(symbol, {}))
              self._current[symbol] = {}

          if not raw:
              return

          fp = _analyse(raw, candle_time, candle_high, candle_low)
          if not fp:
              return

          with self._lock:
              hist = self._history[symbol]
              hist.append(fp)
              if len(hist) > _MAX_CANDLES:
                  hist.pop(0)

      # ── Public read ─────────────────────────────────────────────────────────

      def get_summary(self, symbol: str, n: int = 5) -> list[dict]:
          """Return the last *n* completed candle footprints for the AI prompt."""
          with self._lock:
              return list(self._history.get(symbol, []))[-n:]

      def get_partial(self, symbol: str) -> dict | None:
          """Return a quick summary of the currently forming candle."""
          with self._lock:
              raw = dict(self._current.get(symbol, {}))
          return _partial(raw) if raw else None


    # ── Internal analysis ────────────────────────────────────────────────────────

    def _analyse(levels: dict, candle_time: int,
               candle_high: float, candle_low: float) -> dict:
      if not levels:
          return {}

      prices     = sorted(levels.keys())
      total_bid  = sum(v["bid"] for v in levels.values())
      total_ask  = sum(v["ask"] for v in levels.values())
      total_vol  = total_bid + total_ask
      if total_vol == 0:
          return {}

      total_delta = round(total_ask - total_bid, 4)

      # ── Point of Control ───────────────────────────────────────────────────
      poc = max(prices, key=lambda p: levels[p]["bid"] + levels[p]["ask"])

      # ── Value Area (70 % of volume, expanding from POC) ───────────────────
      poc_idx   = prices.index(poc)
      va_vol    = levels[poc]["bid"] + levels[poc]["ask"]
      lo, hi    = poc_idx, poc_idx
      target    = total_vol * _VALUE_AREA_PCT

      while va_vol < target and (lo > 0 or hi < len(prices) - 1):
          add_lo = (levels[prices[lo - 1]]["bid"] + levels[prices[lo - 1]]["ask"]) if lo > 0 else 0
          add_hi = (levels[prices[hi + 1]]["bid"] + levels[prices[hi + 1]]["ask"]) if hi < len(prices) - 1 else 0
          if add_lo >= add_hi and lo > 0:
              lo -= 1; va_vol += add_lo
          elif hi < len(prices) - 1:
              hi += 1; va_vol += add_hi
          else:
              break

      va_high = prices[hi]
      va_low  = prices[lo]

      # ── Imbalances ─────────────────────────────────────────────────────────
      imbalances = []
      for p, v in levels.items():
          b, a = v["bid"], v["ask"]
          if a > 0 and b > 0:
              if b / a >= _IMBALANCE_RATIO:
                  imbalances.append({"p": p, "side": "bid", "ratio": round(b / a, 1)})
              elif a / b >= _IMBALANCE_RATIO:
                  imbalances.append({"p": p, "side": "ask", "ratio": round(a / b, 1)})
          elif a == 0 and b > 0:
              imbalances.append({"p": p, "side": "bid", "ratio": 99})
          elif b == 0 and a > 0:
              imbalances.append({"p": p, "side": "ask", "ratio": 99})

      imbalances.sort(key=lambda x: -x["ratio"])
      imbalances = imbalances[:_MAX_IMBALANCES]

      # ── High / low volume nodes ────────────────────────────────────────────
      by_vol    = sorted(prices, key=lambda p: -(levels[p]["bid"] + levels[p]["ask"]))
      hvn       = sorted(by_vol[:_MAX_HVN])
      lvn       = sorted(by_vol[-_MAX_LVN:]) if len(by_vol) > _MAX_HVN + _MAX_LVN else []

      # ── Unfinished auction ─────────────────────────────────────────────────
      bsz       = _bin_size(candle_high)
      hi_bin    = _snap(candle_high, bsz)
      lo_bin    = _snap(candle_low,  bsz)
      unfinished = None
      if hi_bin in levels:
          v = levels[hi_bin]
          if v["bid"] == 0 and v["ask"] > 0:
              unfinished = {"p": hi_bin, "side": "ask"}   # only buyers at top → magnet
      if lo_bin in levels and unfinished is None:
          v = levels[lo_bin]
          if v["ask"] == 0 and v["bid"] > 0:
              unfinished = {"p": lo_bin, "side": "bid"}   # only sellers at bottom → magnet

      return {
          "t":          candle_time,
          "poc":        round(poc,      2),
          "delta":      total_delta,
          "bid_vol":    round(total_bid,  4),
          "ask_vol":    round(total_ask,  4),
          "va_high":    round(va_high,  2),
          "va_low":     round(va_low,   2),
          "imbalances": imbalances,
          "hvn":        [round(p, 2) for p in hvn],
          "lvn":        [round(p, 2) for p in lvn],
          "unfinished": unfinished,
      }


    def _partial(levels: dict) -> dict:
      total_bid = sum(v["bid"] for v in levels.values())
      total_ask = sum(v["ask"] for v in levels.values())
      if total_bid + total_ask == 0:
          return {}
      poc = max(levels, key=lambda p: levels[p]["bid"] + levels[p]["ask"])
      return {
          "partial": True,
          "poc":     round(poc, 2),
          "delta":   round(total_ask - total_bid, 4),
          "bid_vol": round(total_bid, 4),
          "ask_vol": round(total_ask, 4),
      }


    footprint = FootprintAggregator()
    