# -*- coding: utf-8 -*-
"""发布中心统一出入参基础类型（#publish-contract 全面标准化）。

约定：
- 所有「成功响应」统一包 `ApiResp[data=...]` 信封（code=0 成功）。
- 业务/校验错误仍走 HTTPException（4xx/5xx），由前端 client 按 status 分类，
  不在此处信封化（避免改动前端 client.ts 影响其余模块）。
- 时间字段 I/O 统一 `yyyy-MM-dd HH:mm:ss`；入参宽松接受 `HH:mm`（自动补 :00）。
"""

from datetime import datetime
from typing import Annotated, Generic, Optional, TypeVar

from fastapi import Query
from pydantic import BaseModel

T = TypeVar("T")


class ApiResp(BaseModel, Generic[T]):
    """统一响应信封。code=0 表示成功；非 0 由前端按业务码处理。"""

    code: int = 0
    message: str = "ok"
    data: Optional[T] = None


def ok(data: T = None, message: str = "ok") -> "ApiResp[T]":
    """成功响应构造。"""
    return ApiResp(code=0, message=message, data=data)


class PageQuery(BaseModel):
    """列表查询统一分页参数（#publish-contract）。作为 query 参数展开：page / page_size。

    #fix-pagequery-query-annotation：字段必须加 Query() 注解，否则 FastAPI 把 Pydantic model
    默认从 body 解析 → GET 请求无 body → 422 'Field required'。
    """
    page: Annotated[int, Query(default=1, ge=1, description="页码（从 1 开始）")] = 1
    page_size: Annotated[int, Query(default=20, ge=1, le=200, description="每页条数（1-200）")] = 20


def normalize_naive_datetime(value: str | None) -> str | None:
    """入参时间宽松归一化：接受 `yyyy-MM-dd HH:mm` 与 `yyyy-MM-dd HH:mm:ss`，
    统一输出 `yyyy-MM-dd HH:mm:ss`；空值原样返回。非法格式抛 ValueError。"""
    if value is None or value == "":
        return value
    v = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"时间格式无法解析：{value!r}（应为 yyyy-MM-dd HH:mm 或 yyyy-MM-dd HH:mm:ss）")
