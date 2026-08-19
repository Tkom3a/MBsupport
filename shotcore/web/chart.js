(function () {
  const DOWN = "#e15b64";
  const UP = "#4fba7a";
  const GRID = "#243044";
  const MUTED = "#8b95a8";
  const TEXT = "#d7deea";
  const AMBER = "#d7a13b";
  const PATH = "#5aa8ff";

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

  function orderPrice(shot) {
    const start = Number(shot.start_price) || 0;
    const dist = Number(shot.suggest_distance) || 0;
    if (!start || !dist) return 0;
    return shot.direction === "UP" ? start * (1 + dist / 100) : start * (1 - dist / 100);
  }

  function xAtTs(candles, ts, padL, slot) {
    const n = candles.length;
    if (!n || !ts) return padL + slot / 2;
    function xAt(i) { return padL + slot * i + slot / 2; }
    if (ts <= candles[0].ts) return xAt(0);
    if (ts >= candles[n - 1].ts) return xAt(n - 1);
    for (let i = 1; i < n; i++) {
      if (candles[i].ts >= ts) {
        const t0 = candles[i - 1].ts;
        const t1 = candles[i].ts;
        const a = t1 === t0 ? 1 : (ts - t0) / (t1 - t0);
        return xAt(i - 1) + (xAt(i) - xAt(i - 1)) * Math.max(0, Math.min(1, a));
      }
    }
    return xAt(n - 1);
  }

  function inferFillTs(candles, shot, fillPx) {
    if (Number(shot.fill_ts) > 0) return Number(shot.fill_ts);
    if (!fillPx) return Number(shot.peak_ts) || 0;
    const startTs = Number(shot.start_ts) || 0;
    for (let i = 0; i < candles.length; i++) {
      const c = candles[i];
      if (startTs && c.ts + 1000 < startTs) continue;
      if (shot.direction === "UP" && c.h >= fillPx) return c.ts;
      if (shot.direction !== "UP" && c.l <= fillPx) return c.ts;
    }
    return Number(shot.peak_ts) || (candles[0] && candles[0].ts) || 0;
  }

  function pathPoints(shot, candles, fillTs, fillPx) {
    const raw = Array.isArray(shot.path) ? shot.path : [];
    const pts = raw
      .filter((p) => p && p.length >= 2 && Number(p[0]) >= fillTs - 5 && Number(p[1]) > 0)
      .map((p) => [Number(p[0]), Number(p[1])]);
    if (pts.length >= 2) return pts;
    const out = fillTs && fillPx ? [[fillTs, fillPx]] : [];
    candles.forEach((c) => {
      if (c.ts + 500 < fillTs) return;
      out.push([c.ts, c.c]);
    });
    const last = Number(shot.exit_price) || Number(shot.last_price) || 0;
    if (last && out.length) out.push([out[out.length - 1][0], last]);
    return out;
  }

  function pill(ctx, text, x, y, bg, fg) {
    ctx.font = "bold 11px Segoe UI, sans-serif";
    const w = ctx.measureText(text).width + 12;
    const h = 16;
    ctx.fillStyle = bg;
    ctx.beginPath();
    const r = 4;
    ctx.moveTo(x + r, y - 11);
    ctx.lineTo(x + w - r, y - 11);
    ctx.quadraticCurveTo(x + w, y - 11, x + w, y - 11 + r);
    ctx.lineTo(x + w, y - 11 + h - r);
    ctx.quadraticCurveTo(x + w, y - 11 + h, x + w - r, y - 11 + h);
    ctx.lineTo(x + r, y - 11 + h);
    ctx.quadraticCurveTo(x, y - 11 + h, x, y - 11 + h - r);
    ctx.lineTo(x, y - 11 + r);
    ctx.quadraticCurveTo(x, y - 11, x + r, y - 11);
    ctx.fill();
    ctx.fillStyle = fg;
    ctx.textAlign = "left";
    ctx.fillText(text, x + 6, y + 1);
    return w;
  }

  function drawOrderOverlay(ctx, g) {
    const { candles, shot, fillPx, exitPx, startPx, padL, padT, plotW, plotH, xAt, yAt, xAtTs } = g;
    if (!fillPx || !startPx) return;
    const dist = Number(shot.suggest_distance) || 0;
    const fillTs = inferFillTs(candles, shot, fillPx);
    const yFill = yAt(fillPx);
    const yStart = yAt(startPx);

    ctx.save();
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = AMBER;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    ctx.moveTo(padL, yFill);
    ctx.lineTo(padL + plotW, yFill);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();

    const label = `ордер ${fmt(dist, 2)}%  ${fmtPx(fillPx)}`;
    const labelY = yFill < padT + 18 ? yFill + 18 : yFill - 6;
    pill(ctx, label, padL + 8, labelY, "#d7a13bcc", "#1a1408");

    const xfMark = xAtTs(fillTs);
    const xf = Math.max(padL + 16, xfMark - 18);
    ctx.strokeStyle = AMBER;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(xf, yStart);
    ctx.lineTo(xf, yFill);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(xf - 5, yStart);
    ctx.lineTo(xf + 5, yStart);
    ctx.moveTo(xf - 5, yFill);
    ctx.lineTo(xf + 5, yFill);
    ctx.stroke();
    ctx.fillStyle = AMBER;
    ctx.font = "bold 12px Segoe UI, sans-serif";
    ctx.textAlign = "right";
    const distY = (yStart + yFill) / 2;
    ctx.fillText(fmt(dist, 2) + "%", xf - 8, distY + 4);

    ctx.beginPath();
    ctx.fillStyle = AMBER;
    ctx.arc(xfMark, yFill, 4.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = TEXT;
    ctx.font = "bold 10px Segoe UI, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("вход", xfMark + 8, yFill + 4);

    const pts = pathPoints(shot, candles, fillTs, fillPx);
    if (pts.length >= 2) {
      ctx.beginPath();
      ctx.strokeStyle = PATH;
      ctx.lineWidth = 2.2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      pts.forEach((pt, i) => {
        const x = xAtTs(pt[0]);
        const y = yAt(pt[1]);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      const last = pts[pts.length - 1];
      const xLast = xAtTs(last[0]);
      const yLast = yAt(exitPx || last[1]);
      ctx.beginPath();
      ctx.fillStyle = UP;
      ctx.arc(xLast, yLast, 4.5, 0, Math.PI * 2);
      ctx.fill();
      const tp = Number(shot.pnl_pct);
      const tpText = Number.isFinite(tp) ? `TP ${tp >= 0 ? "+" : ""}${fmt(tp, 2)}%` : "выход";
      pill(ctx, tpText, Math.min(xLast + 8, padL + plotW - 70), yLast < padT + 18 ? yLast + 16 : yLast - 6, "#4fba7acc", "#07140c");
    }
  }

  function setTitle(shot, bar) {
    const dir = shot.direction || "";
    const cls = dir === "UP" ? "up" : "down";
    $("chartTitle").innerHTML =
      `${shot.symbol || ""} <span class="${cls}">${dir} ${fmt(shot.percent || 0)}%</span>` +
      ` <span class="meta">${shot.time || ""} · свеча ${bar}</span>`;
    $("chartSub").textContent =
      `ордер ${fmt(shot.suggest_distance || 0)}%` +
      (shot.fill_price ? ` @ ${fmtPx(shot.fill_price)}` : "") +
      ` · окно ${shot.window_ms || 0} мс` +
      (shot.lever ? ` · x${Math.round(shot.lever)}` : "") +
      (shot.pnl_pct ? ` · TP ${Number(shot.pnl_pct) >= 0 ? "+" : ""}${fmt(shot.pnl_pct, 2)}%` : "");
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
    $("chartHint").textContent =
      "Янтарная линия — дистанция виртуального ордера. Голубая — движение цены после входа. Наведите на свечу: объём и USDT.";

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
    const fillPx = Number(shot.fill_price) || orderPrice(shot);
    const exitPx = Number(shot.exit_price) || Number(shot.last_price) || 0;
    const highs = candles.map((c) => c.h);
    const lows = candles.map((c) => c.l);
    if (startPx) { highs.push(startPx); lows.push(startPx); }
    if (extPx) { highs.push(extPx); lows.push(extPx); }
    if (fillPx) { highs.push(fillPx); lows.push(fillPx); }
    if (exitPx) { highs.push(exitPx); lows.push(exitPx); }
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

    drawOrderOverlay(ctx, {
      candles, shot, fillPx, exitPx, startPx,
      padL, padT, plotW, plotH, slot, bodyW,
      xAt, yAt, xAtTs: (ts) => xAtTs(candles, ts, padL, slot),
    });

    ctx.textAlign = "left";
    ctx.font = "11px Segoe UI, sans-serif";
    for (let pct = startTick; pct <= pctMax + 1e-9; pct += step) {
      const price = startPx * (1 + pct / 100);
      const y = yAt(price);
      if (y < padT || y > padT + plotH) continue;
      const label = (pct >= 0 ? "+" : "") + fmt(pct, step < 0.5 ? 2 : 1) + "%";
      const nearShot = extPx && Math.abs(pct - pctOf(extPx)) < step * 0.35;
      const orderPct = fillPx && startPx ? (fillPx - startPx) / startPx * 100 : null;
      const nearOrder = orderPct != null && Math.abs(pct - orderPct) < step * 0.35;
      ctx.fillStyle = nearOrder ? AMBER : (nearShot ? shotColor : MUTED);
      ctx.fillText(label, padL + plotW + 8, y + 4);
      ctx.strokeStyle = nearOrder ? AMBER : (nearShot ? shotColor : GRID);
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
