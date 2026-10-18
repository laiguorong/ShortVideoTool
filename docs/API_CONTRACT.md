# 前后端 API 设计规范（#publish-contract）

> 本规范源于 2026-10-18 的大规模重构（C 方案 + 异常处理统一）。**所有路由必须遵守**。

## 1. 总体架构

```
┌─────────────────┐                ┌─────────────────┐                ┌──────────────┐
│  Renderer (React) │  ──HTTP──▶  │  FastAPI 路由层  │  ──直接调──▶ │  service 层   │
│                   │                │  (薄薄一层)       │                │ (业务逻辑)    │
│ - request<T>(path)│                │ - 入参 Pydantic  │                │ - raise HTTP-│
│ - .data! 解包     │                │ - 路由层不做业务  │                │   Exception   │
│ - try/catch 局部  │                │   (0 样板 try/    │                │ - raise Value-│
│   + global        │                │    except)        │                │   Error       │
│   unhandledrejec- │                │ - 直接 ok(svc())  │                │   (业务校验)   │
│   tion listener   │                │                   │                │               │
└─────────────────┘                └─────────────────┘                └──────────────┘
```

**核心约束**：
- 路由层只做 3 件事：(1) Pydantic 校验入参 (2) 调 service (3) 包 ApiResp 信封返。**不写 try/except**
- 业务错误全靠 service raise，由 main.py 全局 handler 统一转 ApiResp 信封
- 错误契约对前后端**唯一**入口：`{code, message, data}`

## 2. URL 路径规范

### 2.1 基础结构
- 基础前缀：`/api/<domain>/<resource>`
- 域划分：`/publish/`、`/accounts/`、`/selection/`、`/materials/`、`/creation/`、`/records/`、`/settings/`、`/tasks/`、`/startup/`、`/files/`、`/dashboard/`、`/health`
- 资源名：复数（`/tasks`、`/accounts`、`/materials`）

### 2.2 路由命名
- 集合操作：`GET /api/<domain>/<resource>`（列表）、`POST /api/<domain>/<resource>`（创建）
- 单项操作：`GET /api/<domain>/<resource>/{id}`、`PUT /api/<domain>/<resource>/{id}`、`DELETE /api/<domain>/<resource>/{id}`
- 动作路由：`POST /api/<domain>/<resource>/{id}/<verb>`（如 `/tasks/{id}/toggle`、`/tasks/{id}/cancel`）

### 2.3 跨域特殊资源
- 视频简介 + 项目-门店绑定 路由前缀：`/api/publish/intro/*`（与 publish 域同 router，不分文件）
- 上传文件 / 头像：`/api/files/*`（走 FileResponse，不走 ApiResp 信封）

## 3. 入参规范

### 3.1 Pydantic 模型（必须）
- 所有 POST/PUT/PATCH 入参**必须**用 Pydantic BaseModel（不能用 `body: dict`）
- 字段必填/可选：`Field(...)`、`Optional[X] = None`
- 字符串约束：`Field(min_length=N, max_length=M)`、`pattern=...`
- 嵌套：`list[SubModel]`、`dict[str, X]`

### 3.2 时间字段
- 入参：宽松接受 `yyyy-MM-dd HH:mm` 与 `yyyy-MM-dd HH:mm:ss`（自动归一）
- 出参：统一 `yyyy-MM-dd HH:mm:ss`
- 落库：统一 `yyyy-MM-dd HH:mm:ss`（DB 列 `TEXT`）

### 3.3 路径/query 参数
- 必填路径参数：`{id: str}`
- 可选 query：`page: int = 1`、`page_size: int = 20`（用 `PageQuery` 基类，publish/records 域统一）
- **关键**：`PageQuery` 派生类（Pydantic model）字段加 `Query()` 注解**无效**（仅影响 json_schema），
  **必须**在路由函数参数上用 `Annotated[Model, Query()]` 告诉 FastAPI 从 query string 解析。否则 GET 请求 → 422 'Field required'。

## 4. 出参规范（ApiResp 信封）

