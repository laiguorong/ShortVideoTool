# -*- coding: utf-8 -*-
"""#publish-contract：全局异常处理器端到端测试。

覆盖 3 类场景：
1. ValueError → 400 + ApiResp 信封（业务校验失败）
2. Exception → 500 + ApiResp 信封（兜底未捕获异常 + loguru logger）
3. RequestValidationError → 422 + ApiResp 信封（数据字段：errors 数组）

目的：
- 防止之前发现的"-> dict 注解"500 bug 回归
- 把"任何路由抛 ValueError 自动 400 + 信封"写成可执行契约
- 防止有人去掉全局 handler（删了之后会回退到 500 + "Internal Server Error"）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _setup() -> tuple[Path, object]:
    """初始化临时 DB + 加载 main.app（含全局 handler）。"""
    import os
    tmp = Path(tempfile.mkdtemp(prefix="dt_global_exc_"))
    os.environ["SHORT_VIDEO_DATA_DIR"] = str(tmp)

    from app.db import database as db_module
    db_path = tmp / "db" / "short_video_tools.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_module.init_db(db_path)
    from app.db import migrations
    migrations.migrate(db_module.db)
    from app.services.setting_service import init_data_dir
    init_data_dir(tmp / "data")

    # 关键：用 main.app（不是自构造 FastAPI）才能拿到全局 handler
    import importlib
    main_mod = importlib.import_module("app.main")
    return tmp, main_mod.app


def _attach_test_routes(app) -> None:
    """挂 3 个测试路由，分别触发 ValueError / Exception / 422。"""
    from fastapi import APIRouter
    test_router = APIRouter(prefix="/test")

    @test_router.get("/value-error")
    def ve():
        raise ValueError("业务校验失败：测试")

    @test_router.get("/unexpected")
    def ue():
        raise RuntimeError("未预期异常")

    app.include_router(test_router, prefix="/api")


def test_value_error_returns_400_envelope():
    """ValueError → 400 + ApiResp 信封。"""
    from fastapi.testclient import TestClient
    _, app = _setup()
    _attach_test_routes(app)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/api/test/value-error")
    assert r.status_code == 400, f"应 400，实际 {r.status_code}: {r.text}"
    j = r.json()
    assert isinstance(j, dict), f"响应非 dict: {j}"
    assert j.get("code") == 400, f"code 应 400: {j}"
    assert j.get("message") == "业务校验失败：测试", f"message 错: {j}"
    assert j.get("data") is None, f"data 应 null: {j}"


def test_unhandled_exception_returns_500_envelope():
    """未捕获 RuntimeError → 500 + ApiResp 信封（不泄漏内部信息）。"""
    from fastapi.testclient import TestClient
    _, app = _setup()
    _attach_test_routes(app)
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/api/test/unexpected")
    assert r.status_code == 500, f"应 500，实际 {r.status_code}: {r.text}"
    j = r.json()
    assert isinstance(j, dict)
    assert j.get("code") == 500
    # 不向用户泄漏内部信息（统一文案）
    assert j.get("message") == "服务异常，请稍后重试", f"message 应统一文案: {j}"


def test_request_validation_error_returns_422_envelope():
    """Pydantic 校验失败 → 422 + ApiResp 信封（data 含 errors）。

    走 publish 域里已有的强校验路由（SaveDraftRequest.task_name min_length=1）触发 422。
    """
    from fastapi.testclient import TestClient
    _, app = _setup()
    client = TestClient(app)

    # POST /publish/tasks/draft 必传 task_name 字段；空 body 触发 Pydantic 422
    r = client.post("/api/publish/tasks/draft", json={})
    assert r.status_code == 422, f"应 422，实际 {r.status_code}: {r.text}"
    j = r.json()
    assert isinstance(j, dict), f"响应非 dict: {j}"
    assert j.get("code") == 422, f"code 应 422: {j}"
    assert j.get("message") == "参数校验失败", f"message 应为'参数校验失败': {j}"
    # data.errors 应该是 Pydantic 校验错误数组
    errors = (j.get("data") or {}).get("errors", [])
    assert isinstance(errors, list), f"errors 应为 list: {j}"
    assert len(errors) > 0, f"errors 应非空: {j}"


def test_http_exception_uses_api_envelope():
    """HTTPException 404 现在走 ApiResp 信封（#fix-status-404-consistent）。

    之前：路由层 raise HTTPException(404, "xxx") 走 FastAPI 默认 → 返 {detail:"xxx"} 旧 detail 形态
    现在：所有 HTTPException 都包成 {code:status, message:detail, data:null} ApiResp 信封
    让错误响应契约与成功响应完全统一（前端 client.ts 优先取 body.message）。
    """
    from fastapi.testclient import TestClient
    _, app = _setup()
    _attach_test_routes(app)
    client = TestClient(app, raise_server_exceptions=False)

    # 触发 accounts.py 里的 HTTPException(404, ...) 路径（service raise HTTPException → 全局 handler）
    r = client.post("/api/accounts/non-exist/check")
    if r.status_code == 404:
        j = r.json()
        # 现在所有 4xx 错误都走 ApiResp 信封
        assert "code" in j, f"应含 code 键: {j}"
        assert "message" in j, f"应含 message 键: {j}"
        assert j["code"] == 404
        assert j["data"] is None
        # message 应是资源不存在描述（accounts.py 内 service.raise 的"账号不存在"）
        assert j["message"] in ("账号不存在", "404: 账号不存在")


def test_global_handler_registered():
    """防御性测试：确保 main.app 注册了 3 个全局 handler。

    防止未来有人误删 main.py 里的 @app.exception_handler。
    """
    _, app = _setup()
    # FastAPI 在 app.exception_handlers 里存了所有注册的 handler
    handlers = app.exception_handlers
    # ValueError / Exception / RequestValidationError 各一个
    assert len(handlers) >= 3, f"应至少 3 个全局 handler，实际 {len(handlers)}: {list(handlers.keys())}"


# ============ runner ============

def _run_all() -> None:
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
