# 凡人道盘 · 凡人修仙传非官方热度数据站

在线站点：**<https://fanren.cdqyfdbymn.me>**（Cloudflare Workers 静态部署）

用股票 K 线的方式看《凡人修仙传》（ss28747 / md28223043）：**周六开盘，道友盯盘**。
大盘总览、个股行情（每集 = 一只个股）、7 指标 K 线（日K/周K/分时）、涨停板盘点、自动收评、股民词典——
全部建立在真实快照之上：梗是语气，数字是数据。

站点分三层，铁律不变：
- **数据层（大盘/行情）永不修饰**：涨跌如实展示，一根阴线都不隐藏
- **观点层（观点页）全部标注**：站长立场以「个人观点」标签呈现
- 「开盘/涨停/持有」均为玩梗，不构成任何真实投资建议

## 快速开始

```bash
# CMD / PowerShell
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python server.py

# Git Bash
py -3 -m venv .venv
./.venv/Scripts/pip install -r requirements.txt
./.venv/Scripts/python.exe server.py
```

打开 <http://127.0.0.1:8000>。

- **自动采集默认开启**：每小时全量一轮（189 集 × 1.2s/集 ≈ 4 分钟），数据持续积累；
  关闭用 `FANREN_AUTOCOLLECT=0`
- 页面上的「刷新数据」按钮**只读本地快照，不触发对 B 站的请求**（公开站防滥用的设计）；
  采集状态与排期由页头徽章如实展示。需要手动补采（管理用途）：

  ```bash
  curl -X POST "http://127.0.0.1:8000/api/collect?limit=189&background=1"
  # 默认全量、后台执行、60s 冷却；进度查询 GET /api/collect/status
  ```
- **历史数据回填**：2025-10-21 ~ 2026-08-29 的日更粒度快照来自开源项目
  yansheng836/bilibili-Fanrenpc（MIT），已由 `import_history.py` 导入（source='import'，
  页脚标注来源）。同一天与自采数据并存时只采用自采（小时级）。
- **每日自动备份**：每轮采集后导出 `data/backup/state_YYYY-MM-DD.json`（当日全量状态）
- **站长观点**：编辑 `public/opinions.js`，数组里增删卡片即可，刷新生效，无需改代码

## 页面结构

| Tab | 层 | 内容 |
| --- | --- | --- |
| 数据总览 | 数据层 | 全剧累计、半年度每集热度走势（折算满龄）、篇章互动率（六年新高标记）、留存漏斗、TOP10 榜单（7 指标） |
| 播放K线 | 数据层 | 每集日增播放蜡烛（日K/周K）+ 弹幕增量成交量 + MA5/MA10 + 分时；2026-08-30 起小时级自采，更早为日更回填 |
| 站长观点 | 观点层 | 站长立场卡片，全部标注「个人观点」，可带数据锚点 |

## API

| 接口 | 说明 |
| --- | --- |
| `GET /api/episodes` | 剧集表（实时拉取，磁盘缓存，离线兜底） |
| `GET /api/overview` | 总览聚合：总量、半年度走势、篇章互动率、留存 |
| `GET /api/kline?ep=1&days=90&mode=inc\|total\|intraday` | K 线 JSON |
| `GET /api/latest` | 每集最近一条快照 |
| `GET /api/collect/status` | 采集进度与排期（页面徽章的数据源） |
| `POST /api/collect?start=1&limit=189&background=1` | 管理用手动补采（默认全量后台，冷却 60s） |

换番剧：`FANREN_SEASON_ID` 环境变量 + 删 `data/` 目录。
离线分析脚本：`./.venv/Scripts/python.exe analyze.py`。

## 合规与风控约定

1. **只做无鉴权公开接口的只读 GET**：不登录、不采集个人信息、不存储视频/音频/字幕内容
2. **低频**：每集间隔 1.2s；手动采集冷却 60s；自动采集默认 1 小时一轮——都不要调快
3. **访问方式**：统计接口对非浏览器客户端有指纹校验，代码用 curl_cffi 以浏览器兼容方式访问；
   请勿将相关技术用于本项目之外的高频场景
4. **页面声明**：非官方粉丝站、数据可能有误差、仅供个人学习研究；不使用 B 站名称/Logo 做站名
5. **执法现实**：同类接口文档库 bilibili-API-collect 已于 2026-01 收到 B 站律师函关停。
   本站保持个人非商业（**不挂广告**）；收到权利方任何通知，当天停止服务
6. **商业化警告**：挂广告 = 从"灰色自用"变"商业寄生"，是本项目最大的法律风险跃迁；
   修饰数据 + 商业化 = 虚假宣传，双倍不要

## 已验证的接口（2026-08 实测）

- 剧集列表：`/pgc/web/season/section?season_id=28747` → 189 集正片 + 每集 aid
- 单集统计：`/x/web-interface/view?aid=xxx` → 播放/弹幕/评论/投币/收藏/点赞
- 接口随时可能变更或加强风控；失效时优先降低频率、暂停服务，而不是加强对抗

## 部署架构（全免费资源，7×24 在线）

```
GitHub Actions（每2小时）──采集──▶ data/site/*.json（仓库内，事实源）
                                      │
Cloudflare Worker ◀──代理 /data/*──── ┘（边缘缓存5分钟，提交后自动生效）
   │
   └─ 静态资产（public/ 构建成 dist/）：K线在浏览器端计算（compute.js）
```

- **采集**：`.github/workflows/collect.yml` 每 2 小时跑 `collect.py`（1.2s/集 礼貌间隔），
  快照提交回本仓库——数据全程公开可审计，「欢迎查源码」
- **托管**：Cloudflare Workers 静态资产 + 自定义域名，零服务器成本；
  `/data/*` 由 Worker 实时代理 GitHub raw（缓存 5 分钟 → raw 回落 → 部署包副本）
- **双模式前端**：`public/config.js` 的 `mode` 决定走 `/api/*`（本地 FastAPI）还是
  `/data/*.json`（静态部署）；`build_dist.py` 负责组装部署包
- **数据文件**：`snapshots.json` 全量快照（审计用）；`eps/N.json` 每集样本（K线懒加载）；
  `derived.json` 剧集表/总览/收评（首屏一次拉取，~40KB）
- 本仓库公开（Actions 分钟数不设限 + 数据可验证）；改私有需注意 2000 分钟/月限额

日常更新只需改 `public/` 后重跑 `build_dist.py` + `npx wrangler deploy`（数据更新无需重新部署）。

## 转正式版路线（已完成的与剩余的）

1. ~~常驻部署 + 每日备份~~ → 已由 GitHub Actions + 仓库内 JSON 快照承担（每次提交即备份）
2. 时区已钉死 `Asia/Shanghai`（Actions env + 前端计算）
3. 上线前咨询知识产权律师，过一遍免责声明与采集策略
4. 评论功能如上线：只管违规内容，不参与立场裁判——记分牌不吹哨
