# -*- coding: utf-8 -*-
"""选品中心服务层（重写 #449）。

模块边界：
- 任务 CRUD（DB 直读直写，不走 ORM）；
- _run_one_round 驱动 poi_service 拉数据 + 增量入库；
- 门店查询 / 聚合（下拉数据源）；
- 暴露给 APScheduler 的注册回调 register_shop_pull。

硬编码约定（前端不可配置）：
- 全部门店入库（不按 is_cps 过滤——#505：门店佣金状态会变动，不能硬判断有佣金）；
- fetch_detail=True（is_cps=1 时拉 cps/detail/v2 详情；非 CPS 门店跳过详情只入库基础字段）；
- 全量重拉模式（每轮从 page=1 拉起，依赖 poi_id UNIQUE 幂等）。
"""

import json
import re
from typing import Any

from loguru import logger

from app.core.douyin import poi_service
from app.core.douyin.browser import browser_actor
from app.core import notifier
from app.core.task_scheduler import (
    register_interval_job,
    remove_job,
    touch_last_run,
)
from app.db import get_db
from app.db.utils import fill_common_fields, new_id, now_str
from app.services.douyin_account import get_account_manager
from app.services.task_service import _fmt_hms, raise_for_cancel, task_service

# 每轮最大翻页数（防漏：search/poi 接口每页 12 条，100 页 ≈ 1200 条）
MAX_PAGES_PER_ROUND = 100

# shop 字段全集（与 migrations.py v1 选品表 + poi_service.merge_poi_to_shop_dict 对齐）
# 增量更新分组（base/metric/detail）见 _upsert_shop
_SHOP_BASE_COLS = (
    "poi_id", "name", "category", "category_full",
    "city", "province", "district", "ad_code", "address",
    "lat_gcj02", "lng_gcj02",
    "is_cps", "spu_count", "cps_spu_count", "delivery_spu_count",
    "spu_type_groupon", "spu_type_delivery",
)

# v36 字段分组：
# - SEARCH_ALWAYS_COLS：search 接口每次都返回（l1/l2/l3_name），允许"总是更新"
# - SEARCH_OPTIONAL_COLS：search 接口不保证返回（business_area / poi_score / score_content
#   / search_tags_v2），必须"仅在非 None 时写入"，否则每次 update 会把旧值清空
_SEARCH_ALWAYS_COLS = ("l1_name", "l2_name", "l3_name")
_SEARCH_OPTIONAL_COLS = (
    "business_area",
    "poi_search_tags_v2",
    "poi_score", "poi_score_content",
)

_SHOP_DETAIL_COLS = (
    "address_detail", "platform_name", "platform_source",
    "take_rate_min", "take_rate_max", "take_rate_avg",
    "total_sold", "total_gmv", "total_commission",
    "top_spu_name", "top_spu_sold",
)

# NOT NULL 兜底（migrations.py 声明 NOT NULL DEFAULT 0 的列）
_NOT_NULL_DEFAULTS: dict[str, Any] = {
    "is_cps": 0,
    "spu_count": 0,
    "cps_spu_count": 0,
    "delivery_spu_count": 0,
    "spu_type_groupon": 0,
    "spu_type_delivery": 0,
    "detail_fetched": 0,
}

# 查询参数列白名单（list_shops 动态构造 SQL 防注入）
_SHOP_SORT_COLS = {
    "spu_count", "cps_spu_count", "total_gmv", "total_sold",
    "commission_rate", "take_rate_avg", "detail_updated_time",
    "create_time", "update_time", "city",
}


# ============ 任务 CRUD ============

def create_task(task_name: str, keyword: str, cities: list[str],
                account_id: str, interval: dict,
                shop_name_pattern: str = "", city_pattern: str = "") -> dict:
    """创建选品拉取任务。"""
    db = get_db()
    cities_json = json.dumps(cities or [], ensure_ascii=False)
    interval_json = json.dumps(interval, ensure_ascii=False)
    # 正则预编译校验：非法正则立即抛错，避免任务跑起来才报
    _compile_pattern(shop_name_pattern, "店名正则")
    _compile_pattern(city_pattern, "城市正则")

    record = {
        "id": new_id(),
        "task_name": task_name,
        "keyword": keyword,
        "cities": cities_json,
        "account_id": account_id,
        "interval_config": interval_json,
        "shop_name_pattern": shop_name_pattern.strip(),
        "city_pattern": city_pattern.strip(),
        "status": "enabled",
        "last_run_time": None,
        "next_run_time": None,
    }
    fill_common_fields(record, is_insert=True)
    db.insert("shop_pull_task", record)

    # 立即注册到调度器（status=enabled）
    register_shop_pull(record)
    logger.info("[选品] 创建任务 {} keyword={}", task_name, keyword)
    return _serialize_task(record)


