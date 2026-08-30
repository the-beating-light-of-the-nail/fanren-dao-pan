# -*- coding: utf-8 -*-
"""无头采集 + 静态导出（GitHub Actions 数据管线，也可本地运行同步数据）。

数据流（仓库内 JSON 是部署侧事实源，SQLite 只是工作副本）：
  data/site/snapshots.json   全量快照事实源（供审计与灾备重建）
  data/site/eps/N.json       每集原始样本序列（浏览器端算K线用，懒加载）
  data/site/derived.json     剧集表/总览/收评/榜单（预计算，首屏一次拉取）
  data/site/season_meta.json 篇章元信息缓存（B站接口失败时的兜底）

流程：水合（JSON→SQLite，仅当库为空）→ 刷新剧集表 → 采集一轮（server.py 原逻辑，
1.2s/集 礼貌间隔）→ 导出上述文件。采集频率由 Actions 排期控制，严禁调密。

用法：
  python collect.py                # 采集 + 导出
  python collect.py --export-only  # 只导出（不请求 B 站统计接口）
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("FANREN_AUTOCOLLECT", "0")  # 本脚本自持流程，不需要自动采集线程

import server  # noqa: E402  (复用采集/总览/收评逻辑，保证与本地版口径一致)

ROOT = Path(__file__).resolve().parent
SITE_DIR = ROOT / "data" / "site"
EPS_DIR = SITE_DIR / "eps"
SNAP_FILE = SITE_DIR / "snapshots.json"
DERIVED_FILE = SITE_DIR / "derived.json"
DN_FILE = SITE_DIR / "dn.json"
META_FILE = SITE_DIR / "season_meta.json"
SITEMAP_FILE = SITE_DIR / "sitemap.xml"

BASE_URL = "https://fanren.cdqyfdbymn.me"
# IndexNow 提交密钥：与 public/<key>.txt 保持一致（key 文件随壳部署）
INDEXNOW_KEY = os.environ.get("FANREN_INDEXNOW_KEY", "")

# 导出行格式：[ts, views, danmaku, reply, coin, likes, favorite, share, src]
# src: 1=自采(real，小时级) / 0=开源日更回填(import)
_COLS = ("views", "danmaku", "reply", "coin", "likes", "favorite", "share")


def _src_int(source: str) -> int:
    return 1 if source == "real" else 0


def hydrate_if_empty() -> int:
    """仓库快照 JSON → SQLite（仅当库为空时；Actions 是全新 runner，本地是现成库）。"""
    if not SNAP_FILE.exists():
        return 0
    with server._lock:
        conn = server.db()
        (n,) = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()
        if n:
            return 0
        data = json.loads(SNAP_FILE.read_text(encoding="utf-8"))
        batch = []
        for ep_str, item in data.get("eps", {}).items():
            aid, rows = item["aid"], item["rows"]
            for r in rows:
                ts, v, dm, rp, cn, lk, fv, sh, src = r
                batch.append((int(ep_str), aid, ts, v, dm, rp, cn, lk, fv, sh,
                              "real" if src else "import"))
        conn.executemany(
            "INSERT INTO snapshots(ep_index, aid, ts, views, danmaku, reply, coin,"
            " likes, favorite, share, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
        conn.commit()
        print(f"[hydrate] 从仓库快照水合 {len(batch)} 行")
        return len(batch)


def export_all() -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    EPS_DIR.mkdir(parents=True, exist_ok=True)
    eps_list = server.load_episodes()["episodes"]
    titles = {e["ep_index"]: e["title"] for e in eps_list}
    aids = {e["ep_index"]: e.get("aid") for e in eps_list}
    pubs = {e["ep_index"]: e.get("pub") or 0 for e in eps_list}

    with server._lock:
        rows = server.db().execute(
            "SELECT ep_index, aid, ts, views, danmaku, reply, coin, likes,"
            " favorite, share, source FROM snapshots ORDER BY ep_index, ts"
        ).fetchall()

    snap: dict = {"updated_ts": int(time.time()), "eps": {}}
    per_ep: dict[int, list] = {}
    for ep, aid, ts, v, dm, rp, cn, lk, fv, sh, source in rows:
        r = [ts, v, dm, rp, cn, lk, fv, sh, _src_int(source)]
        per_ep.setdefault(ep, []).append(r)
        snap["eps"].setdefault(str(ep), {"aid": aid, "rows": []})["rows"].append(r)

    # 清掉已经没有数据的旧文件（换番/修数时避免幽灵集）
    for old in EPS_DIR.glob("ep-*.json"):
        if int(old.stem[3:]) not in per_ep:
            old.unlink()
    for ep, rlist in sorted(per_ep.items()):
        (EPS_DIR / f"ep-{ep}.json").write_text(json.dumps(
            {"ep": ep, "aid": aids.get(ep), "pub": pubs.get(ep, 0),
             "title": titles.get(ep, ""), "rows": rlist},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    derived = {
        "generated_ts": int(time.time()),
        "episodes": server.api_episodes(),
        "overview": server.api_overview(),
        "latest": server.api_latest(),
        "review": server._compute_review(),
        "collect": {
            "finished_ts": server._bg_state.get("finished_ts"),
            "ok_count": server._bg_state.get("ok_count"),
            "total": server._bg_state.get("total"),
        },
    }
    SNAP_FILE.write_text(json.dumps(snap, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    DERIVED_FILE.write_text(json.dumps(derived, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    # 开播日对齐（D+N）叠加序列：前端「开播对齐」页签的数据源
    DN_FILE.write_text(json.dumps(server._compute_dn(), ensure_ascii=False,
                                  separators=(",", ":")), encoding="utf-8")

    # 站点地图：单页站只列首页，lastmod 随本轮采集时间刷新（Worker 代理本文件，无需重新部署）
    lastmod = datetime.fromtimestamp(derived["generated_ts"]).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    SITEMAP_FILE.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <url>\n    <loc>{BASE_URL}/</loc>\n'
        f'    <lastmod>{lastmod}</lastmod>\n'
        '    <changefreq>daily</changefreq>\n    <priority>1.0</priority>\n  </url>\n'
        '</urlset>\n', encoding="utf-8")
    kb = SNAP_FILE.stat().st_size / 1024
    print(f"[export] snapshots.json {len(per_ep)}集 / {len(rows)}行 / {kb:.0f}KB；"
          f"derived.json {DERIVED_FILE.stat().st_size / 1024:.0f}KB；sitemap lastmod {lastmod}")


def ping_indexnow() -> None:
    """提交首页到 IndexNow（Bing/Yandex 等；百度需站长平台手动添加）。失败不影响数据管线。"""
    if not INDEXNOW_KEY:
        return
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://api.indexnow.org/indexnow?url={BASE_URL}/&key={INDEXNOW_KEY}")
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"[indexnow] HTTP {r.status}")
    except Exception as exc:  # noqa: BLE001
        print(f"[indexnow] 提交失败（不影响数据管线）：{exc}")


def refresh_season_meta() -> None:
    """篇章元信息：优先 B 站实时，失败回落仓库缓存，成功则刷新缓存文件。"""
    try:
        meta, sections = server._season_meta()
    except Exception as exc:  # noqa: BLE001
        print(f"[meta] B站接口失败（{exc}），回落仓库缓存")
        if META_FILE.exists():
            cached = json.loads(META_FILE.read_text(encoding="utf-8"))
            server._season_meta_cache.update(
                ts=time.time(), meta=cached["meta"], sections=cached["sections"])
    else:
        META_FILE.parent.mkdir(parents=True, exist_ok=True)
        META_FILE.write_text(json.dumps({"meta": meta, "sections": sections},
                                        ensure_ascii=False, separators=(",", ":")),
                             encoding="utf-8")


def main() -> None:
    export_only = "--export-only" in sys.argv
    hydrate_if_empty()
    refresh_season_meta()
    eps_data = server.load_episodes(force=True)
    eps = eps_data["episodes"]
    print(f"[episodes] {eps_data['source']} · {len(eps)}集 · {eps_data.get('error') or 'ok'}")

    if not export_only:
        if not eps[0].get("aid"):
            print("[collect] 剧集表无 aid（离线兜底），跳过采集")
        else:
            with server._collect_lock:
                server._run_collection(1, len(eps), kind="auto")
            res = server._bg_state.get("result") or {}
            print(f"[collect] 完成 {res.get('fetched')}/{res.get('total')}")

    refresh_season_meta()  # 采集后再刷新一次，让 derived 用上最新元信息
    export_all()
    ping_indexnow()


if __name__ == "__main__":
    main()
