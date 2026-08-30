# -*- coding: utf-8 -*-
"""凡人修仙传播放 K 线 —— FastAPI 后端（全真实数据版，无任何伪造历史）

数据流：
1. POST /api/collect：拉取 B 站真实累计快照入库（每集间隔 1.2s，手动采集冷却 60s）
2. 可选自动采集：FANREN_AUTOCOLLECT=1 时启动后台线程，默认每 3600s 全量一轮
3. GET /api/kline：真实快照序列 → OHLC K 线 JSON

合规设计（改动前请三思，这是本项目的存续前提）：
- 只调用无鉴权的公开网页只读接口；不登录、不采集个人信息、不存储视频内容
- 请求保持低频；K 线从首次采集起积累（平台无历史 API，不伪造历史数据）
- 页面标注非官方、数据可能有误差；收到权利方通知立即停止

K 线口径：
- mode=inc       蜡烛 = 当天各时段「播放增量」的开高低收（红涨绿跌），成交量 = 弹幕日增量
- mode=total     蜡烛 = 当天「累计播放」的开高低收，成交量 = 播放日增量
- mode=intraday  最近一个样本充足日子的累计播放分时曲线
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import bilibili

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "fanren.db"
EP_CACHE_FILE = DATA_DIR / "episodes.json"

# 凡人修仙传：https://www.bilibili.com/bangumi/play/ss28747 （md28223043）
SEASON_ID = int(os.environ.get("FANREN_SEASON_ID", "28747"))

# —— 合规相关参数：调整前请评估请求频率 ——
EP_INTERVAL_SEC = 1.2          # 采集两集之间的间隔，严禁调小
MANUAL_COOLDOWN_SEC = 60       # 手动采集冷却，防止连续点击
# 自动采集默认开启（每小时全量一轮）；FANREN_AUTOCOLLECT=0 可关闭
AUTOCOLLECT = os.environ.get("FANREN_AUTOCOLLECT", "1") == "1"
AUTOCOLLECT_INTERVAL_SEC = int(os.environ.get("FANREN_AUTOCOLLECT_INTERVAL_SEC", "3600"))

TZ_SHIFT = -time.timezone  # lightweight-charts 的 unix time 按 UTC 显示，平移到本地时区

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_collect_lock = threading.Lock()
_last_collect_ts = 0.0


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ep_index INTEGER NOT NULL,
                aid INTEGER,
                ts INTEGER NOT NULL,
                views INTEGER,
                danmaku INTEGER,
                reply INTEGER,
                coin INTEGER,
                likes INTEGER,
                favorite INTEGER,
                share INTEGER,
                source TEXT DEFAULT 'real'
            )
            """
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_ep_ts ON snapshots(ep_index, ts)")
        try:  # 老库迁移：补 share 列
            _conn.execute("ALTER TABLE snapshots ADD COLUMN share INTEGER")
        except sqlite3.OperationalError:
            pass
        _conn.commit()
    return _conn


_ep_lock = threading.Lock()
_episodes_cache: Optional[dict] = None


def _stamp_pubdate(eps: list[dict]) -> None:
    """给剧集表盖上上市日 pub（unix 秒）：season 接口的 pub_time，与 view 接口
    的 pubdate 同源同值（抽样差 0~10 秒）。meta 缓存键可能是 int（实时）或
    str（仓库缓存水合）；meta 拿不到时保留磁盘旧值，避免新集上市瞬间丢字段。"""
    pubs: dict = {}
    try:
        pubs = _season_meta()[0] or {}
    except Exception:  # noqa: BLE001  上市日补不齐不影响剧集表本身
        pass
    prev: dict = {}
    try:
        if EP_CACHE_FILE.exists():
            old = json.loads(EP_CACHE_FILE.read_text(encoding="utf-8"))
            prev = {e.get("aid"): e.get("pub") for e in old.get("episodes") or [] if e.get("pub")}
    except Exception:  # noqa: BLE001
        pass
    for e in eps:
        aid = e.get("aid")
        m = pubs.get(aid) or pubs.get(str(aid)) or {}
        e["pub"] = int(m.get("pub") or prev.get(aid) or 0)


