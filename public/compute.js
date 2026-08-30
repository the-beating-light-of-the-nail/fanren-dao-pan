/* 凡人道盘 · 浏览器端K线计算（静态部署模式用）
   —— 逐行移植 server.py /api/kline 的分桶逻辑，时区钉死 Asia/Shanghai（B站时区），
   与服务器版口径完全一致：同日自采优先、周桶=周一、carry-open、MA5/MA10。 */
window.FanrenCompute = (function () {
  const METRIC_LABELS = { views: "播放量", danmaku: "弹幕", likes: "点赞", coin: "投币",
                          favorite: "收藏", share: "分享", reply: "评论" };
  // ep-N.json 行格式：[ts, views, danmaku, reply, coin, likes, favorite, share, src]
  const IDX = { views: 1, danmaku: 2, reply: 3, coin: 4, likes: 5, favorite: 6, share: 7 };
  const TZ_SHIFT = 8 * 3600; // lightweight-charts 按 UTC 显示，平移到北京时间

  function shDate(ts) {
    return new Date(ts * 1000).toLocaleDateString("en-CA", { timeZone: "Asia/Shanghai" });
  }
  function shHour(ts) {
    return +new Date(ts * 1000).toLocaleString("en-GB", { timeZone: "Asia/Shanghai", hour: "2-digit", hour12: false });
  }
  function mondayOf(dstr) { // 自然周桶 key = 周一日期
    const [y, m, d] = dstr.split("-").map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d));
    return new Date(dt.getTime() - dt.getUTCDay() * 86400000).toISOString().slice(0, 10);
  }
  function ma(points, win) {
    const out = [];
    for (let i = win - 1; i < points.length; i++) {
      let s = 0;
      for (let j = i - win + 1; j <= i; j++) s += points[j][1];
      out.push({ time: points[i][0], value: s / win });
    }
    return out;
  }
  const sum = a => a.reduce((s, x) => s + x, 0);

  function kline(epFile, opts) {
    const { metric, mode, freq } = opts;
    const days = Math.max(1, Math.min(opts.days ?? 90, 400));
    const ci = IDX[metric], vi = metric === "views" ? 2 : 1; // 价量分离
    const label = METRIC_LABELS[metric] || metric;

    const since = Math.floor(Date.now() / 1000) - days * 86400;
    let rows = epFile.rows.filter(r => r[0] >= since && r[ci] != null)
                          .map(r => [r[0], r[ci], r[vi], r[8]]);
    // 同一天里自采（real，小时级）与回填（import，日更）并存时只取自采，避免假跳变
    const realDays = new Set(rows.filter(r => r[3] === 1).map(r => shDate(r[0])));
    rows = rows.filter(r => r[3] !== 0 || !realDays.has(shDate(r[0])));

    const base = { ep: epFile.ep, title: epFile.title, mode, freq, days,
                   metric, metric_label: label, candles: [], bars: [], volume: [],
                   ma5: [], ma10: [], intraday: [], meta: { points: rows.length } };
    if (!rows.length) return base;
    const last = rows[rows.length - 1];

    if (mode === "intraday") {
      const dayGroups = new Map();
      for (const [ts, v] of rows) {
        const k = shDate(ts);
        (dayGroups.get(k) || dayGroups.set(k, []).get(k)).push([ts, v]);
      }
      let picked = null; // 回退到最近一个样本充足的日子，分时才有形状
      for (const [k, samples] of dayGroups) if (samples.length >= 3) picked = [k, samples];
      if (!picked) picked = [...dayGroups.entries()].pop();
      base.intraday = picked[1].map(([ts, v]) => ({ time: ts + TZ_SHIFT, value: v }));
      Object.assign(base.meta, { date: picked[0], latest_view: last[1],
                                 latest_ts: last[0], first_ts: rows[0][0] });
      return base;
    }

    const byDay = new Map();
    let pv = null, pvol = null;
    for (const [ts, v, vol] of rows) {
      const d = freq === "week" ? mondayOf(shDate(ts)) : shDate(ts);
      let b = byDay.get(d);
      if (!b) byDay.set(d, b = { view0: v, vlast: v, vmax: v, vmin: v, incs: [], dincs: [] });
      b.vlast = v;
      b.vmax = Math.max(b.vmax, v); b.vmin = Math.min(b.vmin, v);
      if (pv !== null) { b.incs.push(v - pv); b.dincs.push(vol - pvol); } // 保留负值：真·阴线
      pv = v; pvol = vol;
    }

    const candles = [], volume = [], closes = [], bars = [];
    let prevClose = null;
    for (const [d, b] of byDay) {
      if (!b.incs.length) continue; // 无上一样本基线的第一天，算不出增量
      bars.push({ time: d, value: sum(b.incs) });  // 桶总增量（日增柱状图的柱高）
      let o, c, h, l, vol;
      if (mode === "total") {
        c = b.vlast;
        // 日更粒度没有日内采样，开盘沿用上一周期收盘（carry-open）
        o = (b.incs.length <= 1 && prevClose !== null) ? prevClose : b.view0;
        h = Math.max(o, c); l = Math.min(o, c);
        vol = sum(b.incs);
      } else {
        const incs = b.incs.length ? b.incs : [0];
        if (incs.length >= 2) {
          o = incs[0]; c = incs[incs.length - 1]; h = Math.max(...incs); l = Math.min(...incs);
        } else {
          c = incs[incs.length - 1];
          o = prevClose !== null ? prevClose : c;
          h = Math.max(o, c); l = Math.min(o, c);
        }
        vol = sum(b.dincs);
      }
      prevClose = c;
      candles.push({ time: d, open: o, high: h, low: l, close: c });
      volume.push({ time: d, value: vol });
      closes.push([d, c]);
    }

    const dayIncs = [...byDay.values()].map(b => sum(b.incs));
    base.candles = candles; base.bars = bars; base.volume = volume;
    // MA 跟随主图形态：inc=日增柱的均增量，total=累计曲线的均累计
    const maSrc = mode === "total" ? closes : bars.map(x => [x.time, x.value]);
    base.ma5 = ma(maSrc, 5); base.ma10 = ma(maSrc, 10);
    Object.assign(base.meta, {
      latest_view: last[1], latest_ts: last[0], first_ts: rows[0][0],
      today_inc: dayIncs.length ? dayIncs[dayIncs.length - 1] : 0,
      prev_inc: dayIncs.length > 1 ? dayIncs[dayIncs.length - 2] : 0,
      today_vs_prev_pct: dayIncs.length > 1 && dayIncs[dayIncs.length - 2]
        ? (dayIncs[dayIncs.length - 1] - dayIncs[dayIncs.length - 2]) / dayIncs[dayIncs.length - 2] * 100
        : null,
    });
    return base;
  }

  /* 开播日对齐（D+N）序列 —— 逐行移植 server._compute_dn 的口径（时区同样钉死
     Asia/Shanghai）：同日自采优先、每日取最接近日中的一快照（最近一天取最新）、
     相邻日跨度 0.85~1.15 天才算一日增量。供老集（无开播行情、只有长尾段）
     懒加载时在浏览器端现算，与 dn.json 里服务端算好的 28 集完全同口径。 */
  function dn(epFile, maxDay = 1400) {
    const pub = epFile.pub || 0;
    const rows = epFile.rows || [];
    const out = { pub, title: epFile.title || "", series: [] };
    if (!pub || !rows.length) return out;
    const realDays = new Set(rows.filter(r => r[8] === 1).map(r => shDate(r[0])));
    const items = rows.filter(r => r[8] !== 0 || !realDays.has(shDate(r[0])))
                      .map(r => [r[0], r[1], r[8]]);
    const byDay = new Map();
    for (const it of items) {
      const k = shDate(it[0]);
      (byDay.get(k) || byDay.set(k, []).get(k)).push(it);
    }
    const days = [...byDay.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1));
    const daily = days.map(([d, lst], i) =>
      i === days.length - 1
        ? lst.reduce((a, b) => (b[0] > a[0] ? b : a))
        : lst.reduce((a, b) => Math.abs(shHour(b[0]) - 12) < Math.abs(shHour(a[0]) - 12) ? b : a));
    let prev = null;
    for (const [t, v, s] of daily) {
      const n = Math.floor((t - pub) / 86400);
      if (n > maxDay) break;
      let gain = null;
      if (prev && (t - prev[0]) / 86400 > 0.85 && (t - prev[0]) / 86400 < 1.15) {
        gain = Math.round((v - prev[1]) / 1e3) / 10;  // 万，1 位小数
      }
      out.series.push([n, gain, Math.round(v / 1e3) / 10, s === 1 ? 0 : 1]);  // 与服务端同语义：1=回填
      prev = [t, v];
    }
    return out;
  }

  return { kline, dn, METRIC_LABELS };
})();