### 4.1 统一形态
```python
# 后端
from app.models.common import ok, ApiResp

@router.get("/xxx")
def get_xxx():
    return ok(some_business_data)

# 实际响应
{"code": 0, "message": "ok", "data": {...}}
```

### 4.2 字段
| 字段 | 类型 | 含义 |
|---|---|---|
| `code` | int | 0=成功；其他=业务码（一般等于 HTTP status） |
| `message` | string | 业务消息（成功是 "ok"，错误是"xxx不存在"）|
| `data` | any / null | 业务数据；错误时统一 `null`（除 422 字段级 errors）|

### 4.3 list 分页
```python
# PageResult 由 service 内部用 d.query_page 拼
return ok({
    "total": int,
    "page": int,
    "page_size": int,
    "list": [T, ...]
})
```

## 5. 错误响应规范（#fix-status-404-consistent + 全局 handler）

### 5.1 6 种状态码

| 状态码 | 触发场景 | 响应形态 | 触发方式 |
|---|---|---|---|
| **200/201** | 成功 | `{code:0, message:"ok", data:T}` | 路由 `return ok(svc())` |
| **400** | 业务校验（参数错/状态错）| `{code:400, message:"xxx非法", data:null}` | service `raise ValueError("xxx")` |
| **404** | 资源不存在 | `{code:404, message:"xxx不存在", data:null}` | service `raise HTTPException(404, "xxx不存在")` |
| **405** | method 错 | `{code:405, message:"Method Not Allowed", data:null}` | FastAPI 自动（走 starlette handler）|
| **422** | Pydantic 入参校验 | `{code:422, message:"参数校验失败", data:{errors:[...]}}` | FastAPI 自动（走 RequestValidationError handler）|
| **500** | 未捕获异常 | `{code:500, message:"服务异常，请稍后重试", data:null}` | 任何未 catch 的异常（走 Exception handler）|

### 5.2 service raise 规则（关键）

| 业务语义 | 抛出方式 | 路由层动作 |
|---|---|---|
| **资源不存在**（404） | `raise HTTPException(status_code=404, detail="xxx不存在")` | 不需要 try/except，直接冒到 FastAPI |
| **业务校验**（400） | `raise ValueError("xxx非法")` 或 `raise ValueError("xxx不在范围内")` | 不需要 try/except |
| **真异常**（500） | 不抛，让 Python 异常冒泡 | 不需要 try/except，全局 handler 兜底 |

**反模式**（禁止）：
```python
# ❌ 路由层 try/except ValueError → HTTPException(400)
@router.put("/xxx")
def update():
    try:
        return ok(svc.update())
    except ValueError as e:
        raise HTTPException(400, str(e))

# ✅ 正确写法（路由层 0 try/except 样板）
@router.put("/xxx")
def update():
    return ok(svc.update())  # service 内部 raise HTTPException/ValueError
```

### 5.3 资源存在性预检（必须）
所有"资源不存在" raise HTTPException(404) **必须**先 `query_one` 预检，**不能**仅依赖 `update_by_id` 静默返 0 行。

```python
# ✅ 正确：先 query_one 预检
def update(account_id: str, remark: str) -> None:
    d = get_db()
    if not d.query_one("SELECT id FROM account WHERE id=? AND deleted=0", (account_id,)):
        raise HTTPException(status_code=404, detail="账号不存在")
    d.update_by_id("account", account_id, {"remark": remark})

# ❌ 错误：update_by_id 静默返 0 行 → 200 假成功
def update(account_id: str, remark: str) -> None:
    get_db().update_by_id("account", account_id, {"remark": remark})  # 0 行受影响但返成功！
```

## 6. 后端全局异常处理器（main.py）

main.py 注册 **4 个**全局 handler（`@app.exception_handler`）：

```python
# 1) ValueError → 400 + ApiResp 信封
# 2) RequestValidationError (Pydantic 422) → 422 + ApiResp 信封，data 含 errors
# 3) HTTPException (FastAPI) → 同 status_code + ApiResp 信封
# 4) starlette.exceptions.HTTPException (FastAPI 自动 404/405) → 同 status_code + ApiResp 信封
# 5) Exception (兜底) → 500 + ApiResp 信封 + logger.exception 落 traceback
```