def load_episodes(force: bool = False) -> dict:
    """剧集表：优先 B 站实时 → 磁盘缓存 → 离线兜底（换番剧删 data/episodes.json 即可）。"""
    global _episodes_cache
    with _ep_lock:
        if _episodes_cache and not force:
            return _episodes_cache
        err = ""
        try:
            info, eps = bilibili.fetch_season(SEASON_ID)
            _stamp_pubdate(eps)
            data = {"source": "bilibili", "title": info.get("title") or "凡人修仙传",
                    "season_id": SEASON_ID, "episodes": eps}
            EP_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            _episodes_cache = data
            return data
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        if EP_CACHE_FILE.exists():
            data = json.loads(EP_CACHE_FILE.read_text(encoding="utf-8"))
            data["source"] = "cache"
            data["error"] = err
            _episodes_cache = data
            return data
        eps = [{"ep_index": i, "aid": None, "bvid": "", "title": f"第{i}话", "badge": "",
                "pub": 0} for i in range(1, 190)]
        _episodes_cache = {"source": "fallback", "title": "凡人修仙传（离线兜底）",
                           "season_id": SEASON_ID, "episodes": eps, "error": err}
        return _episodes_cache


app = FastAPI(title="凡人修仙传播放K线")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/episodes")
def api_episodes(force: bool = False):
    data = load_episodes(force=force)
    eps = data["episodes"]
    with _lock:
        have = {r[0] for r in db().execute("SELECT DISTINCT ep_index FROM snapshots")}
    default_ep = next((e["ep_index"] for e in eps if e["ep_index"] in have), 1)
    return {
        "source": data["source"],
        "title": data["title"],
        "season_id": data.get("season_id"),
        "count": len(eps),
        "default_ep": default_ep,  # 默认选中已有数据的集
        "episodes": eps,
        "error": data.get("error", ""),
    }


def _ma(points: list[tuple], window: int) -> list[dict]:
    out = []
    for i in range(len(points)):
        if i + 1 < window:
            continue
        chunk = points[i + 1 - window: i + 1]
        out.append({"time": points[i][0], "value": sum(v for _, v in chunk) / window})
    return out


METRIC_COLS = {  # K 线可选指标：白名单列名 + 中文标签（防注入，只允许这些）
    "views": ("views", "播放量"), "danmaku": ("danmaku", "弹幕"), "likes": ("likes", "点赞"),
    "coin": ("coin", "投币"), "favorite": ("favorite", "收藏"),
    "share": ("share", "分享"), "reply": ("reply", "评论"),
}


