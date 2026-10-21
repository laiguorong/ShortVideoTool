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

### 开发环境准备（**新机器必跑**）

**项目要求 Python 3.12.10**（与 `backend/requirements.txt` 注释锁定的运行时一致）。venv 是项目隔离的 site-packages，**不要装到 system Python**（会污染依赖图）。

**1. 装 Python 3.12.10 + 加 PATH**
- 下载：https://www.python.org/downloads/release/python-31210/
- 装时勾选 **"Add Python 3.12 to PATH"**（Windows）
- 验证：`python --version` 应输出 `Python 3.12.10`

**2. 一键建 venv + 装依赖**（从项目根）
```powershell
cd frontend
npm run setup
```
或**直接调跨平台 Python 脚本**（不依赖 npm）：
```powershell
python frontend/scripts/setup_venv.py
# 加 -E 屏蔽 shim (WorkBuddy / 360 杀软环境):
python -E frontend/scripts/setup_venv.py
```
脚本做的事（详见 `frontend/scripts/setup_venv.py`，跨平台，stdlib only）：
- 检查 Python 版本（**必须 3.12.10**，否则报错退出）
- 创建 `backend/.venv`（**不入库**，已加 `.gitignore`）
- 装 `backend/requirements.txt` 列的 16 个项目直接依赖
- 装 `pyinstaller==6.22.3` + `pyinstaller-hooks-contrib`（**不在** `requirements.txt`，build 时需要）
- 跨平台：Windows 用 `.venv/Scripts/python.exe`，macOS/Linux 用 `.venv/bin/python`
- **绝对不要**直接 `cd backend && python -m pip install -r requirements.txt`（会装到 system Python，污染依赖图，PyInstaller 静态分析会误拉 jaxlib/torch/polars 400+MB 进 `_internal/`）

**3. 验证 venv**（可选）
```powershell
backend\.venv\Scripts\python.exe -m pip list
# 应该只显示 80 个包, 没有 jaxlib/torch/transformers/polars/ultralytics
```

**macOS / Linux 等价**：
```bash
cd frontend
python3.12 -m venv ../backend/.venv
../backend/.venv/bin/pip install -r ../backend/requirements.txt
../backend/.venv/bin/pip install pyinstaller==6.22.3 pyinstaller-hooks-contrib
```

**4. WorkBuddy / IDE 注意**
- 装过 360 杀软的环境, `pip install` 会被 safe-delete shim 拦 (`os.unlink` 累计 > 50 触发 fail-closed)
- 解决: 用 `python -E -m pip install` (屏蔽 PYTHONPATH 避开 shim 注入)
- 或在 `python -m venv` 前先关 360 实时扫描

### 后端

**核心原则**：**所有** Python 命令必须用 venv 内的 python（`backend\.venv\Scripts\python.exe`），**不要**用 system python。

**1. 跑 dev server**（uvicorn 自动 reload）
```powershell
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
# FFmpeg：放置 ffmpeg.exe / ffprobe.exe 到 backend/assets/ffmpeg/
```

**2. 激活 venv (可选, 让 `python` 命令直接指向 venv)**

PowerShell (Windows):
```powershell
# 临时激活 (当前 shell session)
backend\.venv\Scripts\Activate.ps1

# 验证: python --version 应输出 Python 3.12.10 + (backend) 前缀
python -c "import sys; print(sys.executable)"  # 期望 backend\.venv\Scripts\python.exe

# 退出: deactivate
```

bash (macOS / Linux / WSL):
```bash
source backend/.venv/bin/activate
which python  # 期望 backend/.venv/bin/python
deactivate
```

激活后 `python` / `pip` 自动指向 venv，**不**用每次敲 `.venv\Scripts\python.exe`。但**风险**：IDE 终端有时**不会**激活 venv，跑命令会落到 system python。**最稳**：不激活，**直接**用 `.venv\Scripts\python.exe` 绝对路径。

### 前端

```powershell
cd frontend
npm install
npm run dev      # 构建 + 启动 Electron（自动拉起后端）
```

**前端 dev 不依赖 venv**（Node.js 自己的运行时）。但 `npm run build:backend` 会调 venv python（已在 build script 里写死），见 `frontend/package.json` 的 `build:backend` script。

### 测试

**单条**（venv 内的 pytest）：
```powershell
cd backend
.venv\Scripts\python.exe -m pytest tests/ -v
# 单个文件: .venv\Scripts\python.exe -m pytest tests/test_xxx.py -v
```

**全量回归**（脚本 5 节：后端语法 + 后端单测 + 前端 typecheck + 前端单测 + GBK stdout）：
```bash
bash run_all_tests.sh
```

**算法验证脚本**（独立运行）：
```powershell
backend\.venv\Scripts\python.exe scripts/algo_full_test.py
# 输出 /tmp/algo_full_test.md
```

视频组合算法 4 项核心约束自检：同源率 / 位置重叠 / 权重采样 / 随机均匀，详见 `backend/tests/test_v25_pick_combinations.py`。

### IDE 配置（推荐）

**VSCode**（`.vscode/settings.json`）:
```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/backend/.venv/Scripts/python.exe",
  "python.analysis.extraPaths": ["${workspaceFolder}/backend"],
  "python.testing.pytestPath": "${workspaceFolder}/backend/.venv/Scripts/pytest.exe"
}
```

**PyCharm**:
- Settings → Project Interpreter → Add → Virtualenv Environment → Existing
- 选 `backend/.venv/Scripts/python.exe`

**关键**: IDE 配 venv interpreter 后, IntelliSense / 类型检查 / debug 都走 venv, **不**会**误**导入 system site-packages 的包 (避开了 "venv 装 80 个包, system 装 177 个" 的污染问题).