**禁止在路由层写 try/except ValueError** —— 全局 handler 统一处理。

## 7. 后端路由层规范（0 样板）

```python
# ✅ 标准路由
@router.get("/xxx/{xxx_id}", summary="Xxx 详情")
def get_xxx(xxx_id: str):
    """Read-only detail."""
    return ok(svc.get_xxx(xxx_id))  # service raise HTTPException(404) / ValueError(400)

@router.put("/xxx/{xxx_id}", summary="更新 Xxx")
def update_xxx(xxx_id: str, req: UpdateXxxRequest):
    """Update with Pydantic validation."""
    return ok(svc.update_xxx(xxx_id, **req.model_dump()))

@router.post("/xxx/{xxx_id}/toggle", summary="Xxx 启停")
def toggle_xxx(xxx_id: str, req: ToggleRequest):
    """Action endpoint."""
    return ok(svc.toggle_xxx(xxx_id, enabled=req.enabled))
```

**反模式**（禁止）：
```python
# ❌ 路由层 try/except
try:
    return ok(svc.update())
except ValueError as e:
    raise HTTPException(400, str(e))
except Exception as e:
    raise HTTPException(500, str(e))

# ❌ 路由层 None 检查（service 内部 raise HTTPException(404) 即可）
task = svc.get_task(task_id)
if not task:
    raise HTTPException(404, "任务不存在")
return ok(task)
```

## 8. 前端 API 客户端规范

### 8.1 方法形态（必须）
```typescript
// ✅ 异步 + ApiResp 解包
export const xxxApi = {
  getXxx: async (id: string): Promise<Xxx> => {
    const r = await request<ApiResp<Xxx>>(`/api/xxx/${id}`)
    return r.data!
  },

  listXxx: async (page = 1, pageSize = 20): Promise<PageResult<Xxx>> => {
    const r = await request<ApiResp<PageResult<Xxx>>>(`/api/xxx?page=${page}&page_size=${pageSize}`)
    return r.data!
  },

  // ⚠️ 仅当调用方真的需要后端 code（如需要区分多种业务状态）才返回 ApiResp
  // 大多数情况直接返回 data（T）类型
}
```

### 8.2 反模式（禁止）
```typescript
// ❌ 同步函数 + 直接 return request<>
listXxx: () => request<Xxx[]>(...)

// ❌ 不解包：调用方拿到 ApiResp<T>，还要再 .data
listXxx: async () => {
  const r = await request<ApiResp<Xxx[]>>(...)
  return r  // 应该是 r.data!
}

// ❌ 包装成 {ok, data} 双层
listXxx: async () => {
  return { ok: true, data: (await ...).data }
}
```

## 9. 前端错误处理规范

### 9.1 client.ts 错误提取优先级（已修）
```typescript
// 1) 422 字段级错误（data.errors 数组 → formatApiError 处理）
// 2) 业务 message（4xx 业务错误）
// 3) 旧 detail 兼容（路由层 raise HTTPException 但 detail 是数组/对象）
// 4) 原文本 fallback
const dataObj = body?.data as { errors?: unknown } | null
const pydanticErrors = dataObj?.errors
const fieldMsg = Array.isArray(pydanticErrors) ? formatApiError(pydanticErrors, '') : ''
const errorMsg = fieldMsg || body?.message || body?.detail || raw.slice(0, 200)
```

### 9.2 全局 unhandledrejection listener（App.tsx）
- 漏 catch 的 ApiError 自动 toast，避免静默失败
- 401 跳登录（待实现，目前仅 toast）

### 9.3 局部 catch（业务需要时）
```typescript
// ✅ 调用方真的想覆盖默认处理时，才写 catch
try {
  const result = await xxxApi.create(data)
  toast('创建成功', 'success')
} catch (e) {
  // 全局已 toast，但业务侧要更新 UI 状态
  setLoading(false)
}
```

## 10. 数据库设计约定

### 10.1 软删统一
- 所有表有 `deleted` 字段（`INTEGER DEFAULT 0`）
- 业务查询统一 `WHERE deleted=0`
- 软删：`d.soft_delete_by_id(table, id)` 自动设 deleted=1 + update_time