@app.get("/api/kline")
def api_kline(ep: Optional[int] = None, days: int = 90, mode: str = "inc",
              freq: str = "day", metric: str = "views"):
    if mode not in ("inc", "total", "intraday"):
        raise HTTPException(400, "mode 必须是 inc / total / intraday")
    if freq not in ("day", "week"):
        raise HTTPException(400, "freq 必须是 day / week")
    if metric not in METRIC_COLS:
        raise HTTPException(400, f"metric 必须是 {'/'.join(METRIC_COLS)}")
    col, metric_label = METRIC_COLS[metric]
    # 成交量始终用另一个维度：播放K线的量=弹幕增量，其余指标的量=播放增量（价量分离）
    vol_col = "danmaku" if metric == "views" else "views"
    days = max(1, min(days, 400))

    eps = load_episodes()["episodes"]
    if ep is None:
        with _lock:
            have = {r[0] for r in db().execute("SELECT DISTINCT ep_index FROM snapshots")}
        ep = next((e["ep_index"] for e in eps if e["ep_index"] in have), 1)
    if not 1 <= ep <= len(eps):
        raise HTTPException(404, f"ep={ep} 超出范围 1..{len(eps)}")
    title = eps[ep - 1]["title"]

    since = int(time.time()) - days * 86400
    with _lock:
        rows = db().execute(
            f"SELECT ts, {col}, {vol_col}, source FROM snapshots "
            f"WHERE ep_index=? AND ts>=? AND {col} IS NOT NULL ORDER BY ts",
            (ep, since),
        ).fetchall()
    # 同一天里自采（real，小时级）与回填（import，日更）并存时只取自采：两套采集口径的
    # 绝对值存在小偏差，混在同一天会算出假跳变（例如假阴线）
    def _day_of(ts: int) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    real_days = {_day_of(r[0]) for r in rows if r[3] == "real"}
    rows = [r for r in rows if r[3] != "import" or _day_of(r[0]) not in real_days]

    base = {"ep": ep, "title": title, "mode": mode, "freq": freq, "days": days,
            "metric": metric, "metric_label": metric_label,
            "candles": [], "volume": [], "ma5": [], "ma10": [], "intraday": [],
            "meta": {"points": len(rows)}}
    if not rows:
        return base

    if mode == "intraday":
        day_groups: "OrderedDict[str, list]" = OrderedDict()
        for ts, v, _dan, _src in rows:
            day_groups.setdefault(datetime.fromtimestamp(ts).strftime("%Y-%m-%d"), []).append((ts, v))
        # 当天刚开始采样时样本太少，回退到最近一个样本充足的日子，分时才有形状
        picked = None
        for dkey, samples in day_groups.items():
            if len(samples) >= 3:
                picked = (dkey, samples)
        if picked is None:
            picked = list(day_groups.items())[-1]
        last_day, samples = picked
        base["intraday"] = [{"time": ts + TZ_SHIFT, "value": v} for ts, v in samples]
        base["meta"].update({
            "date": last_day,
            "latest_view": rows[-1][1], "latest_ts": rows[-1][0],
            "first_ts": rows[0][0],
        })
        return base

    # 按日（或自然周，key=周一日期）分桶，桶内保存样本 + 与上一样本的差分（增量）
    def _bucket_key(ts: int) -> str:
        dt_ = datetime.fromtimestamp(ts)
        if freq == "week":
            monday = dt_ - timedelta(days=dt_.weekday())
            return monday.strftime("%Y-%m-%d")
        return dt_.strftime("%Y-%m-%d")

    by_day: "OrderedDict[str, dict]" = OrderedDict()
    prev_view = prev_dan = None
    for ts, view, dan, _src in rows:
        d = _bucket_key(ts)
        b = by_day.setdefault(d, {"view0": view, "vlast": view, "vmax": view, "vmin": view,
                                  "tslast": ts, "incs": [], "dincs": []})
        b["vlast"], b["tslast"] = view, ts
        b["vmax"], b["vmin"] = max(b["vmax"], view), min(b["vmin"], view)
        if prev_view is not None:
            b["incs"].append(view - prev_view)   # 保留负值：平台回滚会体现为真·阴线
            b["dincs"].append(dan - prev_dan)
        prev_view, prev_dan = view, dan

    candles, volume, closes = [], [], []
    prev_close = None
    for d, b in by_day.items():
        if not b["incs"]:
            continue  # 没有基线的第一天（无上一样本），算不出增量，跳过
        if mode == "total":
            c = b["vlast"]
            # 日更粒度（source=import 的历史快照）没有日内采样，开盘沿用上一周期收盘
            o = prev_close if (len(b["incs"]) <= 1 and prev_close is not None) else b["view0"]
            h, l = max(o, c), min(o, c)
            vol = sum(b["incs"])  # 成交量 = 播放日增量
        else:  # inc
            incs = b["incs"] or [0]
            if len(incs) >= 2:
                o, c = incs[0], incs[-1]
                h, l = max(incs), min(incs)
            else:
                c = incs[-1]
                o = prev_close if prev_close is not None else c
                h, l = max(o, c), min(o, c)
            vol = sum(b["dincs"])  # 成交量 = 弹幕日增量
        prev_close = c
        candles.append({"time": d, "open": o, "high": h, "low": l, "close": c})
        volume.append({"time": d, "value": vol})
        closes.append((d, c))

    day_incs = [sum(b["incs"]) for b in by_day.values()]
    today_inc = day_incs[-1] if day_incs else 0
    prev_inc = day_incs[-2] if len(day_incs) > 1 else 0
    base["candles"], base["volume"] = candles, volume
    base["ma5"], base["ma10"] = _ma(closes, 5), _ma(closes, 10)
    base["meta"].update({
        "latest_view": rows[-1][1], "latest_ts": rows[-1][0],
        "first_ts": rows[0][0],
        "today_inc": today_inc, "prev_inc": prev_inc,
        "today_vs_prev_pct": ((today_inc - prev_inc) / prev_inc * 100) if prev_inc else None,
    })
    return base


@app.get("/api/latest")
def api_latest():
    """每集最近一条真实快照（页面上方的实时数据条）。"""
    with _lock:
        rows = db().execute(
            """
            SELECT s.ep_index, s.ts, s.views, s.danmaku
            FROM snapshots s
            WHERE s.ts = (SELECT MAX(x.ts) FROM snapshots x WHERE x.ep_index = s.ep_index)
            ORDER BY s.ep_index
            """
        ).fetchall()
    eps = {e["ep_index"]: e for e in load_episodes()["episodes"]}
    return {"items": [
        {"ep": r[0], "title": eps.get(r[0], {}).get("title", ""), "ts": r[1],
         "view": r[2], "danmaku": r[3]}
        for r in rows
    ]}


