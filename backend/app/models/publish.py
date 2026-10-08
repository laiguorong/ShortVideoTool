# -*- coding: utf-8 -*-
"""发布与记录模块请求模型（F-05 / F-06 + #413）。"""

from pydantic import BaseModel, Field


class CreatePublishTaskRequest(BaseModel):
    """创建发布任务（向后兼容旧接口；新版向导走 DraftRequest）。"""
    task_name: str = Field(min_length=1)
    project_ids: list[str] = Field(min_length=1, description="项目列表")
    account_ids: list[str] = Field(min_length=1, description="账号列表（须正常状态）")
    shop_ids: list[str] = Field(min_length=1, description="门店列表")
    per_account_count: int = Field(ge=1, le=50, description="每账号发布数量")
    start_time: str = Field(description="开始时间 yyyy-MM-dd HH:mm:ss")
    interval_minutes: int = Field(ge=5, description="同账号发布间隔（分钟）")
    shop_assign_mode: str = Field(default="round", description="round 轮流 / random 随机")


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


# ---------- #413 向导模型 ----------

class ShopDraftEntry(BaseModel):
    """#450 向导中一个门店的完整快照（id + name + city + poi_id）。"""
    id: str
    name: str = ""
    city: str = ""
    poi_id: str = ""


class IntroDraftEntry(BaseModel):
    """#450 向导中一个简介。
    id 是前端生成的 uuid（不入 video_intro 表）；content 为多行文本框输入；topics 可选。"""
    id: str
    content: str
    topics: list[str] = Field(default_factory=list)


class ProjectDraftSnapshot(BaseModel):
    """#450 向导中一个项目的完整快照：含项目元数据 + 门店列表 + 简介列表。"""
    id: str
    title: str = ""
    shops: list[ShopDraftEntry] = Field(default_factory=list, description="勾选顺序")
    intros: list[IntroDraftEntry] = Field(default_factory=list, description="勾选顺序；为文本输入模式")


class AccountDraftSnapshot(BaseModel):
    """#450 向导中一个账号的完整快照：含 id + nickname + remark。"""
    id: str
    nickname: str | None = None
    remark: str | None = None


class ProjectDraftEntry(BaseModel):
    """向导中一个项目的配置（勾选顺序）。"""
    project_id: str
    # #449：带上项目名称，后端 preview-direct / confirm-direct 写日志/快照用；空字符串也 OK
    project_title: str = Field(default="", description="项目名称快照（仅日志/快照，不参与校验）")
    shop_ids: list[str] = Field(default_factory=list, description="门店顺序；空=回查 project_shop")
    # #449：与 shop_ids 同长度 + 同序，存名称/城市/POI 快照
    shop_names: list[str] = Field(default_factory=list, description="门店名称快照，与 shop_ids 顺序一致")
    shop_cities: list[str] = Field(default_factory=list, description="门店城市快照，与 shop_ids 顺序一致")
    shop_poi_ids: list[str] = Field(default_factory=list, description="门店 POI 快照，与 shop_ids 顺序一致")
    intro_ids: list[str] = Field(default_factory=list, description="简介顺序；空=回查全部")
    # #449：与 intro_ids 同长度 + 同序，存内容预览/话题
    intro_contents: list[str] = Field(default_factory=list, description="简介内容预览（截前 60 字），与 intro_ids 顺序一致")
    intro_topics: list[str] = Field(default_factory=list, description="简介话题 JSON 数组字符串（用于快照），与 intro_ids 顺序一致")


class DailyLimitEntry(BaseModel):
    """分账号每天上限 + 该账号算模式（#596）。"""
    account_id: str
    # #449：账号昵称/备注快照（与 account_id 同序）
    account_label: str = Field(default="", description="账号显示名快照（nickname/remark），仅日志用")
    limit: int = Field(ge=1, le=75)
    # #596：分账号下，每个账号可独立选固定/均衡 + 两个独立间隔字段（切 mode 不影响值）
    schedule_mode: str = Field(default="balanced", description="'fixed' | 'balanced'，仅 daily_limit_mode='per_account' 时生效")
    # #596：两个独立字段——切 mode 时另一字段保留旧值（与全局行为对齐）
    fixed_interval_min: int = Field(default=10, ge=1, description="固定间隔（分钟），仅 daily_limit_mode='per_account' + mode='fixed' 时使用")
    balanced_step_min: int = Field(default=60, ge=1, description="均衡间隔（步长，分钟），仅 daily_limit_mode='per_account' + mode='balanced' 时使用")


