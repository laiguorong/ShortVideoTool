# 短视频工具（ShortVideoTool）

一站式个人短视频平台运营工作台：**账号 → 选品 → 素材 → 创作 → 发布 → 数据** 全流程本地化管理。

## 功能总览

| 模块 | 功能 |
| --- | --- |
| 账号管理 | 多账号添加（扫码 / 粘贴 Cookie）、登录态定时检测、失效告警、重新登录、启停、chromium profile 持久化 |
| 选品中心 | 定时拉取选品广场门店（佣金率/类目/达人数/销量/核销率/销售额条件）、门店库筛选标记 |
| 素材库 | 树形分类、视频拉取任务（正则/话题/位置/横竖屏）、分享链接解析、本地文件上传 |
| 创作中心 | 项目+分镜、批量切割分配（固定时长/去头尾）、组合生成（组合键唯一防重复）、BGM、去重处理（抽帧/水印/干扰）、标题话题池、项目门店绑定 / 视频简介管理 |
| 发布中心 | 8 步向导（账号→项目→门店→简介→数量→排期→声明→预览）、draft/preview/confirm 三段流、视频目录模式、明细状态机、失败重试、挂起恢复、复制草稿、只读详情 |
| 发布记录 | 全字段留痕、简介+话题快照冗余、自主声明、是否允许下载、Excel 导出、成品文件定位 |
| 系统设置 | data 目录管理、健康检查、备份恢复、缓存清理、通知中心 |

## 技术栈

- **后端**：Python 3.12 · FastAPI · SQLite（WAL + 27 张业务表） · APScheduler · FFmpeg · cryptography · openpyxl · loguru
- **前端**：Electron 28 · React 18 · TypeScript · Vite · Zustand · Radix UI · Tailwind CSS
- **架构**：三层（渲染界面 → FastAPI 业务服务 HTTP → FFmpeg 子进程）

详细架构与表结构见 `docs/短视频工具需求文档.md`。

## 目录结构

```
├─ docs/                    # 需求文档（PRD）/ 排期算法 / 视频组合算法约束 / 数据目录布局
├─ scripts/                 # 一次性算法验证脚本（如 algo_full_test.py）
├─ backend/                 # FastAPI 后端
│  ├─ app/
│  │  ├─ api/               # 路由（10 模块）
│  │  ├─ models/            # Pydantic 请求/响应模型
│  │  ├─ services/          # 业务服务（含统一任务队列 task_service）
│  │  ├─ core/              # ffmpeg/crypto/调度器/通知/平台客户端
│  │  └─ db/                # SQLite 封装 + 建表迁移（user_version=v43）
│  ├─ assets/ffmpeg/        # ffmpeg.exe / ffprobe.exe（本地放置；git 屏蔽）
│  ├─ tests/                # 14 套件正式单测
│  └─ build.spec            # PyInstaller 打包配置
└─ frontend/                # Electron 前端（vite 三入口：main/preload/renderer）
   └─ src/renderer/src/
      ├─ api/               # HTTP 客户端（9 模块）
      ├─ pages/             # 9 页面 + 4 对话框/抽屉
      ├─ components/        # ui 基础组件 + shared 通用组件
      └─ store/             # Zustand 单 store
```

## 本地开发

### 后端