def collect_range(start: int, limit: int, state: dict | None = None) -> dict:
    """按区间采集真实快照（共用的采集实现；调用方负责锁与冷却）。"""
    ep_data = load_episodes()
    eps = ep_data["episodes"]
    if not eps[0].get("aid"):
        raise HTTPException(503, "剧集表是离线兜底数据（B 站接口不可达），无法采集")
    limit = max(1, min(limit, len(eps)))
    picked = [e for e in eps if start <= e["ep_index"] < start + limit]
    if state is not None:
        state["total"] = len(picked)
    items = []
    for e in picked:
        try:
            st = bilibili.fetch_stat(e["aid"])
            ts = int(time.time())
            with _lock:
                conn = db()
                conn.execute(
                    "INSERT INTO snapshots(ep_index, aid, ts, views, danmaku, reply, coin, likes, favorite, share, source)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,'real')",
                    (e["ep_index"], e["aid"], ts, st["view"], st["danmaku"],
                     st["reply"], st["coin"], st["likes"], st["favorite"], st.get("share")),
                )
                conn.commit()
            items.append({"ep": e["ep_index"], "title": e["title"], "ok": True, "ts": ts, **st})
        except Exception as exc:  # noqa: BLE001
            items.append({"ep": e["ep_index"], "title": e["title"], "ok": False, "error": str(exc)})
        if state is not None:
            state["done"] += 1
            state["ok_count"] += 1 if items[-1].get("ok") else 0
        time.sleep(EP_INTERVAL_SEC)  # 礼貌间隔，严禁调小
    return {"title": ep_data["title"], "fetched": sum(1 for i in items if i.get("ok")),
            "total": len(items), "items": items}


_bg_state: dict = {"running": False, "kind": "", "total": 0, "done": 0, "ok_count": 0,
                   "started_ts": None, "finished_ts": None, "result": None}


def _maybe_backup() -> None:
    """每日首轮采集后导出当日全量状态 JSON（容灾：sqlite 损坏时可从备份重建）。"""
    try:
        day = datetime.now().strftime("%Y-%m-%d")
        path = DATA_DIR / "backup" / f"state_{day}.json"
        if path.exists():
            return
        path.parent.mkdir(exist_ok=True)
        with _lock:
            rows = db().execute(
                """
                SELECT ep_index, aid, views, danmaku, reply, coin, likes, favorite, share FROM snapshots s
                WHERE ts = (SELECT MAX(x.ts) FROM snapshots x WHERE x.ep_index = s.ep_index)
                ORDER BY ep_index
                """
            ).fetchall()
        keys = ("ep_index", "aid", "views", "danmaku", "reply", "coin", "likes", "favorite", "share")
        state = {"date": day, "generated_ts": int(time.time()),
                 "episodes": [dict(zip(keys, r)) for r in rows]}
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        print(f"[backup] 已导出每日状态 {path.name}（{len(rows)} 集）")
    except Exception as exc:  # noqa: BLE001
        print(f"[backup] 备份失败：{exc}")


def _run_collection(start: int, limit: int, kind: str = "manual") -> None:
    """执行一轮采集并维护进度状态（调用方必须已持有 _collect_lock）。"""
    _bg_state.update(running=True, kind=kind, total=0, done=0, ok_count=0,
                     started_ts=time.time(), finished_ts=None, result=None)
    try:
        _bg_state["result"] = collect_range(start, limit, state=_bg_state)
    except Exception as exc:  # noqa: BLE001
        _bg_state["result"] = {"error": str(exc)}
    finally:
        _bg_state["running"] = False
        _bg_state["finished_ts"] = time.time()
        _maybe_backup()


def _bg_collect_locked(start: int, limit: int) -> None:
    """后台采集线程：启动时已持有 _collect_lock，结束时释放。"""
    try:
        _run_collection(start, limit, kind="manual")
    finally:
        _collect_lock.release()