def _compile_pattern(pattern: str, label: str) -> re.Pattern | None:
    """预编译正则；空字符串返回 None，否则非法正则抛 ValueError。"""
    p = (pattern or "").strip()
    if not p:
        return None
    try:
        return re.compile(p)
    except re.error as e:
        raise ValueError(f"{label}不合法: {e}")


def get_task(task_id: str) -> dict | None:
    """读取单个任务（含 cities 反序列化）。"""
    db = get_db()
    row = db.query_one(
        "SELECT * FROM shop_pull_task WHERE id=? AND deleted=0", (task_id,))
    if not row:
        return None
    return _serialize_task(row)


def list_tasks(page: int = 1, page_size: int = 20) -> dict:
    """任务列表（分页，按 create_time 倒序）。"""
    db = get_db()
    offset = (page - 1) * page_size
    total = db.query_one(
        "SELECT COUNT(*) AS c FROM shop_pull_task WHERE deleted=0")["c"]
    rows = db.query_all(
        "SELECT * FROM shop_pull_task WHERE deleted=0 "
        "ORDER BY create_time DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "list": [_serialize_task(r) for r in rows],
    }


def update_task(task_id: str, task_name: str | None = None,
                keyword: str | None = None, cities: list[str] | None = None,
                account_id: str | None = None,
                interval: dict | None = None,
                shop_name_pattern: str | None = None,
                city_pattern: str | None = None) -> dict | None:
    """编辑任务（仅更新传入项，触发调度重置）。"""
    db = get_db()
    row = db.query_one("SELECT id FROM shop_pull_task WHERE id=? AND deleted=0", (task_id,))
    if not row:
        return None

    # 正则预校验
    if shop_name_pattern is not None:
        _compile_pattern(shop_name_pattern, "店名正则")
    if city_pattern is not None:
        _compile_pattern(city_pattern, "城市正则")

    updates: dict = {}
    if task_name is not None:
        updates["task_name"] = task_name
    if keyword is not None:
        updates["keyword"] = keyword
    if cities is not None:
        updates["cities"] = json.dumps(cities, ensure_ascii=False)
    if account_id is not None:
        updates["account_id"] = account_id
    if interval is not None:
        updates["interval_config"] = json.dumps(interval, ensure_ascii=False)
    if shop_name_pattern is not None:
        updates["shop_name_pattern"] = shop_name_pattern.strip()
    if city_pattern is not None:
        updates["city_pattern"] = city_pattern.strip()

    if updates:
        fill_common_fields(updates, is_insert=False)
        db.update_by_id("shop_pull_task", task_id, updates)

    # 重新注册调度（可能 interval / status 变化）
    new_row = db.query_one("SELECT * FROM shop_pull_task WHERE id=?", (task_id,))
    if new_row and new_row["status"] == "enabled":
        register_shop_pull(new_row)
    return get_task(task_id)


def delete_task(task_id: str) -> bool:
    """删除任务（软删 + 移除调度）。"""
    db = get_db()
    row = db.query_one("SELECT id FROM shop_pull_task WHERE id=?", (task_id,))
    if not row:
        return False
    db.update_by_id("shop_pull_task", task_id, {"deleted": 1, "update_time": now_str()})
    remove_job(f"shop_pull:{task_id}")
    return True


def toggle_task(task_id: str, enabled: bool) -> dict | None:
    """启停任务。"""
    db = get_db()
    row = db.query_one("SELECT id FROM shop_pull_task WHERE id=? AND deleted=0", (task_id,))
    if not row:
        return None

    new_status = "enabled" if enabled else "disabled"
    db.update_by_id("shop_pull_task", task_id, {
        "status": new_status,
        "update_time": now_str(),
    })

    if enabled:
        register_shop_pull(db.query_one(
            "SELECT * FROM shop_pull_task WHERE id=?", (task_id,)))
    else:
        remove_job(f"shop_pull:{task_id}")
    return get_task(task_id)


