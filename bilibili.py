# -*- coding: utf-8 -*-
"""B 站公开网页端数据采集（只读 GET，仅供个人学习研究，务必保持低频！）

实测结论（2026-08，Win11 + 家用宽带直连）：
1. /pgc/web/season/section   正片剧集列表（aid/badge/标题），普通 requests 可用
2. /pgc/view/web/season      番剧标题等元信息，普通 requests 可用
3. /x/web-interface/view     单集累计播放/弹幕/投币……
   —— 该接口对 TLS 指纹有风控：curl / python-requests 直接被 -412 拦截，
   必须用 curl_cffi 的 impersonate="chrome" 模拟浏览器指纹才能通过。
"""
from __future__ import annotations

import time

try:  # 优先 curl_cffi（模拟 Chrome TLS 指纹，绕过 -412）
    from curl_cffi import requests as http

    _KW = {"impersonate": "chrome"}
except ImportError:  # 兜底：没装 curl_cffi 时用普通 requests（stat 接口大概率 -412）
    import requests as http

    _KW = {}

# 注意：curl_cffi 模拟 Chrome 指纹时会自带一套浏览器请求头，
# 手动传 User-Agent 反而和指纹头冲突，会触发间歇性 -412，所以只用 Referer/Accept。
HEADERS = {
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}
FALLBACK_HEADERS = {  # 没装 curl_cffi 时的普通 requests 才带 UA
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    **HEADERS,
}

SECTION_URL = "https://api.bilibili.com/pgc/web/season/section"
SEASON_URL = "https://api.bilibili.com/pgc/view/web/season"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"

RETRYABLE = {-412, -352, -509, -411}  # 瞬时风控/请求过快，值得重试


def _get_json(url: str, params: dict) -> dict:
    headers = HEADERS if _KW else FALLBACK_HEADERS
    last_err = None
    for attempt in range(3):  # 瞬时 412 重试
        try:
            resp = http.get(url, params=params, headers=headers, timeout=15, **_KW)
            resp.raise_for_status()
            payload = resp.json()
            code = payload.get("code")
            if code == 0:
                return payload.get("result") or payload.get("data") or {}
            err = RuntimeError(f"B站接口 code={code} message={payload.get('message')}")
            if code not in RETRYABLE:
                raise err
            last_err = err
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(2 + attempt * 2)
    raise last_err


def fetch_season(season_id: int) -> tuple[dict, list[dict]]:
    """返回 (番剧信息, 正片剧集列表)。

    剧集列表来自 main_section，已剔除预告/花絮；title 字段即集数（"1".."189"）。
    """
    res = _get_json(SECTION_URL, {"season_id": season_id})
    main = res.get("main_section") or {}
    episodes = []
    for e in main.get("episodes") or []:
        if (e.get("badge") or "") == "预告":
            continue
        try:
            idx = int(e.get("title"))
        except (TypeError, ValueError):
            continue
        episodes.append({
            "ep_index": idx,
            "aid": e.get("aid"),
            "bvid": e.get("bvid") or "",
            "title": (e.get("long_title") or "").strip(),
            "badge": (e.get("badge") or ""),
        })
    if not episodes:
        raise RuntimeError("main_section 没有解析到正片")
    episodes.sort(key=lambda x: x["ep_index"])

    info = {"title": "", "season_id": season_id}
    try:  # 元信息失败不影响剧集表
        meta = _get_json(SEASON_URL, {"season_id": season_id})
        info["title"] = meta.get("title") or ""
        info["media_id"] = meta.get("media_id")
    except Exception:  # noqa: BLE001
        pass
    return info, episodes


def fetch_stat(aid: int) -> dict:
    """单集累计统计快照（播放/弹幕/评论/投币/收藏/点赞）。"""
    data = _get_json(VIEW_URL, {"aid": aid})
    st = data.get("stat") or {}
    return {
        "view": int(st.get("view") or 0),
        "danmaku": int(st.get("danmaku") or 0),
        "reply": int(st.get("reply") or 0),
        "coin": int(st.get("coin") or 0),
        "likes": int(st.get("like") or 0),
        "favorite": int(st.get("favorite") or 0),
        "share": int(st.get("share") or 0),
    }
