# -*- coding: utf-8 -*-
"""抖音创作者中心身份回源：从 creator.douyin.com 域拿真实身份。

#132：检测路径与登录保存路径对齐——
- 登录侧 open_login_window 走 creator.douyin.com，cookies 来自该 context
- 检测侧 fetch_profile 改为同域拉取，避免 sessionid 在 creator 域有效但
  发给 www.douyin.com/user/self 时被静默拒绝（返回 None → 误判 invalid）

技术要点：
- 用 curl_cffi 模拟 Chrome TLS 指纹（urllib 无指纹，易被风控空响应）
- 创作者中心为 Next.js 架构，HTML 内嵌 RSC payload (__next_f.push)
- 解析器递归扫描所有内嵌 JSON 块，找含 userInfo 的对象提取身份

失败策略：任何异常返回 None，调用方回退模拟值（不阻塞主流程）。
"""

import json
import re
from urllib.parse import unquote

from loguru import logger

# curl_cffi 优先（Chrome JA3 指纹 + HTTP/2），urllib 兜底（环境异常时仍能跑）
try:
    from curl_cffi import requests as creq
except ImportError:  # pragma: no cover
    creq = None

# 与登录侧 BrowserActor 共用 UA（保持请求特征一致）
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 创作者中心 RSC payload 用 __next_f.push([1, "..."]) 包裹 JSON 转义字符串
# 用字符扫描定位起止（避免正则受 \" 转义影响）
_NEXT_F_PREFIX = 'self.__next_f.push([1, "'
# 兜底：标准 RENDER_DATA / __NEXT_DATA__ 内嵌
_RENDER_RE = re.compile(r'id="RENDER_DATA"[^>]*>([^<]+)<')
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(\{.+?\})</script>', re.DOTALL
)


def _extract_next_f_payloads(html: str) -> list[str]:
    """扫描 HTML 中所有 __next_f.push([1, "..."]) 的 payload 字符串。

    #132：朴素正则匹配大括号会被内部 \" 转义干扰；手动跳过 \\X 序列，
    找到未被转义的 " 之后跟随 ] 或 ) 视为结束。
    """
    payloads: list[str] = []
    start = 0
    while True:
        idx = html.find(_NEXT_F_PREFIX, start)
        if idx < 0:
            break
        i = idx + len(_NEXT_F_PREFIX)
        n = len(html)
        while i < n:
            ch = html[i]
            if ch == "\\":
                # 跳过 \X（X 为任意字符，含转义 \" \\ \/ 等）
                i += 2
                continue
            if ch == '"':
                # 看后续（跳过空白）是否 ] 或 )
                j = i + 1
                while j < n and html[j] in " \t\r\n":
                    j += 1
                if j < n and html[j] in "])":
                    payloads.append(html[idx + len(_NEXT_F_PREFIX):i])
                    break
            i += 1
        start = idx + 1
    return payloads


def _scan_json_objects(html: str) -> list:
    """扫描 HTML 中所有可能的 JSON 对象。

    #132：__next_f.push 出来的字符串是转义 JSON（\" 转义 + \\ 转义），
    手动定位起止再反转义、json.loads 解析。
    """
    candidates: list = []
    # 路径 1: __next_f.push 片段（创作者中心主用）
    for payload in _extract_next_f_payloads(html):
        try:
            unescaped = payload.replace('\\"', '"').replace('\\\\', '\\')
            start = unescaped.find("{")
            if start < 0:
                continue
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(unescaped[start:])
            candidates.append(obj)
        except (json.JSONDecodeError, ValueError):
            continue
    # 路径 2: RENDER_DATA（旧 www.douyin.com 模板，兜底）
    m = _RENDER_RE.search(html)
    if m:
        try:
            data = json.loads(unquote(m.group(1)))
            candidates.append(data)
        except (json.JSONDecodeError, ValueError):
            pass
    # 路径 3: __NEXT_DATA__（Next.js 标准）
    m = _NEXT_DATA_RE.search(html)
    if m:
        try:
            candidates.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass
    return candidates


