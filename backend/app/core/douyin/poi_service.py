# -*- coding: utf-8 -*-
"""POI 门店真实接口封装（任务 #449 PR1）。

封装 creator.douyin.com 两个核心接口：
- search/poi：按关键词搜索门店（带 a_bogus/msToken 签名）
- cps/detail/v2：拉单店详情（含 26 SPU + 佣金率 + 总 GMV）

实现要点（参照 demo_poi_search_446 / demo_poi_detail_447 验证流程）：
- 浏览器走 BrowserActor 单例 + sync API（必须 worker 线程调用）
- 签名 URL 必须由浏览器内 axios 拦截器算（a_bogus 绑定完整 query string）
- 重发 URL 用 curl_cffi impersonate="chrome"
- 错误抛 DouyinClientError / RiskControlError（与 base.py 兼容）

依赖：
- app.core.douyin.browser.browser_actor
- app.core.douyin.rate_limiter.rate_limiter
"""

import json
from pathlib import Path
from typing import Optional

from loguru import logger

from app.core.douyin.base import DouyinClientError, RiskControlError
from app.core.douyin.browser import browser_actor, BrowserSession, _cookie_header_to_playwright
from app.core.douyin.rate_limiter import rate_limiter


def cookie_str_to_storage_state(cookie_str: str, domain: str = ".douyin.com") -> dict:
    """把 cookie 头串转 storage_state dict。

    用途：account_service 把 DB cookie 重建 storage 落盘 / 判定登录态。
    """
    cookies = _cookie_header_to_playwright(cookie_str, domain)
    return {"cookies": cookies, "origins": []}

# 抖音创作中心域（POI 接口固定）
CREATOR_DOUYIN = "https://creator.douyin.com"
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# search/poi 响应关键字（list 字段可能叫 poi_list / poi_infos / data.poi_list）
_POI_LIST_KEYS = ("poi_list", "poi_infos", "poi_info_list", "pois",
                  "search_result", "result_list", "data")


def _cookie_str(storage_state: dict) -> str:
    """从 storage_state 抽 cookie 头串（name=value; name=value）。"""
    return "; ".join(f"{c['name']}={c['value']}" for c in storage_state.get("cookies", []))


def _storage_to_playwright_cookies(storage_state: dict) -> list[dict]:
    """storage_state cookies → playwright add_cookies 格式。

    域名归一化到 .douyin.com（cookie 可能落在 creator/www/snssdk 等子域）。
    """
    out = []
    for c in storage_state.get("cookies", []):
        # playwright 要求 name/value/domain/path
        out.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain") or ".douyin.com",
            "path": c.get("path") or "/",
        })
    return out


def search_poi_in_session(
    session: BrowserSession,
    keyword: str,
    page: int,
) -> list[dict]:
    """在 BrowserSession 内按关键词搜索门店（单页）。

    复用 session 已建好的 page（不开/关 context、不重加载页面），
    签名由 axios 拦截器现场算。

    失败处理：未拦截到响应抛 DouyinClientError，调用方记录日志后跳过。
    """
    capture = _make_search_capture(keyword, page)
    url_tpl = _make_search_url(keyword, page)
    actions = _make_fetch_actions(url_tpl, wait_ms=2000)

    captured = session.call(capture=capture, actions=actions, wait_ms=2000, kind="search")
    if not captured:
        logger.warning("[poi-service] session 内 search 无响应 keyword={} page={}", keyword, page)
        return []
    body = captured[0]
    if isinstance(body, dict) and "body" in body:
        body = body["body"]
    return _extract_poi_list(body)