@app.post("/api/collect")
def api_collect(request: Request, limit: int = 189, start: int = 1, background: bool = True):
    """管理用手动补采（默认全量、后台执行、60s 冷却）。

    公开页面上的「刷新数据」按钮不会调用本接口——面向访客的采集一律由
    定时任务驱动，避免多人点击打爆对 B 站的请求预算。
    隧道公开访问时，仅允许本机触发采集。
    """
    global _last_collect_ts
    if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "公开访问下不允许触发采集；数据由服务端定时任务维护")
    if not _collect_lock.acquire(blocking=False):
        raise HTTPException(429, "已有一轮采集在进行（手动或自动），可通过 /api/collect/status 查看进度")
    try:
        if time.time() - _last_collect_ts < MANUAL_COOLDOWN_SEC:
            left = int(MANUAL_COOLDOWN_SEC - (time.time() - _last_collect_ts))
            raise HTTPException(429, f"采集太频繁，请 {left} 秒后再试（低频采集是本项目的前提）")
        _last_collect_ts = time.time()
    except Exception:
        _collect_lock.release()
        raise
    if background:
        threading.Thread(target=_bg_collect_locked, args=(start, limit), daemon=True).start()
        return {"started": True, "status_url": "/api/collect/status"}
    try:
        return collect_range(start, limit)
    finally:
        _collect_lock.release()


@app.get("/api/collect/status")
def api_collect_status():
    state = dict(_bg_state)
    state["interval"] = AUTOCOLLECT_INTERVAL_SEC if AUTOCOLLECT else None
    return state


def _autocollector_loop(interval: int) -> None:
    while True:
        try:
            ep_data = load_episodes()
            n = len(ep_data["episodes"])
            with _collect_lock:
                _run_collection(1, n, kind="auto")
            res = _bg_state.get("result") or {}
            print(f"[auto] 全量采集完成：{res.get('fetched')}/{res.get('total')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[auto] 采集失败：{exc}")
        time.sleep(interval)


_season_meta_cache: dict = {"ts": 0.0, "meta": {}, "sections": []}


def _season_meta() -> tuple[dict, list]:
    """aid→(pub_time, long_title) 映射 + 额外分区（2020原版/虚天战纪，接口自带播放数），1 小时缓存。"""
    if _season_meta_cache["meta"] and time.time() - _season_meta_cache["ts"] < 3600:
        return _season_meta_cache["meta"], _season_meta_cache["sections"]
    res = bilibili._get_json(bilibili.SEASON_URL, {"season_id": SEASON_ID})
    meta, sections = {}, []
    for e in res.get("episodes") or []:
        if (e.get("badge") or "") == "预告" or not e.get("aid"):
            continue
        meta[e["aid"]] = {"pub": e.get("pub_time") or 0,
                          "long": (e.get("long_title") or "").strip()}
    for sec in res.get("section") or []:
        if (sec.get("title") or "") not in {"2020版", "虚天战纪"}:
            continue
        for e in sec.get("episodes") or []:
            st = e.get("stat") or {}
            if not e.get("aid") or not st.get("play"):
                continue
            sections.append({"arc": f"[{sec['title']}]", "pub": e.get("pub_time") or 0,
                             "v": st.get("play") or 0, "dm": st.get("danmakus") or 0,
                             "coin": st.get("coin") or 0})
    _season_meta_cache.update(ts=time.time(), meta=meta, sections=sections)
    return meta, sections


