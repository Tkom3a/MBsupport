(function () {
  const DOWN = "#e15b64";
  const UP = "#4fba7a";
  const GRID = "#243044";
  const MUTED = "#8b95a8";
  const TEXT = "#d7deea";
  const AMBER = "#d7a13b";

  function $(id) { return document.getElementById(id); }

  function fmt(n, d) { return Number(n).toFixed(d == null ? 2 : d); }

  function fmtMoney(n) {
    const v = Number(n) || 0;
    if (v >= 1e6) return fmt(v / 1e6, 2) + "M USDT";
    if (v >= 1e3) return fmt(v / 1e3, 1) + "k USDT";
    if (v > 0) return fmt(v, 0) + " USDT";
    return "—";
  }

  function fmtVol(n) {
    const v = Number(n) || 0;
    if (v >= 1e6) return fmt(v / 1e6, 2) + "M";
    if (v >= 1e3) return fmt(v / 1e3, 1) + "k";
    return fmt(v, v >= 10 ? 0 : 2);
  }

  function fmtPx(n) {
    const v = Number(n);
    if (!v) return "—";
    if (v >= 100) return fmt(v, 2);
    if (v >= 1) return fmt(v, 4);
    return fmt(v, 6);
  }

  function fmtTime(ts) {
    const d = new Date(ts);
    const p = (x) => String(x).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function niceStep(range) {
    if (range <= 0.4) return 0.1;
    if (range <= 1) return 0.25;
    if (range <= 2) return 0.5;
    if (range <= 4) return 1;
    return Math.ceil(range / 6);
  }

  function openOverlay() {
    $("chartOverlay").classList.add("open");
  }

  function closeOverlay() {
    $("chartOverlay").classList.remove("open");
  }

  function setTitle(shot, bar) {
    const dir = shot.direction || "";
    const cls = dir === "UP" ? "up" : "down";
    $("chartTitle").innerHTML =
      `${shot.symbol || ""} <span class="${cls}">${dir} ${fmt(shot.percent || 0)}%</span>` +
      ` <span class="meta">${shot.time || ""} · свеча ${bar}</span>`;
    $("chartSub").textContent =
      `ордер ${fmt(shot.suggest_distance || 0)}% · окно ${shot.window_ms || 0} мс` +
      (shot.lever ? ` · x${Math.round(shot.lever)}` : "");
  }

  function synthetic(shot) {
    const start = Number(shot.start_price) || 0;
    const ext = Number(shot.extreme_price) || start;
    if (!start) return [];
    const ts = Number(shot.peak_ts) || Date.now();
    return [{
      ts,
      o: start,
      h: Math.max(start, ext),
      l: Math.min(start, ext),
      c: ext,
      vol: 0,
      vol_quote: Number(shot.quote_volume) || 0,
    }];
  }

  function drawChart(payload) {
    const canvas = $("chartCanvas");
    const shot = payload.shot || {};
    let candles = payload.candles || [];
    if (!candles.length) candles = synthetic(shot);
    if (!candles.length) {
      $("chartHint").textContent = "Нет свечей OKX вокруг этого времени";
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    $("chartHint").textContent = "Наведите на свечу: объём контрактов и USDT. Линейка справа — % от цены старта прострела.";

    const wrap = canvas.parentElement;
    const cssW = Math.max(640, wrap.clientWidth);
    const cssH = 520;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    canvas.style.width = cssW + "px";
    canvas.style.height = cssH + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const padL = 72, padR = 70, padT = 16, padB = 22;
    const volH = Math.floor(cssH * 0.2);
    const gap = 10;
    const plotW = cssW - padL - padR;
    const plotH = cssH - padT - padB - volH - gap;
    const volTop = padT + plotH + gap;

    const startPx = Number(shot.start_price) || candles[0].o;
    const extPx = Number(shot.extreme_price) || 0;
    const highs = candles.map((c) => c.h);
    const lows = candles.map((c) => c.l);
    if (startPx) { highs.push(startPx); lows.push(startPx); }
    if (extPx) { highs.push(extPx); lows.push(extPx); }
    let pMax = Math.max.apply(null, highs);
    let pMin = Math.min.apply(null, lows);
    const pad = (pMax - pMin) * 0.12 || pMax * 0.002;
    pMax += pad;
    pMin -= pad;
    const volMax = Math.max.apply(null, candles.map((c) => c.vol || 0)) || 1;

    const n = candles.length;
    const slot = plotW / n;
    const bodyW = Math.max(1.5, Math.min(9, slot * 0.62));

    function xAt(i) { return padL + slot * i + slot / 2; }
    function yAt(p) { return padT + (pMax - p) / (pMax - pMin) * plotH; }
    function pctOf(p) { return startPx ? (p - startPx) / startPx * 100 : 0; }

    ctx.fillStyle = "#10141c";
    ctx.fillRect(0, 0, cssW, cssH);

    ctx.strokeStyle = GRID;
    ctx.lineWidth = 1;
    const pctMin = pctOf(pMin);
    const pctMax = pctOf(pMax);
    const step = niceStep(Math.abs(pctMax - pctMin));
    const startTick = Math.floor(pctMin / step) * step;
    for (let pct = startTick; pct <= pctMax + 1e-9; pct += step) {
      const price = startPx * (1 + pct / 100);
      const y = yAt(price);
      if (y < padT || y > padT + plotH) continue;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.fillStyle = MUTED;
      ctx.font = "11px Segoe UI, sans-serif";
      ctx.textAlign = "right";
      ctx.fillText(fmtPx(price), padL - 8, y + 4);
    }

    const peak = Number(shot.peak_ts) || 0;
    const win = Number(shot.window_ms) || 0;
    if (peak) {
      const t0 = peak - win;
      const i0 = candles.findIndex((c) => c.ts >= t0);
      const i1 = candles.reduce((acc, c, i) => (c.ts <= peak + 400 ? i : acc), 0);
      if (i0 >= 0) {
        ctx.fillStyle = shot.direction === "UP" ? "rgba(79,186,122,0.10)" : "rgba(225,91,100,0.10)";
        const x0 = xAt(Math.max(0, i0)) - slot / 2;
        const x1 = xAt(Math.max(i0, i1)) + slot / 2;
        ctx.fillRect(x0, padT, Math.max(slot, x1 - x0), plotH);
      }
    }

    candles.forEach((c, i) => {
      const up = c.c >= c.o;
      const color = up ? UP : DOWN;
      const x = xAt(i);
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, yAt(c.h));
      ctx.lineTo(x, yAt(c.l));
      ctx.stroke();
      const y1 = yAt(Math.max(c.o, c.c));
      const y2 = yAt(Math.min(c.o, c.c));
      const h = Math.max(1, y2 - y1);
      ctx.fillRect(x - bodyW / 2, y1, bodyW, h);
    });

    if (startPx) {
      const y = yAt(startPx);
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = MUTED;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    const shotColor = shot.direction === "UP" ? UP : DOWN;
    if (startPx && extPx) {
      const y0 = yAt(startPx);
      const y1 = yAt(extPx);
      const peakIdx = candles.reduce((best, c, i) => {
        if (!peak) return best;
        return Math.abs(c.ts - peak) < Math.abs(candles[best].ts - peak) ? i : best;
      }, 0);
      const xr = xAt(peakIdx) + Math.max(10, bodyW);
      ctx.strokeStyle = shotColor;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(xr, y0);
      ctx.lineTo(xr, y1);
      ctx.stroke();
      const ticks = 6;
      for (let i = 0; i <= ticks; i++) {
        const y = y0 + (y1 - y0) * i / ticks;
        ctx.beginPath();
        ctx.moveTo(xr - 4, y);
        ctx.lineTo(xr + 4, y);
        ctx.stroke();
      }
      const midY = (y0 + y1) / 2;
      ctx.fillStyle = shotColor;
      ctx.font = "bold 13px Segoe UI, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(fmt(shot.percent || Math.abs(pctOf(extPx)), 2) + "%", xr + 8, midY + 4);
    }

    ctx.textAlign = "left";
    ctx.font = "11px Segoe UI, sans-serif";
    for (let pct = startTick; pct <= pctMax + 1e-9; pct += step) {
      const price = startPx * (1 + pct / 100);
      const y = yAt(price);
      if (y < padT || y > padT + plotH) continue;
      const label = (pct >= 0 ? "+" : "") + fmt(pct, step < 0.5 ? 2 : 1) + "%";
      const nearShot = extPx && Math.abs(pct - pctOf(extPx)) < step * 0.35;
      ctx.fillStyle = nearShot ? shotColor : MUTED;
      ctx.fillText(label, padL + plotW + 8, y + 4);
      ctx.strokeStyle = nearShot ? shotColor : GRID;
      ctx.beginPath();
      ctx.moveTo(padL + plotW, y);
      ctx.lineTo(padL + plotW + 6, y);
      ctx.stroke();
    }

    candles.forEach((c, i) => {
      const x = xAt(i);
      const h = (c.vol || 0) / volMax * (volH - 4);
      ctx.fillStyle = c.c >= c.o ? UP + "cc" : DOWN + "cc";
      ctx.fillRect(x - bodyW / 2, volTop + volH - h, bodyW, h);
    });
    ctx.fillStyle = MUTED;
    ctx.font = "11px Segoe UI, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("объём", padL, volTop - 2);

    if (peak) {
      const peakC = candles.reduce((best, c) => Math.abs(c.ts - peak) < Math.abs(best.ts - peak) ? c : best, candles[0]);
      const idx = candles.indexOf(peakC);
      const x = xAt(idx);
      const money = peakC.vol_quote || shot.quote_volume || 0;
      ctx.fillStyle = AMBER;
      ctx.font = "bold 11px Segoe UI, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(fmtVol(peakC.vol) + " · " + fmtMoney(money), x, volTop + 12);
    }

    ctx.fillStyle = MUTED;
    ctx.font = "10px Segoe UI, sans-serif";
    ctx.textAlign = "center";
    [0, Math.floor(n / 2), n - 1].forEach((i) => {
      if (candles[i]) ctx.fillText(fmtTime(candles[i].ts), xAt(i), cssH - 6);
    });

    canvas.onmousemove = function (ev) {
      const rect = canvas.getBoundingClientRect();
      const x = (ev.clientX - rect.left);
      const i = Math.max(0, Math.min(n - 1, Math.floor((x - padL) / slot)));
      const c = candles[i];
      if (!c) return;
      $("chartHint").textContent =
        `${fmtTime(c.ts)}  O ${fmtPx(c.o)}  H ${fmtPx(c.h)}  L ${fmtPx(c.l)}  C ${fmtPx(c.c)}` +
        `  ·  vol ${fmtVol(c.vol)}  ·  ${fmtMoney(c.vol_quote)}` +
        `  ·  ${startPx ? ((c.c - startPx) / startPx * 100 >= 0 ? "+" : "") + fmt(pctOf(c.c), 2) + "%" : ""}`;
    };
  }

  window.openShotChart = async function (api, symbol, ts) {
    openOverlay();
    $("chartTitle").textContent = symbol;
    $("chartSub").textContent = "загрузка свечей…";
    $("chartHint").textContent = "Один запрос к OKX, без фоновой нагрузки.";
    try {
      const payload = await api(`/api/chart?symbol=${encodeURIComponent(symbol)}&ts=${ts}`);
      setTitle(payload.shot || { symbol: symbol }, payload.bar || "1s");
      drawChart(payload);
    } catch (err) {
      $("chartHint").textContent = "Не удалось загрузить график: " + err.message;
    }
  };

  window.closeShotChart = closeOverlay;
})();
