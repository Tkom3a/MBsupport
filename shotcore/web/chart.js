(function () {
  const DOWN = "#e15b64";
  const UP = "#4fba7a";
  const GRID = "#243044";
  const MUTED = "#8b95a8";
  const TEXT = "#d7deea";
  const ORANGE = "#d7a13b";
  const VIEW_MS = 3000;
  const PAD_BEFORE = 200;
  const BUCKET_MS = 100;

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

  function fmtVol(n) {
    const v = Number(n) || 0;
    if (v <= 0) return "—";
    if (v >= 1e6) return fmt(v / 1e6, 2) + "M";
    if (v >= 1e3) return fmt(v / 1e3, 2) + "K";
    return fmt(v, v >= 10 ? 0 : 2);
  }

  function fmtUsdt(n) {
    const v = Number(n) || 0;
    if (v <= 0) return "—";
    if (v >= 1e6) return fmt(v / 1e6, 2) + "M USDT";
    if (v >= 1e3) return fmt(v / 1e3, 2) + "K USDT";
    return fmt(v, v >= 10 ? 0 : 2) + " USDT";
  }

  function fmtTime(ts) {
    const d = new Date(ts);
    const p = (x) => String(x).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function fmtTimeAxis(ts) {
    const d = new Date(ts);
    const p = (x) => String(x).padStart(2, "0");
    const tenth = Math.floor(d.getMilliseconds() / 100);
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()) + "." + tenth;
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
      fake.push({ ts: c.ts + 500, price: c.c, side: c.c >= c.o ? 1 : -1, qty: (c.vol || 0) / 4 });
    });
    return fake;
  }

  function niceStep(range) {
    if (range <= 0.4) return 0.1;
    if (range <= 1) return 0.25;
    if (range <= 2) return 0.5;
    if (range <= 4) return 1;
    return Math.ceil(range / 6);
  }

  function volumeBuckets(ticks, view0, view1) {
    const n = Math.max(1, Math.round((view1 - view0) / BUCKET_MS));
    const bars = [];
    for (let i = 0; i < n; i++) {
      bars.push({
        t0: view0 + i * BUCKET_MS,
        t1: view0 + (i + 1) * BUCKET_MS,
        qty: 0,
        usdt: 0,
        buyQty: 0,
        sellQty: 0,
      });
    }
    ticks.forEach((t) => {
      if (t.ts < view0 || t.ts > view1) return;
      let idx = Math.floor((t.ts - view0) / BUCKET_MS);
      if (idx < 0) idx = 0;
      if (idx >= bars.length) idx = bars.length - 1;
      const qty = Number(t.qty) || 0;
      const usdt = qty * (Number(t.price) || 0);
      bars[idx].qty += qty;
      bars[idx].usdt += usdt;
      if (t.side > 0) bars[idx].buyQty += qty;
      else bars[idx].sellQty += qty;
    });
    return bars;
  }

  function drawMtChart(canvas, shot, ticks, cssW, cssH, dpr) {
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const padL = 84, padR = 72, padT = 12, padB = 36;
    const gap = 10;
    const innerH = cssH - padT - padB;
    const volH = Math.floor(innerH * 0.28);
    const plotH = innerH - volH - gap;
    const plotW = cssW - padL - padR;
    const volTop = padT + plotH + gap;

    const startPx = Number(shot.start_price) || ticks[0].price;
    const extPx = Number(shot.extreme_price) || ticks[ticks.length - 1].price;
    const startTs = Number(shot.start_ts) || ticks[0].ts;
    const peakTs = Number(shot.peak_ts) || startTs;
    const view0 = startTs - PAD_BEFORE;
    const view1 = view0 + VIEW_MS;

    const draw = ticks.filter((t) => t.ts >= view0 && t.ts <= view1);
    const use = draw.length ? draw : ticks;
    const prices = use.map((t) => t.price).concat([startPx, extPx]);
    let pMax = Math.max.apply(null, prices);
    let pMin = Math.min.apply(null, prices);
    const pad = (pMax - pMin) * 0.12 || pMax * 0.002;
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

    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = ORANGE;
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(padL, yAt(startPx));
    ctx.lineTo(padL + plotW, yAt(startPx));
    ctx.stroke();
    ctx.setLineDash([]);

    if (peakTs >= view0 && peakTs <= view1) {
      ctx.strokeStyle = "rgba(215,161,59,0.35)";
      ctx.beginPath();
      ctx.moveTo(xAt(peakTs), padT);
      ctx.lineTo(xAt(peakTs), padT + plotH);
      ctx.stroke();
    }

    ctx.beginPath();
    ctx.strokeStyle = "#9aa3b5";
    ctx.lineWidth = 1;
    ctx.lineJoin = "round";
    use.forEach((t, i) => {
      const x = xAt(t.ts);
      const y = yAt(t.price);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    use.forEach((t) => {
      ctx.beginPath();
      ctx.fillStyle = t.side > 0 ? UP : DOWN;
      ctx.arc(xAt(t.ts), yAt(t.price), 2.1, 0, Math.PI * 2);
      ctx.fill();
    });

    const bars = volumeBuckets(use, view0, view1);
    const maxVol = Math.max.apply(null, bars.map((b) => b.qty)) || 1;
    const barW = Math.max(2, plotW / bars.length - 1);
    bars.forEach((b) => {
      if (b.qty <= 0) return;
      const h = (b.qty / maxVol) * (volH - 4);
      const x = xAt((b.t0 + b.t1) / 2) - barW / 2;
      const buyH = b.qty > 0 ? h * (b.buyQty / b.qty) : 0;
      ctx.fillStyle = DOWN;
      ctx.fillRect(x, volTop + (volH - h), barW, h);
      ctx.fillStyle = UP;
      ctx.fillRect(x, volTop + (volH - buyH), barW, buyH);
    });

    ctx.strokeStyle = GRID;
    ctx.beginPath();
    ctx.moveTo(padL, volTop);
    ctx.lineTo(padL + plotW, volTop);
    ctx.stroke();
    ctx.fillStyle = MUTED;
    ctx.font = "10px Segoe UI, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("объём: токены / USDT", padL + 4, volTop + 11);

    ctx.beginPath();
    ctx.strokeStyle = "#1c2430";
    ctx.moveTo(padL, cssH - padB + 4);
    ctx.lineTo(padL + plotW, cssH - padB + 4);
    ctx.stroke();
    ctx.fillStyle = MUTED;
    ctx.font = "10px Segoe UI, sans-serif";
    ctx.textAlign = "center";
    const axisStep = 500;
    const seen = {};
    for (let ts = Math.ceil(view0 / axisStep) * axisStep; ts <= view1 + 1; ts += axisStep) {
      const label = fmtTimeAxis(ts);
      if (seen[label]) continue;
      seen[label] = 1;
      const x = xAt(ts);
      ctx.strokeStyle = GRID;
      ctx.beginPath();
      ctx.moveTo(x, cssH - padB + 4);
      ctx.lineTo(x, cssH - padB + 10);
      ctx.stroke();
      ctx.fillStyle = MUTED;
      ctx.fillText(label, x, cssH - 8);
    }
    ctx.fillStyle = TEXT;
    ctx.font = "10px Segoe UI, sans-serif";
    ctx.textAlign = "right";
    ctx.fillText("3 сек", padL + plotW, cssH - 8);

    canvas.onmousemove = function (ev) {
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      if (y >= volTop) {
        let best = bars[0];
        let bestD = 1e9;
        bars.forEach((b) => {
          const d = Math.abs(xAt((b.t0 + b.t1) / 2) - x);
          if (d < bestD) { bestD = d; best = b; }
        });
        if (!best) return;
        $("chartHint").textContent =
          `${fmtTimeAxis(best.t0)}–${fmtTimeAxis(best.t1)}  ` +
          `токены ${fmtVol(best.qty)}  ·  ${fmtUsdt(best.usdt)}` +
          `  ·  buy ${fmtVol(best.buyQty)} / sell ${fmtVol(best.sellQty)}`;
        return;
      }
      let best = use[0];
      let bestD = 1e9;
      use.forEach((t) => {
        const d = Math.abs(xAt(t.ts) - x);
        if (d < bestD) { bestD = d; best = t; }
      });
      if (!best) return;
      const usdt = (best.qty || 0) * (best.price || 0);
      $("chartHint").textContent =
        `${fmtTimeAxis(best.ts)}  ${fmtPx(best.price)}  ${best.side > 0 ? "buy" : "sell"}` +
        `  ·  ${fmtVol(best.qty)} ток.  ·  ${fmtUsdt(usdt)}`;
    };
  }

  function setTitle(shot) {
    const dir = shot.direction || "";
    const cls = dir === "UP" ? "up" : "down";
    $("chartTitle").innerHTML =
      `${shot.symbol || ""} <span class="${cls}">${dir} ${fmt(shot.percent || 0)}%</span>` +
      ` <span class="meta">${shot.time || ""} · 3 сек</span>`;
    $("chartSub").textContent =
      `тики цены + объём под свечой (токены и USDT)  ·  окно ${VIEW_MS / 1000} с` +
      (shot.lever ? `  ·  x${Math.round(shot.lever)}` : "");
  }

  function drawChart(payload) {
    const canvas = $("chartCanvas");
    const shot = payload.shot || {};
    const ticks = ticksFromShot(shot, payload.candles || []);
    const wrap = canvas.parentElement;
    const cssW = Math.max(720, wrap.clientWidth);
    const cssH = 560;
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
      "Наведи на тик или столбик объёма: токены и эквивалент в USDT. Снизу — шкала 3 секунд.";
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