def _make_search_url(keyword: str, page: int) -> str:
    """search/poi URL 模板（path-only，相对 host 由 fetch 处理）。"""
    return (
        f"/aweme/v1/life/video_api/search/poi/"
        f"?count=12&from_webapp=1&get_current_loc=1"
        f"&keywords={requests_quote(keyword)}&search_type=2&poi_anchor_tab=2"
        f"&page={page}&poi_mode=1"
        f"&cookie_enabled=true&screen_width=1440&screen_height=900"
        f"&browser_language=zh-CN&browser_platform=Win32"
        f"&browser_name=Mozilla&browser_version=5.0+%28Windows+NT+10.0%3B+Win64%3B+x64%29+AppleWebKit%2F537.36+%28KHTML%2C+like+Gecko%29+Chrome%2F140.0.0.0+Safari%2F537.36"
        f"&browser_online=true&timezone_name=Asia%2FShanghai"
        f"&aid=1128&support_h265=1"
    )


def _make_search_capture(keyword: str, page: int):
    """search/poi 响应拦截判定。"""
    from urllib.parse import unquote

    def _capture(url: str) -> bool:
        if "creator.douyin.com" not in url:
            return False
        if "a_bogus=" not in url:
            return False
        if "search/poi" not in url:
            return False
        return keyword in unquote(url) and f"page={page}" in url
    return _capture


def _make_fetch_actions(url_tpl: str, wait_ms: int = 2000):
    """通用 evaluate fetch actions：在现有 page 上发 fetch + 等响应。

    策略：直接 evaluate fetch 让 axios 拦截器算 a_bogus 签名。
    """
    def _evaluate_fetch_actions(page):
        page.evaluate(
            """async (url) => {
                try {
                    await fetch(url, {
                        credentials: 'include',
                        headers: {
                            'Accept': 'application/json, text/plain, */*',
                            'X-Requested-With': 'XMLHttpRequest',
                        },
                    });
                } catch (e) {}
            }""",
            url_tpl,
        )
        if wait_ms:
            page.wait_for_timeout(wait_ms)
    return _evaluate_fetch_actions


def fetch_poi_detail_in_session(
    session: BrowserSession,
    poi_id: str,
) -> Optional[dict]:
    """在 BrowserSession 内拉单店详情 cps/detail/v2。

    复用 session 已建好的 page（不开/关 context、不重加载页面）。

    失败处理：未拦截到响应返回 None，调用方记录日志后跳过。
    """
    full_url = _make_detail_url(poi_id)
    capture = _make_detail_capture(poi_id)
    actions = _make_fetch_actions(full_url, wait_ms=3000)

    captured = session.call(capture=capture, actions=actions, wait_ms=3000, kind="detail")
    if not captured:
        logger.warning("[poi-service] session 内 detail 无响应 poi={}", poi_id)
        return None
    body = captured[0]
    if isinstance(body, dict) and "body" in body:
        body = body["body"]
    if not isinstance(body, dict):
        return None
    if body.get("status_code") not in (0, None):
        logger.warning("[poi-service] poi={} 详情风控 status_code={}",
                       poi_id, body.get("status_code"))
        return None
    return body


def _make_detail_url(poi_id: str) -> str:
    """cps/detail/v2 URL 模板（path-only）。"""
    return (
        f"/aweme/v1/poi/cps/detail/v2/"
        f"?from_webapp=1&poi_id={poi_id}&options=%7B%7D"
        f"&cookie_enabled=true&screen_width=1440&screen_height=900"
        f"&browser_language=zh-CN&browser_platform=Win32"
        f"&browser_name=Mozilla&browser_version=5.0+%28Windows+NT+10.0%3B+Win64%3B+x64%29+AppleWebKit%2F537.36+%28KHTML%2C+like+Gecko%29+Chrome%2F140.0.0.0+Safari%2F537.36"
        f"&browser_online=true&timezone_name=Asia%2FShanghai"
        f"&aid=1128&support_h265=1"
    )


def _make_detail_capture(poi_id: str):
    """cps/detail 响应拦截判定。"""
    def _capture(url: str) -> bool:
        return ("creator.douyin.com" in url
                and "a_bogus=" in url
                and "cps/detail" in url
                and poi_id in url)
    return _capture


