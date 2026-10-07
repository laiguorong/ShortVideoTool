# -*- coding: utf-8 -*-
"""启动检查 API（前端启动页配套）。

端点：
- GET  /api/startup/state         拉所有步骤当前状态（页面刷新/恢复用）
- POST /api/startup/check/{key}   执行某步；已 ok 返缓存，其他重跑
- POST /api/startup/data-dir      设置数据目录（步骤 2 用户选完路径调，自动 reset 下游）

路径契约：router prefix="/startup" 走 api_router 转发，main.py 挂 /api 前缀
后实际路径是 /api/startup/*。前端 request() 拼 baseUrl 时不要再加 /api 前缀
（baseUrl 已含），路径直接写 /startup/... 即可。两端任一改路径会立即 404
（不再是静默路径错位），便于排查。
"""

import os
import sys as _sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import startup_state
from app.services.setting_service import init_data_dir, save_settings

router = APIRouter(prefix="/startup", tags=["startup"])


@router.get("/state", summary="拉取所有启动检查步骤的当前状态")
def get_state() -> dict:
    """前端页面刷新 / 重新挂载时拉取，避免每步都跑一次。"""
    return {key: result.to_dict() for key, result in startup_state.get_all().items()}


@router.post("/check/{key}", summary="执行某步启动检查")
def check_step(key: str) -> dict:
    """执行 startup_state.check(key)。

    - 已 ok：返缓存（禁止重复调用）
    - pending/running/failed：调 runner 重跑
    - 未注册：返 failed
    """
    result = startup_state.check(key)
    return result.to_dict()


class SetDataDirBody(BaseModel):
    """设置数据目录请求体。"""

    path: str


# Windows 系统保护目录（拒绝写入，避免误操作）
# 路径已用 os.path.normcase 标准化（小写 + 正斜杠转反斜杠），比较时无需再 lower()
_WINDOWS_PROTECTED = (
    os.path.normcase("C:/Windows"),
    os.path.normcase("C:/Windows/System32"),
    os.path.normcase("C:/Program Files"),
    os.path.normcase("C:/Program Files (x86)"),
    os.path.normcase("C:/ProgramData"),
    os.path.normcase("C:/$Recycle.Bin"),
    os.path.normcase("C:/Recovery"),
)


def _validate_data_dir_path(p: Path) -> str | None:
    """轻量路径校验：拒绝非绝对路径、Windows 系统目录（含变体）。

    返回 None = 通过；返 str = 错误描述（用户可读）。
    磁盘空间 / 写权限检查由 init_data_dir 内部 mkdir 阶段触发。

    已知限制（不防御）：
    - 8.3 短路径（C:\\PROGRA~1）：Python Path.resolve() 不展开 8.3 短名，
      需要 win32api.GetLongPathNameW。实际场景用户极少使用 8.3 短名手工敲路径，
      不是真实安全威胁（用户仅创建数据目录，不执行程序）。
    """
    try:
        if not p.is_absolute():
            return "路径必须是绝对路径"
        if _sys.platform == "win32":
            # normcase：转小写 + 反斜杠（Windows 不区分大小写）
            # normpath：合并重复分隔符 + 解析 .. + 去掉末尾分隔符
            # rstrip(" .")：防 Windows 目录流攻击（"C:\\Windows.\\foo" 实际等价 "C:\\Windows\\foo"）
            # 末尾再补一次 normpath 收尾：处理 "C:\\Windows.\\System32" 这种尾部点号残留
            resolved = os.path.normpath(os.path.normcase(os.path.normpath(str(p))).rstrip(" ."))
            for protected in _WINDOWS_PROTECTED:
                if resolved == protected or resolved.startswith(protected + "\\"):
                    return f"禁止使用系统目录: {protected}"
        return None
    except OSError as exc:
        return f"路径无效: {exc}"


def _rollback_settings_data_dir(old_value: str | None) -> None:
    """init_data_dir 失败时回滚 settings.json 中刚写的 data_dir 字段。

    避免 settings.json 已写新路径 + DATA_DIR 仍为旧值的脏状态：
    下次启动 resolve_default_data_dir 读到 recorded 但目录不存在 → 兜底到默认盘 +
    覆盖 settings.json → 用户上次选的路径被悄悄替换为默认。
    回滚后下次启动仍能从 userData JSON 读默认目录。
    """
    try:
        if old_value is None:
            # 原 settings.json 没 data_dir 字段 → 删掉我们刚写的
            save_settings({"data_dir": ""})  # 设为空串，配合 load_settings 行为
        else:
            save_settings({"data_dir": old_value})
    except Exception:  # noqa: BLE001 回滚失败不阻断主错误流
        pass


@router.post("/data-dir", summary="设置数据目录（步骤 2 用户选完路径调）")
def set_data_dir(body: SetDataDirBody) -> dict:
    """初始化新数据目录 + 持久化到 settings.json + 清下游步骤缓存。

    步骤 2 用户在启动页选完路径 → 前端调此端点 → 前端再 POST /api/startup/check/data_dir。
    下游缓存（database/settings/ffmpeg/playwright/scheduler）全部清空，因 DATA_DIR 已变。

    调用顺序：reset → save_settings → init_data_dir
    - reset 先清缓存（清 ok/failed；pending 不变），失败时保留旧 cache
    - save_settings 先写 settings.json 持久化用户选择（即便后续 init 失败，重启时 settings.json
      已记录用户意图，可重新尝试）
    - init_data_dir 最后调（mkdir + DATA_DIR 赋值）。init_data_dir 内部失败时不改 DATA_DIR
      （保持旧值），避免后续 check 走错路径
    """
    path_str = (body.path or "").strip()
    if not path_str:
        raise HTTPException(status_code=400, detail="路径不能为空")

    p = Path(path_str)
    err = _validate_data_dir_path(p)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # 先 reset（不影响 init_data_dir / save_settings，reset 只清 startup_state）
    startup_state.reset()

    # 先写 settings.json 持久化用户选择；即便 init 失败，回滚后重启仍可恢复
    try:
        # 记下旧值用于回滚
        old_settings = save_settings.__wrapped__ if hasattr(save_settings, "__wrapped__") else None  # noqa
        from app.services.setting_service import load_settings
        old_data_dir = load_settings().get("data_dir")
    except Exception:
        old_data_dir = None
    try:
        save_settings({"data_dir": str(p)})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"保存配置失败: {exc}") from exc

    # 最后 init_data_dir（mkdir + DATA_DIR 赋值）。失败时回滚 settings.json + 不改 DATA_DIR
    try:
        init_data_dir(p)
    except RuntimeError as exc:
        # 回滚 settings.json 中刚写的 data_dir 字段（保留旧值或删字段）
        _rollback_settings_data_dir(old_data_dir)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _rollback_settings_data_dir(old_data_dir)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    return {"ok": True, "data_dir": str(p)}
