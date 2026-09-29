# -*- coding: utf-8 -*-
"""创作中心请求模型（F-04）。"""

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    """创建项目。"""
    title: str = Field(min_length=1, max_length=50)
    # #427：项目画幅，默认 1080x1920 (9:16 竖屏)
    width: int | None = Field(default=1080, ge=1)
    height: int | None = Field(default=1920, ge=1)
    # 画面层显模式（默认 cover），项目内可改
    fit_mode: str | None = Field(default="cover")


class UpdateProjectRequest(BaseModel):
    """更新项目配置。"""
    title: str | None = None
    bgm_strategy: str | None = Field(default=None, description="order / random")
    dedup_rules: dict | None = Field(default=None, description="抽帧/水印/干扰开关与强度")
    output_resolution: str | None = Field(default=None, description="如 1080x1920")
    # #96：项目级"保留原视频声音"开关（None=不变；True=保留；False=不保留）
    keep_original_audio: bool | None = None
    # 项目级 BGM 音量比例（None=不变；范围 0.05~1.0，生成时应用到混音）
    bgm_volume: float | None = Field(default=None, ge=0.05, le=1.0)
    # #多画面适配：cover/contain/fill/blur_bg
    fit_mode: str | None = Field(default=None,
                                  description="cover=铺满裁剪 / contain=居中补黑 / fill=拉伸 / blur_bg=模糊背景+前景居中")
    # #427：项目画幅（width/height 与 output_resolution 二选一；width/height 优先）
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class CopyProjectRequest(BaseModel):
    """复制项目。"""
    new_title: str


class DeleteProjectRequest(BaseModel):
    """删除项目。"""
    delete_files: bool = Field(default=False, description="是否一并删除成品文件")


class AddShotRequest(BaseModel):
    """添加分镜。"""
    name: str = Field(default="")
    after_shot_id: str | None = Field(default=None, description="在该分镜后插入（#62）")
    before_shot_id: str | None = Field(default=None, description="在该分镜前插入（#67）")


class RenameShotRequest(BaseModel):
    """分镜重命名。"""
    name: str


class MoveShotRequest(BaseModel):
    """分镜上移/下移。"""
    direction: str = Field(description="up / down")


class AddClipsRequest(BaseModel):
    """批量添加片段（含切割配置，F-04.5 / #56）。"""
    project_id: str = Field(description="目标项目（空分镜时按首视频段数自动建分镜）")
    material_ids: list[str] = Field(min_length=1)
    cut_mode: str = Field(default="whole", description="whole / fixed / scene / trim_after")
    fixed_seconds: float = Field(default=10, description="固定切割秒数")
    trim_head_s: float = Field(default=0)
    trim_tail_s: float = Field(default=0)
    min_clip_seconds: float = Field(default=3)
    target_shot_ids: list[str] | None = Field(default=None, description="已废弃（#56 改为自动按分镜序分配），保留兼容")


class MoveClipRequest(BaseModel):
    """片段移动。"""
    target_shot_id: str


class ReorderClipsRequest(BaseModel):
    """分镜内片段重排（#56 拖拽换位）。"""
    clip_ids: list[str] = Field(min_length=1, description="目标顺序的片段 ID 列表")


class BatchClipsRequest(BaseModel):
    """片段批量操作（#57 分镜管理模式）。"""
    clip_ids: list[str] = Field(min_length=1)
    action: str = Field(description="delete / mirror / move / retry_render")
    target_shot_id: str | None = Field(default=None, description="action=move 时必填")


class DeleteByMaterialsRequest(BaseModel):
    """按素材批量删除项目内片段（#399）。"""
    material_ids: list[str] = Field(min_length=1, description="要移除的素材 ID 列表")


class AddBgmsRequest(BaseModel):
    """批量添加 BGM。"""
    material_ids: list[str] = Field(min_length=1)


class RemoveBgmRequest(BaseModel):
    """移除 BGM。"""
    material_id: str


class AddTextPoolRequest(BaseModel):
    """批量添加文案池条目。"""
    contents: list[str] = Field(min_length=1, description="每行一条")
    scope: str = Field(default="global", description="global / project")
    project_id: str | None = None


class CreateGenerateTaskRequest(BaseModel):
    """创建生成任务。"""
    count: int = Field(default=10, ge=1, le=1000, description="本次生成条数")


class DeleteVideoRequest(BaseModel):
    """删除成品。"""
    keep_file: bool = Field(default=False)
