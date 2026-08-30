# -*- coding: utf-8 -*-
"""组装静态部署包 dist/：public/ 前端 + mode:"static" 的 config 覆盖 + data/site 数据副本。

数据副本的作用：Worker 代理 GitHub raw 失败时的兜底（宁旧勿断），不是常规数据通道。
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
PUBLIC = ROOT / "public"
SITE = ROOT / "data" / "site"


def main() -> None:
    if not (SITE / "derived.json").exists():
        raise SystemExit("data/site/derived.json 不存在，请先运行 python collect.py --export-only")
    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(PUBLIC, DIST)

    (DIST / "config.js").write_text(
        '/* 构建产物：静态部署模式（build_dist.py 生成，勿手改） */\n'
        'window.FANREN = { mode: "static" };\n', encoding="utf-8")

    data_dir = DIST / "data"
    data_dir.mkdir(exist_ok=True)
    shutil.copy2(SITE / "derived.json", data_dir / "derived.json")
    shutil.copy2(SITE / "dn.json", data_dir / "dn.json")
    shutil.copytree(SITE / "eps", data_dir / "eps", dirs_exist_ok=True)

    n_files = sum(1 for _ in DIST.rglob("*") if _.is_file())
    size_kb = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file()) / 1024
    print(f"[dist] 组装完成：{n_files} 个文件，共 {size_kb:.0f} KB")


if __name__ == "__main__":
    main()
