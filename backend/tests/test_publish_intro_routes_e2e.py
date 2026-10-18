# -*- coding: utf-8 -*-
"""#publish-contract：intro 路由端到端响应形状测试。

目标：每个 intro 路由的成功响应必须含 `code/message/data` 信封（与 publish 域一致）。
背景：上一轮路由合并时，intro 路由的 `ok()` 包装在改动中加上了，但前端 publishIntroApi
没同步加 `.data!` 解包，导致 13 处调用静默失败/运行时崩——因为没有 HTTP 端到端断言。

本测试用 fastapi.testclient.TestClient 直发 HTTP 请求，断言：
1. 响应 JSON 含 `code` / `message` / `data` 三个键
2. `code == 0`（成功）
3. `data` 是预期业务类型

不依赖 main.py 的副作用（不 import main.py，不触发 ProactorEventLoop patch / uvicorn patch / on_startup），
自己构造一个最小 FastAPI app，仅挂 api_router。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 在 import 任何 app.* 之前先初始化临时 DB（get_db() 第一次调用会读 db_module.db）
import os  # noqa: E402

_tmp_data = Path(tempfile.mkdtemp(prefix="dt_intro_routes_"))
os.environ["SHORT_VIDEO_DATA_DIR"] = str(_tmp_data)  # 防止某些 fallback 读用户目录

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import api_router  # noqa: E402
from app.db import database as db_module  # noqa: E402
from app.db import migrations  # noqa: E402


# ============ 测试 app 构造（最小化，避开 main.py 副作用） ============

_app = FastAPI(title="intro-routes-test")
_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
_app.include_router(api_router, prefix="/api")
client = TestClient(_app)


def _fresh_db() -> None:
    """初始化临时 DB + 完整 migrate + fixture。

    每个测试**独立子目录 + 独立 db_path**——必须这样做，因为 db_module 是全局单例，
    上一个测试的连接如果还指向同一文件，新 init_db() 会立刻 `database is locked`。
    """
    test_tmp = Path(tempfile.mkdtemp(prefix="dt_intro_route_"))
    db_path = test_tmp / "db" / "short_video_tools.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_module.init_db(db_path)
    v1_ddl = next(d for v, d in migrations.MIGRATIONS if v == 1)
    db_module.db.executescript(v1_ddl)
    db_module.db.set_user_version(33)
    for fn in ("_apply_v34", "_apply_v35", "_apply_v36", "_apply_v37", "_apply_v38"):
        getattr(migrations, fn)(db_module.db)
    db_module.db.set_user_version(38)
    migrations.migrate(db_module.db)

    # fixture：1 个项目 + 1 个门店（满足 #444 过滤条件）
    db_module.db.insert("project", {
        "id": "p_e2e", "title": "E2E项目", "status": "normal",
        "create_time": "2026-10-18 00:00:00", "update_time": "2026-10-18 00:00:00",
    })
    db_module.db.insert("shop", {
        "id": "s_e2e", "poi_id": "poi_e2e", "name": "E2E门店",
        "is_added_to_library": 1, "is_cps": 1,
        "create_time": "2026-10-18 00:00:00", "update_time": "2026-10-18 00:00:00",
    })


def _assert_envelope(resp_json: dict, *, expected_data_type: type | None = None) -> None:
    """统一断言：响应含 code/message/data 信封，code==0，data 类型正确。"""
    assert "code" in resp_json, f"响应缺 code 键：{resp_json}"
    assert "message" in resp_json, f"响应缺 message 键：{resp_json}"
    assert "data" in resp_json, f"响应缺 data 键：{resp_json}"
    assert resp_json["code"] == 0, f"code 应为 0，实际 {resp_json['code']}: {resp_json}"
    if expected_data_type is not None:
        assert isinstance(resp_json["data"], expected_data_type), (
            f"data 类型应为 {expected_data_type.__name__}，实际 {type(resp_json['data']).__name__}: {resp_json['data']!r}"
        )


# ============ 用例：每个 intro 路由都断言信封形状 ============

def test_get_intros_envelope() -> None:
    _fresh_db()
    r = client.get("/api/publish/intro/projects/p_e2e/intros")
    assert r.status_code == 200, r.text
    _assert_envelope(r.json(), expected_data_type=list)


def test_create_intro_envelope() -> None:
    _fresh_db()
    r = client.post("/api/publish/intro/intros", json={
        "project_id": "p_e2e", "content": "测试简介", "topics": ["#美食"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_envelope(body, expected_data_type=dict)
    assert "id" in body["data"], f"创建响应应含 id 字段：{body}"


def test_update_intro_envelope() -> None:
    _fresh_db()
    # 先创建一个再更新
    create = client.post("/api/publish/intro/intros", json={
        "project_id": "p_e2e", "content": "旧", "topics": [],
    })
    intro_id = create.json()["data"]["id"]
    r = client.patch(f"/api/publish/intro/intros/{intro_id}", json={"content": "新"})
    assert r.status_code == 200, r.text
    _assert_envelope(r.json(), expected_data_type=dict)
    assert "updated" in r.json()["data"]


def test_delete_intro_envelope() -> None:
    _fresh_db()
    create = client.post("/api/publish/intro/intros", json={
        "project_id": "p_e2e", "content": "待删", "topics": [],
    })
    intro_id = create.json()["data"]["id"]
    r = client.delete(f"/api/publish/intro/intros/{intro_id}")
    assert r.status_code == 200, r.text
    _assert_envelope(r.json(), expected_data_type=dict)
    assert "deleted" in r.json()["data"]


def test_set_project_intros_envelope() -> None:
    _fresh_db()
    r = client.put("/api/publish/intro/projects/p_e2e/intros",
                   json={"items": ["标题1 #话题1", "标题2 #话题2"]})
    assert r.status_code == 200, r.text
    _assert_envelope(r.json(), expected_data_type=dict)
    assert "saved" in r.json()["data"]


def test_get_project_shops_envelope() -> None:
    _fresh_db()
    r = client.get("/api/publish/intro/projects/p_e2e/shops")
    assert r.status_code == 200, r.text
    _assert_envelope(r.json(), expected_data_type=list)


def test_set_project_shops_envelope() -> None:
    _fresh_db()
    r = client.put("/api/publish/intro/projects/p_e2e/shops",
                   json={"shop_ids": ["s_e2e"]})
    assert r.status_code == 200, r.text
    _assert_envelope(r.json(), expected_data_type=dict)
    assert "bound" in r.json()["data"]


def test_list_projects_with_shops_envelope() -> None:
    _fresh_db()
    r = client.get("/api/publish/intro/projects-with-shops")
    assert r.status_code == 200, r.text
    _assert_envelope(r.json(), expected_data_type=list)


def test_set_project_status_envelope() -> None:
    _fresh_db()
    r = client.put("/api/publish/intro/projects/p_e2e/status",
                   json={"status": "disabled"})
    assert r.status_code == 200, r.text
    _assert_envelope(r.json(), expected_data_type=dict)
    assert r.json()["data"].get("status") == "disabled"


def test_list_publishable_projects_envelope() -> None:
    _fresh_db()
    r = client.get("/api/publish/intro/projects-publishable")
    assert r.status_code == 200, r.text
    _assert_envelope(r.json(), expected_data_type=list)


# ============ 防回归：响应绝对不能是裸数据 ============

def test_intro_routes_never_return_bare_data() -> None:
    """核心防回归断言：success 响应必含 data 键（不允许返回 list/dict 裸数据）。

    背景：上一轮路由合并时曾因去 ok() 包装而出现裸返回，导致前端 13 处调用静默失败。
    本测试用「必含 data 键」做最低成本断言，任何路由忘了包信封都会被立刻抓到。
    """
    _fresh_db()
    routes = [
        ("GET", "/api/publish/intro/projects/p_e2e/intros", None),
        ("GET", "/api/publish/intro/projects/p_e2e/shops", None),
        ("GET", "/api/publish/intro/projects-with-shops", None),
        ("GET", "/api/publish/intro/projects-publishable", None),
    ]
    for method, path, body in routes:
        if method == "GET":
            r = client.get(path)
        else:
            r = client.post(path, json=body or {})
        assert r.status_code == 200, f"{method} {path} status={r.status_code}: {r.text}"
        j = r.json()
        assert isinstance(j, dict), f"{method} {path} 响应非 dict: {type(j).__name__}"
        assert "data" in j, f"{method} {path} 响应缺 data 键（疑似裸返回！）: {j}"
        assert "code" in j and j["code"] == 0, (
            f"{method} {path} 响应 code 异常: {j}"
        )


# ============ runner ============

def _run_all() -> None:
    import inspect
    funcs = [
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    funcs.sort(key=lambda x: x[0])
    passed = 0
    failed = 0
    for name, fn in funcs:
        try:
            fn()
            passed += 1
            print(f"  [OK] {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed (total {len(funcs)})")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    _run_all()