# #418：成品视频目录项（一个目录 = 一个项目）
class VideoDirEntry(BaseModel):
    """#418：成品视频目录项（一个目录 = 一个项目）。

    id: 前端派生 stable id（wizard 期间唯一，用于手动门店按 id 索引）
    abs_path: 目录绝对路径（用户本地任意路径）
    dir_name: 显示用目录名（= Path(abs_path).name）
    video_count: 目录下合规视频数（仅展示用，实际视频扫描在 confirm 时实时跑）
    """
    id: str
    abs_path: str
    dir_name: str = ""
    video_count: int = 0


class DraftRequest(BaseModel):
    """保存草稿（向导第 8 步前任意时机）。"""
    task_name: str = Field(min_length=1, max_length=50)
    # #450：新形态——直接传完整对象数组（账号 + 项目 + 项目内嵌门店 + 项目内嵌简介文本）
    # 后端解析时优先读 snapshots；缺则 fallback 到 projects/accounts 旧形态
    project_snapshots: list[ProjectDraftSnapshot] = Field(default_factory=list, description="#450 项目对象数组（每项含 shops + intros）")
    account_snapshots: list[AccountDraftSnapshot] = Field(default_factory=list, description="#450 账号对象数组")
    # 旧形态（id-only）保留兼容
    projects: list[ProjectDraftEntry] = Field(default_factory=list)
    account_ids: list[str] = Field(default_factory=list)

    # #450：双形态二选一即可（校验在 service 层 _normalize_payload 做）
    # Pydantic 不再卡 min_length，避免新形态走不通
    # #449：与 account_ids 同序的账号显示名（nickname 或 remark），日志用
    account_labels: list[str] = Field(default_factory=list, description="账号显示名快照，与 account_ids 顺序一致")
    daily_limit_mode: str = Field(default="global", description="global / per_account")
    daily_limit_global: int = Field(default=75, ge=1, le=75, description="#420 默认值 10 → 75")
    daily_limit_per_account: list[DailyLimitEntry] = Field(default_factory=list)
    start_time: str | None = Field(default=None, description="yyyy-MM-dd HH:mm:ss；空=默认明天 07:00")
    end_time: str | None = Field(default=None, description="yyyy-MM-dd HH:mm:ss；空=默认明天 22:00")
    # #calc_mode：计算模式 + 间隔字段（fixed / balanced）
    schedule_mode: str | None = Field(default=None, description="'fixed' | 'balanced'；空=默认 balanced")
    fixed_interval_min: int | None = Field(default=None, ge=1, description="schedule_mode='fixed' 时使用；空=默认 10")
    balanced_step_min: int | None = Field(default=None, ge=1, description="schedule_mode='balanced' 时使用（步长）；空=默认 60")
    # 旧字段保留向后兼容（旧 wizard 仍可能带；后端 fallback 链会把 same → balanced_step_min）
    same_project_interval_min: int = Field(default=60, ge=1)
    diff_project_interval_min: int = Field(default=10, ge=1)
    declaration: str = Field(default="无需添加自主声明", description="#417/#419 自主声明文本内容；前端默认选「无需添加自主声明」")
    allow_download: bool = Field(default=False)
    # R12 修：传 task_id 时改为 UPDATE 旧 draft；不传则 INSERT 新 draft
    task_id: str | None = Field(default=None, description="已有草稿 ID；传入则更新而非新建")

    # #418：项目来源——'project'（项目列表，默认）/ 'video_dir'（成品视频目录）
    project_source: str = Field(default="project", description="'project' | 'video_dir'")
    # #418：成品视频目录列表（仅 video_dir 模式有效；每个目录视为一个项目）
    video_dirs: list["VideoDirEntry"] = Field(default_factory=list, description="#418 成品视频目录（仅 video_dir 模式）")
    # #418：手动输入门店——按 video_dir.id 索引；每目录可独立挂载门店
    manual_shops: dict[str, list[ShopDraftEntry]] = Field(
        default_factory=dict,
        description="#418 手动输入门店（仅 video_dir 模式），key=video_dir.id"
    )
    # #视频简介-按项目一一对应：手动输入视频简介（每目录独立；排期时按各自目录的视频数轮转）
    manual_intros: dict[str, list[str]] = Field(
        default_factory=dict,
        description="手动输入视频简介（仅 video_dir 模式），key=video_dir.id，value=该目录下的简介数组",
    )


# 触发 forward-reference 重建（video_dirs 用字符串前向引用 VideoDirEntry）
DraftRequest.model_rebuild()
