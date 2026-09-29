# -*- coding: utf-8 -*-
"""#data-dir-choice 兜底行为单测：resolve_default_data_dir 三种路径。

- 未记录 → 兜底到最大盘 + 写 settings.json
- 已记录 + exists → 沿用
- 已记录 + 不 exists → 兜底到最大盘 + 覆盖 settings.json（log warn）

复刻 _use_temp_config() 模式重定向配置目录，避开真实 backend/config/。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import setting_service


def _use_temp_config():
    """重定向配置目录到临时目录，避开真实 backend/config/settings.json。"""
    tmp = Path(tempfile.mkdtemp(prefix="dt_setting_"))
    config_dir = tmp / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    setting_service.get_config_dir = lambda: config_dir
    setting_service.get_settings_path = lambda: config_dir / "settings.json"
    return config_dir


def _write_data_dir(config_dir: Path, value: str | None) -> None:
    """写入 settings.json，可指定 data_dir 值；None = 不写（保持缺失）。"""
    path = config_dir / "settings.json"
    if value is None:
        # 删除文件模拟"未记录"
        if path.exists():
            path.unlink()
        return
    import json
    path.write_text(json.dumps({"data_dir": value}, ensure_ascii=False), encoding="utf-8")


def _read_data_dir(config_dir: Path) -> str | None:
    """从 settings.json 读 data_dir；文件不存在返 None。"""
    path = config_dir / "settings.json"
    if not path.exists():
        return None
    import json
    return json.loads(path.read_text(encoding="utf-8")).get("data_dir")


def test_no_recorded_falls_back_to_largest_disk():
    """未记录 → 兜底 pick_largest_disk + 写 settings.json。
    注：resolve_default_data_dir 只返回 Path，不创建目录（init_data_dir 才建）。
    """
    config_dir = _use_temp_config()
    _write_data_dir(config_dir, None)  # 模拟首次启动无配置文件

    result = setting_service.resolve_default_data_dir()
    # 兜底返回 Path（不抛错）
    assert isinstance(result, Path), f"应返 Path，实际 {type(result).__name__}"
    # 已写入 settings.json
    recorded = _read_data_dir(config_dir)
    assert recorded is not None, "兜底后应写入 settings.json"
    assert recorded == str(result), f"settings.json 期望 {result}，实际 {recorded}"


def test_recorded_exists_keeps_value():
    """已记录 + 路径存在 + 非污染特征 → 沿用，不覆盖 settings.json。"""
    config_dir = _use_temp_config()
    # 用 user_keep_ 前缀（不在 dt_/test_/tmp 污染名单）
    tmp = Path(tempfile.mkdtemp(prefix="user_keep_"))
    _write_data_dir(config_dir, str(tmp))

    result = setting_service.resolve_default_data_dir()
    assert result == tmp, f"应沿用原路径 {tmp}，实际 {result}"
    # settings.json 内容不变
    assert _read_data_dir(config_dir) == str(tmp), "settings.json 应保持不变"


def test_recorded_unreachable_falls_back_and_overwrites():
    """已记录 + 路径不存在 → 兜底 + 覆盖 settings.json（不抛错）。"""
    config_dir = _use_temp_config()
    nonexistent = str(Path(tempfile.gettempdir()) / "dt_does_not_exist_xyz_12345" / "data")
    _write_data_dir(config_dir, nonexistent)

    result = setting_service.resolve_default_data_dir()
    # 兜底到最大盘（不是 nonexistent）
    assert isinstance(result, Path), f"应返 Path，实际 {type(result).__name__}"
    assert str(result) != nonexistent, "应替换为兜底路径"
    # settings.json 已被覆盖
    recorded = _read_data_dir(config_dir)
    assert recorded == str(result), f"settings.json 应被覆盖为 {result}，实际 {recorded}"
    # 注意：原路径从 settings.json 中消失（已知行为；用户已通过 log.warn 知情）


def test_recorded_in_temp_dir_is_treated_as_pollution():
    """#data-dir-defense：recorded 在 Temp 下且父目录以 dt_/test_/tmp 开头 → 即使存在也走兜底。

    早期 init_data_dir 显式参数会污染 settings.json::data_dir 残留；现撤回 save_settings
    但历史污染需要兜底防御。"""
    config_dir = _use_temp_config()
    # 模拟"测试 fixture 残留"：真实创建 Temp/dt_pollution_test_xxx/data/ 目录
    polluted_tmp = Path(tempfile.mkdtemp(prefix="dt_pollution_test_"))
    polluted_data = polluted_tmp / "data"
    polluted_data.mkdir(parents=True, exist_ok=True)
    _write_data_dir(config_dir, str(polluted_data))

    result = setting_service.resolve_default_data_dir()
    # 不应沿用 polluted_data
    assert result != polluted_data, f"Temp 残留应被识别为污染，实际沿用 {result}"
    # 应兜底到最大盘 + 覆盖 settings.json
    recorded = _read_data_dir(config_dir)
    assert recorded == str(result), f"settings.json 应被覆盖为 {result}，实际 {recorded}"
    # 清理污染残留（避免跑测试时累积）
    import shutil
    shutil.rmtree(polluted_tmp, ignore_errors=True)


def test_recorded_in_temp_dir_but_not_pollution_prefix_keeps():
    """Temp 目录下但父目录不是 dt_/test_/tmp 前缀 → 不当作污染（用户在 Temp 手动建的合法目录）。"""
    config_dir = _use_temp_config()
    user_tmp = Path(tempfile.mkdtemp(prefix="userdata_"))  # 前缀 userdata_ 不在污染名单
    user_data = user_tmp / "data"
    user_data.mkdir(parents=True, exist_ok=True)
    _write_data_dir(config_dir, str(user_data))

    result = setting_service.resolve_default_data_dir()
    # 应沿用 user_data（不在污染前缀名单）
    assert result == user_data, f"非污染前缀的 Temp 目录应沿用，实际 {result}"
    import shutil
    shutil.rmtree(user_tmp, ignore_errors=True)