```powershell
cd backend
python -m pip install -r requirements.txt
# FFmpeg：放置 ffmpeg.exe / ffprobe.exe 到 backend/assets/ffmpeg/
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

### 前端

```powershell
cd frontend
npm install
npm run dev      # 构建 + 启动 Electron（自动拉起后端）
```

### 全量回归

```bash
bash run_all_tests.sh
```

5 节：后端语法检查 + 后端单测（14 套件）+ 前端 typecheck + 前端单测 + GBK stdout。

### 算法验证脚本（独立运行）

```powershell
python scripts/algo_full_test.py   # 输出 /tmp/algo_full_test.md
```

视频组合算法 4 项核心约束自检：同源率 / 位置重叠 / 权重采样 / 随机均匀，详见 `backend/tests/test_v25_pick_combinations.py`。

## 发布向导

向导 8 步结构：

| 步 | 内容 |
|---|---|
| 1 | 任务名 + 选择账号 |
| 2 | 选择项目（项目模式）或成品视频目录（视频目录模式） |
| 3 | 挂载门店（项目模式自动回查 `project_shop`；视频目录模式手填） |
| 4 | 视频简介（按项目分组，含话题编辑） |
| 5 | 发布数量（global 全局上限 / per_account 分账号上限） |
| 6 | 排期（起始 / 结束 / 固定间隔 vs 均衡间隔 / 同项目间隔） |
| 7 | 自主声明（ai_generated / none）+ 是否允许下载 |
| 8 | 预览明细 + 确认创建并启动 |

**核心能力**：
- **draft / preview / confirm 三段流**：支持 `project_snapshots` + `account_snapshots` 形态持久化草稿
- **视频目录模式**：扫描成品视频目录 → 自动均分到账号 → 入库时按目录顺序排视频文件绝对路径，避免重复使用
- **草稿门店校验**：通过 `PreviewResult.validation.warnings` 拦截 `stale_in_draft` / `extra_in_draft`，避免带病提交
- **直传 payload**：`preview-direct` / `confirm-direct` 一次性接口，不需要先 saveDraft
- **复制任务**：返回 wizard payload（不写库），由前端打开向导回显

**视频数量校验**：`project_source=video_dir` 且目录内合规视频数 < 需发布数 → `ScheduleVideoShortageError`（API 层即拒绝，UI 直接弹出提示）

**排期算法核心**：`publish_schedule.build_schedule()`（`backend/app/services/publish_schedule.py`），纯函数 + 题面例子对账通过。详见 `backend/tests/test_publish_schedule_413.py`。

**数据库**：27 张业务表 + 系统表（账号 / 选品 / 素材 / 创作 / 发布 / 简介门店 / 通知 / 分享导入 / 上传 / 系统状态），UUID 主键，当前 `user_version=v43`。

**异常层次**：`ScheduleOverflowError` 基类，子类 `ScheduleTimeShortageError(category='time')` / `ScheduleVideoShortageError(category='video')`——前端按 `category` 区分错误类型并展示对应 banner。

## 打包分发

### 环境要求

| 段 | 项 | 版本/位置 |
|---|---|---|
| 后端 PyInstaller | OS | **Windows**（PyInstaller 不支持跨平台编译）|
| 后端 PyInstaller | Python | **3.12.10**（与 runtime 一致）|
| 后端 PyInstaller | 依赖 | `requirements.txt` 全部 + `pyinstaller` |
| 前端 electron-builder | OS | **Windows**（NSIS Win target 需 Win 环境）|
| 前端 electron-builder | Node.js | 18+ 或 20+ |
| 前端 electron-builder | electron-builder | 24.9.1（devDependencies 锁定）|
| 必备外部资源 | FFmpeg | `backend/assets/ffmpeg/ffmpeg.exe` + `ffprobe.exe`（含 libx264）|
| 必备外部资源 | MediaPipe 模型 | `backend/app/core/models/face/blaze_face_*.tflite`（已在仓库）|

### 完整流水线

```powershell
# 0. 一次性准备
cd backend
python -m pip install -r requirements.txt
python -m pip install pyinstaller
# 手动：放 ffmpeg.exe + ffprobe.exe 到 backend/assets/ffmpeg/

# 1. 后端：单 exe（捆绑 FFmpeg + MediaPipe 模型）
pyinstaller build.spec
# 产物：backend/dist/shortvideo-backend.exe

