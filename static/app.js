/* Signal Bot dashboard — realtime WebSocket + AI limit-signal charts.
   v5.0 changes:
   - Critic Review removed (3-stage pipeline: Data → Analyst → Signal Out)
   - AI analysis every 60 s with live countdown ring + MM:SS display
   - Active-signal lock banner (no AI while signal is live)
   - Symbol change auto-updates dashboard instantly via WebSocket subscribe
   - Application-level ping/pong (server echos {type:"pong",t:...})
   - Modern animations: particle bursts, glow pulses, typewriter reasoning
*/
(function () {
  "use strict";

  /* ── palette ─────────────────────────────────────────────────────────────── */
  var C = {
    bg: "#131a22", grid: "#1f2a37", text: "#8b98a5",
    green: "#22c55e", red: "#ef4444", amber: "#f59e0b",
    ema7: "#eab308", ema25: "#38bdf8", ema99: "#a3a3a3",
    blue: "#38bdf8", purple: "#a855f7", teal: "#0ea5e9",
  };

  /* ── element refs ─────────────────────────────────────────────────────────── */
  var symbolEl   = document.getElementById("symbol");
  var intervalEl = document.getElementById("interval");
  var liveDot    = document.getElementById("live-dot");
  var latencyEl  = document.getElementById("latency");
  var priceEl    = document.getElementById("price");
  var tapeEl     = document.getElementById("tape");
  var toastsEl   = document.getElementById("toasts");
  var tickRateEl = document.getElementById("tick-rate");
  var clockEl    = document.getElementById("live-clock");

  /* ── localStorage keys ───────────────────────────────────────────────────── */
  var LS_SYM = "sb_symbol";
  var LS_INT = "sb_interval";

  /* ── live clock ──────────────────────────────────────────────────────────── */
  function updateClock() {
    if (clockEl) clockEl.textContent = new Date().toLocaleTimeString("en-US", { hour12: false });
  }
  updateClock();
  setInterval(updateClock, 1000);

  /* ── tick-rate counter ───────────────────────────────────────────────────── */
  var _tickCount = 0;
  setInterval(function () {
    if (tickRateEl) {
      tickRateEl.textContent = Math.round(_tickCount * 6) + " t/m";
      _tickCount = 0;
    }
  }, 10000);

  /* ── buy/sell pressure ───────────────────────────────────────────────────── */
  var _buyVol = 0, _sellVol = 0;
  var pressureBuy = document.getElementById("pressure-buy");
  var buyPctEl    = document.getElementById("buy-pct");
  var sellPctEl   = document.getElementById("sell-pct");

  function updatePressure() {
    var total = _buyVol + _sellVol;
    if (!total) return;
    var buyPct = Math.round((_buyVol / total) * 100);
    if (pressureBuy) pressureBuy.style.width = buyPct + "%";
    if (buyPctEl)    buyPctEl.textContent    = buyPct + "%";
    if (sellPctEl)   sellPctEl.textContent   = (100 - buyPct) + "%";
  }
  setInterval(function () { _buyVol = 0; _sellVol = 0; }, 60000);

  /* ── candle countdown ────────────────────────────────────────────────────── */
  var _intervalMs     = 3600000;
  var _lastCandleTime = 0;
  var countdownEl     = document.getElementById("candle-countdown");
  var INTERVAL_MS = { "1m":60e3,"3m":180e3,"5m":300e3,"15m":900e3,
                      "30m":1800e3,"1h":3600e3,"4h":14400e3,"1d":86400e3 };

  function updateCandleCountdown() {
    if (!countdownEl || !_lastCandleTime) return;
    var rem = Math.max(0, (_lastCandleTime + 1) * 1000 + _intervalMs - Date.now());
    var m = Math.floor(rem / 60000);
    var s = Math.floor((rem % 60000) / 1000);
    countdownEl.textContent = "next candle " + m + ":" + (s < 10 ? "0" : "") + s;
  }
  setInterval(updateCandleCountdown, 1000);

  /* ── AI analysis countdown ───────────────────────────────────────────────── */
  var _aiNextTs       = 0;        // epoch ms when next AI run is scheduled
  var _aiIntervalSec  = 60;       // full cycle length (seconds)
  var _aiRateLimited  = false;
  var nextAnalysisEl  = document.getElementById("es-next-analysis");
  var ringFillEl      = document.getElementById("countdown-ring-fill");
  var RING_CIRCUMFERENCE = 100;   // matches stroke-dasharray 100

  function updateAICountdown() {
    if (!_aiNextTs || !nextAnalysisEl) return;
    var rem = Math.max(0, _aiNextTs - Date.now() / 1000);
    var m = Math.floor(rem / 60);
    var s = Math.floor(rem % 60);
    var label = (m > 0 ? m + "m " : "") + (s < 10 ? "0" : "") + s + "s";
    nextAnalysisEl.textContent = _aiRateLimited ? ("RL " + label) : label;
    nextAnalysisEl.className = "countdown-pill" +
      (_aiRateLimited ? " rate-limited" :
       rem < 10       ? " imminent"     : "");

    // Ring: dashoffset decreases from CIRCUMFERENCE (full = just ran) → 0 (about to run)
    if (ringFillEl && _aiIntervalSec > 0) {
      var frac   = rem / _aiIntervalSec;
      var offset = RING_CIRCUMFERENCE * Math.max(0, Math.min(1, frac));
      ringFillEl.style.strokeDashoffset = offset;
    }
  }
  setInterval(updateAICountdown, 1000);

  /* ── signal-lock banner ──────────────────────────────────────────────────── */
  var lockBannerEl = document.getElementById("signal-lock-banner");
  var lockTextEl   = document.getElementById("signal-lock-text");
  var lockDetailEl = document.getElementById("signal-lock-detail");

  function showSignalLock(active, direction, reason) {
    if (!lockBannerEl) return;
    if (active) {
      lockBannerEl.classList.remove("hidden");
      lockBannerEl.className = lockBannerEl.className.replace(/\block-(long|short)\b/g, "");
      lockBannerEl.classList.add("block-" + (direction || "").toLowerCase());
      if (lockTextEl) lockTextEl.textContent =
        (direction === "LONG" ? "▲ LONG" : direction === "SHORT" ? "▼ SHORT" : "") +
        " signal active — AI paused until stop hit or target reached";
      if (lockDetailEl) lockDetailEl.textContent = reason || "";
    } else {
      lockBannerEl.classList.add("hidden");
    }
  }

  /* ── data-flash utility ──────────────────────────────────────────────────── */
  function flash(el, cls) {
    if (!el) return;
    el.classList.remove(cls);
    void el.offsetWidth;
    el.classList.add(cls);
    setTimeout(function () { el.classList.remove(cls); }, 800);
  }

  /* ── chart factory ───────────────────────────────────────────────────────── */
  function baseOptions(el, extra) {
    return Object.assign({
      width:  el.clientWidth,
      height: el.clientHeight,
      layout: { background: { color: "transparent" }, textColor: C.text,
                fontFamily: "'JetBrains Mono', monospace", fontSize: 10 },
      grid:  { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      rightPriceScale: { borderColor: C.grid },
      timeScale: { borderColor: C.grid, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
    }, extra || {});
  }

  /* ── main chart ──────────────────────────────────────────────────────────── */
  var chartEl = document.getElementById("chart");
  var chart   = LightweightCharts.createChart(chartEl, baseOptions(chartEl));

  var candleSeries = chart.addCandlestickSeries({
    upColor: C.green, downColor: C.red, borderVisible: false,
    wickUpColor: C.green, wickDownColor: C.red,
  });
  var volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: "volume" }, priceScaleId: "vol",
    priceLineVisible: false, lastValueVisible: false,
  });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

  var ema7Series  = chart.addLineSeries({ color: C.ema7,  lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
  var ema25Series = chart.addLineSeries({ color: C.ema25, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
  var ema99Series = chart.addLineSeries({ color: C.ema99, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });

  var cvdEl    = document.getElementById("cvd-chart");
  var cvdChart = LightweightCharts.createChart(cvdEl, baseOptions(cvdEl));
  var cvdSeries = cvdChart.addAreaSeries({
    lineColor: C.ema25, topColor: "rgba(56,189,248,0.25)", bottomColor: "rgba(56,189,248,0.02)",
    lineWidth: 2, priceLineVisible: false,
  });

  window.addEventListener("resize", function () {
    chart.applyOptions({ width: chartEl.clientWidth, height: chartEl.clientHeight });
    cvdChart.applyOptions({ width: cvdEl.clientWidth, height: cvdEl.clientHeight });
    Object.values(_aiCharts).forEach(function (ctx) {
      var el2 = document.getElementById("ai-chart-" + ctx.interval);
      if (el2) ctx.chart.applyOptions({ width: el2.clientWidth, height: el2.clientHeight });
    });
  });

  var priceLines  = [];
  var trendSeries = [];
  var aiLines     = [];
  var lastAI      = null;
  var lastAIKey   = "";
  var firstLoad   = true;
  var lastCandle  = null;
  var cvdBase     = 0;
  var isLoading   = false;

  function volBar(c) {
    return {
      time: c.time, value: c.volume,
      color: c.close >= c.open ? "rgba(34,197,94,0.35)" : "rgba(239,68,68,0.35)",
    };
  }

  function clearOverlays() {
    priceLines.forEach(function (pl) { candleSeries.removePriceLine(pl); });
    priceLines = [];
    trendSeries.forEach(function (s) { chart.removeSeries(s); });
    trendSeries = [];
  }

  function addPriceLine(price, color, title, style) {
    priceLines.push(candleSeries.createPriceLine({
      price: price, color: color, lineWidth: 1,
      lineStyle: style === undefined ? 2 : style,
      axisLabelVisible: true, title: title,
    }));
  }

  function addAILine(price, color, title, style, width) {
    aiLines.push(candleSeries.createPriceLine({
      price: price, color: color, lineWidth: width || 1,
      lineStyle: style === undefined ? 2 : style,
      axisLabelVisible: true, title: title,
    }));
  }

  function renderAI(a) {
    aiLines.forEach(function (pl) { candleSeries.removePriceLine(pl); });
    aiLines = [];
    if (!a || a.error || a.symbol !== symbolEl.value) return;
    if (a.signal !== "LONG" && a.signal !== "SHORT") return;
    var col = a.signal === "LONG" ? C.green : C.red;
    if (a.entry != null) addAILine(a.entry, col, "AI " + a.signal + " ENTRY · " + a.confidence + "%", 0, 2);
    if (a.stop  != null) addAILine(a.stop,  C.red,   "AI STOP", 2);
    if (a.tp1   != null) addAILine(a.tp1,   C.green, "AI TP1",  2);
    if (a.tp2   != null) addAILine(a.tp2,   C.green, "AI TP2",  2);
  }

  /* ── AI predict mini-charts ──────────────────────────────────────────────── */
  var _aiCharts = {};

  function miniChartOptions(el) {
    return baseOptions(el, {
      rightPriceScale: { borderColor: C.grid, scaleMargins: { top: 0.1, bottom: 0.1 } },
      timeScale: { borderColor: C.grid, timeVisible: true, secondsVisible: false,
                   rightOffset: 3, fixLeftEdge: false },
    });
  }

  function getOrCreateAIChart(interval) {
    if (_aiCharts[interval]) return _aiCharts[interval];
    var el = document.getElementById("ai-chart-" + interval);
    if (!el || !el.clientWidth) return null;
    var ch = LightweightCharts.createChart(el, miniChartOptions(el));
    var cs = ch.addCandlestickSeries({
      upColor: C.green, downColor: C.red, borderVisible: false,
      wickUpColor: C.green, wickDownColor: C.red,
    });
    _aiCharts[interval] = { chart: ch, series: cs, lines: [], interval: interval };
    return _aiCharts[interval];
  }

  function renderAIChart(msg) {
    if (!msg || !msg.candles || !msg.ai) return;
    if (msg.symbol !== symbolEl.value) return;

    var emptyEl = document.getElementById("ai-chart-empty");
    var rowEl   = document.getElementById("ai-chart-row");
    var badgeEl = document.getElementById("ai-chart-badge");
    if (emptyEl) emptyEl.style.display = "none";
    if (rowEl)   rowEl.style.display   = "";

    if (badgeEl && msg.ai.signal) {
      badgeEl.className = "ai-signal-badge " + (msg.ai.signal === "LONG" ? "long" : "short");
      badgeEl.textContent = msg.ai.signal + " · " + (msg.ai.confidence || "?") + "% confidence";
    }

    var ctx = getOrCreateAIChart(msg.interval);
    if (!ctx) return;

    ctx.series.setData(msg.candles.map(function (c) {
      return { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close };
    }));
    ctx.lines.forEach(function (l) { ctx.series.removePriceLine(l); });
    ctx.lines = [];

    var col = msg.ai.signal === "LONG" ? C.green : C.red;
    function mkLine(price, color, title, style, width) {
      if (price == null) return;
      ctx.lines.push(ctx.series.createPriceLine({
        price: price, color: color, lineWidth: width || 1,
        lineStyle: style === undefined ? 2 : style,
        axisLabelVisible: true, title: title,
      }));
    }
    mkLine(msg.ai.entry, col,     "ENTRY", 0, 2);
    mkLine(msg.ai.stop,  C.red,   "STOP",  2, 1);
    mkLine(msg.ai.tp1,   C.green, "TP1",   2, 1);
    mkLine(msg.ai.tp2,   C.green, "TP2",   3, 1);
    ctx.chart.timeScale().fitContent();
    flash(document.getElementById("ai-chart-card"), "card-flash");
  }

  /* ── price animation ─────────────────────────────────────────────────────── */
  var shownPrice = null, targetPrice = null, priceDigits = 2;
  function digitsFor(p) { return p >= 1000 ? 2 : p >= 1 ? 4 : 6; }

  function fmt(n, digits) {
    if (n == null) return "—";
    return Number(n).toLocaleString("en-US", {
      minimumFractionDigits: digits === undefined ? 2 : digits,
      maximumFractionDigits: digits === undefined ? 2 : digits,
    });
  }

  function rafLoop() {
    if (targetPrice !== null) {
      if (shownPrice === null) shownPrice = targetPrice;
      var diff = targetPrice - shownPrice;
      if (Math.abs(diff) > Math.abs(targetPrice) * 1e-7) shownPrice += diff * 0.22;
      else shownPrice = targetPrice;
      priceEl.textContent = fmt(shownPrice, priceDigits);
    }
    requestAnimationFrame(rafLoop);
  }
  requestAnimationFrame(rafLoop);

  var lastFlashPrice = null;
  function flashPrice(p) {
    if (lastFlashPrice !== null && p !== lastFlashPrice) {
      var cls = p > lastFlashPrice ? "flash-up" : "flash-down";
      priceEl.classList.remove("flash-up", "flash-down");
      void priceEl.offsetWidth;
      priceEl.classList.add(cls);
    }
    lastFlashPrice = p;
  }

  /* ── loading skeleton ────────────────────────────────────────────────────── */
  function showLoading(sym, intv) {
    isLoading = true;
    chartEl.classList.add("chart-loading");
    var v = document.getElementById("verdict");
    v.className = "verdict-badge neutral loading-pulse";
    v.textContent = "Loading " + sym + " · " + intv + "…";
    priceEl.classList.remove("flash-up", "flash-down");
    priceEl.textContent = "—";
    var chg = document.getElementById("chg");
    chg.textContent = ""; chg.className = "chg";
    tapeEl.textContent = ""; tapeEl.className = "tape";
    document.getElementById("gauge-needle").style.left = "50%";
    document.getElementById("gauge-score").textContent = "—";
    if (countdownEl) countdownEl.textContent = "";
    document.getElementById("plan-card").classList.add("hidden");
    document.getElementById("fund-card").classList.add("hidden");
    clearOverlays();
    renderAI(null);
    candleSeries.setData([]);
    volumeSeries.setData([]);
    ema7Series.setData([]);
    ema25Series.setData([]);
    ema99Series.setData([]);
    cvdSeries.setData([]);
    var host = document.getElementById("breakdown");
    host.innerHTML = "";
    for (var i = 0; i < 10; i++) {
      var sk = document.createElement("div");
      sk.className = "bd-row bd-skeleton";
      sk.innerHTML =
        '<div class="bd-top">' +
          '<span class="skel skel-name"></span>' +
          '<span class="skel skel-val"></span>' +
        '</div>' +
        '<div class="bd-bar"><div class="bd-mid"></div><div class="skel skel-bar"></div></div>' +
        '<div class="skel skel-why"></div>';
      host.appendChild(sk);
    }
  }

  function clearLoading() {
    isLoading = false;
    chartEl.classList.remove("chart-loading");
    document.getElementById("verdict").classList.remove("loading-pulse");
  }

  /* ── tick / kline ────────────────────────────────────────────────────────── */
  function onTick(t) {
    _tickCount++;
    if (t.sell) _sellVol += t.qty; else _buyVol += t.qty;
    updatePressure();
    if (isLoading) return;
    priceDigits = digitsFor(t.price);
    targetPrice = t.price;
    flashPrice(t.price);
    var sizeTag = t.qty > 10 ? " ●" : t.qty > 1 ? " ·" : "";
    tapeEl.textContent = (t.sell ? "▼ " : "▲ ") + fmt(t.qty, 3) + sizeTag + "  @ " + fmt(t.price, priceDigits);
    tapeEl.className   = "tape " + (t.sell ? "down" : "up") + (t.qty > 5 ? " big" : "");
    if (lastCandle && t.time / 1000 >= lastCandle.time) {
      lastCandle.close = t.price;
      if (t.price > lastCandle.high) lastCandle.high = t.price;
      if (t.price < lastCandle.low)  lastCandle.low  = t.price;
      candleSeries.update({ time: lastCandle.time, open: lastCandle.open,
                            high: lastCandle.high, low: lastCandle.low, close: lastCandle.close });
      lastCandle.volume = (lastCandle.volume || 0) + t.qty;
      lastCandle.delta  = (lastCandle.delta  || 0) + (t.sell ? -t.qty : t.qty);
      volumeSeries.update(volBar(lastCandle));
      cvdSeries.update({ time: lastCandle.time, value: cvdBase + lastCandle.delta });
    }
  }

  function onKline(m) {
    var c = m.candle;
    if (lastCandle && c.time > lastCandle.time) cvdBase += lastCandle.delta || 0;
    lastCandle = c;
    _lastCandleTime = c.time;
    _intervalMs = INTERVAL_MS[intervalEl.value] || 3600000;
    candleSeries.update({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });
    volumeSeries.update(volBar(c));
    cvdSeries.update({ time: c.time, value: cvdBase + (c.delta || 0) });
    priceDigits = digitsFor(c.close);
    targetPrice = c.close;
    flashPrice(c.close);
  }

  /* ── snapshot rendering ──────────────────────────────────────────────────── */
  function renderChart(d) {
    var ov = d.overlays || {};
    candleSeries.setData(d.candles.map(function (c) {
      return { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close };
    }));
    volumeSeries.setData(d.candles.map(volBar));
    ema7Series.setData(ov.ema7 || []);
    ema25Series.setData(ov.ema25 || []);
    ema99Series.setData(ov.ema99 || []);
    cvdSeries.setData(ov.cvd || []);
    lastCandle = d.candles.length ? Object.assign({}, d.candles[d.candles.length - 1]) : null;
    if (lastCandle) {
      _lastCandleTime = lastCandle.time;
      _intervalMs = INTERVAL_MS[intervalEl.value] || 3600000;
    }
    var cvd = ov.cvd || [];
    cvdBase = cvd.length >= 2 ? cvd[cvd.length - 2].value : 0;
    clearOverlays();
    (ov.support    || []).forEach(function (lv) { addPriceLine(lv.price, C.green, "S x" + lv.touches); });
    (ov.resistance || []).forEach(function (lv) { addPriceLine(lv.price, C.red,   "R x" + lv.touches); });
    if (ov.fibonacci)
      ov.fibonacci.levels.forEach(function (lv) { addPriceLine(lv.price, C.amber, "fib " + lv.ratio, 3); });
    if (ov.volume_profile) {
      addPriceLine(ov.volume_profile.poc, "#c084fc", "POC", 0);
      addPriceLine(ov.volume_profile.vah, "#8b98a5", "VAH", 3);
      addPriceLine(ov.volume_profile.val, "#8b98a5", "VAL", 3);
    }
    (ov.order_blocks || []).forEach(function (ob) {
      var col = ob.type === "bullish" ? C.green : C.red;
      addPriceLine(ob.top,    col, "OB " + (ob.type === "bullish" ? "demand" : "supply"), 4);
      addPriceLine(ob.bottom, col, "", 4);
    });
    (ov.fvgs || []).forEach(function (f) {
      addPriceLine(f.mid, f.type === "bullish" ? C.green : C.red, "FVG", 1);
    });
    (ov.trendlines || []).forEach(function (tl) {
      var s = chart.addLineSeries({
        color: tl.type === "support" ? C.green : C.red,
        lineWidth: 1, lineStyle: 0, priceLineVisible: false,
        lastValueVisible: false, crosshairMarkerVisible: false,
      });
      s.setData([{ time: tl.start.time, value: tl.start.price },
                 { time: tl.end.time,   value: tl.end.price }]);
      trendSeries.push(s);
    });
    var markers = [];
    (ov.sweeps || []).forEach(function (sw) {
      markers.push({
        time: sw.time,
        position: sw.type === "bullish" ? "belowBar" : "aboveBar",
        color: sw.type === "bullish" ? C.green : C.red,
        shape: sw.type === "bullish" ? "arrowUp" : "arrowDown",
        text: "SWEEP",
      });
    });
    markers.sort(function (a, b) { return a.time - b.time; });
    candleSeries.setMarkers(markers);
    renderAI(lastAI);
    if (firstLoad) {
      chart.timeScale().fitContent();
      cvdChart.timeScale().fitContent();
      firstLoad = false;
    }
  }

  var shownScore = 0;
  function renderVerdict(d) {
    priceDigits = digitsFor(d.price);
    if (targetPrice === null) targetPrice = d.price;
    var chg = document.getElementById("chg");
    if (d.ticker) {
      var pct = d.ticker.change_pct;
      chg.textContent = (pct >= 0 ? "+" : "") + pct.toFixed(2) + "% 24h";
      chg.className = "chg " + (pct >= 0 ? "up" : "down");
      flash(chg, "data-flash");
    }
    var v = document.getElementById("verdict");
    if (d.direction === "LONG") {
      v.className = "verdict-badge long";
      v.textContent = (d.strength ? d.strength + " " : "") + "LONG SIGNAL";
    } else if (d.direction === "SHORT") {
      v.className = "verdict-badge short";
      v.textContent = (d.strength ? d.strength + " " : "") + "SHORT SIGNAL";
    } else {
      v.className = "verdict-badge neutral";
      v.textContent = "NEUTRAL · waiting for confluence";
    }
    document.getElementById("gauge-needle").style.left = (50 + d.composite / 2) + "%";
    var scoreEl = document.getElementById("gauge-score");
    var from = shownScore, to = d.composite, t0 = performance.now();
    (function step(now) {
      var k = Math.min((now - t0) / 700, 1);
      var val = from + (to - from) * (1 - Math.pow(1 - k, 3));
      scoreEl.textContent = (val > 0 ? "+" : "") + val.toFixed(1) + "  (threshold ±" + d.threshold + ")";
      if (k < 1) requestAnimationFrame(step); else shownScore = to;
    })(t0);
    var planCard = document.getElementById("plan-card");
    if (d.plan) {
      planCard.classList.remove("hidden");
      document.getElementById("plan-entry").textContent = fmt(d.plan.entry, priceDigits);
      document.getElementById("plan-stop").textContent  = fmt(d.plan.stop,  priceDigits);
      document.getElementById("plan-tp1").textContent   = fmt(d.plan.tp1,   priceDigits);
      document.getElementById("plan-tp2").textContent   = fmt(d.plan.tp2,   priceDigits);
      flash(planCard, "card-flash");
    } else {
      planCard.classList.add("hidden");
    }
  }

  function renderBreakdown(d) {
    var host = document.getElementById("breakdown");
    var hasSkeleton = host.querySelector(".bd-skeleton");
    if (hasSkeleton || host.childElementCount !== d.breakdown.length) host.innerHTML = "";
    var build = host.childElementCount !== d.breakdown.length;
    d.breakdown.forEach(function (b, i) {
      var pct = Math.min(Math.abs(b.score) * 50, 50);
      var cls = b.contribution > 0.5 ? "pos" : b.contribution < -0.5 ? "neg" : "zero";
      var row;
      if (build || !host.children[i]) {
        row = document.createElement("div");
        row.className = "bd-row";
        row.innerHTML =
          '<div class="bd-top"><span class="bd-name"></span><span class="bd-val"></span></div>' +
          '<div class="bd-bar"><div class="bd-mid"></div><div class="bd-fill"></div></div>' +
          '<div class="bd-why"></div>';
        host.appendChild(row);
      } else {
        row = host.children[i];
      }
      row.querySelector(".bd-name").innerHTML =
        b.label + ' <span class="bd-wt">(w' + b.weight + ")</span>";
      var valEl = row.querySelector(".bd-val");
      valEl.className = "bd-val " + cls;
      valEl.textContent = (b.contribution > 0 ? "+" : "") + b.contribution;
      var fill = row.querySelector(".bd-fill");
      fill.className = "bd-fill " + (b.score >= 0 ? "pos" : "neg");
      fill.style.width = "0%";
      (function (f, p) {
        requestAnimationFrame(function () {
          requestAnimationFrame(function () { f.style.width = p + "%"; });
        });
      })(fill, pct);
      row.children[2].textContent = b.reasons.join(" · ");
    });
  }

  function renderFundamentals(d) {
    var f = (d.overlays || {}).fundamentals;
    var card = document.getElementById("fund-card");
    if (!f) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    var fr = f.funding_rate * 100;
    var frEl = document.getElementById("f-funding");
    frEl.textContent = fr.toFixed(4) + "%";
    frEl.className = "v " + (fr > 0.03 ? "red" : fr < 0 ? "green" : "");
    document.getElementById("f-oi").textContent   = (f.oi_change_pct >= 0 ? "+" : "") + f.oi_change_pct.toFixed(1) + "%";
    document.getElementById("f-ls").textContent   = f.long_short_ratio.toFixed(2);
    document.getElementById("f-mark").textContent = fmt(f.mark_price, priceDigits);
    flash(card, "card-flash");
  }

  function renderSnapshot(d) {
    clearLoading();
    renderChart(d);
    renderVerdict(d);
    renderBreakdown(d);
    renderFundamentals(d);
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     AI ENGINE STATUS WIDGET
  ═══════════════════════════════════════════════════════════════════════════ */
  var waveBarsEl = document.getElementById("wave-bars");
  var WAVE_COUNT = 28;
  (function buildWave() {
    for (var i = 0; i < WAVE_COUNT; i++) {
      var b = document.createElement("div");
      b.className = "wave-bar";
      waveBarsEl.appendChild(b);
    }
  })();

  var wavePhase    = 0;
  var engineOnline = false;

  function animateWave() {
    wavePhase += engineOnline ? 0.08 : 0.03;
    var bars = waveBarsEl.children;
    for (var i = 0; i < bars.length; i++) {
      var base  = Math.sin(wavePhase + i * 0.45) * 0.38 + 0.5;
      var noise = Math.sin(wavePhase * 2.1 + i * 0.85) * 0.18;
      var h = Math.max(0.05, Math.min(1.0, base + noise));
      bars[i].style.height  = (h * 30) + "px";
      bars[i].style.opacity = engineOnline ? (0.25 + h * 0.75) : 0.1;
    }
    requestAnimationFrame(animateWave);
  }
  animateWave();

  /* Latency sparkline */
  var _latencyRing  = [];
  var _sparkCanvas  = document.getElementById("latency-spark");
  var _sparkCtx     = _sparkCanvas ? _sparkCanvas.getContext("2d") : null;

  function pushLatencySample(ms) {
    if (ms == null) return;
    _latencyRing.push(ms);
    if (_latencyRing.length > 20) _latencyRing.shift();
    drawSparkline();
  }

  function drawSparkline() {
    if (!_sparkCtx || _latencyRing.length < 2) return;
    var W = _sparkCanvas.width, H = _sparkCanvas.height;
    _sparkCtx.clearRect(0, 0, W, H);
    var min = Math.min.apply(null, _latencyRing);
    var max = Math.max.apply(null, _latencyRing);
    var range = max - min || 1;
    var step = W / (_latencyRing.length - 1);
    _sparkCtx.beginPath();
    _latencyRing.forEach(function (v, i) {
      var x = i * step;
      var y = H - ((v - min) / range) * (H - 2) - 1;
      if (i === 0) _sparkCtx.moveTo(x, y); else _sparkCtx.lineTo(x, y);
    });
    var last = _latencyRing[_latencyRing.length - 1];
    var latColor = last < 1000 ? "#22c55e" : last < 3000 ? "#f59e0b" : "#ef4444";
    var gradient = _sparkCtx.createLinearGradient(0, 0, W, 0);
    gradient.addColorStop(0, "rgba(56,189,248,0.3)");
    gradient.addColorStop(1, latColor);
    _sparkCtx.strokeStyle = gradient;
    _sparkCtx.lineWidth = 1.5;
    _sparkCtx.lineJoin = "round";
    _sparkCtx.stroke();
    var lx = (_latencyRing.length - 1) * step;
    var ly = H - ((last - min) / range) * (H - 2) - 1;
    _sparkCtx.beginPath();
    _sparkCtx.arc(lx, ly, 2, 0, Math.PI * 2);
    _sparkCtx.fillStyle = latColor;
    _sparkCtx.fill();
  }

  var _prevProvider = null;

  function renderEngineStatus(s) {
    if (!s) return;
    engineOnline = s.online;

    var dot       = document.getElementById("es-dot");
    var label     = document.getElementById("es-status-label");
    var version   = document.getElementById("es-version");
    var latency   = document.getElementById("es-latency");
    var inference = document.getElementById("es-inference");
    var totalEl   = document.getElementById("es-total");
    var provBadge = document.getElementById("es-provider-badge");
    var modelName = document.getElementById("es-model-name");
    var fallback  = document.getElementById("es-fallback-badge");

    dot.className       = s.online ? "es-dot online" : "es-dot";
    label.textContent   = s.online ? "Online" : "Offline";
    version.textContent = s.version || "v5.0";

    var prov = s.provider || null;
    if (prov !== _prevProvider) {
      _prevProvider = prov;
      if (provBadge) {
        provBadge.classList.remove("provider-switch-anim");
        void provBadge.offsetWidth;
        provBadge.classList.add("provider-switch-anim");
      }
    }

    if (provBadge) {
      if (prov === "groq") {
        provBadge.className   = "es-provider-badge groq";
        provBadge.textContent = "GROQ";
      } else {
        provBadge.className   = "es-provider-badge muted";
        provBadge.textContent = "—";
      }
    }

    if (modelName) {
      var mn = s.current_model || "";
      var displayName = mn.replace(/:free$/, "").replace(/^meta-llama\//, "").replace(/^google\//, "");
      modelName.textContent = displayName || "—";
      modelName.title = mn;
    }

    if (fallback) {
      var rlModels = s.rate_limited_models || {};
      var anyRL = Object.keys(rlModels).length > 0;
      if (anyRL) {
        fallback.classList.remove("hidden");
        fallback.textContent = "RL";
      } else {
        fallback.classList.add("hidden");
      }
    }

    if (s.latency_ms != null) {
      latency.textContent = s.latency_ms + "ms";
      latency.className = "es-metric-value " +
        (s.latency_ms < 1000 ? "green" : s.latency_ms < 3000 ? "amber" : "red");
      pushLatencySample(s.latency_ms);
    } else {
      latency.textContent = "—";
      latency.className   = "es-metric-value";
    }

    var rate = s.inference_per_min || 0;
    if (inference) inference.textContent = rate + "/min";

    if (totalEl) {
      var tot = s.total_inferences || 0;
      totalEl.textContent = tot.toLocaleString();
      if (tot > _lastInferenceCount) {
        _lastInferenceCount = tot;
        triggerPipelineInFlight(s.current_model, s.provider);
      }
    }

    // Active-signal lock banner from status
    var activeSigs = s.active_signals || {};
    var sym = symbolEl ? symbolEl.value : "";
    var activeSig = activeSigs[sym];
    if (activeSig) {
      showSignalLock(true, activeSig.direction,
        "entry " + fmt(activeSig.entry, digitsFor(activeSig.entry || 0)) +
        " · stop " + fmt(activeSig.stop, digitsFor(activeSig.stop || 0)) +
        " · tp1 " + fmt(activeSig.tp1, digitsFor(activeSig.tp1 || 0))
      );
    } else {
      showSignalLock(false);
    }

    flash(document.getElementById("engine-status-card"), "card-flash-subtle");
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     AI COUNTDOWN from server push
  ═══════════════════════════════════════════════════════════════════════════ */
  function onAICountdown(msg) {
    // Only update if it's for the currently selected symbol
    if (msg.symbol && msg.symbol !== symbolEl.value) return;
    _aiNextTs      = msg.next_ts;           // Unix epoch seconds (float)
    _aiIntervalSec = msg.interval_s || 60;
    _aiRateLimited = msg.rate_limited || false;
    updateAICountdown();
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     API PIPELINE — 3 stages: Market Data → AI Analyst → Signal Out
  ═══════════════════════════════════════════════════════════════════════════ */

  var _lastInferenceCount = 0;
  var _pipelineActive     = false;
  var _pipelineTimers     = [];
  var _pipeCallStart      = 0;

  var STAGE_SUBS_IDLE = ["idle", "idle", "—"];

  function _clearPipelineTimers() {
    _pipelineTimers.forEach(clearTimeout);
    _pipelineTimers = [];
  }

  function _setPipeStage(idx, state, subText) {
    var stageEl = document.getElementById("pipe-stage-" + idx);
    var subEl   = document.getElementById("pipe-sub-" + idx);
    if (!stageEl) return;
    stageEl.className = "pipe-stage " + state;
    if (subEl && subText !== undefined) subEl.textContent = subText;
  }

  function _flowConnector(idx, colorCls, dur) {
    var conn     = document.getElementById("pipe-conn-" + idx);
    var particle = document.getElementById("pipe-particle-" + idx);
    if (!conn || !particle) return;
    conn.className = "pipe-connector flowing";
    particle.style.setProperty("--flow-dur", (dur || 0.65) + "s");
    particle.className = "pipe-particle";
    void particle.offsetWidth;
    particle.className = "pipe-particle flowing" + (colorCls ? " " + colorCls : "");
  }

  function _doneConnector(idx, state) {
    var conn = document.getElementById("pipe-conn-" + idx);
    if (conn) conn.className = "pipe-connector " + (state || "done");
  }

  function _resetPipeline() {
    for (var i = 0; i <= 2; i++) _setPipeStage(i, "idle", STAGE_SUBS_IDLE[i]);
    for (var j = 0; j <= 1; j++) {
      var conn = document.getElementById("pipe-conn-" + j);
      if (conn) conn.className = "pipe-connector";
    }
  }

  function _updateAnalystDetail(ai) {
    var model     = (ai.model_used || ai.model || "—").replace(/:free$/, "");
    var signal    = ai.signal || "WAIT";
    var conf      = ai.confidence || 0;
    var setup     = ai.setup_type || "—";
    var reasoning = (ai.reasoning || ai.orderflow_read || "—").slice(0, 220);
    var prov      = ai.provider || "groq";

    var modelEl = document.getElementById("pd-model");
    var sigEl   = document.getElementById("pd-signal");
    var confEl  = document.getElementById("pd-confidence");
    var setupEl = document.getElementById("pd-setup");
    var reasEl  = document.getElementById("pd-reasoning");

    if (modelEl) {
      modelEl.textContent = model || "—";
      modelEl.className   = "pipe-detail-val prov-" + prov;
    }
    if (sigEl) {
      sigEl.textContent = signal;
      sigEl.className   = "pipe-detail-val " +
        (signal === "LONG" ? "sig-long" : signal === "SHORT" ? "sig-short" : "sig-wait");
    }
    if (confEl) confEl.textContent = conf ? conf + "%" : "—";
    if (setupEl) setupEl.textContent = setup || "—";

    // Typewriter effect for reasoning
    if (reasEl) {
      _typewriter(reasEl, reasoning, 12);
    }
  }

  /* Simple typewriter effect */
  function _typewriter(el, text, msPerChar) {
    el.textContent = "";
    var i = 0;
    var tid = setInterval(function () {
      if (i >= text.length) { clearInterval(tid); return; }
      el.textContent += text[i++];
    }, msPerChar);
  }

  function _updateSignalOutDetail(ai) {
    var signal = ai.signal || "WAIT";
    var verdictEl  = document.getElementById("pd-signal-out-verdict");
    var iconEl     = document.getElementById("pd-signal-out-icon");
    var labelEl    = document.getElementById("pd-signal-out-label");
    var entryRow   = document.getElementById("pd-entry-row");
    var stopRow    = document.getElementById("pd-stop-row");
    var tpRow      = document.getElementById("pd-tp-row");
    var rrRow      = document.getElementById("pd-rr-row");

    if (!verdictEl) return;

    if (signal === "LONG" || signal === "SHORT") {
      var d = digitsFor(ai.entry || ai.price || 1);
      verdictEl.className = "pipe-signal-out-verdict " + (signal === "LONG" ? "sig-long" : "sig-short");
      if (iconEl)  iconEl.textContent  = signal === "LONG" ? "▲" : "▼";
      if (labelEl) labelEl.textContent = signal + " · " + (ai.confidence || 0) + "% confidence";
      if (entryRow) { entryRow.style.display = ""; document.getElementById("pd-entry").textContent = fmt(ai.entry, d); }
      if (stopRow)  { stopRow.style.display  = ""; document.getElementById("pd-stop").textContent  = fmt(ai.stop,  d); }
      if (tpRow) {
        tpRow.style.display = "";
        document.getElementById("pd-tp").textContent =
          fmt(ai.tp1, d) + (ai.tp2 ? " / " + fmt(ai.tp2, d) : "");
      }
      if (rrRow && ai.risk_reward != null) {
        rrRow.style.display = "";
        document.getElementById("pd-rr").textContent = ai.risk_reward + ":1";
      } else if (rrRow) {
        rrRow.style.display = "none";
      }
    } else {
      verdictEl.className = "pipe-signal-out-verdict sig-wait";
      if (iconEl)  iconEl.textContent  = "◎";
      if (labelEl) labelEl.textContent = "WAIT — no trade setup";
      if (entryRow) entryRow.style.display = "none";
      if (stopRow)  stopRow.style.display  = "none";
      if (tpRow)    tpRow.style.display    = "none";
      if (rrRow)    rrRow.style.display    = "none";
    }
  }

  function triggerPipelineInFlight(model, provider) {
    if (_pipelineActive) return;
    _pipelineActive = true;
    _pipeCallStart  = Date.now();
    _clearPipelineTimers();
    _setPipeStage(0, "done", "fetched");
    _flowConnector(0, "", 0.5);
    _pipelineTimers.push(setTimeout(function () {
      _doneConnector(0, "done");
      var shortModel = (model || "").replace(/:free$/, "").split("/").pop() || "model";
      _setPipeStage(1, "active", shortModel + "…");
    }, 560));
  }

  function completePipeline(ai) {
    _pipelineActive = false;
    _clearPipelineTimers();

    var signal    = ai.signal || "WAIT";
    var model     = ai.model_used || ai.model || null;
    var latencyMs = ai.latency_ms || (Date.now() - _pipeCallStart);
    var conf      = ai.confidence || 0;

    var sigClass  = signal === "LONG" ? "done-long" : signal === "SHORT" ? "done-short" : "wait";
    var connClass = signal === "LONG" ? "long"       : signal === "SHORT" ? "short"      : "done";
    var shortModel = (model || "").replace(/:free$/, "").split("/").pop() || "model";

    if (!document.getElementById("pipe-stage-0").classList.contains("done")) {
      _setPipeStage(0, "done", "fetched");
    }
    _doneConnector(0, connClass);

    var analystSub = shortModel + (conf ? " · " + conf + "%" : "");
    _setPipeStage(1, "done", analystSub);
    _updateAnalystDetail(ai);
    _flowConnector(1, connClass, 0.45);

    _pipelineTimers.push(setTimeout(function () {
      _doneConnector(1, connClass);
      // Stage 2: Signal Out
      var outSub;
      if (signal === "LONG")       outSub = "▲ LONG · " + conf + "%";
      else if (signal === "SHORT") outSub = "▼ SHORT · " + conf + "%";
      else                         outSub = "WAIT";
      _pipelineTimers.push(setTimeout(function () {
        _setPipeStage(2, sigClass, outSub);
        _updateSignalOutDetail(ai);
        // Signal burst animation
        if (signal === "LONG" || signal === "SHORT") {
          _burstSignal(signal);
        }
      }, 300));
    }, 500));

    var lastCallEl = document.getElementById("pipe-last-call");
    var durationEl = document.getElementById("pipe-duration");
    if (lastCallEl) lastCallEl.textContent = "Last call: just now";
    if (durationEl) durationEl.textContent = latencyMs + "ms";

    addPipelineLogEntry(signal, model, ai.provider, latencyMs, conf, ai.signal_active);

    _pipelineTimers.push(setTimeout(_resetPipeline, 30000));
  }

  /* Particle burst on signal fire */
  function _burstSignal(signal) {
    var card = document.getElementById("verdict-card");
    if (!card) return;
    card.classList.remove("card-burst-long", "card-burst-short");
    void card.offsetWidth;
    card.classList.add(signal === "LONG" ? "card-burst-long" : "card-burst-short");
    setTimeout(function () {
      card.classList.remove("card-burst-long", "card-burst-short");
    }, 1200);
  }

  /* Keep updating "X ago" on the last-call label */
  var _lastCallTime = 0;
  setInterval(function () {
    if (!_lastCallTime) return;
    var el = document.getElementById("pipe-last-call");
    if (!el) return;
    var diff = Math.round((Date.now() - _lastCallTime) / 1000);
    if (diff < 5)  el.textContent = "Last call: just now";
    else if (diff < 60)  el.textContent = "Last call: " + diff + "s ago";
    else el.textContent = "Last call: " + Math.floor(diff / 60) + "m ago";
  }, 5000);

  /* Pipeline call log */
  var _logEntries = 0;

  function addPipelineLogEntry(signal, model, provider, latencyMs, conf, signalActive) {
    _lastCallTime = Date.now();
    var logEl = document.getElementById("pipe-log");
    if (!logEl) return;
    var empty = logEl.querySelector(".pipe-log-empty");
    if (empty) empty.remove();

    var row = document.createElement("div");
    row.className = "pipe-log-row new-entry";
    var now     = new Date().toLocaleTimeString("en-US", { hour12: false });
    var provCls = provider === "groq" ? "groq" : "unknown";
    var sigCls  = signal === "LONG" ? "long" : signal === "SHORT" ? "short" : "wait";
    var confTag = conf ? '<span class="pipe-log-conf">' + conf + "%</span>" : "";
    var lockTag = signalActive ? '<span class="pipe-log-lock" title="signal active — analysis skipped">🔒</span>' : "";
    row.innerHTML =
      '<span class="pipe-log-time">' + now + "</span>" +
      '<span class="pipe-log-model ' + provCls + '">' + (provider || "?").toUpperCase() + "</span>" +
      '<span class="pipe-log-signal ' + sigCls + '">' + (signal || "WAIT") + "</span>" +
      confTag + lockTag +
      '<span class="pipe-log-sym">' + (symbolEl ? symbolEl.value : "") + "</span>" +
      '<span class="pipe-log-lat">' + (latencyMs || "—") + "ms</span>";
    logEl.insertBefore(row, logEl.firstChild);
    setTimeout(function () { row.classList.remove("new-entry"); }, 1000);
    _logEntries++;
    while (logEl.children.length > 30) logEl.removeChild(logEl.lastChild);
  }

  /* ── Pipeline Events ────────────────────────────────────────────────────── */
  function _pipeEvtIcon(stage) {
    var icons = {
      "market_data": "◈", "market_data_regime": "◈", "memory_context": "◇",
      "ai_call": "▶", "model_attempt": "↻", "model_rate_limited": "⚠",
      "model_success": "✓", "model_error": "✗", "provider_fallback": "↪",
      "provider_recovered": "↩", "ai_parsed": "✓", "trade_quality": "◆",
      "signal_out": "◎",
    };
    return icons[stage] || "·";
  }

  function _pipeEvtLabel(evt) {
    var s = evt.stage || "";
    switch (s) {
      case "market_data":
        return evt.status === "fetching" ? "Fetching market data…"
             : "Market data · price " + (evt.price ? evt.price.toFixed(2) : "—");
      case "market_data_regime":
        return "Regime: " + (evt.regime || "—") + " · score " + (evt.composite != null ? evt.composite : "—");
      case "memory_context":
        return evt.status === "loading" ? "Loading signal memory…"
             : "Memory: " + (evt.found || 0) + " similar setup" + (evt.found === 1 ? "" : "s") + " found";
      case "ai_call":
        return evt.status === "start" ? "AI call started"
             : "AI responded · " + (evt.latency_ms || "—") + "ms · " + (evt.model ? evt.model.replace(/:free$/, "") : "—");
      case "model_attempt":
        return "Trying model: " + (evt.model || "—");
      case "model_rate_limited":
        return "Rate-limited: " + (evt.model || "—") + (evt.cooldown_s ? " · cooldown " + evt.cooldown_s + "s" : "");
      case "model_success":
        return "Model OK: " + (evt.model || "—");
      case "model_error":
        return "Model error: " + (evt.model || "—");
      case "provider_fallback":
        return "All models rate-limited" + (evt.cooldown_s ? " for " + evt.cooldown_s + "s" : "");
      case "provider_recovered":
        return "Primary provider recovered";
      case "ai_parsed":
        return "Signal: " + (evt.signal || "WAIT") +
               (evt.confidence ? " · " + evt.confidence + "% conf" : "");
      case "trade_quality":
        return evt.status === "computing" ? "Computing trade quality…"
             : "Quality grade: " + (evt.grade || "—");
      case "signal_out":
        return "Signal out: " + (evt.signal || "WAIT") +
               (evt.confidence ? " · " + evt.confidence + "%" : "") +
               (evt.latency_ms ? " · " + evt.latency_ms + "ms" : "");
      default:
        return s.replace(/_/g, " ");
    }
  }

  function _pipeEvtCls(evt) {
    var s = evt.stage || "";
    if (s === "model_rate_limited" || s === "model_error") return "pev-warn";
    if (s === "provider_fallback")  return "pev-warn";
    if (s === "model_success" || s === "provider_recovered") return "pev-ok";
    if (s === "signal_out") {
      if (evt.signal === "LONG")  return "pev-long";
      if (evt.signal === "SHORT") return "pev-short";
      return "pev-wait";
    }
    if (s === "ai_parsed") {
      if (evt.signal === "LONG")  return "pev-long";
      if (evt.signal === "SHORT") return "pev-short";
    }
    return "pev-default";
  }

  function renderPipelineEvents(events) {
    var el = document.getElementById("pipe-events-list");
    if (!el || !events || !events.length) return;
    var countEl = document.getElementById("pipe-events-count");
    if (countEl) countEl.textContent = events.length + " events";
    var latestRunId = null;
    events.forEach(function (e) { if (!latestRunId && e.run_id) latestRunId = e.run_id; });
    el.innerHTML = "";
    var nowS = Date.now() / 1000;
    events.slice(0, 40).forEach(function (evt, i) {
      var row  = document.createElement("div");
      var isNew = i === 0;
      var cls   = "pev-row " + _pipeEvtCls(evt) + (isNew ? " pev-new" : "");
      if (evt.run_id === latestRunId) cls += " pev-latest-run";
      row.className = cls;
      var t = evt.ts
        ? new Date(evt.ts * 1000).toLocaleTimeString("en-US", { hour12: false })
        : "—";
      var ageS = evt.ts ? Math.round(nowS - evt.ts) : null;
      var age  = ageS !== null && ageS >= 0
        ? (ageS < 5 ? "just now" : ageS < 60 ? ageS + "s ago" : Math.floor(ageS / 60) + "m ago")
        : "";
      row.innerHTML =
        '<span class="pev-time">' + t + "</span>" +
        '<span class="pev-icon">' + _pipeEvtIcon(evt.stage) + "</span>" +
        '<span class="pev-label">' + _pipeEvtLabel(evt) + "</span>" +
        (age ? '<span class="pev-age">' + age + "</span>" : "");
      el.appendChild(row);
      if (isNew) setTimeout(function () { row.classList.remove("pev-new"); }, 900);
    });
  }

  _resetPipeline();

  /* ── Binance status ──────────────────────────────────────────────────────── */
  function fetchBinanceStatus() {
    fetch("/api/binance-key-status")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var configured = d.api_key_configured && d.api_secret_configured;
        document.getElementById("bnb-key").textContent    = d.api_key_configured    ? "Configured" : "Not set";
        document.getElementById("bnb-key").className      = "es-metric-value " + (d.api_key_configured    ? "green" : "red");
        document.getElementById("bnb-secret").textContent = d.api_secret_configured ? "Configured" : "Not set";
        document.getElementById("bnb-secret").className   = "es-metric-value " + (d.api_secret_configured ? "green" : "red");
        document.getElementById("bnb-orders").textContent = configured ? "Enabled" : "Read-only";
        document.getElementById("bnb-orders").className   = "es-metric-value " + (configured ? "green" : "");
        document.getElementById("bnb-mode").textContent   = configured ? "Authenticated" : "Public";
        document.getElementById("bnb-mode").className     = "es-version " + (configured ? "green" : "");
        document.getElementById("bnb-hint").style.display = configured ? "none" : "";
      })
      .catch(function () {});
  }
  fetchBinanceStatus();

  /* ── Recent AI Signals table ─────────────────────────────────────────────── */
  var allSignalRows = [];
  var sigFilterMode = "all";
  var filterBtnEl   = document.getElementById("sig-filter-btn");
  var sigCountEl    = document.getElementById("sig-count");
  var relTimeEls    = [];

  if (filterBtnEl) {
    filterBtnEl.addEventListener("click", function () {
      sigFilterMode = sigFilterMode === "all" ? "symbol" : "all";
      filterBtnEl.textContent = sigFilterMode === "all" ? "All symbols" : symbolEl.value + " only";
      filterBtnEl.classList.toggle("active", sigFilterMode === "symbol");
      redrawSigTable();
    });
  }

  function updateFilterLabel() {
    if (!filterBtnEl) return;
    filterBtnEl.textContent = sigFilterMode === "all" ? "All symbols" : symbolEl.value + " only";
  }

  function relTime(ts) {
    var diff = Math.floor(Date.now() / 1000) - ts;
    if (diff < 5)    return "just now";
    if (diff < 60)   return diff + "s ago";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    return Math.floor(diff / 86400) + "d ago";
  }

  function absTime(ts) {
    return new Date(ts * 1000).toLocaleString("en-US", {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
  }

  setInterval(function () {
    relTimeEls.forEach(function (obj) { obj.el.textContent = relTime(obj.ts); });
  }, 15000);

  function redrawSigTable() {
    var rows = allSignalRows;
    if (sigFilterMode === "symbol") {
      var sym = symbolEl.value;
      rows = rows.filter(function (r) { return r.symbol === sym; });
    }
    var tbody = document.getElementById("ai-sig-tbody");
    tbody.innerHTML = "";
    relTimeEls = [];
    if (!rows.length) {
      var empty = document.createElement("tr");
      empty.className = "ai-sig-empty";
      var msg = sigFilterMode === "symbol"
        ? "No AI signals for " + (symbolEl.value || "this symbol") + " yet"
        : "No AI signals yet — watching for high-quality setups…";
      empty.innerHTML = "<td colspan='5'>" + msg + "</td>";
      tbody.appendChild(empty);
      sigCountEl.textContent = "";
      return;
    }
    sigCountEl.textContent = rows.length + " signal" + (rows.length !== 1 ? "s" : "");
    rows.forEach(function (row, idx) {
      var isLong  = row.direction === "LONG";
      var conf    = row.confidence || 0;
      var confCls = conf >= 75 ? "conf-high" : conf >= 55 ? "conf-mid" : "conf-low";
      var symBase = (row.symbol || "").replace("USDT", "").toLowerCase();
      var symCls  = "sym-badge sym-" + symBase;
      var tr = document.createElement("tr");
      tr.setAttribute("data-symbol", row.symbol || "");
      if (idx === 0) tr.classList.add("ai-sig-new");
      tr.style.cursor = "pointer";

      var timeEl = document.createElement("td");
      timeEl.className = "time-cell";
      timeEl.textContent = relTime(row.time);
      timeEl.title = absTime(row.time);
      relTimeEls.push({ el: timeEl, ts: row.time });

      var symTd  = document.createElement("td");
      var badge  = document.createElement("span");
      badge.className = symCls;
      badge.textContent = row.symbol || "—";
      symTd.appendChild(badge);

      var setupTd = document.createElement("td");
      setupTd.className = "setup-cell";
      setupTd.textContent = row.setup_type || "—";

      var dirTd = document.createElement("td");
      dirTd.className = isLong ? "dir-long" : "dir-short";
      dirTd.innerHTML = '<span class="dir-arrow">' + (isLong ? "↑" : "↓") + "</span> " + row.direction;

      var confTd = document.createElement("td");
      confTd.innerHTML =
        '<div class="conf-cell">' +
          '<div class="conf-bar-track">' +
            '<div class="conf-bar-fill ' + confCls + '" style="width:' + Math.max(4, conf) + '%"></div>' +
          '</div>' +
          '<span class="conf-num ' + confCls + '">' + conf + "%</span>" +
        "</div>";

      tr.appendChild(timeEl);
      tr.appendChild(symTd);
      tr.appendChild(setupTd);
      tr.appendChild(dirTd);
      tr.appendChild(confTd);
      tbody.appendChild(tr);

      tr.addEventListener("click", function () {
        var s = this.getAttribute("data-symbol");
        if (s && s !== symbolEl.value) {
          symbolEl.value = s;
          localStorage.setItem(LS_SYM, s);
          reset();
        }
      });
    });
  }

  function renderAISignalsTable(rows) {
    if (!rows) return;
    allSignalRows = rows;
    redrawSigTable();
  }

  /* ── signal cards ────────────────────────────────────────────────────────── */
  var _pendingLimits = [];

  function signalCard(s) {
    var el = document.createElement("div");
    el.className = "sig " + s.direction.toLowerCase();
    var when = new Date(s.time * 1000).toLocaleString();
    var orderBadge = s.order_type === "LIMIT"
      ? ' <span class="badge-limit">LIMIT</span>'
      : ' <span class="badge-market">MARKET</span>';
    el.innerHTML =
      '<div class="sig-top">' +
        '<span class="sig-dir">' + s.direction + (s.strength ? " · " + s.strength : "") + orderBadge + "</span>" +
        '<span class="sig-meta">' + s.symbol + " " + (s.interval || "") + " · " + s.score + "% confidence</span>" +
      "</div>" +
      '<div class="sig-meta">' + when + " · @ " + fmt(s.price, digitsFor(s.price)) + "</div>" +
      (s.reasons && s.reasons.length
        ? '<div class="sig-reasons">' + s.reasons.filter(Boolean).slice(0, 2).join(" · ") + "</div>"
        : "");
    return el;
  }

  function renderSignals(list) {
    var host = document.getElementById("signals");
    if (!list.length) return;
    host.innerHTML = "";
    list.forEach(function (s) { host.appendChild(signalCard(s)); });
  }

  function pushSignal(s) {
    var host  = document.getElementById("signals");
    var empty = host.querySelector(".empty");
    if (empty) empty.remove();
    var el = signalCard(s);
    el.classList.add("sig-new");
    host.insertBefore(el, host.firstChild);
    toast(s);
  }

  function toast(s) {
    var t = document.createElement("div");
    t.className = "toast " + s.direction.toLowerCase();
    t.innerHTML =
      '<span class="toast-dir">' + s.direction + (s.strength ? " · " + s.strength : "") + "</span>" +
      '<span class="toast-meta">' + s.symbol + " " + (s.interval || "") + " @ " + fmt(s.price, digitsFor(s.price)) + "</span>";
    toastsEl.appendChild(t);
    setTimeout(function () {
      t.classList.add("toast-out");
      setTimeout(function () { t.remove(); }, 400);
    }, 6000);
  }

  /* ── Pending Limit Orders ───────────────────────────────────────────────── */
  function renderPendingLimits(data) {
    _pendingLimits = data || [];
    var container = document.getElementById("pending-limits-list");
    var emptyEl   = document.getElementById("pending-limits-empty");
    var countEl   = document.getElementById("pending-limits-count");
    if (!container) return;
    if (!_pendingLimits.length) {
      container.innerHTML = "";
      if (emptyEl) emptyEl.style.display = "";
      if (countEl) countEl.textContent   = "";
      return;
    }
    if (emptyEl) emptyEl.style.display = "none";
    if (countEl) countEl.textContent   = _pendingLimits.length + " pending";
    container.innerHTML = "";
    _pendingLimits.forEach(function (order) {
      var d    = digitsFor(order.entry || 1);
      var row  = document.createElement("div");
      row.className = "pending-limit-row " + order.direction.toLowerCase();
      var tpText = order.tp1 != null
        ? fmt(order.tp1, d) + (order.tp2 != null ? " / " + fmt(order.tp2, d) : "")
        : "—";
      row.innerHTML =
        '<div class="pl-left">' +
          '<span class="pl-dir ' + order.direction.toLowerCase() + '">' + order.direction + "</span>" +
          '<span class="pl-sym">' + order.symbol + "</span>" +
          '<span class="badge-limit">LIMIT</span>' +
          '<span class="pl-k" style="margin-left:6px;font-size:.68rem;color:#4b5563">' + (order.setup_type || "") + "</span>" +
        "</div>" +
        '<div class="pl-levels">' +
          '<span class="pl-kv"><span class="pl-k">Entry</span> <span class="pl-v">'     + fmt(order.entry, d) + "</span></span>" +
          '<span class="pl-kv"><span class="pl-k">Stop</span>  <span class="pl-v red">' + fmt(order.stop,  d) + "</span></span>" +
          '<span class="pl-kv"><span class="pl-k">TP</span>    <span class="pl-v green">' + tpText + "</span></span>" +
          (order.confidence ? '<span class="pl-kv"><span class="pl-k">Conf</span> <span class="pl-v">' + order.confidence + "%</span></span>" : "") +
        "</div>" +
        '<div class="pl-meta">' + (order.reasoning || "—").slice(0, 100) + "</div>";
      container.appendChild(row);
    });
  }

  function onLimitTriggered(order) {
    /* Push triggered order into the signals feed with a LIMIT badge */
    pushSignal({
      direction:  order.direction,
      strength:   "LIMIT HIT · " + order.confidence + "%",
      order_type: "LIMIT",
      symbol:     order.symbol,
      interval:   "—",
      score:      order.confidence,
      time:       order.trigger_time || Math.floor(Date.now() / 1000),
      price:      order.trigger_price || order.entry,
      reasons:    ["limit entry triggered"],
    });
    /* Flash a more prominent toast */
    var t = document.createElement("div");
    t.className = "toast " + order.direction.toLowerCase();
    t.innerHTML =
      "<span class=\"toast-dir\">⚡ LIMIT " + order.direction + " TRIGGERED</span>" +
      "<span class=\"toast-meta\">" + order.symbol + " @ " +
        fmt(order.trigger_price || order.entry, digitsFor(order.entry || 1)) + "</span>";
    toastsEl.appendChild(t);
    setTimeout(function () {
      t.classList.add("toast-out");
      setTimeout(function () { t.remove(); }, 400);
    }, 8000);
  }

  function onAI(a) {
    lastAI = a;
    renderAI(a);

    // Signal-lock banner
    if (a && a.signal_active) {
      showSignalLock(true, a.direction || a.signal,
        a.signal_lock_reason || "signal active");
    }

    if (a && !a.error) {
      completePipeline(a);
    } else {
      _pipelineActive = false;
      _clearPipelineTimers();
      _resetPipeline();
    }

    if (!a || a.error || a.signal === "WAIT" || a.signal_active) return;
    var key = a.symbol + ":" + a.signal + ":" + a.entry;
    if (key === lastAIKey) return;
    lastAIKey = key;
    pushSignal({
      direction:  a.signal,
      strength:   "AI · " + a.confidence + "%",
      order_type: a.order_type || "MARKET",
      symbol:     a.symbol,
      interval:   a.interval,
      score:      a.confidence,
      time:       a.updated,
      price:      a.entry != null ? a.entry : a.price,
      reasons:    [a.setup_type, a.orderflow_read].filter(Boolean),
    });
  }

  /* ── selectors with localStorage restore ────────────────────────────────── */
  function fillSelect(el, values, serverDefault) {
    if (el.childElementCount) return;
    var lsKey = el === symbolEl ? LS_SYM : LS_INT;
    var saved = localStorage.getItem(lsKey);
    var pick  = (saved && values.indexOf(saved) >= 0) ? saved : serverDefault;
    values.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v; o.textContent = v;
      if (v === pick) o.selected = true;
      el.appendChild(o);
    });
    if (pick !== serverDefault) {
      setTimeout(function () {
        if (symbolEl.childElementCount && intervalEl.childElementCount) subscribe();
      }, 80);
    }
  }

  /* ── WebSocket ───────────────────────────────────────────────────────────── */
  var ws = null, reconnectDelay = 1000, pingTimer = null;

  function subscribe() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "subscribe", symbol: symbolEl.value, interval: intervalEl.value }));
    }
  }

  function connect() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    liveDot.className = "dot";
    ws = new WebSocket(proto + "//" + location.host + "/ws");

    ws.onopen = function () {
      reconnectDelay = 1000;
      liveDot.className = "dot live";
      if (symbolEl.value) subscribe();
      clearInterval(pingTimer);
      pingTimer = setInterval(function () {
        if (ws.readyState === WebSocket.OPEN)
          ws.send(JSON.stringify({ type: "ping", t: performance.now() }));
      }, 5000);
    };

    ws.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      switch (m.type) {
        case "config":
          fillSelect(symbolEl,   m.symbols,   m.default_symbol);
          fillSelect(intervalEl, m.intervals, m.default_interval);
          if (m.ai_refresh_seconds) _aiIntervalSec = m.ai_refresh_seconds;
          break;
        case "snapshot":
          if (m.data.symbol === symbolEl.value && m.data.interval === intervalEl.value)
            renderSnapshot(m.data);
          break;
        case "tick":
          if (m.symbol === symbolEl.value) onTick(m);
          break;
        case "kline":
          if (m.symbol === symbolEl.value && m.interval === intervalEl.value) onKline(m);
          break;
        case "signal":
          pushSignal(m.data);
          break;
        case "signals":
          renderSignals(m.data);
          break;
        case "ai":
          if (m.data.symbol === symbolEl.value) onAI(m.data);
          break;
        case "ai_chart":
          renderAIChart(m);
          break;
        case "engine_status":
          renderEngineStatus(m.data);
          break;
        case "ai_signals_table":
          renderAISignalsTable(m.data);
          break;
        case "pending_limits":
          renderPendingLimits(m.data);
          break;
        case "limit_triggered":
          onLimitTriggered(m.data);
          break;
        case "pipeline_log":
          renderPipelineEvents(m.data);
          break;
        case "ai_countdown":
          onAICountdown(m);
          break;
        case "pong":
          latencyEl.textContent = Math.max(1, Math.round(performance.now() - m.t)) + "ms";
          break;
        case "error":
          liveDot.className = "dot err";
          break;
      }
    };

    ws.onclose = function () {
      liveDot.className = "dot err";
      clearInterval(pingTimer);
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    };
    ws.onerror = function () { ws.close(); };
  }

  /* ── reset on market change ──────────────────────────────────────────────── */
  function reset() {
    var sym  = symbolEl.value;
    var intv = intervalEl.value;
    localStorage.setItem(LS_SYM, sym);
    localStorage.setItem(LS_INT, intv);

    firstLoad      = true;
    lastCandle     = null;
    lastAI         = null;
    lastAIKey      = "";
    lastFlashPrice = null;
    shownPrice     = null;
    targetPrice    = null;
    shownScore     = 0;
    _buyVol        = 0;
    _sellVol       = 0;
    _lastCandleTime = 0;

    // Clear signal lock banner
    showSignalLock(false);

    // Clear pending limits display (will be refreshed via subscribe)
    renderPendingLimits([]);

    // Reset AI chart section
    var emptyEl = document.getElementById("ai-chart-empty");
    var rowEl   = document.getElementById("ai-chart-row");
    var badgeEl = document.getElementById("ai-chart-badge");
    if (emptyEl) emptyEl.style.display = "";
    if (rowEl)   rowEl.style.display   = "none";
    if (badgeEl) { badgeEl.textContent = ""; badgeEl.className = "ai-signal-badge hidden"; }
    Object.values(_aiCharts).forEach(function (ctx) { ctx.chart.remove(); });
    _aiCharts = {};

    // Reset pipeline detail
    var pdSignal = document.getElementById("pd-signal");
    if (pdSignal) pdSignal.textContent = "—";
    var pdReas = document.getElementById("pd-reasoning");
    if (pdReas) pdReas.textContent = "—";

    showLoading(sym, intv);
    updateFilterLabel();
    if (sigFilterMode === "symbol") redrawSigTable();

    subscribe();
  }

  symbolEl.addEventListener("change",  function () { localStorage.setItem(LS_SYM, symbolEl.value);   reset(); });
  intervalEl.addEventListener("change", function () { localStorage.setItem(LS_INT, intervalEl.value); reset(); });

  connect();
})();
