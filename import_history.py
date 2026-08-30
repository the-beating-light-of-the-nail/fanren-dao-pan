# -*- coding: utf-8 -*-
"""从社区开源仓库 bilibili-Fanrenpc（MIT）的每日备份 JSON 回填历史快照。

数据口径：对方每天零点后采集一份全量快照（日更粒度），本脚本取其中「正片」部分、
按文件名日期入库，source='import'。与自采的 source='real'（小时级）在 K 线里天然衔接
——都是真实数据，出处不同，页面已标注来源。

用法：./.venv/Scripts/python.exe import_history.py [--repo 仓库路径]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = Path(r"G:\global-projects\_public-repos\bilibili-Fanrenpc")
SOURCE_NOTE = "yansheng836/bilibili-Fanrenpc (MIT)"


def main() -> None:
    repo = Path(sys.argv[sys.argv.index("--repo") + 1]) if "--repo" in sys.argv else DEFAULT_REPO
    backup_dir = repo / "backup_jsondata"
    files = sorted(backup_dir.glob("bilibili_episodes_infos-*.json"))
    if not files:
        print(f"未找到备份文件：{backup_dir}")
        return

    # ep_index -> aid（用我们的剧集缓存；对不上就存 NULL，不影响 K 线）
    aid_map: dict[int, int | None] = {}
    ep_cache = ROOT / "data" / "episodes.json"
    if ep_cache.exists():
        for e in json.loads(ep_cache.read_text(encoding="utf-8")).get("episodes", []):
            aid_map[e["ep_index"]] = e.get("aid")

    conn = sqlite3.connect(ROOT / "data" / "fanren.db")
    try:  # 老库迁移：补 share 列（与 server.db() 同款）
        conn.execute("ALTER TABLE snapshots ADD COLUMN share INTEGER")
    except sqlite3.OperationalError:
        pass

    # 已导入的 (ep_index, day) 去重，支持重复执行
    have = {(ep, datetime.fromtimestamp(ts).strftime("%Y-%m-%d"))
            for ep, ts in conn.execute("SELECT ep_index, ts FROM snapshots WHERE source='import'")}

    rows = []
    inserted = skipped = bad = 0
    for f in files:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        if not m:
            continue
        day = m.group(1)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"[warn] 解析失败 {f.name}: {exc}")
            continue
        # 用当天 12:00 作为时间戳（不晚于导入时刻，避免"来自未来"的快照打乱排序）
        ts = min(int(datetime.strptime(day + " 12:00:00", "%Y-%m-%d %H:%M:%S").timestamp()),
                 int(datetime.now().timestamp()))
        for item in data:
            if (item.get("type_title") or "") != "正片":
                continue
            try:
                ep = int(item.get("title"))
            except (TypeError, ValueError):
                continue
            st = item.get("stat") or {}
            view = st.get("view") or 0
            if view <= 0:
                continue
            if (ep, day) in have:
                skipped += 1
                continue
            rows.append((ep, aid_map.get(ep), ts, view,
                         st.get("dm") or 0, st.get("reply") or 0, st.get("coin") or 0,
                         st.get("like") or 0, st.get("favorite") or 0, st.get("share") or 0, "import"))
            have.add((ep, day))
            inserted += 1

    conn.executemany(
        "INSERT INTO snapshots(ep_index, aid, ts, views, danmaku, reply, coin, likes, favorite, share, source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    print(f"回填完成：导入 {inserted} 条（跳过已存在 {skipped}，坏文件 {bad}）")
    print(f"来源：{SOURCE_NOTE}，共 {len(files)} 份日快照（{files[0].name} ~ {files[-1].name}）")


if __name__ == "__main__":
    main()
