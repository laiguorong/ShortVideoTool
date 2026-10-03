# -*- coding: utf-8 -*-
"""选品中心请求/响应模型（重写 #449）。

设计原则：
- 任务配置扁平化为 keyword + cities 两个字段（不再是 conditions_json 嵌套）；
- 后端硬编码仅 CPS 门店拉详情，前端不暴露 fetch_detail / is_cps_only 开关；
- 门店字段按 POI 真实接口（search/poi + cps/detail/v2）整合的字段集组织；
- 素材 / 视频拉取的模型（VideoPullConditions 等）保留不动。
"""

from pydantic import BaseModel, Field


# ============ 通用 ============

class IntervalConfig(BaseModel):
    """任务执行间隔配置（定时调度使用）。"""
    type: str = Field(description="minute / hour / daily")
    value: int | None = Field(default=None, description="间隔数值（minute/hour 必填，最小 10 分钟）")
    time: str | None = Field(default=None, description="每日执行时刻 HH:mm（daily 必填）")


# ============ 任务模型 ============

class CreateShopPullTaskRequest(BaseModel):
    """创建选品拉取任务。"""
    task_name: str = Field(min_length=1, max_length=50, description="任务名称")
    keyword: str = Field(min_length=1, max_length=50, description="搜索关键词（如 '黑眼熊寿司'）")
    cities: list[str] = Field(default_factory=list, description="城市列表，空=不限；后端仅保留 POI 城市在此列表内的门店")
    account_id: str = Field(description="拉取账号 ID（必须已登录）")
    interval: IntervalConfig = Field(description="执行间隔")
    # v37：正则过滤（任务 #407）。空=不过滤。
    shop_name_pattern: str = Field(default="", description="店名正则（如 ^.*?火锅.*?$），空=不过滤")
    city_pattern: str = Field(default="", description="城市正则（如 ^.*?北京.*?$），空=不过滤")


class UpdateShopPullTaskRequest(BaseModel):
    """编辑任务（全字段可选，仅更新传入项）。"""
    task_name: str | None = Field(default=None, min_length=1, max_length=50)
    keyword: str | None = Field(default=None, min_length=1, max_length=50)
    cities: list[str] | None = None
    account_id: str | None = None
    interval: IntervalConfig | None = None
    shop_name_pattern: str | None = None
    city_pattern: str | None = None


class ToggleRequest(BaseModel):
    """任务启停。"""
    enabled: bool


class ShopPullTaskRead(BaseModel):
    """任务读取（响应模型）。"""
    id: str
    task_name: str
    keyword: str
    cities: list[str]
    account_id: str
    interval: IntervalConfig
    status: str
    last_run_time: str | None = None
    next_run_time: str | None = None
    create_time: str
    update_time: str


class ShopPullLogRead(BaseModel):
    """执行日志读取。"""
    id: str
    task_id: str
    run_time: str
    pages_done: int
    new_count: int
    update_count: int
    fail_reason: str | None = None
    create_time: str


# ============ 门店模型 ============

class ShopRead(BaseModel):
    """门店详情（响应）。"""
    id: str
    poi_id: str
    name: str

    # 地理
    province: str | None = None
    city: str | None = None
    district: str | None = None
    ad_code: str | None = None
    address: str | None = None
    lat_gcj02: float | None = None
    lng_gcj02: float | None = None

    # 分类
    category: str | None = None
    category_full: str | None = None

    # CPS
    is_cps: int = 0

    # 佣金 / 比率
    commission_rate: float | None = None
    take_rate_min: int | None = None
    take_rate_max: int | None = None
    take_rate_avg: float | None = None

    # 销量 / GMV
    total_sold: int | None = None
    total_gmv: float | None = None
    total_commission: float | None = None

    # 商品
    spu_count: int = 0
    cps_spu_count: int = 0
    delivery_spu_count: int = 0
    spu_type_groupon: int = 0
    spu_type_delivery: int = 0

    # 平台
    platform_name: str | None = None
    platform_source: int | None = None

    # TOP SPU
    top_spu_name: str | None = None
    top_spu_sold: int | None = None

    # 详情
    detail_fetched: int = 0
    detail_updated_time: str | None = None

    # 通用
    create_time: str
    update_time: str


# ============ 素材 / 视频拉取相关（保留不动）============