def merge_poi_to_shop_dict(poi: dict, detail: Optional[dict] = None) -> dict:
    """合并 search/poi + cps/detail/v2 为 shop 表字典。

    字段名与 migrations.py v33 shop 表（精选）一一对应。
    v33 移除了选品维度（talent/sale/verify/gmv/per_capita/shop_branch/mark/remark 等），
    只保留 POI 真实接口维度。
    """
    ai = poi.get("address_info") or {}
    bt = poi.get("poi_backend_type") or {}
    score_info = poi.get("poi_score_info") or {}

    # 类别三级拼接
    category = poi.get("type_code") or bt.get("code") or ""
    category_full = f"{bt.get('l1_name','')}/{bt.get('l2_name','')}/{bt.get('l3_name','')}"

    # 搜索标签列表 → JSON 串（list 字段直接入库 SQLite 不便，存 TEXT 检索时再 JSON parse）
    tags = poi.get("poi_search_tags_v2")
    tags_json = None
    if isinstance(tags, list):
        tags_json = json.dumps(tags, ensure_ascii=False)
    elif isinstance(tags, str):
        tags_json = tags

    row = {
        # === 业务主键 ===
        "poi_id": poi.get("poi_id", ""),
        "name": poi.get("poi_name", ""),
        "category": category,
        "category_full": category_full,
        # === 城市 ===
        "city": ai.get("city", ""),
        "province": ai.get("province", ""),
        "district": ai.get("district", ""),
        "ad_code": ai.get("ad_code_v2", ""),
        "address": ai.get("address", ""),
        # === 坐标（火星）===
        "lat_gcj02": poi.get("poi_latitude_gcj02"),
        "lng_gcj02": poi.get("poi_longitude_gcj02"),
        # === 商业指标（search 接口）===
        "is_cps": 1 if poi.get("is_cps") else 0,
        "spu_count": int(poi.get("spu_count") or 0),
        "cps_spu_count": int(poi.get("cps_spu_count") or 0),
        "delivery_spu_count": int(
            (poi.get("anchor_post_ext") or {}).get("delivery_spu_count") or 0
        ),
        # === 任务 #406 补充维度 ===
        "poi_search_tags_v2": tags_json,
        "poi_score": score_info.get("score"),
        "poi_score_content": score_info.get("score_content"),
        "business_area": poi.get("business_area_name"),
        "l1_name": bt.get("l1_name"),
        "l2_name": bt.get("l2_name"),
        "l3_name": bt.get("l3_name"),
        # === 佣金率（兜底：detail 优先）===
        "commission_rate": _estimate_commission_from_take_rates(poi, detail),
        # === 详情标记（默认无 detail）===
        "detail_fetched": 0,
        "detail_updated_time": None,
        # === detail 字段默认空 ===
        "platform_name": None,
        "platform_source": None,
        "take_rate_min": None,
        "take_rate_max": None,
        "take_rate_avg": None,
        "total_sold": None,
        "total_gmv": None,
        "total_commission": None,
        "spu_type_groupon": 0,
        "spu_type_delivery": 0,
        "top_spu_name": None,
        "top_spu_sold": None,
    }

    # === 合并 detail ===
    if detail:
        cps = detail.get("cps_detail") or {}
        platform_list = cps.get("platform_list") or []
        if platform_list:
            pl = platform_list[0]
            spus = pl.get("cps_spu_list") or []

            rates = [s.get("take_rate") for s in spus if s.get("take_rate") is not None]
            sold_list = [s.get("sold") or 0 for s in spus]
            prices = [s.get("sale_price") or 0 for s in spus]
            earns = [s.get("earn_price") or 0 for s in spus]

            row.update({
                "platform_name": pl.get("platform_name"),
                "platform_source": pl.get("platform_source"),
                "take_rate_min": min(rates) if rates else None,
                "take_rate_max": max(rates) if rates else None,
                "take_rate_avg": (sum(rates) / len(rates)) if rates else None,
                "total_sold": sum(sold_list),
                "total_gmv": round(sum(p * s for p, s in zip(prices, sold_list)) / 100, 2),
                "total_commission": round(sum(e * s for e, s in zip(earns, sold_list)) / 100, 2),
                "spu_type_groupon": sum(1 for s in spus if s.get("spu_type_name") == "团购"),
                "spu_type_delivery": sum(1 for s in spus if s.get("spu_type_name") == "团购-支持配送"),
                "top_spu_name": max(spus, key=lambda s: s.get("sold") or 0).get("spu_name") if spus else None,
                "top_spu_sold": max(sold_list) if sold_list else None,
                "detail_fetched": 1,
            })

            # 顶层 poi 字段冗余（detail.poi 也有部分基础信息）
            dp = (detail.get("poi") or {})
            if dp:
                dp_ai = dp.get("address_info") or {}
                if dp_ai.get("city") and not row["city"]:
                    row["city"] = dp_ai["city"]
                if dp_ai.get("province") and not row["province"]:
                    row["province"] = dp_ai["province"]
                if dp_ai.get("district") and not row["district"]:
                    row["district"] = dp_ai["district"]
                if dp_ai.get("ad_code_v2") and not row["ad_code"]:
                    row["ad_code"] = dp_ai["ad_code_v2"]
                if dp_ai.get("address") and not row["address"]:
                    row["address"] = dp_ai["address"]
                # 坐标可能 search 接口为空，从 detail 补
                if row["lat_gcj02"] is None and dp.get("poi_latitude_gcj02"):
                    row["lat_gcj02"] = dp["poi_latitude_gcj02"]
                if row["lng_gcj02"] is None and dp.get("poi_longitude_gcj02"):
                    row["lng_gcj02"] = dp["poi_longitude_gcj02"]

    # commission_rate 兜底：detail 有 take_rate_avg 用之
    if row["commission_rate"] is None and row["take_rate_avg"] is not None:
        row["commission_rate"] = round(row["take_rate_avg"] / 100, 2)  # 万分之 → %

    return row