def _find_user_info(obj) -> dict | None:
    """递归扫描 JSON 树，找含 userInfo / uniqueId / nickname 之一的对象。

    抖音创作者中心 RSC payload 结构分散（路由数据 + 组件 props 嵌套），
    不能假设单一固定路径，递归遍历更稳。
    """
    if isinstance(obj, dict):
        # 直接命中 userInfo 节点
        if "userInfo" in obj and isinstance(obj["userInfo"], dict):
            return obj["userInfo"]
        # 命中含 nickname + 抖音号字段的 dict
        if "nickname" in obj and (
            "uniqueId" in obj or "short_id" in obj or "uid" in obj
        ):
            return obj
        for v in obj.values():
            hit = _find_user_info(v)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_user_info(v)
            if hit:
                return hit
    return None


def _extract_avatar(info: dict) -> str:
    """avatar 字段多形态兼容：avatar300Url / avatarUrl / avatar_thumb.url_list。"""
    avatar = info.get("avatar300Url") or info.get("avatarUrl") or ""
    if not avatar:
        thumb = info.get("avatar_thumb") or info.get("avatar_medium") or {}
        if isinstance(thumb, dict):
            urls = thumb.get("url_list") or []
            avatar = urls[0] if urls else ""
    return avatar


def fetch_profile(cookie: str, timeout: int = 10) -> dict | None:
    """用 Cookie 拉取当前登录用户真实身份。

    #132：与登录侧对齐，请求 creator.douyin.com 域。
    登录保存路径走 creator.douyin.com 的 context，sessionid 可能仅在该域
    有效——发给 www.douyin.com/user/self 会被静默拒绝。
    创作者中心身份信息内嵌在 HTML RSC payload 中，无需签名即可解析。

    参数:
        cookie: 完整 Cookie 串（需含 sessionid）
        timeout: 请求超时秒数
    返回:
        {"douyin_id": 抖音号, "uid": 用户ID, "nickname": 昵称, "avatar": 头像URL}
        未登录/网络失败/结构变化返回 None
    """
    if "sessionid" not in cookie:
        return None
    # 路径 1：creator.douyin.com（与登录保存路径同域，curl_cffi 模拟 Chrome TLS）
    html, err = _fetch_html(
        "https://creator.douyin.com/creator-micro/home",
        cookie, timeout,
    )
    if html is None:
        # 路径 2：creator 域根路径（首页重定向后页面同样含 userInfo）
        html, _ = _fetch_html(
            "https://creator.douyin.com/", cookie, timeout,
        )
    if html is None:
        logger.debug("[fetch_profile] 双入口均失败，最后错误：{}", err)
        return None

    candidates = _scan_json_objects(html)
    if not candidates:
        logger.debug("[fetch_profile] HTML 未匹配任何内嵌 JSON（html 头 200）：{}",
                     html[:200].replace("\n", " "))
        return None

    for obj in candidates:
        info = _find_user_info(obj)
        if not info:
            continue
        nickname = (info.get("nickname") or "").strip()
        # 抖音号：uniqueId 是公开抖音号，short_id 是数字短号，uid 是内部 ID
        douyin_id = (info.get("uniqueId") or info.get("short_id")
                     or info.get("uid") or "")
        douyin_id = str(douyin_id).strip()
        uid = str(info.get("uid") or "")
        avatar = _extract_avatar(info)
        if nickname and douyin_id:
            return {
                "douyin_id": douyin_id,
                "uid": uid,
                "nickname": nickname,
                "avatar": avatar,
            }
    return None


def _fetch_html(url: str, cookie: str, timeout: int) -> tuple[str | None, str | None]:
    """拉 HTML：curl_cffi 优先（Chrome TLS），urllib 兜底。返回 (html, last_err)。"""
    if creq is not None:
        try:
            resp = creq.get(
                url,
                headers={
                    "User-Agent": _UA,
                    "Cookie": cookie,
                    "Referer": "https://creator.douyin.com/",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
                impersonate="chrome",
                timeout=timeout,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                return resp.text, None
            return None, f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_err = f"curl_cffi {type(exc).__name__}: {exc}"
    else:
        last_err = "curl_cffi 未安装"
    # 兜底：urllib（无 TLS 指纹，易被风控空响应）
    try:
        from urllib.request import Request, urlopen
        req = Request(url, headers={
            "User-Agent": _UA,
            "Cookie": cookie,
            "Referer": "https://creator.douyin.com/",
        })
        with urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", "ignore"), None
            return None, f"urllib HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{last_err} | urllib {type(exc).__name__}: {exc}"