### 10.2 时间戳
- 所有表有 `create_time`、`update_time`（TEXT，格式 `yyyy-MM-dd HH:mm:ss`）
- 用 `app.db.utils.now_str()` 写入

### 10.3 *_json 列兼容
- 新加 `*_json` 列时，`default="[]"` 或 `"{}"`
- 读取时 `json.loads(...)`，解析失败 fallback 默认值 + logger.warning
- **不在前端的 `*_json` 接口里暴露原始 JSON**（前端只消费结构化对象）

## 11. 测试规范

### 11.1 测试分层
| 测试类型 | 范围 | 工具 |
|---|---|---|
| 单元 | service 函数（DB 隔离）| pytest + temp DB |
| 端到端 | 路由响应（信封/状态码/字段级错误）| TestClient + TestClient(raise_server_exceptions=False) |
| 全局异常 | 全局 handler 行为 | TestClient + 触发已知异常 |

### 11.2 端到端测试必备断言
```python
# ✅ 测信封
assert j["code"] == 422
assert "errors" in j["data"]
assert j["data"]["errors"][0]["loc"] == ["body", "task_name"]

# ✅ 测 404（不是 400 / 200）
assert r.status_code == 404
assert j["code"] == 404
```

### 11.3 端到端测试文件清单
- `tests/test_publish_draft_413.py` — publish 域核心业务
- `tests/test_publish_intro_service_413.py` — publish_intro service 业务
- `tests/test_publish_intro_routes_e2e.py` — publish_intro 路由信封
- `tests/test_accounts_available.py` — accounts 域 200 业务
- `tests/test_global_exception_handler.py` — 全局 handler 5 类异常

## 12. 新加路由 Checklist

新加一个路由时，按以下 checklist 自检：

- [ ] 入参用 Pydantic BaseModel（不是 `body: dict`）
- [ ] 资源存在性预检：`if not d.query_one(...): raise HTTPException(404, "xxx不存在")`
- [ ] 业务校验：`raise ValueError("xxx非法")`（不 raise HTTPException）
- [ ] 路由层不写 try/except ValueError（全局 handler 兜底）
- [ ] 路由层不写 None 检查（service 内部 raise HTTPException(404)）
- [ ] 路由 `return ok(svc_call(...))` 一行式（业务复杂就分多行但不嵌套 try）
- [ ] 资源操作加 `_disabled=0 AND deleted=0` 过滤
- [ ] 前端 API 方法 async + `request<ApiResp<...>>` + `r.data!`
- [ ] 前端 .ts 类型注解（`Promise<T>` 明确返回类型）
- [ ] 加端到端测试：200（正常）+ 404（资源不存在）+ 422（缺字段）
- [ ] memory 记新决策

## 13. 禁止清单（review checklist）

- ❌ 路由层 `try/except ValueError → raise HTTPException(400)`
- ❌ 路由层 `if not result: raise HTTPException(404)`
- ❌ 路由层返回 `return {...}` （不包 `ok()`）
- ❌ service `raise HTTPException(500)` （500 留给兜底）
- ❌ 路由函数 `-> dict` 类型注解（与 `ok()` 返 ApiResp 冲突，500 bug）
- ❌ service `update_by_id` 不先 `query_one` 预检（0 行假成功）
- ❌ 前端 `request<T>(...)` 不解包 `r.data!`
- ❌ 前端错误处理只取 `body.message`（422 字段级错误丢失）
- ❌ 业务时间用 `datetime.now()` 直传（应走 `now_str()` 统一格式）
- ❌ 新加路由用 `body: dict` 接收（必须 Pydantic BaseModel）

## 14. 相关文档

- 业务需求：`docs/短视频工具需求文档.md`
- 排期算法：`docs/排期算法说明.md`
- 数据布局：`docs/DATA_LAYOUT.md`
- 视频组合算法：`docs/视频组合算法约束.md`
- 用户操作手册：`docs/用户操作手册.md`
- 项目长期备忘：`.workbuddy/memory/MEMORY.md`（短期，4k 字符内）
- 当日工作日志：`.workbuddy/memory/2026-10-XX.md`（每日）