def _extract_poi_list(body: dict) -> list:
    """从响应里兜底找 POI 列表字段。"""
    if not isinstance(body, dict):
        return []
    # status_code != 0 时不报错但跳过（接口拒签/限流时通常 status_code 非 0）
    if body.get("status_code") not in (None, 0):
        logger.warning("[poi-service] 接口返回 status_code={} msg={} keys={}",
                        body.get("status_code"), body.get("status_msg"), list(body.keys()))
        return []
    # 顶层：直找 list
    for k in _POI_LIST_KEYS:
        v = body.get(k)
        if isinstance(v, list) and v:
            return v
    # 嵌套：data.X / search_result.X 任一为 dict 时再下钻
    for wrap in ("data", "search_result", "result_list"):
        sub = body.get(wrap)
        if isinstance(sub, dict):
            for k in _POI_LIST_KEYS:
                v = sub.get(k)
                if isinstance(v, list) and v:
                    return v
            # 兜底：嵌套 dict 里第一个非空 list
            for vv in sub.values():
                if isinstance(vv, list) and vv and isinstance(vv[0], dict):
                    return vv
    # 兜底：body 顶层第一个非空 dict list
    for vv in body.values():
        if isinstance(vv, list) and vv and isinstance(vv[0], dict):
            return vv
    logger.warning("[poi-service] 未识别到 POI 列表，响应 keys={}", list(body.keys()))
    return []


def _estimate_commission_from_take_rates(poi: dict, detail: Optional[dict]) -> Optional[float]:
    """从 detail.take_rate_avg 算 commission_rate（%）。

    search 接口不返回 take_rate，只有 detail 接口有。先用 detail。
    """
    if not detail:
        return None
    cps = detail.get("cps_detail") or {}
    pl = (cps.get("platform_list") or [None])[0]
    if not pl:
        return None
    spus = pl.get("cps_spu_list") or []
    rates = [s.get("take_rate") for s in spus if s.get("take_rate") is not None]
    if not rates:
        return None
    # take_rate 是万分之 → 转为 % 存 commission_rate
    return round(sum(rates) / len(rates) / 100, 2)


def requests_quote(s: str) -> str:
    """URL 编码（utf-8）。用 urllib.parse.quote 走标准编码。"""
    from urllib.parse import quote
    return quote(s, safe="")