def _serialize_task(row: dict) -> dict:
    """DB 行 → API 返回 dict（cities / interval 反序列化 + interval_config 字段改名）。"""
    out = dict(row)
    try:
        out["cities"] = json.loads(out.get("cities") or "[]")
    except Exception:
        out["cities"] = []
    try:
        out["interval"] = json.loads(out.get("interval_config") or "{}")
    except Exception:
        out["interval"] = {}
    out.pop("interval_config", None)
    out.pop("deleted", None)
    return out


# ============ 任务执行 ============

def run_task_now(task_id: str) -> str:
    """手动触发一轮（投递到 task_service 队列，返回 bg_task_id）。

    PR2 #55：type 从 pull 拆为 shop_pull；name 直接用 shop_pull_task.task_name
    （不再拼接"选品拉取 #xxx" / "手动/定时"后缀）。
    """
    row = get_db().query_one("SELECT task_name FROM shop_pull_task WHERE id=? AND deleted=0", (task_id,))
    name = row["task_name"] if row else "未命名任务"
    bg_id = task_service.submit(
        task_type="shop_pull",
        name=name,
        func=lambda info: _run_one_round(task_id, info),
    )
    return bg_id


def _run_one_round(task_id: str, info=None) -> str:
    """单轮执行（task_service worker 内调用）。

    流程：
    1. 读任务 → 关键词 + 账号；
    2. 检查 storage_state 有效（账号未登录立即 fail）；
    3. 翻页搜索：page=1..MAX_PAGES_PER_ROUND；
    4. 全部 POI 入库（不按 is_cps 过滤——#505：门店佣金状态会变动，不能硬判断）；
    5. _upsert_shop 写入；
    6. 写 shop_pull_log。
    """
    db = get_db()
    task = db.query_one("SELECT * FROM shop_pull_task WHERE id=? AND deleted=0", (task_id,))
    if not task:
        logger.warning("[选品] 任务 {} 不存在或已删除", task_id)
        return "failed"

    keyword = task["keyword"]
    # cities 字段保留 DB 列兼容旧任务，但已不再用于过滤（#406：仅拉 CPS 全量）
    account_id = task["account_id"]
    # 正则预编译（创建/编辑时已校验；这里兜底容错：非法正则视为不过滤）
    try:
        shop_re = _compile_pattern(task.get("shop_name_pattern") or "", "店名正则")
    except ValueError:
        shop_re = None
    try:
        city_re = _compile_pattern(task.get("city_pattern") or "", "城市正则")
    except ValueError:
        city_re = None

    # 1. 刷新 last_run_time
    try:
        touch_last_run("shop_pull_task", task_id)
    except Exception as exc:
        logger.warning("[选品] 刷新 last_run_time 失败 task={}: {}", task_id, exc)

    # 2. 校验账号登录态（storage 缺失 → 标 invalid + 跳过本轮，不抛异常避免死循环失败）
    try:
        storage_state = get_account_manager().load_storage(account_id)
    except FileNotFoundError as exc:
        # DB 有账号记录但 FS 无 storage.json（账号列表与 accounts 目录漂移）
        # 不再自动弹内置浏览器，让用户去账号管理手动重新登录（更可控）
        msg = "账号登录态文件丢失，请到「账号管理」重新登录"
        logger.warning("[选品] {}（原异常：{}）", msg, exc)
        _mark_account_invalid(account_id, reason="storage 目录缺失")
        _write_log(task_id, 0, 0, 0, msg)
        if info:
            info.message = msg
        # 任务 #59 P0 #3：返 "failed" 而非 dict（状态机契约对齐）
        return "failed"
    except Exception as exc:
        msg = f"账号登录态文件读取失败：{exc}"
        _mark_account_invalid(account_id, reason=f"storage 读取失败: {exc}")
        _write_log(task_id, 0, 0, 0, msg)
        if info:
            info.message = msg
        # 任务 #59 P0 #3：返 "failed" 而非 dict（状态机契约对齐）
        return "failed"

    if not storage_state.get("cookies"):
        msg = "账号未登录，请到「账号管理」重新登录"
        _mark_account_invalid(account_id, reason="storage 无 cookie")
        _write_log(task_id, 0, 0, 0, msg)
        if info:
            info.message = msg
        # 任务 #59 P0 #3：返 "failed" 而非 dict（状态机契约对齐）
        return "failed"

    # PR3 #56：progress 模板统一，加 start_ts 算耗时；skip_count 累加正则过滤跳过数
    import time
    # 任务 #66：统一用 task_service 注入的 start_ts
    start_ts = info.start_ts or time.time()
    pages_done = 0
    new_count = 0
    update_count = 0
    skip_count = 0
    fail_reason: str | None = None
    # 任务 #63：进度日志兜底（之前每页完成仅写 info.progress，日志看不到节奏）
    logger.info(
        "[选品] 任务 {} keyword={!r} 拉取开始",
        task_id[:8], keyword,
    )

    # 3. 一次会话内翻页 search + 批量 detail（任务 #449：复用 context/page）
    cookies = poi_service._storage_to_playwright_cookies(storage_state)
    try:
        with browser_actor.new_session(
            url=poi_service.UPLOAD_URL,
            storage_state=storage_state,
            cookies=cookies,
            timeout_ms=60000,
            referer=poi_service.UPLOAD_URL,
            label=f"shop_pull:{task_id[:8]}",
        ) as session:
            # 3a. 翻页拉取
            for page in range(1, MAX_PAGES_PER_ROUND + 1):
                if info and info.cancel_requested:
                    raise_for_cancel(info)

                # 进度更新（PR3 #56：模板统一）
                if info:
                    info.progress = (
                        f"拉取门店第 {page}/{MAX_PAGES_PER_ROUND} 页，"
                        f"新增 {new_count}，更新 {update_count}，跳过 {skip_count}，"
                        f"用时 {_fmt_hms(time.time() - start_ts)}"
                    )

                try:
                    # 搜索（带 a_bogus 签名，复用会话 page）
                    poi_list = poi_service.search_poi_in_session(
                        session, keyword=keyword, page=page,
                    )
                except Exception as exc:  # noqa: BLE001 单页失败视情况而定
                    logger.warning("[选品] task={} page={} 搜索失败: {}", task_id, page, exc)
                    if page == 1:
                        # 首页就失败 → 整个任务失败（账号 / 风控问题）
                        # 直接 raise 让 task_service 把状态标为 "failed"，日志已写入
                        _write_log(task_id, 0, 0, 0, f"搜索首页失败: {exc}")
                        raise
                    # 后续页失败视为结束
                    break

                if not poi_list:
                    break
                pages_done = page

                # 4. 处理每条 POI（全部门店入库，不按 is_cps 硬过滤——#505：门店可能佣金状态变动）
                page_new = 0
                page_update = 0
                page_skip = 0   # PR3 #56：本页拦截（正则/poi_id 缺失）数
                for poi in poi_list:
                    if info and info.cancel_requested:
                        raise_for_cancel(info)

                    poi_id = poi.get("poi_id", "")
                    if not poi_id:
                        page_skip += 1
                        continue

                    # 正则过滤（任务 #407）：店名 / 城市任一不匹配则跳过
                    if shop_re:
                        poi_name = poi.get("poi_name") or ""
                        if not shop_re.search(poi_name):
                            page_skip += 1
                            continue
                    if city_re:
                        poi_city = (poi.get("address_info") or {}).get("city") or ""
                        if not city_re.search(poi_city):
                            page_skip += 1
                            continue

                    # 拉详情（is_cps=1 的门店走 cps/detail/v2；非 CPS 门店跳过详情，只入库基础字段）
                    detail = None
                    if poi.get("is_cps"):
                        try:
                            detail = poi_service.fetch_poi_detail_in_session(
                                session, poi_id=poi_id,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("[选品] task={} poi={} 详情失败: {}（继续入库基础字段）",
                                           task_id, poi_id, exc)

                    shop_dict = poi_service.merge_poi_to_shop_dict(poi, detail)

                    try:
                        action, _ = _upsert_shop(shop_dict)
                    except Exception as exc:  # noqa: BLE001 单条 POI 写入失败不阻塞其他
                        logger.error("[选品] task={} poi={} 入库失败: {}", task_id, poi_id, exc)
                        continue

                    if action == "new":
                        page_new += 1
                    else:
                        page_update += 1

                new_count += page_new
                update_count += page_update
                skip_count += page_skip
                # 任务 #63：每页完成日志（运维按日志复盘翻页节奏）
                logger.info(
                    "[选品] task={} page={} 入库+{} 更新+{} 拦截-{}",
                    task_id[:8], page, page_new, page_update, page_skip,
                )

                # has_next 判断（poi_list 长度 < 12 表示最后一页）
                if len(poi_list) < 12:
                    # 不是 MAX_PAGES_PER_ROUND 触发的早退，而是抖音接口返回了最后一页
                    if not fail_reason:
                        fail_reason = (
                            f"搜索返回未满一页（{len(poi_list)} < 12），"
                            f"本轮已无更多数据（共 {pages_done} 页）"
                        )
                    break
    except Exception as exc:  # noqa: BLE001
        # session 创建失败（首次 goto / context 新建失败）→ 整任务失败
        logger.error("[选品] task={} 浏览器会话失败: {}", task_id, exc)
        _write_log(task_id, 0, 0, 0, f"浏览器会话失败: {exc}")
        raise

    # PR3 #56：实时即终态，循环内的 info.progress 即为终态（删除"完成 X 页..."覆写）

    # 写日志包 try 兜底（DB 写异常不应再抛到外层把 worker 状态打 failed）
    try:
        _write_log(task_id, pages_done, new_count, update_count, fail_reason)
    except Exception as exc:
        logger.error("[选品] task={} 写日志失败: {}", task_id, exc)
    # 任务 #63：拉取结束汇总（按 status 直接对应状态机返回值）
    elapsed = _fmt_hms(time.time() - start_ts)
    if fail_reason:
        logger.info(
            "[选品] task={} 结束，{} 页 / 入库+{} 更新+{} 拦截-{} 用时 {} reason={}",
            task_id[:8], pages_done, new_count, update_count, skip_count,
            elapsed, fail_reason,
        )
        # partial 时把失败原因挂到 info.message，前端信息列可见（避免仅显示"部分成功"）
        if info:
            info.message = f"部分失败：{fail_reason}"
        return "partial"
    logger.info(
        "[选品] task={} 结束，{} 页 / 入库+{} 更新+{} 拦截-{} 用时 {}",
        task_id[:8], pages_done, new_count, update_count, skip_count, elapsed,
    )
    return "success"


# ============ 辅助函数 ============

def _mark_account_invalid(account_id: str, reason: str) -> None:
    """账号 storage 缺失/损坏时标记为 invalid（DB 与 FS 漂移自动恢复）。

    不抛异常：选品任务是辅助场景，账号失效不该连带把整任务标 failed。
    不影响前端 /accounts/available：available 用 FS JOIN DB，会自动过滤掉。
    """
    try:
        d = get_db()
        d.update_by_id("account", account_id, {"status": "invalid"})
        logger.warning("[选品] 账号 {} 标记 invalid：{}", account_id, reason)
        notifier.notify("warn", "selection",
                        f"账号 storage 缺失，自动标记失效",
                        f"{account_id[:8]}：{reason}",
                        action=f"relogin:{account_id}")
    except Exception as exc:
        logger.error("[选品] 标记账号失效失败 account={}: {}", account_id, exc)


def _get_shop_columns() -> set[str]:
    """读 shop 表实际列名集合（用于 merge 字典防御性裁列）。

    SELECT PRAGMA table_info 在每轮每条 POI 都跑代价高，缓存到模块级。
    表 schema 变更后需重启进程生效（v33 migration 重建后会刷新缓存）。
    """
    global _SHOP_COLS_CACHE
    if _SHOP_COLS_CACHE is None:
        db = get_db()
        _SHOP_COLS_CACHE = {r['name'] for r in db.query_all('PRAGMA table_info(shop)')}
    return _SHOP_COLS_CACHE


_SHOP_COLS_CACHE: set[str] | None = None


def _upsert_shop(shop: dict) -> tuple[str, str]:
    """门店增量入库（按 poi_id 幂等）。

    三层更新策略（保护历史数据）：
    1. base_fields：search 接口的基础字段（名称、分类、坐标等）—— 总是更新；
    2. metric_fields：销售/佣金指标（is_cps/spu_count/total_gmv 等）—— 仅在非 None 时写入；
    3. detail_fields：cps/detail/v2 详情维度（take_rate_*/platform_*/total_commission 等）——
       仅当 detail_fetched=1 时写入（避免被 None 覆盖历史详情）。

    返回:
        (action, shop_id) —— action ∈ {"new", "update"}
    """
    db = get_db()
    poi_id = shop.get("poi_id", "")
    if not poi_id:
        raise ValueError("poi_id 不能为空")

    # 防御性裁列：merge 函数输出可能含 shop 表不存在的列（旧版本残留 / 后续扩展未对齐）。
    # 只保留表实际列名拼 SQL，避免 "no such column" 崩溃整轮。
    valid_cols = _get_shop_columns()
    shop = {k: v for k, v in shop.items() if k in valid_cols}

    existing = db.query_one("SELECT id FROM shop WHERE poi_id=?", (poi_id,))

    # === 1. base 字段（总是更新）===
    base_fields = {col: shop.get(col) for col in _SHOP_BASE_COLS if col in shop}

    # === 1b. v36 search 接口总返回字段（l1/l2/l3_name，总是写入）===
    search_always = {col: shop.get(col) for col in _SEARCH_ALWAYS_COLS if col in shop}

    # === 1c. v36 search 接口可能不返回字段（仅在非 None 时写入，避免覆盖旧值）===
    search_optional = {
        col: shop.get(col) for col in _SEARCH_OPTIONAL_COLS
        if col in shop and shop.get(col) is not None
    }

    # === 2. metric 字段（仅在非 None 时更新，保护历史 NULL 状态）===
    metric_fields = {}
    for k in ("commission_rate", "total_sold", "total_gmv", "total_commission"):
        v = shop.get(k)
        if v is not None:
            metric_fields[k] = v

    # === 3. detail 字段（仅 detail_fetched=1 时更新）===
    detail_fields = {}
    if shop.get("detail_fetched"):
        for col in _SHOP_DETAIL_COLS:
            v = shop.get(col)
            if v is not None:
                detail_fields[col] = v
        detail_fields["detail_fetched"] = 1
        detail_fields["detail_updated_time"] = now_str()

    updates: dict = {}
    updates.update(base_fields)
    updates.update(search_always)
    updates.update(search_optional)
    updates.update(metric_fields)
    updates.update(detail_fields)

    # === 4. NOT NULL 兜底（防止 None 写入被拒）===
    for k, default in _NOT_NULL_DEFAULTS.items():
        if k not in updates or updates[k] is None:
            updates[k] = default

    if existing:
        fill_common_fields(updates, is_insert=False)
        db.update_by_id("shop", existing["id"], updates)
        return "update", existing["id"]

    # 新增
    record = {"id": new_id(), "poi_id": poi_id}
    record.update(updates)
    fill_common_fields(record, is_insert=True)
    db.insert("shop", record)
    return "new", record["id"]


def _write_log(task_id: str, pages_done: int, new_count: int,
               update_count: int, fail_reason: str | None) -> None:
    """写执行日志（每轮一行）。"""
    db = get_db()
    db.insert("shop_pull_log", {
        "id": new_id(),
        "task_id": task_id,
        "run_time": now_str(),
        "pages_done": pages_done,
        "new_count": new_count,
        "update_count": update_count,
        "fail_reason": fail_reason,
        "create_time": now_str(),
    })


# ============ 门店查询 ============

def list_shops(
    keyword: str | None = None,
    category: str | None = None,
    city: str | None = None,
    province: str | None = None,
    # 三态可选：None=全部 / True=仅 true / False=仅 false（API 同步契约）
    is_cps_only: bool | None = None,
    is_added_only: bool | None = None,
    spu_count_min: int | None = None,
    spu_count_max: int | None = None,
    total_gmv_min: float | None = None,
    total_gmv_max: float | None = None,
    take_rate_min: int | None = None,
    take_rate_max: int | None = None,
    commission_min: float | None = None,
    commission_max: float | None = None,
    sort: str = "total_gmv",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """门店查询（多维筛选 + 分页）。"""
    db = get_db()
    where: list[str] = ["deleted=0"]
    params: list = []

    if keyword:
        where.append("name LIKE ?")
        params.append(f"%{keyword}%")
    if category:
        # category 参数为一级分类中文名（如"美食"），
        # 直接查 v36 入库的 l1_name 列（等价于 category_full 起始段）
        where.append("l1_name = ?")
        params.append(category)
    if city:
        where.append("city = ?")
        params.append(city)
    if province:
        where.append("province = ?")
        params.append(province)
    if is_cps_only is True:
        where.append("is_cps = 1")
    elif is_cps_only is False:
        where.append("is_cps != 1")
    if is_added_only is True:
        # #414：仅返回「已加库」门店（用户在选品中心 toggle-added 标记过的）
        where.append("is_added_to_library = 1")
    elif is_added_only is False:
        where.append("is_added_to_library != 1")
    if spu_count_min is not None:
        where.append("spu_count >= ?")
        params.append(spu_count_min)
    if spu_count_max is not None:
        where.append("spu_count <= ?")
        params.append(spu_count_max)
    if total_gmv_min is not None:
        where.append("total_gmv >= ?")
        params.append(total_gmv_min)
    if total_gmv_max is not None:
        where.append("total_gmv <= ?")
        params.append(total_gmv_max)
    if take_rate_min is not None:
        where.append("take_rate_min >= ?")
        params.append(take_rate_min)
    if take_rate_max is not None:
        where.append("take_rate_max <= ?")
        params.append(take_rate_max)
    if commission_min is not None:
        where.append("commission_rate >= ?")
        params.append(commission_min)
    if commission_max is not None:
        where.append("commission_rate <= ?")
        params.append(commission_max)

    where_sql = " AND ".join(where)

    # 排序（NULLS LAST 让空值排到末尾）
    sort_col = sort if sort in _SHOP_SORT_COLS else "total_gmv"
    # 名称 / 城市 类文本字段默认升序，其余降序
    sort_dir = "ASC" if sort_col in ("name", "city") else "DESC"

    total = db.query_one(f"SELECT COUNT(*) AS c FROM shop WHERE {where_sql}",
                         tuple(params))["c"]

    offset = (page - 1) * page_size
    rows = db.query_all(
        f"SELECT * FROM shop WHERE {where_sql} "
        f"ORDER BY {sort_col} {sort_dir} LIMIT ? OFFSET ?",
        tuple(params) + (page_size, offset),
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "list": [_serialize_shop(r) for r in rows],
    }


def list_cities() -> list[str]:
    """已入库城市列表（distinct，用于前端筛选下拉）。"""
    db = get_db()
    rows = db.query_all(
        "SELECT DISTINCT city FROM shop "
        "WHERE deleted=0 AND city IS NOT NULL AND city != '' "
        "ORDER BY city")
    return [r["city"] for r in rows if r["city"]]


def list_provinces() -> list[str]:
    """已入库省份列表（distinct）。"""
    db = get_db()
    rows = db.query_all(
        "SELECT DISTINCT province FROM shop "
        "WHERE deleted=0 AND province IS NOT NULL AND province != '' "
        "ORDER BY province")
    return [r["province"] for r in rows if r["province"]]


def list_categories() -> list[str]:
    """已入库一级分类列表（中文名，distinct）。

    直接从 v36 新增的 l1_name 列查（语义等价于 category_full 第一段，
    但建索引后可走索引扫描，不依赖字符串切分）。
    """
    db = get_db()
    rows = db.query_all(
        "SELECT DISTINCT l1_name FROM shop "
        "WHERE deleted=0 AND l1_name IS NOT NULL AND l1_name != '' "
        "ORDER BY l1_name")
    return [r["l1_name"] for r in rows if r["l1_name"]]


def _serialize_shop(row: dict) -> dict:
    """DB 行 → API 返回 dict（剔除 deleted / 内部字段）。"""
    out = dict(row)
    out.pop("deleted", None)
    return out


# ============ 加库标记 ============

def set_shop_added(shop_id: str, added: bool) -> dict:
    """切换门店加库标记。

    返回 {"shop_id": ..., "is_added_to_library": 0|1, "added_time": ...|null}
    """
    db = get_db()
    row = db.query_one("SELECT id FROM shop WHERE id=? AND deleted=0", (shop_id,))
    if not row:
        raise ValueError("门店不存在")
    new_val = 1 if added else 0
    updates = {
        "is_added_to_library": new_val,
        "added_time": now_str() if added else None,
        "update_time": now_str(),
    }
    db.update_by_id("shop", shop_id, updates)
    logger.info("[选品] 门店 {} 加库标记 -> {}", shop_id[:8], "已加库" if added else "取消")
    return {"shop_id": shop_id, "is_added_to_library": new_val, "added_time": updates["added_time"]}


# ============ 日志 ============

def list_logs(task_id: str, page: int = 1, page_size: int = 50) -> dict:
    """执行日志列表（按 run_time 倒序）。"""
    db = get_db()
    total = db.query_one(
        "SELECT COUNT(*) AS c FROM shop_pull_log WHERE task_id=?",
        (task_id,))["c"]
    offset = (page - 1) * page_size
    rows = db.query_all(
        "SELECT * FROM shop_pull_log WHERE task_id=? "
        "ORDER BY run_time DESC LIMIT ? OFFSET ?",
        (task_id, page_size, offset),
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "list": [dict(r) for r in rows],
    }


def list_all_logs(page: int = 1, page_size: int = 50) -> dict:
    """全部门店拉取执行记录（JOIN 任务名），按 run_time 倒序。

    用于"任务执行记录"页签的"门店拉取记录"子页签（任务 #407）。
    """
    db = get_db()
    total = db.query_one(
        "SELECT COUNT(*) AS c FROM shop_pull_log")["c"]
    offset = (page - 1) * page_size
    rows = db.query_all(
        "SELECT l.id, l.task_id, l.run_time, l.pages_done, "
        "       l.new_count, l.update_count, l.fail_reason, "
        "       l.create_time, t.task_name "
        "FROM shop_pull_log l "
        "LEFT JOIN shop_pull_task t ON t.id = l.task_id AND t.deleted = 0 "
        "ORDER BY l.run_time DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "list": [dict(r) for r in rows],
    }


def delete_logs(log_ids: list[str]) -> dict:
    """批量软删门店拉取执行记录（任务 #408：与素材拉取记录批量删除对齐）。

    返回 {"deleted": int, "missing": [id, ...]}：deleted 实际删除数；
    missing 是没找到的 ID（前端可忽略）。
    """
    db = get_db()
    deleted = 0
    missing: list[str] = []
    # shop_pull_log 表无 deleted 列，硬删即可（执行日志是临时数据，重跑会重新写入）
    for log_id in log_ids:
        row = db.query_one("SELECT id FROM shop_pull_log WHERE id=?", (log_id,))
        if not row:
            missing.append(log_id)
            continue
        db.execute("DELETE FROM shop_pull_log WHERE id=?", (log_id,))
        deleted += 1
    return {"deleted": deleted, "missing": missing}


# ============ 调度器注册 ============

def register_shop_pull(task_row: dict) -> None:
    """APScheduler 注册回调（main.py 启动时 + create_task / toggle / update 后调用）。"""
    task_id = task_row["id"]
    config_json = task_row["interval_config"]
    register_interval_job(
        job_id=f"shop_pull:{task_id}",
        config_json=config_json,
        func=lambda: _scheduled_trigger(task_id),
        replace=True,
    )


def _scheduled_trigger(task_id: str) -> None:
    """APScheduler 定时回调（投递到 task_service 队列，不阻塞调度线程）。

    PR2 #55：type 改为 shop_pull；name 直接取 task_row.task_name（不拼接"定时/手动"后缀）。
    """
    row = get_db().query_one("SELECT task_name FROM shop_pull_task WHERE id=? AND deleted=0", (task_id,))
    name = row["task_name"] if row else "未命名任务"
    task_service.submit(
        task_type="shop_pull",
        name=name,
        func=lambda info: _run_one_round(task_id, info),
    )