class VideoPullConditions(BaseModel):
    """视频拉取条件。

    设计要点：
    - 字段扁平全列，便于 JSON 序列化与版本兼容；新增字段保持向后兼容。
    - `keyword` 为抖音搜索接口的主搜索词；`title_regex` 用于搜索结果的标题二次过滤。
    - 发布时间档位（`publish_range`）替代旧版 datetime 起止输入（`publish_after/publish_before` 字段
      保留但已废弃，仅用于兼容老 JSON 数据，前端不再展示）。
    - 时长档位（`duration_range`）/ 最大入库数量（`max_count`）/ BGM 开关（`fetch_bgm`）
      / 字幕过滤（`filter_subtitle`）/ 主播人脸过滤（`filter_face`）为本次新增。
    - v120 移除的话题/描述关键词/发布位置字段保留在 schema 中标 deprecated，
      仅用于兼容老 JSON 数据，前端不再展示。
    - v127 新增 `sort_type`：抖音服务端排序方式（0 综合 / 1 最多点赞 / 2 最新发布），
      拉取场景推荐 2（最新发布），与"拉新素材"诉求一致。
    """
    # 搜索词与内容关键词
    keyword: str = Field(default="", description="抖音搜索主关键词")
    title_regex: str = Field(default="", description="标题正则（对搜索结果标题二次过滤，如：探店|测评）")
    # 业务属性
    shop_name: str = Field(default="", description="门店名称关键词")
    orientation: str = Field(default="", description="画面方向 vertical / horizontal / 空=不限")
    # 排序方式（v127）：服务端下推 filter_selected.sort_type
    # 默认 2=最新发布，与拉取新素材的诉求一致（前端不展示此项）
    sort_type: int = Field(
        default=2,
        ge=0, le=2,
        description="排序方式 0=综合 / 1=最多点赞 / 2=最新发布（默认 2）",
    )
    # 时间筛选档位（替代旧的 datetime 起止输入）
    publish_range: str = Field(
        default="any",
        description="发布时间档位 any/1d/7d/180d；any=不限，1d=一天内，7d=一周内，180d=半年内",
    )
    # 时长筛选档位
    duration_range: str = Field(
        default="any",
        description="视频时长档位 any/lt1m/1to5m/gt5m；any=不限，lt1m=1分钟以下，1to5m=1-5分钟，gt5m=5分钟以上",
    )
    # 行为开关与上限
    fetch_bgm: bool = Field(default=False, description="是否拉取视频背景音乐（原声）入音乐库")
    filter_subtitle: bool = Field(default=False, description="是否过滤带字幕的视频（按 OCR 检测画面文字判断）")
    filter_face: bool = Field(default=False, description="是否过滤含主播人脸的视频（按 OpenCV 人脸检测判断）")
    max_count: int = Field(default=100, ge=1, le=1000, description="单轮最大入库数量，达到上限后任务自动停用")
    # 兼容字段：仅用于兼容老 JSON 数据，前端不再写入与展示
    publish_after: str | None = Field(default=None, description="[已废弃] 发布时间起，旧版字段")
    publish_before: str | None = Field(default=None, description="[已废弃] 发布时间止，旧版字段")
    topics: str = Field(default="", description="[已废弃] 话题，v120 移除")
    desc_keywords: str = Field(default="", description="[已废弃] 描述关键词，v120 移除")
    location: str = Field(default="", description="[已废弃] 发布位置，v120 移除")


class CreateVideoPullTaskRequest(BaseModel):
    """创建视频拉取任务。"""
    task_name: str = Field(min_length=1)
    conditions: VideoPullConditions = Field(default_factory=VideoPullConditions)
    account_id: str = Field(description="拉取账号 ID")
    category_id: str = Field(description="入库分类（视频类型）")
    interval: IntervalConfig


class UpdateVideoPullTaskRequest(BaseModel):
    """编辑视频拉取任务。"""
    task_name: str | None = None
    conditions: VideoPullConditions | None = None
    account_id: str | None = None
    category_id: str | None = None
    interval: IntervalConfig | None = None


class CreateCategoryRequest(BaseModel):
    """新增素材分类。"""
    name: str = Field(min_length=1, max_length=30)
    type: str = Field(description="video / music")
    parent_id: str = Field(default="", description="父分类 ID，空=顶级")


class RenameCategoryRequest(BaseModel):
    """重命名分类。"""
    name: str


class MoveCategoryRequest(BaseModel):
    """移动分类。"""
    new_parent_id: str = Field(default="")
    sort_order: int = Field(default=0)


class DeleteCategoryRequest(BaseModel):
    """删除分类。"""
    strategy: str = Field(default="to_parent", description="to_parent=素材移入父分类")


class ImportShareRequest(BaseModel):
    """分享链接批量导入。

    type 由前端页签决定（视频页签传 video，音乐页签传 music），
    与 category_id 解耦——未分类下也能按 type 选入库类型。
    """
    share_texts: list[str] = Field(min_length=1, description="分享文本列表")
    category_id: str = Field(
        description="入库分类（UNCATEGORIZED_ID = '-' 表示虚拟未分类）")
    type: str = Field(description="入库类型 video/music（由页签决定）")


class RetryShareRequest(BaseModel):
    """分享导入重试请求（#fix-type-param：retry 也需 type，原任务可能不再一致）。

    type 可选：未传时从 share_import_task 表读（v43+ 存了 type）；
    显式传值可覆盖（兼容老 API 调用方）。
    """
    type: str | None = Field(default=None, description="入库类型 video/music（可选；None 时从 task 表读）")


class UploadFilesRequest(BaseModel):
    """本地文件上传（路径由 Electron 对话框选择）。"""
    file_paths: list[str] = Field(min_length=1)
    category_id: str


class UpdateMaterialRequest(BaseModel):
    """素材编辑。"""
    title: str | None = None
    category_id: str | None = None


class RelocateMaterialRequest(BaseModel):
    """素材重新定位。"""
    new_abs_path: str


class DeleteMaterialRequest(BaseModel):
    """素材删除。"""
    keep_file: bool = Field(default=False, description="True=仅删记录保留文件")