# -*- coding: utf-8 -*-
"""发布与记录模块请求模型（#publish-contract 全面标准化）。

设计原则（与前端 api/publish.ts 一一对应）：
- 向导只发「对象数组」形态（project_snapshots / account_snapshots / shops / intros），
  不再有 projects / account_ids / account_labels 等 id-only 平行数组（消除双形态契约破裂）。
- intro_topics 全程 list[str]（模型层语义统一；落库 JSON 字符串由 service 内部处理）。
- 时间字段入参宽松接受 `yyyy-MM-dd HH:mm` 与 `yyyy-MM-dd HH:mm:ss`，归一为 `HH:mm:ss`。
- 三类向导请求共用 DraftPayload 基类：SaveDraftRequest(含 task_id) / PreviewRequest /
  ConfirmRequest（无 task_id），语义清晰、不再共用一个既当草稿又当直传的 DraftRequest。
"""

from pydantic import BaseModel, Field, field_validator

from app.models.common import normalize_naive_datetime


class ShopDraftEntry(BaseModel):
    """向导中一个门店的完整快照（id + name + city + poi_id）。"""

    id: str
    name: str = ""
    city: str = ""
    poi_id: str = ""


class IntroDraftEntry(BaseModel):
    """向导中一个简介（文本输入模式）。id 前端生成 uuid（不入 video_intro 表）。"""

    id: str
    content: str
    topics: list[str] = Field(default_factory=list)


class ProjectDraftSnapshot(BaseModel):
    """向导中一个项目的完整快照：含项目元数据 + 门店列表 + 简介列表。"""

    id: str
    title: str = ""
    shops: list[ShopDraftEntry] = Field(default_factory=list, description="勾选顺序")
    intros: list[IntroDraftEntry] = Field(default_factory=list, description="勾选顺序；文本输入模式")
    # wizard step 4 选中的 'list' | 'manual' 模式
    introMode: str = Field(default="list", description="'list' | 'manual'；wizard step 4 当前模式")


class AccountDraftSnapshot(BaseModel):
    """向导中一个账号的完整快照：含 id + nickname + remark。"""

    id: str
    nickname: str | None = None
    remark: str | None = None


class DailyLimitEntry(BaseModel):
    """分账号每天上限 + 算模式（#596）。"""

    account_id: str
    # 账号昵称/备注快照（仅日志/快照用）
    account_label: str = Field(default="", description="账号显示名快照（日志用）")
    limit: int = Field(ge=1, le=75)
    # 分账号下每个账号可独立选固定/均衡 + 两个独立间隔字段（切 mode 不影响值）
    schedule_mode: str = Field(default="balanced", description="'fixed' | 'balanced'，仅 per_account 生效")
    # 两个独立字段——切 mode 时另一字段保留旧值（与全局行为对齐）
    fixed_interval_min: int = Field(default=10, ge=1, description="固定间隔（分钟），fixed 模式使用")
    balanced_step_min: int = Field(default=60, ge=1, description="均衡间隔（步长，分钟），balanced 模式使用")


class VideoDirEntry(BaseModel):
    """成品视频目录项（一个目录 = 一个项目）。"""

    id: str
    abs_path: str
    dir_name: str = ""
    video_count: int = 0


class DraftPayload(BaseModel):
    """向导统一请求载荷（save_draft / preview-direct / confirm-direct 共用）。"""

    task_name: str = Field(min_length=1, max_length=50)
    # 新形态（唯一权威）：对象数组
    project_snapshots: list[ProjectDraftSnapshot] = Field(default_factory=list, description="项目对象数组")
    account_snapshots: list[AccountDraftSnapshot] = Field(default_factory=list, description="账号对象数组")
    # 分账号上限
    daily_limit_mode: str = Field(default="global", description="global / per_account")
    daily_limit_global: int = Field(default=75, ge=1, le=75)
    daily_limit_per_account: list[DailyLimitEntry] = Field(default_factory=list)
    # 时间窗（宽松归一为 yyyy-MM-dd HH:mm:ss）
    start_time: str | None = Field(default=None, description="yyyy-MM-dd HH:mm[:ss]；空=默认明天 07:00")
    end_time: str | None = Field(default=None, description="yyyy-MM-dd HH:mm[:ss]；空=默认明天 22:00")
    # 计算模式 + 间隔字段（fixed / balanced）
    schedule_mode: str | None = Field(default=None, description="'fixed' | 'balanced'；空=默认 balanced")
    fixed_interval_min: int | None = Field(default=None, ge=1, description="schedule_mode='fixed' 时使用")
    balanced_step_min: int | None = Field(default=None, ge=1, description="schedule_mode='balanced' 步长")
    # 自主声明 / 允许下载
    declaration: str = Field(default="无需添加自主声明")
    allow_download: bool = Field(default=False)
    # 项目来源
    project_source: str = Field(default="project", description="'project' | 'video_dir'")
    # 成品视频目录（仅 video_dir 模式）
    video_dirs: list[VideoDirEntry] = Field(default_factory=list)
    # 手动输入门店（仅 video_dir 模式），key=video_dir.id
    manual_shops: dict[str, list[ShopDraftEntry]] = Field(default_factory=dict)
    # 手动输入视频简介（仅 video_dir 模式），key=video_dir.id
    manual_intros: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("start_time", "end_time")
    @classmethod
    def _normalize_time(cls, v: str | None) -> str | None:
        return normalize_naive_datetime(v)


class SaveDraftRequest(DraftPayload):
    """保存草稿（向导第 8 步前任意时机）。传 task_id 则更新已有 draft。"""

    task_id: str | None = Field(default=None, description="已有草稿 ID；传入则更新而非新建")


class PreviewRequest(DraftPayload):
    """直传 payload 预览（不入库；无 task_id）。"""


class ConfirmRequest(DraftPayload):
    """直传 payload 确认（一次性入库 → running；无 task_id）。"""


class ScanVideoDirRequest(BaseModel):
    abs_path: str


class ToggleTaskRequest(BaseModel):
    """任务暂停/恢复。"""

    paused: bool


class ExportRecordsRequest(BaseModel):
    """导出发布记录。"""

    out_path: str = Field(description="导出文件路径（.xlsx）")
    account_id: str = ""
    shop_id: str = ""
    project_id: str = ""
    start_time: str = ""
    end_time: str = ""