### 验证 venv 是"干净" (避 system 污染)

```powershell
# 1. 列出 venv 装的包 (期望 ~80 个, 无 jaxlib/torch/transformers/polars/ultralytics)
backend\.venv\Scripts\python.exe -m pip list

# 2. 列出 system site-packages (期望 0 个项目依赖, 避免污染)
"C:\Users\laiguorong\AppData\Local\Programs\Python\Python312\python.exe" -m pip list
# (上面这个路径是荣少机器实际 system python 路径, 不同机器不同)

# 3. 关键: dev 跑后端时 sys.path 应该只有 venv, **不**含 system site-packages
backend\.venv\Scripts\python.exe -c "import sys; [print(p) for p in sys.path]"
# 期望看到: backend\.venv\Lib\site-packages (有)
# 期望**不**看到: C:\Users\laiguorong\AppData\Local\Programs\Python\Python312\Lib\site-packages (system)
```

### 写进 SOP 的常见陷阱

- **不要**用 system python 跑 backend 命令 (`python -m uvicorn ...` 落到 system site-packages → 装到 system → 污染依赖图)
- **不要**在 backend/ 跑 `python -m pip install` (装到 system, 同样污染)
- **不要**用 IDE 默认 interpreter (VSCode 默认用 PATH 第一个 python, 可能是 system) — **必须**显式配 venv interpreter
- **不要**激活 venv 后**不**用 `python` 命令直接跑 — 有时 IDE / 终端**不**继承 venv env, 跑出去仍用 system

### 写进 .gitignore (已加)

```
backend/.venv/                # venv 不入库
backend/requirements-*.txt    # 临时 pip freeze 备份
```

`backend/requirements.txt` 16 个项目直接依赖手写, **不要**用 `pip freeze` 覆盖 (会污染).

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
npm run dist                     # electron-builder → release/ShortVideoTool Setup <version>.exe
# 产物：frontend/release/ShortVideoTool Setup <version>.exe
# 副产物：frontend/release/win-unpacked/（解压版，可直接跑）

# 版本号在两处定义：
#   frontend/package.json: "version"  (NSIS 安装包名 + electron-builder 读取)
#   backend/app/main.py:   FastAPI(version="...")  (API 标识,与安装包无关但建议同步)
```

> **跨平台说明**：`build:backend` 用 `node scripts/rmrf.cjs` 删旧 dist（Node 内置 fs.rmSync 跨平台），
> 之前用 `rm -rf` 在 Windows cmd 报错 `'rm' 不是内部或外部命令`。

**ffmpeg 必须含 libx264**——否则后端启动时 `check_ffmpeg_env` 抛 RuntimeError，主进程会弹错误框提示。

> **`win.artifactName` 不能删（自动更新 404 的坑）**
>
> electron-builder 有两套命名，对「含空格」的处理不一致：
> - 磁盘实际产出走默认模板 `${productName} Setup ${version}.${ext}` → `ShortVideoTool Setup 1.0.5.exe`
> - 写进 `latest.yml` 的名字走 `computeSafeArtifactNameIfNeeded()`，发现含空格（GitHub 不允许）
>   会**只把空格换成 `-`** → `ShortVideoTool-Setup-1.0.5.exe`
>
> 它改了 yml 里的名字，却**不重命名磁盘文件**，上传时也不会用改名后的名字 →
> electron-updater 按 yml 里的连字符名去下载 → **404，自动更新永久失败**。
> 实测 v1.0.3 / v1.0.4 / v1.0.5 全部踩中（`generateGithubInstallerName()` 那条 fallback 分支没走，
> 因为空格替换后就已合法，不会再 fallback）。
>
> 修法：`win.artifactName` 显式钉成无空格模板 `${productName}-Setup-${version}.${ext}`，
> 于是 `computeSafeArtifactNameIfNeeded()` 直接返回 `null`（沿用磁盘原名），两边天然一致。
> **改版本号 / 改 productName 时别把这个字段删掉。**

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

%APPDATA%\com.rong-info.shortvideotool\                    ← Electron 用户态（自动管）
└── logs\main-YYYY-MM-DD.log + Cache + Local Storage
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

## 开源协议与免责声明

### 开源协议

本项目基于 **GNU Affero General Public License v3.0**（AGPL-3.0）开源发布。
完整协议文本请参见 [GNU 官方页面](https://www.gnu.org/licenses/agpl-3.0.html)。

基于 AGPL-3.0 的核心要求：

- 你可以自由地**使用、学习、修改、再分发**本项目的源码；
- 如果你基于本项目进行了修改并通过网络对外提供服务，必须**同样以 AGPL-3.0 开源**你修改后的完整源码；
- 任何衍生作品必须保留**版权声明与协议文本**。

### 免责声明

- **源码仅供学习研究使用**：本项目源码以「**按原样**」（AS IS）原则提供，仅供个人学习、技术研究与合法合规场景下的二次开发参考。
- **程序可以免费使用**：本程序可免费用于个人和非商业用途；商业用途须严格遵守 AGPL-3.0 协议条款。
- **不承担任何责任**：本程序的作者及贡献者**不承担**因使用、修改或再分发本程序所引起的任何直接或间接损失，包括但不限于：
  - 因程序 Bug、缺陷、错误或功能异常导致的**数据丢失、数据损坏、业务损失**；
  - 因不当使用造成的设备故障、系统损坏或第三方平台账号封禁；
  - 因目标平台接口变更、规则调整或策略变化导致的功能失效；
  - 其他任何因本程序引起的直接或间接损失。

**使用、修改或再分发本程序即视为您已阅读、理解并同意上述声明。**

## 打赏

如果这个工具对你有帮助，欢迎扫码支持作者一杯咖啡 ☕

![打赏码](./wechat.png)