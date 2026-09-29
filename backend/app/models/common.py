# -*- coding: utf-8 -*-
"""通用 Pydantic 模型：分页结果与通用请求。"""

from typing import Annotated

from pydantic import BaseModel, Field


class PageResult(BaseModel):
    """分页查询结果。

    字段名沿用 `list`（与前端 `r.list` 一致）。
    Pydantic 2.9 不允许字段名与类型注解同名（list=int? 解析冲突），用 Annotated 把类型从字段签名里摘出来。
    """
    total: Annotated[int, Field(description="总条数")]
    page: Annotated[int, Field(description="当前页码")]
    page_size: Annotated[int, Field(description="每页条数")]
    list: Annotated[list, Field(default_factory=lambda: [], description="记录列表")]


class OkResponse(BaseModel):
    """通用操作成功响应。"""
    ok: bool = Field(default=True, description="是否成功")
    message: str = Field(default="", description="说明")