# 2. 前端：NSIS 安装包（extraResources 自动拷 backend/ 进去）
cd ../frontend
npm install
npm run build                    # 三段 vite 编译：main + preload + renderer
npm run dist                     # electron-builder → release/ShortVideoTool Setup 1.0.0.exe
# 产物：frontend/release/ShortVideoTool Setup 1.0.0.exe
# 副产物：frontend/release/win-unpacked/（解压版，可直接跑）
```

**ffmpeg 必须含 libx264**——否则后端启动时 `check_ffmpeg_env` 抛 RuntimeError，主进程会弹错误框提示。

### 跨平台可行性

| 目标安装包 | 在 Windows 打 | 在 macOS 打 | 在 Linux 打 |
|---|---|---|---|
| Windows NSIS | ✅ 原生 | ❌ 需 wine + mono | ❌ 同上 |
| macOS DMG | ❌ 需 macOS | ✅ 原生 | ❌ |
| Linux AppImage | ❌ | ❌ | ✅ 原生 |

**Windows 安装包只能 Windows 打**。项目当前只出 Win target，无跨平台需求。

### 可选：代码签名

| 平台 | 工具 | 作用 |
|---|---|---|
| Windows | EV 代码签名证书 + signtool.exe | 消除 SmartScreen "未知发布者"警告 |
| macOS | Developer ID + notarytool | 通过 Gatekeeper 公证 |

未签名：Windows 安装弹"未知发布者"；macOS Gatekeeper 直接拦截。本地测试可跳过，分发前必须签。

### 时间预估

| 步骤 | 首次 | 缓存命中后 |
|---|---|---|
| `pip install -r requirements.txt` | 2-3 min | 跳过 |
| `npm install` | 1-2 min | 跳过 |
| `npm run build` | 30-60 s | 30-60 s |
| `pyinstaller build.spec` | 2-4 min | 30-60 s |
| `electron-builder` | 1-3 min | 30-60 s |
| **合计** | **~10 min** | **~3 min** |

### 国内网络环境

electron-builder 启动时会从 GitHub Releases 下载 Electron 二进制，国内访问经常超时。已内置镜像配置（`build.electronDownload.mirror` → npmmirror），首次打包自动走镜像。

如需切换/自定义镜像：

```json
// frontend/package.json → build.electronDownload.mirror
"mirror": "https://npmmirror.com/mirrors/electron/"   // 默认
"mirror": "https://cdn.npmmirror.com/binaries/electron/"  // 备用
```

或临时通过环境变量覆盖（不写入 package.json）：

```powershell
$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
npm run dist
```

### 360 安全卫士 / 杀软实时扫描冲突

electron-builder 重打包时会**先删旧 `release/win-unpacked/resources/app.asar` 再写新文件**。如果机器装了 360 安全卫士、火绒等第三方杀软，实时扫描会**持续锁定** `app.asar`（持续读 mmapped 句柄），导致 `npm run dist` 报：

```
⨯ remove ...\resources\app.asar: The process cannot access the file
because it is being used by another process.
```

**解决方案**：在杀软里把以下目录加入「信任区 / 白名单」：

| 目录 | 原因 |
|---|---|
| `frontend/release/` | electron-builder 写入 / 删除 NSIS 中间产物 |
| `frontend/node_modules/app-builder-bin/` | 打包工具本身 |
| `frontend/node_modules/` | 全部 native 模块 + 临时解压的 Electron |

**360 安全卫士**：右上角「菜单」 → 设置 → 安全防护中心 → 信任区 → 添加文件夹 → 选上面 3 个目录 → 确认。

**火绒**：菜单 → 信任区 → 添加。

> 加完信任区后无需重启电脑，下次 `npm run dist` 即生效。
>
> 如果暂时无法加信任区，可用临时绕过（产物不写到 `release/`）：
> ```powershell
> npx electron-builder --config.directories.output=release_v2
> ```

### 安装后布局

```
C:\Users\<用户>\AppData\Local\Programs\ShortVideoTool\    ← 应用目录（NSIS 安装）
├── ShortVideoTool.exe                                     ← 主启动 exe
├── Uninstall ShortVideoTool.exe
├── resources\
│   ├── app.asar                                           ← 应用代码
│   └── backend\                                           ← extraResources
│       └── shortvideo-backend.exe                          ← PyInstaller 单 exe
└── ...（Chromium runtime + DLL）

H:\ShortVideoToolData\                                     ← 数据目录（用户可改）
├── db\short_video_tools.db
├── accounts\<id>\profile\ + storage.json
├── material\ + create\ + finished\ + cache\ + temp\ + log\

%APPDATA%\shortvideo-tool\                                 ← Electron 用户态（自动管）
└── main-YYYY-MM-DD.log + Cache + Local Storage
```

应用目录（安装时可选）+ 数据目录（设置面板可改）+ 用户态目录（不可改）三段分工详见 `docs/DATA_LAYOUT.md`。

## 平台客户端说明

固定使用 **real 客户端**（`backend/app/core/douyin/client.py`），调用真实视频平台创作者中心接口，需账号登录态正常。联调改用 `unittest.mock.patch` 桩接 ffmpeg/db。

## 数据目录

运行时自动创建于后端工作目录 `data/`：

| 子目录 | 用途 |
|---|---|
| `db/` | SQLite 单库（含 WAL/SHM，user_version=v43） |
| `create/` | 创作中转（按 uuid 命名的持久片段视频） |
| `finished/` | 成品视频（按 project_id 分组） |
| `material/` | 素材（按 uuid + ext） |
| `temp/` `cache/` `log/` | 可清理（启动期按 retention_days 自动清旧） |
| `accounts/<account_id>/` | 账号登录态（`profile/` chromium 持久化；`storage.json` 备份） |

数据目录默认落在所选盘根下 `ShortVideoData/`。完整布局见 `docs/DATA_LAYOUT.md`。

## 风险提示

本工具为个人效率工具，依赖目标视频平台 Web 接口（非官方开放能力）；平台规则可能限制自动化行为，
过度使用可能影响账号权重。首次启动需确认风险告知，使用频率请自行把控。

## 打赏

如果这个工具对你有帮助，欢迎扫码支持作者一杯咖啡 ☕

![打赏码](./wechat.png)