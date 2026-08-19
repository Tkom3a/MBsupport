(function () {
  const DOWN = "#e15b64";
  const UP = "#4fba7a";
  const GRID = "#243044";
  const MUTED = "#8b95a8";
  const TEXT = "#d7deea";
  const BLUE = "#4aa3ff";
  const ORANGE = "#d7a13b";
  const PURPLE_FILL = "rgba(120, 80, 190, 0.14)";
  const PURPLE_LINE = "#c4a6f0";

  function $(id) { return document.getElementById(id); }
  function fmt(n, d) { return Number(n).toFixed(d == null ? 2 : d); }

  function fmtPx(n) {
    const v = Number(n);
    if (!v && v !== 0) return "—";
    if (v >= 100) return fmt(v, 2);
    if (v >= 1) return fmt(v, 4);
    if (v >= 0.01) return fmt(v, 5);
    return fmt(v, 6);
  }

  function fmtDeltaAbs(n) {
    const v = Math.abs(Number(n) || 0);
    if (v >= 1) return fmt(v, 4);
    if (v >= 0.01) return fmt(v, 5);
    return fmt(v, 6);
  }

  function fmtVol(n) {
    const v = Number(n) || 0;
    if (v <= 0) return "—";
    if (v >= 1e6) return fmt(v / 1e6, 2) + "M";
    if (v >= 1e3) return fmt(v / 1e3, 2) + "K";
    return fmt(v, v >= 10 ? 0 : 2);
  }

  function fmtTime(ts) {
    const d = new Date(ts);
    const p = (x) => String(x).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function ticksFromShot(shot, candles) {
    const raw = Array.isArray(shot.path) ? shot.path : [];
    const ticks = raw
      .filter((p) => p && p.length >= 2 && Number(p[0]) > 0 && Number(p[1]) > 0)
      .map((p) => ({
        ts: Number(p[0]),
        price: Number(p[1]),
        side: p.length > 2 ? Number(p[2]) : 0,
        qty: p.length > 3 ? Number(p[3]) : 0,
      }));
    if (ticks.length) return ticks;
    const fake = [];
    (candles || []).forEach((c) => {
      fake.push({ ts: c.ts, price: c.o, side: -1, qty: 0 });
      fake.push({ ts: c.ts + 150, price: c.h, side: 1, qty: 0 });
      fake.push({ ts: c.ts + 300, price: c.l, side: -1, qty: 0 });
      fake.push({ ts: c.ts + 500, price: c.c, side: c.c >= c.o ? 1 : -1, qty: c.vol || 0 });
    });
    return fake;
  }

  function fillsOf(shot, ticks) {
    const start = Number(shot.start_price) || 0;
    const holdMs = Math.max(50, Number(shot.hold_ms) || 300);
    const startTs = Number(shot.start_ts) || 0;
    const rows = Array.isArray(shot.distance_report) ? shot.distance_report.filter((r) => r && r.filled) : [];
    const out = [];
    const add = (row) => {
      const dist = Number(row.distance || 0);
      const fillPx = Number(row.fill_price) || (start
        ? (shot.direction === "UP" ? start * (1 + dist / 100) : start * (1 - dist / 100))
        : 0);
      if (!fillPx) return;
      let fillTs = Number(row.fill_ts) || 0;
      if (!fillTs) {
        for (let i = 0; i < ticks.length; i++) {
          if (startTs && ticks[i].ts < startTs) continue;
          if (shot.direction === "UP" && ticks[i].price >= fillPx) { fillTs = ticks[i].ts; break; }
          if (shot.direction !== "UP" && ticks[i].price <= fillPx) { fillTs = ticks[i].ts; break; }
        }
      }
      const closeTs = fillTs + holdMs;
      let closePx = Number(row.exit_price) || 0;
      if (!closePx) {
        for (let i = 0; i < ticks.length; i++) {
          if (ticks[i].ts <= closeTs) closePx = ticks[i].price;
        }
      }
      out.push({ dist, fillPx, fillTs, closeTs, closePx: closePx || fillPx });
    };
    rows.forEach(add);
    if (!out.length && (Number(shot.fill_price) || Number(shot.suggest_distance))) {
      add({
        distance: shot.suggest_distance,
        fill_price: shot.fill_price,
        fill_ts: shot.fill_ts,
        exit_price: shot.exit_price,
        filled: true,
      });
    }
    return out;
  }

  function triangle(ctx, x, y, up, color) {
    ctx.beginPath();
    ctx.fillStyle = color;
    if (up) {
      ctx.moveTo(x, y - 8);
      ctx.lineTo(x - 5.5, y + 5);
      ctx.lineTo(x + 5.5, y + 5);
    } else {
      ctx.moveTo(x, y + 8);
      ctx.lineTo(x - 5.5, y - 5);
      ctx.lineTo(x + 5.5, y - 5);
    }
    ctx.closePath();
    ctx.fill();
  }

  function niceStep(range) {
    if (range <= 0.4) return 0.1;
    if (range <= 1) return 0.25;
    if (range <= 2) return 0.5;
    if (range <= 4) return 1;
    return Math.ceil(range / 6);
  }

  function drawMtChart(canvas, shot, ticks, cssW, cssH, dpr) {
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const padL = 84, padR = 78, padT = 14, padB = 26;
    const plotW = cssW - padL - padR;
    const plotH = cssH - padT - padB;

    const startPx = Number(shot.start_price) || ticks[0].price;
    const extPx = Number(shot.extreme_price) || ticks[ticks.length - 1].price;
    const startTs = Number(shot.start_ts) || ticks[0].ts;
    const peakTs = Number(shot.peak_ts) || startTs;
    const fills = fillsOf(shot, ticks);

    const view0 = Math.min(startTs, peakTs) - 1500;
    const lastClose = fills.reduce((m, f) => Math.max(m, f.closeTs || 0), peakTs);
    const view1 = Math.max(peakTs, lastClose) + 1800;

    const use = ticks.filter((t) => t.ts >= view0 && t.ts <= view1);
    const draw = use.length ? use : ticks;
    const prices = draw.map((t) => t.price).concat([startPx, extPx]);
    fills.forEach((f) => { prices.push(f.fillPx); prices.push(f.closePx); });
    let pMax = Math.max.apply(null, prices);
    let pMin = Math.min.apply(null, prices);
    const pad = (pMax - pMin) * 0.14 || pMax * 0.002;
    pMax += pad;
    pMin -= pad;

    function xAt(ts) {
      if (view1 <= view0) return padL + plotW / 2;
      return padL + (ts - view0) / (view1 - view0) * plotW;
    }
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
      ctx.textAlign = "left";
      ctx.fillText((pct >= 0 ? "+" : "") + fmt(pct, step < 0.5 ? 2 : 1) + "%", padL + plotW + 8, y + 4);
    }

    const xShot = xAt(peakTs);
    const yStart = yAt(startPx);
    const yExt = yAt(extPx);
    ctx.fillStyle = PURPLE_FILL;
    ctx.fillRect(xAt(startTs) - 2, padT, Math.max(6, xShot - xAt(startTs) + 4), plotH);

    ctx.strokeStyle = PURPLE_LINE;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(xShot, yStart);
    ctx.lineTo(xShot, yExt);
    ctx.stroke();
    ctx.fillStyle = PURPLE_LINE;
    ctx.beginPath(); ctx.arc(xShot, yStart, 5, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(xShot, yExt, 5, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "#1a1228";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(xShot, yStart, 5, 0, Math.PI * 2); ctx.stroke();
    ctx.beginPath(); ctx.arc(xShot, yExt, 5, 0, Math.PI * 2); ctx.stroke();

    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = ORANGE;
    ctx.lineWidth = 1.3;
    ctx.beginPath();
    ctx.moveTo(padL, yStart);
    ctx.lineTo(padL + plotW, yStart);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.beginPath();
    ctx.strokeStyle = "#9aa3b5";
    ctx.lineWidth = 1;
    ctx.lineJoin = "round";
    draw.forEach((t, i) => {
      const x = xAt(t.ts);
      const y = yAt(t.price);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    draw.forEach((t) => {
      ctx.beginPath();
      ctx.fillStyle = t.side > 0 ? UP : DOWN;
      ctx.arc(xAt(t.ts), yAt(t.price), 2.1, 0, Math.PI * 2);
      ctx.fill();
    });

    const openUp = shot.direction !== "UP";
    fills.forEach((f) => {
      if (!f.fillPx || !f.fillTs) return;
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = UP;
      ctx.lineWidth = 1.3;
      ctx.beginPath();
      ctx.moveTo(padL, yAt(f.fillPx));
      ctx.lineTo(padL + plotW, yAt(f.fillPx));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = UP;
      ctx.font = "bold 11px Segoe UI, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText(fmt(f.dist, 2) + "%", padL + 8, yAt(f.fillPx) - 6);

      ctx.strokeStyle = "rgba(74,163,255,0.8)";
      ctx.lineWidth = 1.3;
      ctx.beginPath();
      ctx.moveTo(xAt(f.fillTs), yAt(f.fillPx));
      ctx.lineTo(xAt(f.closeTs), yAt(f.closePx));
      ctx.stroke();
      triangle(ctx, xAt(f.fillTs), yAt(f.fillPx), openUp, UP);
      triangle(ctx, xAt(f.closeTs), yAt(f.closePx), !openUp, BLUE);
    });

    const shotTicks = ticks.filter((t) => t.ts >= startTs && t.ts <= peakTs + 50);
    let buyQty = 0, sellQty = 0;
    shotTicks.forEach((t) => {
      if (t.side > 0) buyQty += t.qty || 0;
      else sellQty += t.qty || 0;
    });
    const dAbs = extPx - startPx;
    const dPct = startPx ? dAbs / startPx * 100 : 0;
    const durMs = Math.max(0, peakTs - startTs);
    const timeLabel = durMs < 400 ? "0 сек" : (durMs < 1000 ? durMs + " мс" : fmt(durMs / 1000, 1) + " сек");
    const boxW = 248;
    const boxH = 124;
    let boxX = xShot + 14;
    if (boxX + boxW > padL + plotW - 6) boxX = xShot - boxW - 14;
    if (boxX < padL + 6) boxX = padL + 6;
    const boxY = Math.min(Math.max(yExt, yStart) + 10, padT + plotH - boxH - 6);
    ctx.fillStyle = "rgba(58, 36, 102, 0.94)";
    ctx.strokeStyle = PURPLE_LINE;
    ctx.lineWidth = 1;
    ctx.fillRect(boxX, boxY, boxW, boxH);
    ctx.strokeRect(boxX, boxY, boxW, boxH);
    const rows = [
      ["Время", timeLabel],
      ["Исходная цена", fmtPx(startPx)],
      ["Изменение цены", (dAbs >= 0 ? "+" : "−") + fmtDeltaAbs(dAbs) + ", " + (dPct >= 0 ? "+" : "") + fmt(dPct, 2) + "%"],
      ["Объём покупок", fmtVol(buyQty)],
      ["Объём продаж", fmtVol(sellQty)],
      ["Общий объём", fmtVol(buyQty + sellQty)],
    ];
    ctx.font = "12px Segoe UI, sans-serif";
    ctx.textAlign = "left";
    rows.forEach((row, i) => {
      ctx.fillStyle = MUTED;
      ctx.fillText(row[0], boxX + 10, boxY + 18 + i * 17);
      ctx.fillStyle = i === 2 ? (dPct >= 0 ? UP : DOWN) : TEXT;
      ctx.fillText(row[1], boxX + 122, boxY + 18 + i * 17);
    });

    ctx.fillStyle = MUTED;
    ctx.font = "10px Segoe UI, sans-serif";
    ctx.textAlign = "center";
    const marks = [view0, startTs, peakTs, view1];
    const seen = {};
    marks.forEach((ts) => {
      const label = fmtTime(ts);
      if (seen[label]) return;
      seen[label] = 1;
      ctx.fillText(label, xAt(ts), cssH - 7);
    });

    canvas.onmousemove = function (ev) {
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      let best = draw[0];
      let bestD = 1e9;
      draw.forEach((t) => {
        const d = Math.abs(xAt(t.ts) - x);
        if (d < bestD) { bestD = d; best = t; }
      });
      if (!best) return;
      $("chartHint").textContent =
        `${fmtTime(best.ts)}  ${fmtPx(best.price)}  ${best.side > 0 ? "buy" : "sell"}` +
        (best.qty ? `  qty ${fmtVol(best.qty)}` : "") +
        `  ·  линейка ${fmt(shot.percent || 0, 2)}%  ·  тиков в простреле ${shotTicks.length}`;
    };
  }

  function setTitle(shot) {
    const dir = shot.direction || "";
    const cls = dir === "UP" ? "up" : "down";
    $("chartTitle").innerHTML =
      `${shot.symbol || ""} <span class="${cls}">${dir} ${fmt(shot.percent || 0)}%</span>` +
      ` <span class="meta">${shot.time || ""} · тики</span>`;
    $("chartSub").textContent =
      `линейка дистанции ${fmt(shot.percent || 0, 2)}%  ·  ордер ${fmt(shot.suggest_distance || 0)}%` +
      `  ·  выход ${shot.hold_ms || 300} мс` +
      (shot.lever ? `  ·  x${Math.round(shot.lever)}` : "");
  }

  function drawChart(payload) {
    const canvas = $("chartCanvas");
    const shot = payload.shot || {};
    const ticks = ticksFromShot(shot, payload.candles || []);
    const wrap = canvas.parentElement;
    const cssW = Math.max(720, wrap.clientWidth);
    const cssH = 540;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    canvas.style.width = cssW + "px";
    canvas.style.height = cssH + "px";
    if (!ticks.length) {
      $("chartHint").textContent = "Нет тиков вокруг этого прострела";
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    $("chartHint").textContent =
      "Фиолетовая линейка — дистанция прострела. Зелёная стрелка — открытие лимита, синяя — закрытие через 0.3 с.";
    drawMtChart(canvas, shot, ticks, cssW, cssH, dpr);
  }

  window.openShotChart = async function (api, symbol, ts) {
    $("chartOverlay").classList.add("open");
    $("chartTitle").textContent = symbol;
    $("chartSub").textContent = "загрузка тиков…";
    $("chartHint").textContent = "";
    try {
      const payload = await api(`/api/chart?symbol=${encodeURIComponent(symbol)}&ts=${ts}`);
      setTitle(payload.shot || { symbol: symbol });
      drawChart(payload);
    } catch (err) {
      $("chartHint").textContent = "Не удалось загрузить график: " + err.message;
    }
  };

  window.closeShotChart = function () {
    $("chartOverlay").classList.remove("open");
  };
})();
