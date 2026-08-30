# -*- coding: utf-8 -*-
"""凡人修仙传播放数据 · 全历史截面分析（2020 开播 → 现在，全部真实数据）

原理：B 站不提供历史日播放，无法回溯每天的数据；但「每集上线时间 + 该集当前累计播放」
就是全部历史的真实足迹。按上线时间把 6 年的所有集排成时间轴：
- 同一集的累计播放只会涨不会跌，越老的集"吸收"的尾巴越长；
- 用饱和模型 V(t)=V∞·t/(t+τ)（τ=60 天）把不同年龄的集折算到"满年龄"口径，
  即可跨年代公平比较每集的最终热度量级（对 τ 做 30/90 敏感性检验）。

数据源：
- data/fanren.db：每集最近一条真实快照（先跑全量 /api/collect）
- season 接口：每集 pub_time；2020原版/虚天战纪分区的播放数（接口自带，无需额外请求）
"""
from __future__ import annotations

import re
import sqlite3
import statistics
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import bilibili

ROOT = Path(__file__).resolve().parent
SEASON_ID = 28747
TAU = 60.0  # 天；热度饱和时间常数
EXTRA_SECTIONS = {"2020版", "虚天战纪"}  # 额外纳入的分区（正片之外）


def fmt(n):
    if n is None:
        return "--"
    n = float(n)
    if n >= 1e8:
        return f"{n/1e8:.2f}亿"
    if n >= 1e4:
        return f"{n/1e4:.1f}万"
    return f"{n:.0f}"


def adjusted(v, age_d, tau=TAU):
    """把 age_d 天的累计播放折算到满年龄口径（越年轻修正越大）。"""
    if not age_d or age_d < 45:
        return None  # 热映中的集不做折算，避免过度修正
    return v * (age_d + tau) / age_d


