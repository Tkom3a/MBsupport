(function () {
  const DOWN = "#e15b64";
  const UP = "#4fba7a";
  const GRID = "#243044";
  const MUTED = "#8b95a8";
  const TEXT = "#d7deea";
  const BLUE = "#5aa8ff";
  const PURPLE = "rgba(128, 90, 196, 0.18)";
  const PURPLE_LINE = "rgba(190, 160, 230, 0.85)";

  function $(id) { return document.getElementById(id); }
  function fmt(n, d) { return Number(n).toFixed(d == null ? 2 : d); }

  function fmtPx(n) {
    const v = Number(n);
    if (!v) return "—";
    if (v >= 100) return fmt(v, 2);
    if (v >= 1) return fmt(v, 4);
    return fmt(v, 6);
  }

  function fmtVol(n) {
    const v = Number(n) || 0;
    if (v >= 1e6) return fmt(v / 1e6, 2) + "M";
    if (v >= 1e3) return fmt(v / 1e3, 1) + "k";
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
      fake.push({ ts: c.ts + 200, price: c.h, side: 1, qty: 0 });
      fake.push({ ts: c.ts + 400, price: c.l, side: -1, qty: 0 });
      fake.push({ ts: c.ts + 700, price: c.c, side: c.c >= c.o ? 1 : -1, qty: c.vol || 0 });
    });
    return fake;
  }

  function orderPrice(shot) {
    const start = Number(shot.start_price) || 0;
    const dist = Number(shot.suggest_distance) || 0;
    if (Number(shot.fill_price) > 0) return Number(shot.fill_price);
    if (!start || !dist) return 0;
    return shot.direction === "UP" ? start * (1 + dist / 100) : start * (1 - dist / 100);
  }

  function fillTsOf(shot, ticks, fillPx) {
    if (Number(shot.fill_ts) > 0) return Number(shot.fill_ts);
    const startTs = Number(shot.start_ts) || 0;
    for (let i = 0; i < ticks.length; i++) {
      const t = ticks[i];
      if (startTs && t.ts < startTs) continue;
      if (shot.direction === "UP" && t.price >= fillPx) return t.ts;
      if (shot.direction !== "UP" && t.price <= fillPx) return t.ts;
    }
    return Number(shot.peak_ts) || (ticks[0] && ticks[0].ts) || 0;
  }

  function priceAt(ticks, ts, fallback) {
    let chosen = fallback;
    for (let i = 0; i < ticks.length; i++) {
      if (ticks[i].ts <= ts) chosen = ticks[i].price;
      else return chosen;
    }
    return chosen;
  }

  function triangle(ctx, x, y, up, color) {
    ctx.beginPath();
    ctx.fillStyle = color;
    if (up) {
      ctx.moveTo(x, y - 9);
      ctx.lineTo(x - 6, y + 5);
      ctx.lineTo(x + 6, y + 5);
    } else {
      ctx.moveTo(x, y + 9);
      ctx.lineTo(x - 6, y - 5);
      ctx.lineTo(x + 6, y - 5);
    }
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = "#0b0d12";
    ctx.lineWidth = 0.8;
    ctx.stroke();
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
    const padL = 78, padR = 72, padT = 16, padB = 24;
    const plotW = cssW - padL - padR;
    const plotH = cssH - padT - padB;

    const startPx = Number(shot.start_price) || ticks[0].price;
    const extPx = Number(shot.extreme_price) || ticks[ticks.length - 1].price;
    const fillPx = orderPrice(shot);
    const holdMs = Math.max(50, Number(shot.hold_ms) || 300);
    const startTs = Number(shot.start_ts) || ticks[0].ts;
    const peakTs = Number(shot.peak_ts) || startTs;
    const fillTs = fillTsOf(shot, ticks, fillPx);
    const closeTs = fillTs + holdMs;
    const closePx = Number(shot.exit_price) || priceAt(ticks, closeTs, fillPx);

    const sel0 = startTs;
    const sel1 = startTs + 1000;
    const view0 = sel0 - 700;
    const view1 = Math.max(sel1, closeTs) + 900;
    const t0 = view0;
    const t1 = view1;

    const visible = ticks.filter((t) => t.ts >= view0 - 50 && t.ts <= view1 + 50);
    const use = visible.length ? visible : ticks;
    const prices = use.map((t) => t.price);
    if (startPx) prices.push(startPx);
    if (extPx) prices.push(extPx);
    if (fillPx) prices.push(fillPx);
    if (closePx) prices.push(closePx);
    let pMax = Math.max.apply(null, prices);
    let pMin = Math.min.apply(null, prices);
    const pad = (pMax - pMin) * 0.16 || pMax * 0.002;
    pMax += pad;
    pMin -= pad;

    function xAt(ts) {
      if (t1 <= t0) return padL + plotW / 2;
      return padL + (ts - t0) / (t1 - t0) * plotW;
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
      const nearFill = fillPx && Math.abs(pct - pctOf(fillPx)) < step * 0.35;
      ctx.fillStyle = nearFill ? UP : MUTED;
      ctx.fillText((pct >= 0 ? "+" : "") + fmt(pct, step < 0.5 ? 2 : 1) + "%", padL + plotW + 8, y + 4);
    }

    const xSel0 = xAt(sel0);
    const xSel1 = xAt(sel1);
    const yTop = yAt(startPx);
    const yBot = yAt(extPx);
    ctx.fillStyle = PURPLE;
    ctx.fillRect(xSel0, padT, Math.max(8, xSel1 - xSel0), plotH);
    ctx.strokeStyle = PURPLE_LINE;
    ctx.lineWidth = 1.4;
    ctx.strokeRect(xSel0, padT, Math.max(8, xSel1 - xSel0), plotH);

    ctx.beginPath();
    ctx.strokeStyle = "#efe8ff";
    ctx.lineWidth = 1.2;
    ctx.setLineDash([4, 3]);
    ctx.moveTo(xSel0, yTop);
    ctx.lineTo(xSel1, yBot);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#efe8ff";
    ctx.beginPath(); ctx.arc(xSel0, yTop, 4, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(xSel1, yBot, 4, 0, Math.PI * 2); ctx.fill();

    use.forEach((t) => {
      ctx.beginPath();
      ctx.fillStyle = t.side > 0 ? UP : DOWN;
      ctx.arc(xAt(t.ts), yAt(t.price), 2.2, 0, Math.PI * 2);
      ctx.fill();
    });

    if (fillPx) {
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = UP;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.moveTo(padL, yAt(fillPx));
      ctx.lineTo(padL + plotW, yAt(fillPx));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = UP;
      ctx.font = "bold 11px Segoe UI, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText("ордер " + fmt(shot.suggest_distance || 0, 2) + "%", padL + 8, yAt(fillPx) - 8);
    }

    if (fillPx && fillTs) {
      const xOpen = xAt(fillTs);
      const xClose = xAt(closeTs);
      const yOpen = yAt(fillPx);
      const yClose = yAt(closePx);
      ctx.strokeStyle = "rgba(90, 168, 255, 0.85)";
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.moveTo(xOpen, yOpen);
      ctx.lineTo(xClose, yClose);
      ctx.stroke();
      const openUp = shot.direction !== "UP";
      triangle(ctx, xOpen, yOpen, openUp, UP);
      triangle(ctx, xClose, yClose, !openUp, BLUE);
    }

    const inside = ticks.filter((t) => t.ts >= sel0 && t.ts <= sel1);
    let buyQty = 0, sellQty = 0;
    inside.forEach((t) => {
      const q = t.qty || 0;
      if (t.side > 0) buyQty += q;
      else sellQty += q;
    });
    const firstIn = inside[0] ? inside[0].price : startPx;
    const lastIn = inside.length ? inside[inside.length - 1].price : extPx;
    const extremeIn = shot.direction === "UP"
      ? Math.max.apply(null, inside.map((t) => t.price).concat([firstIn]))
      : Math.min.apply(null, inside.map((t) => t.price).concat([firstIn]));
    const dAbs = extremeIn - firstIn;
    const dPct = firstIn ? dAbs / firstIn * 100 : 0;
    const boxW = 228;
    const boxH = 118;
    let boxX = xSel0 + 10;
    if (boxX + boxW > padL + plotW - 8) boxX = padL + plotW - boxW - 8;
    const boxY = padT + plotH - boxH - 8;
    ctx.fillStyle = "rgba(62, 42, 110, 0.92)";
    ctx.strokeStyle = PURPLE_LINE;
    ctx.lineWidth = 1;
    ctx.fillRect(boxX, boxY, boxW, boxH);
    ctx.strokeRect(boxX, boxY, boxW, boxH);
    ctx.font = "12px Segoe UI, sans-serif";
    ctx.textAlign = "left";
    const lines = [
      ["Время", "1 сек  ·  " + inside.length + " тик."],
      ["Исходная цена", fmtPx(firstIn)],
      ["Изменение цены", (dPct >= 0 ? "+" : "") + fmt(dPct, 2) + "%"],
      ["Объём покупок", fmtVol(buyQty) || "—"],
      ["Объём продаж", fmtVol(sellQty) || "—"],
      ["Общий объём", fmtVol(buyQty + sellQty) || "—"],
    ];
    lines.forEach((row, i) => {
      ctx.fillStyle = MUTED;
      ctx.fillText(row[0], boxX + 10, boxY + 18 + i * 16);
      ctx.fillStyle = i === 2 ? (dPct >= 0 ? UP : DOWN) : TEXT;
      ctx.fillText(row[1], boxX + 118, boxY + 18 + i * 16);
    });

    ctx.fillStyle = MUTED;
    ctx.font = "10px Segoe UI, sans-serif";
    ctx.textAlign = "center";
    const marks = [t0, sel0, sel1, t1];
    marks.forEach((ts) => ctx.fillText(fmtTime(ts), xAt(ts), cssH - 6));

    canvas.onmousemove = function (ev) {
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      let best = use[0];
      let bestD = 1e9;
      use.forEach((t) => {
        const d = Math.abs(xAt(t.ts) - x);
        if (d < bestD) { bestD = d; best = t; }
      });
      if (!best) return;
      $("chartHint").textContent =
        `${fmtTime(best.ts)}  ${fmtPx(best.price)}  ${best.side > 0 ? "buy" : "sell"}` +
        (best.qty ? `  qty ${fmtVol(best.qty)}` : "") +
        `  ·  в рамке 1с: ${inside.length} тик.`;
    };
  }

  function setTitle(shot) {
    const dir = shot.direction || "";
    const cls = dir === "UP" ? "up" : "down";
    $("chartTitle").innerHTML =
      `${shot.symbol || ""} <span class="${cls}">${dir} ${fmt(shot.percent || 0)}%</span>` +
      ` <span class="meta">${shot.time || ""} · тики</span>`;
    $("chartSub").textContent =
      `рамка 1 с  ·  ордер ${fmt(shot.suggest_distance || 0)}%` +
      (shot.fill_price ? ` @ ${fmtPx(shot.fill_price)}` : "") +
      `  ·  выход ${shot.hold_ms || 300} мс` +
      (shot.lever ? `  ·  x${Math.round(shot.lever)}` : "");
  }

  function drawChart(payload) {
    const canvas = $("chartCanvas");
    const shot = payload.shot || {};
    const ticks = ticksFromShot(shot, payload.candles || []);
    const wrap = canvas.parentElement;
    const cssW = Math.max(640, wrap.clientWidth);
    const cssH = 520;
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
      "Фиолетовая рамка — ровно 1 секунда тиков. Зелёная стрелка — открытие ордера, синяя — закрытие через 0.3 с.";
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
