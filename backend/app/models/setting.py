# -*- coding: utf-8 -*-
"""系统设置模块请求/响应模型（F-08）。"""

from pydantic import BaseModel, Field


class SettingsUpdateRequest(BaseModel):
    """配置修改（合并保存）。"""
    account_check_hours: int | None = Field(default=None, ge=1, le=24)
    log_retention_days: int | None = Field(default=None, ge=7, le=90)
    temp_retention_days: int | None = Field(default=None, ge=1, le=90)
    # 任务 #369/v22：新增字段必须显式声明,否则 pydantic model_dump 会丢弃
    pull_base_pages: int | None = Field(default=None, ge=10, le=500,
                                         description="v22 阶梯翻页：第 1 轮最大页数（10~500）")
    # #191：浏览器显示开关（#410 调试）—— SettingsUpdateRequest 漏声明此字段，
    # 前端 PUT { browser_show_window: false } 被 model_dump 静默丢弃，save_settings({})
    # 不写入 settings.json，API 仍返 200，toast '已保存并生效' 误导用户。
    # 修法：显式声明 bool 字段（Optional 兼容 PUT 部分更新）。
    browser_show_window: bool | None = Field(default=None,
                                              description="#410 调试：显示浏览器窗口（登录窗固定显示不受此开关影响）")


class BackupRequest(BaseModel):
    """备份请求。"""
    target_path: str = Field(description="备份 zip 输出路径")
    include_material: bool = Field(default=False, description="是否包含素材文件")
    include_finished: bool = Field(default=False, description="是否包含成品文件")


class RestoreRequest(BaseModel):
    """恢复请求。"""
    zip_path: str = Field(description="备份 zip 路径")


class RiskConfirmRequest(BaseModel):
    """首次启动风险告知确认。"""
    confirmed: bool = Field(description="是否确认知晓并承担平台规则风险")


class DataDirMigrateRequest(BaseModel):
    """数据目录迁移请求。"""
    new_dir: str = Field(description="新数据目录绝对路径")