def main():
    now = time.time()

    # ── 1) season 元信息：pub_time + 额外分区（自带播放数） ──
    res = bilibili._get_json(bilibili.SEASON_URL, {"season_id": SEASON_ID})
    meta = {}
    extra = []
    for e in res.get("episodes") or []:
        if (e.get("badge") or "") == "预告":
            continue
        if e.get("aid"):
            meta[e["aid"]] = {"pub": e.get("pub_time") or 0,
                              "long": (e.get("long_title") or "").strip()}
    for sec in res.get("section") or []:
        if (sec.get("title") or "") not in EXTRA_SECTIONS:
            continue
        for e in sec.get("episodes") or []:
            st = e.get("stat") or {}
            if not e.get("aid") or not st.get("play"):
                continue
            pub = e.get("pub_time") or 0
            extra.append({
                "idx": None, "arc": f"[{sec['title']}]", "long": (e.get("long_title") or e.get("title") or ""),
                "pub": pub, "age": max((now - pub) / 86400.0, 0.5) if pub else None,
                "v": st.get("play") or 0, "dm": st.get("danmakus") or 0,
                "coin": st.get("coin") or 0, "likes": st.get("likes") or 0,
                "daily": (st.get("play") or 0) / max((now - pub) / 86400.0, 0.5) if pub else None,
            })

    # ── 2) 主线 189 集：库里最近一条真实快照 ──
    conn = sqlite3.connect(ROOT / "data" / "fanren.db")
    rows = conn.execute(
        """
        SELECT ep_index, aid, views, danmaku, coin, likes, ts FROM snapshots s
        WHERE ts = (SELECT MAX(x.ts) FROM snapshots x WHERE x.ep_index = s.ep_index)
        ORDER BY ep_index
        """
    ).fetchall()
    eps = []
    for idx, aid, v, dm, coin, likes, ts in rows:
        m = meta.get(aid) or {}
        long_title = m.get("long", "")
        arc = re.sub(r"\d+", "", long_title) or "未知"
        pub = m.get("pub", 0)
        age = max((now - pub) / 86400.0, 0.5) if pub else None
        eps.append({
            "idx": idx, "arc": arc, "long": long_title, "pub": pub, "age": age,
            "v": v or 0, "dm": dm or 0, "coin": coin or 0, "likes": likes or 0,
            "daily": (v or 0) / age if age else None,
        })

    if not eps:
        print("库里还没有快照：先 POST /api/collect?start=1&limit=189 做一次全量采集")
        return

    all_items = eps + extra
    total_v = sum(e["v"] for e in eps)
    print(f"快照时间 {datetime.now():%Y-%m-%d %H:%M}｜主线 {len(eps)} 集 + 额外分区 {len(extra)} 条")
    print(f"主线累计：播放 {fmt(total_v)} · 弹幕 {fmt(sum(e['dm'] for e in eps))} · 投币 {fmt(sum(e['coin'] for e in eps))}")
    print()

    # ── 3) 分篇章汇总（上线顺序） ──
    arcs: "OrderedDict[str, list]" = OrderedDict()
    for e in sorted(all_items, key=lambda x: (x["pub"] or 0)):
        arcs.setdefault(e["arc"], []).append(e)
    print("=" * 108)
    print(f"{'篇章':<16}{'集数':>4}{'上线跨度':>24}{'中位累计':>10}{'中位年龄':>8}{'折算满龄中位':>10}{'弹幕/播放':>9}{'投币/播放':>9}")
    print("-" * 108)
    arc_adj = {}
    for name, lst in arcs.items():
        pubs = [e["pub"] for e in lst if e["pub"]]
        span = (f"{datetime.fromtimestamp(min(pubs)):%y-%m-%d}~{datetime.fromtimestamp(max(pubs)):%y-%m-%d}"
                if pubs else "--")
        mv = statistics.median(e["v"] for e in lst)
        mage = statistics.median(e["age"] for e in lst if e["age"])
        adjs = [adjusted(e["v"], e["age"]) for e in lst]
        adjs = [a for a in adjs if a]
        madj = statistics.median(adjs) if adjs else None
        arc_adj[name] = madj
        dm_r = sum(e["dm"] for e in lst) / max(sum(e["v"] for e in lst), 1)
        coin_r = sum(e["coin"] for e in lst) / max(sum(e["v"] for e in lst), 1)
        print(f"{name:<16}{len(lst):>4}{span:>24}{fmt(mv):>10}{mage:>7.0f}天{fmt(madj) if madj else '(热映中)':>10}"
              f"{dm_r*100:>8.2f}%{coin_r*100:>8.2f}%")
    print("=" * 108)
    print()

    # ── 4) 半年度时间轴（核心：从开始到现在的走势） ──
    buckets: "OrderedDict[str, list]" = OrderedDict()
    for e in all_items:
        if not e["pub"]:
            continue
        d = datetime.fromtimestamp(e["pub"])
        key = f"{d.year}H{1 if d.month <= 6 else 2}"
        buckets.setdefault(key, []).append(e)
    keys = sorted(buckets.keys())
    adj_med = {}
    for k in keys:
        adjs = [adjusted(e["v"], e["age"]) for e in buckets[k]]
        adj_med[k] = statistics.median([a for a in adjs if a]) or 0
    peak = max(adj_med.values()) or 1

    print("半年度走势（每集热度折算到满年龄口径，█=折算中位数）：")
    for k in keys:
        lst = buckets[k]
        n = len(lst)
        raw = statistics.median(e["v"] for e in lst)
        young = sum(1 for e in lst if e["age"] and e["age"] < 45)
        # 敏感性：τ=30 / 90
        sens = []
        for tau in (30.0, 90.0):
            s = [adjusted(e["v"], e["age"], tau) for e in lst]
            s = [a for a in s if a]
            sens.append(statistics.median(s) if s else None)
        bar = "█" * max(int(adj_med[k] / peak * 34), 1) if adj_med[k] else "·"
        tag = f"（含{young}集热映中未折算）" if young else ""
        print(f"  {k}  {bar:<36} {fmt(adj_med[k]) if adj_med[k] else '--':>9}  原始中位{fmt(raw):>9}  n={n:<3}{tag}")
    print(f"  敏感性检验（最新段落 τ=30/60/90 折算）：", " / ".join(fmt(s) for s in sens) if sens else "--")
    print()

    # ── 5) 留存漏斗 ──
    main_arcs = OrderedDict()
    for e in eps:
        main_arcs.setdefault(e["arc"], []).append(e)
    first = eps[0]
    print("留存漏斗（各篇首集播放 / 第1集播放）：")
    for name, lst in main_arcs.items():
        head = lst[0]
        print(f"  {name:<14} 第{head['idx']:>3}集 {fmt(head['v']):>9} = {head['v']/max(first['v'],1)*100:>5.1f}%")
    print()

    # ── 6) 更新节奏 ──
    pubs = sorted(e["pub"] for e in eps if e["pub"])
    if len(pubs) > 1:
        years = (pubs[-1] - pubs[0]) / 86400 / 365.25
        print(f"更新节奏：{datetime.fromtimestamp(pubs[0]):%Y-%m-%d} → {datetime.fromtimestamp(pubs[-1]):%Y-%m-%d}"
              f"（{years:.1f} 年 {len(pubs)} 集，平均 {years*365.25/max(len(pubs)-1,1):.1f} 天/集）")
    print()

    # ── 7) 最新篇章逐集 ──
    last_name = list(main_arcs.keys())[-1]
    print(f"最新篇章「{last_name}」逐集（尾部 12 集）：")
    for e in main_arcs[last_name][-12:]:
        d = datetime.fromtimestamp(e["pub"]).strftime("%m-%d") if e["pub"] else "--"
        print(f"  第{e['idx']:>3}集 {d} 播放 {fmt(e['v']):>9} 日均 {fmt(e['daily']):>9} 弹幕 {fmt(e['dm']):>7}")
    print()

    # ── 8) 已有真实时序 ──
    ts_rows = conn.execute("SELECT ts, views FROM snapshots WHERE ep_index=1 ORDER BY ts").fetchall()
    if len(ts_rows) > 1:
        print("真实时序增量（ep1，凌晨采样，仅作当前活跃度参考）：")
        for i in range(1, len(ts_rows)):
            dt = (ts_rows[i][0] - ts_rows[i-1][0]) / 3600
            dv = ts_rows[i][1] - ts_rows[i-1][1]
            print(f"  {datetime.fromtimestamp(ts_rows[i][0]):%H:%M} 累计 {ts_rows[i][1]:,}（{dt:.1f}h +{dv:,}）")


if __name__ == "__main__":
    main()