def _compute_dn(max_day: int = 180) -> dict:
    """上市日对齐（D+N）序列：每集日增/累计播放按「上市第 N 天」重排，供多集叠加对比。

    口径与 K 线一致：同日自采(real)优先、每日取最接近日中的一条快照；
    相邻日快照跨度 0.85~1.15 天才计为一日增量，否则该日增量置空（缺日不给猜）。
    只收首条快照距上市 ≤30 天的集：回填墙(2025-10-23)前开播的老集没有上市
    初期行情，不参与对比。series 行：[N天, 日增万|None, 累计万, 回填标记]。"""
    eps = {e["ep_index"]: e for e in load_episodes()["episodes"]}
    with _lock:
        rows = db().execute(
            "SELECT ep_index, ts, views, source FROM snapshots ORDER BY ep_index, ts"
        ).fetchall()

    def day_key(ts: int) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    by_ep: dict[int, list] = {}
    for ep, ts, v, src in rows:
        by_ep.setdefault(ep, []).append((ts, v, src))
    out: dict = {}
    for ep, items in by_ep.items():
        e = eps.get(ep) or {}
        pub = int(e.get("pub") or 0)
        if not pub or not items:
            continue
        real_days = {day_key(t) for t, _v, s in items if s == "real"}
        items = [(t, v, s) for t, v, s in items
                 if s != "import" or day_key(t) not in real_days]
        if not items or (items[0][0] - pub) / 86400 > 30:  # 首见即上市满月后：纯长尾段
            continue
        by_day: dict[str, list] = {}
        for t, v, s in items:
            by_day.setdefault(day_key(t), []).append((t, v, s))
        days = sorted(by_day.items())
        daily = []
        for i, (_d, lst) in enumerate(days):
            if i == len(days) - 1:  # 最近一天取最新快照：新上市集的「今日」还在生长
                daily.append(max(lst, key=lambda x: x[0]))
            else:  # 历史日取最接近日中的一快照，保证日增是完整 24h 口径
                daily.append(min(lst, key=lambda x: abs(datetime.fromtimestamp(x[0]).hour - 12)))
        series, prev = [], None
        for t, v, s in daily:
            n = int((t - pub) // 86400)
            if n > max_day:
                break
            gain = None
            if prev and 0.85 < (t - prev[0]) / 86400 < 1.15:
                gain = round((v - prev[1]) / 1e4, 1)
            series.append([n, gain, round(v / 1e4, 1), 1 if s == "import" else 0])
            prev = (t, v)
        if series:
            out[str(ep)] = {"pub": pub, "title": (e.get("title") or "").strip(),
                            "series": series}
    return {"updated_ts": int(time.time()), "eps": out}


@app.get("/api/dn")
def api_dn():
    """上市日对齐叠加序列（D+N）：多集「同日起跑」对比的数据源。"""
    return _compute_dn()


@app.get("/api/overview")
def api_overview():
    """数据总览：全剧总量、半年度走势（折算满龄）、篇章互动率、留存漏斗。全部来自真实快照。"""
    import re as _re
    import statistics as _stats
    from collections import OrderedDict as _OD

    def adj(v, age, tau=60.0):
        if not age or age < 45:
            return None
        return v * (age + tau) / age

    now = time.time()
    meta, extra = _season_meta()
    with _lock:
        rows = db().execute(
            """
            SELECT ep_index, aid, views, danmaku, reply, coin, likes, favorite, share, ts FROM snapshots s
            WHERE ts = (SELECT MAX(x.ts) FROM snapshots x WHERE x.ep_index = s.ep_index)
            ORDER BY ep_index
            """
        ).fetchall()
    if not rows:
        return {"ep_count": 0}

    eps = []
    snap_ts = 0
    for idx, aid, v, dm, reply, coin, likes, favorite, share, ts in rows:
        m = meta.get(aid) or {}
        arc = _re.sub(r"\d+", "", m.get("long", "")) or "未知"
        pub = m.get("pub", 0)
        age = max((now - pub) / 86400.0, 0.5) if pub else None
        eps.append({"idx": idx, "arc": arc, "pub": pub, "age": age,
                    "v": v or 0, "dm": dm or 0, "coin": coin or 0,
                    "views": v or 0, "danmaku": dm or 0, "likes": likes or 0,
                    "favorite": favorite or 0, "share": share or 0, "reply": reply or 0})
        snap_ts = max(snap_ts, ts)
    items = eps + [{"idx": None, "pub": x["pub"],
                    "age": max((now - x["pub"]) / 86400.0, 0.5) if x["pub"] else None, **x}
                   for x in extra]

    # 分篇章（按首集上线时间排序）
    arcs: "_OD[str, list]" = _OD()
    for e in sorted(items, key=lambda x: x["pub"] or 0):
        arcs.setdefault(e["arc"], []).append(e)
    arc_rows = []
    for name, lst in arcs.items():
        pubs = [e["pub"] for e in lst if e["pub"]]
        adjs = [a for a in (adj(e["v"], e["age"]) for e in lst) if a]
        arc_rows.append({
            "name": name, "n": len(lst),
            "span": ([datetime.fromtimestamp(min(pubs)).strftime("%y-%m-%d") + "~" +
                      datetime.fromtimestamp(max(pubs)).strftime("%y-%m-%d")] if pubs else "--"),
            "median_views": _stats.median(e["v"] for e in lst),
            "median_adj": _stats.median(adjs) if adjs else None,
            "dm_ratio": sum(e["dm"] for e in lst) / max(sum(e["v"] for e in lst), 1),
            "coin_ratio": sum(e["coin"] for e in lst) / max(sum(e["v"] for e in lst), 1),
            "young_n": sum(1 for e in lst if e["age"] and e["age"] < 45),
        })

    # 半年度走势
    buckets: "_OD[str, list]" = _OD()
    for e in items:
        if not e["pub"]:
            continue
        d = datetime.fromtimestamp(e["pub"])
        buckets.setdefault(f"{d.year}H{1 if d.month <= 6 else 2}", []).append(e)
    trend = []
    for k in sorted(buckets.keys()):
        lst = buckets[k]
        adjs = [a for a in (adj(e["v"], e["age"]) for e in lst) if a]
        trend.append({"bucket": k, "n": len(lst),
                      "median_adj": _stats.median(adjs) if adjs else None,
                      "median_raw": _stats.median(e["v"] for e in lst),
                      "young_n": sum(1 for e in lst if e["age"] and e["age"] < 45)})

    # 留存漏斗（主线各篇首集 / 第1集）
    main_arcs: "_OD[str, list]" = _OD()
    for e in eps:
        main_arcs.setdefault(e["arc"], []).append(e)
    first_v = eps[0]["v"]
    retention = [{"arc": name, "ep": lst[0]["idx"], "views": lst[0]["v"],
                  "pct": lst[0]["v"] / max(first_v, 1) * 100}
                 for name, lst in main_arcs.items()]

    latest = max(eps, key=lambda e: e["pub"] or 0)
    pubs = sorted(e["pub"] for e in eps if e["pub"])
    big = [a for a in arc_rows if a["n"] >= 4]

    # TOP10 榜单（7 个指标，取每集最近一条快照）
    metric_keys = [("views", "播放量"), ("danmaku", "弹幕"), ("likes", "点赞"), ("coin", "投币"),
                   ("favorite", "收藏"), ("share", "分享"), ("reply", "评论")]
    top = {}
    for key, label in metric_keys:
        ranked = sorted(eps, key=lambda e: -(e.get(key) or 0))[:10]
        top[key] = {"label": label, "items": [
            {"ep": e["idx"], "arc": e["arc"], "value": e.get(key) or 0} for e in ranked]}

    return {
        "snapshot_ts": snap_ts,
        "ep_count": len(eps),
        "totals": {"views": sum(e["v"] for e in eps),
                   "danmaku": sum(e["dm"] for e in eps),
                   "coin": sum(e["coin"] for e in eps)},
        "latest_ep": {"ep": latest["idx"], "pub_ts": latest["pub"],
                      "age_hours": (now - latest["pub"]) / 3600 if latest["pub"] else None,
                      "views": latest["v"]},
        "span_days": (pubs[-1] - pubs[0]) / 86400 if len(pubs) > 1 else 0,
        "arcs": arc_rows,
        "trend": trend,
        "retention": retention,
        "top": top,
        "record": {"dm_ratio_arc": max(big, key=lambda a: a["dm_ratio"])["name"],
                   "dm_ratio": max(a["dm_ratio"] for a in big),
                   "coin_ratio_arc": max(big, key=lambda a: a["coin_ratio"])["name"],
                   "coin_ratio": max(a["coin_ratio"] for a in big)},
    }


_review_cache: dict = {"ts": 0.0, "data": None}


def _compute_review() -> dict:
    """生成「今日收评」与「历史涨停板」：股评语气是玩梗，所有数字都来自真实快照。"""
    now = time.time()
    if _review_cache["data"] and now - _review_cache["ts"] < 600:
        return _review_cache["data"]

    with _lock:
        all_rows = db().execute(
            "SELECT ep_index, ts, views, source FROM snapshots WHERE views IS NOT NULL"
            " ORDER BY ep_index, ts").fetchall()
    eps_list = load_episodes()["episodes"]

    # 按集分组；同一天自采与回填并存时只认自采（与 K 线同口径）
    per_ep: "OrderedDict[int, list]" = OrderedDict()
    for ep, ts, v, src in all_rows:
        per_ep.setdefault(ep, []).append((ts, v, src))

    def _daily_incs(rows):
        real_days = {datetime.fromtimestamp(t).strftime("%Y-%m-%d") for t, _v, s in rows if s == "real"}
        rows = [r for r in rows if r[2] != "import"
                or datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d") not in real_days]
        days: "OrderedDict[str, int]" = OrderedDict()
        prev = None
        for t, v, _s in rows:
            d = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
            if prev is not None:
                days[d] = days.get(d, 0) + max(v - prev, 0)
            prev = v
        return days

    # 历史涨停板：每集历史上单日最大增量
    best = {}
    for ep, rows in per_ep.items():
        for d, inc in _daily_incs(rows).items():
            if ep not in best or inc > best[ep][1]:
                best[ep] = (d, inc)
    titles = {e["ep_index"]: e["title"] for e in eps_list}
    limitup = sorted(best.items(), key=lambda kv: -kv[1][1])[:10]
    limitup = [{"ep": ep, "title": titles.get(ep, ""), "date": d, "inc": inc}
               for ep, (d, inc) in limitup]

    # 最新一集的今日盘面
    def _fmt(n):
        return f"{n/1e8:.2f}亿" if n >= 1e8 else (f"{n/1e4:.1f}万" if n >= 1e4 else f"{n:.0f}")

    review = {"limitup": limitup, "review": None}
    if per_ep:
        ep = max(per_ep.keys())
        rows = per_ep[ep]
        days = _daily_incs(rows)
        keys = list(days.keys())
        today = keys[-1] if keys else ""
        today_inc = days.get(today, 0)
        y_inc = days.get(keys[-2], 0) if len(keys) > 1 else 0
        hist = [days[k] for k in keys[-6:-1]]
        ma5 = sum(hist) / len(hist) if hist else None
        # 日内形状：今天各小时段增量
        today_rows = [r for r in rows
                      if datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d") == today and r[2] == "real"] \
            or [r for r in rows if datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d") == today]
        incs, prev = [], None
        for t, v, _s in today_rows:
            if prev is not None:
                incs.append((datetime.fromtimestamp(t).hour, v - prev))
            prev = v
        peak_hour = max(incs, key=lambda x: x[1])[0] if incs else None
        early = len(incs) < 4

        parts = []
        shape = ("早盘快速冲高" if peak_hour is not None and peak_hour < 12 else
                 "午后持续走强" if peak_hour is not None and peak_hour >= 14 else "盘中稳步上行")
        parts.append(f"第{ep}集《{titles.get(ep, '')}》今日{shape}"
                     + (f"，日内高点出现在 {peak_hour} 点前后" if peak_hour is not None else ""))
        # 新集上市天数（上市首周是爆发期）
        aid = next((e.get("aid") for e in eps_list if e["ep_index"] == ep), None)
        pub = (meta := _season_meta()[0].get(aid, {})).get("pub")
        if pub:
            age_days = (now - pub) / 86400.0
            if age_days <= 7:
                parts.append(f"新集上市第{int(age_days) + 1}天，处于首周爆发期")
        if y_inc:
            pctv = (today_inc - y_inc) / y_inc * 100
            parts.append(f"量能较昨日{'放大' if pctv >= 0 else '萎缩'} {abs(pctv):.0f}%"
                         f"（{_fmt(today_inc)} vs {_fmt(y_inc)}）")
        else:
            parts.append("上市初期，量能对比待积累")
        if ma5:
            up = today_inc >= ma5
            parts.append(f"{'站上' if up else '跌破'}5日均线（MA5 {_fmt(ma5)}）")
            parts.append(f"短期趋势{'向上，建议继续持有' if up else '承压，道友可逢低加仓摊薄成本'}")
        else:
            parts.append("5日均线待样本积累")
        if early:
            parts.append("（早盘数据，盘中持续更新）")
        text = "；".join(parts) + " ——以上均为玩梗，数字真实，不构成任何真实投资建议"
        review["review"] = {"ep": ep, "title": titles.get(ep, ""), "text": text}
    _review_cache.update(ts=now, data=review)
    return review


@app.get("/api/review")
def api_review():
    """今日收评 + 历史涨停板（自动生成的玩梗文案，数据全部真实）。"""
    return _compute_review()


# 静态前端挂在根路径（注册在 API 路由之后，/api/* 优先匹配）。
# 页面内资源用相对路径引用，本地 FastAPI 与静态部署（dist/）同一套代码。
app.mount("/", StaticFiles(directory=str(ROOT / "public"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    if AUTOCOLLECT:
        threading.Thread(target=_autocollector_loop,
                         args=(AUTOCOLLECT_INTERVAL_SEC,), daemon=True).start()
        print(f"[auto] 自动采集已开启：每 {AUTOCOLLECT_INTERVAL_SEC}s 全量一轮（{EP_INTERVAL_SEC}s/集）")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
