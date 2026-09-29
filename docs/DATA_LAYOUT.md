# 本地数据目录布局

> 适用版本：v1.0.0 起（#361 改造） + #452 账号 profile 持久化

## 总览

应用启动时存在两类目录：

| 类别 | 路径 | 是否可变更 | 用途 |
|------|------|------------|------|
| **应用目录** | `<APP_DIR>/` （main.py 启动时的 cwd） | ❌ 不可变更 | 程序自身需要的家目录：配置、AI 模型 |
| **本地数据目录** | `<DATA_DIR>/` （默认最大盘根下 `ShortVideoData/`，可由设置面板切换） | ✅ 可在设置面板切换 | 业务数据：素材、成品、数据库、日志、缓存、账号登录态 |

```
<APP_DIR>/                              # 应用目录（不可变更）
├── config/                             # 全局配置（settings.json / 加密密钥 .key）
│   ├── settings.json
│   └── .key
├── models/                             # AI 模型（人脸检测模型等）
│   └── face/
└── ...（其他应用文件）

<DATA_DIR>/                             # 本地数据目录（可变更）
├── temp/                               # 临时（可清理）
├── cache/                              # 缓存（可清理，可重建）
│   ├── cover/                          # 素材封面本地缓存
│   ├── inspect/                        # 抽帧（字幕/人脸检测中间帧）
│   └── clip/                           # 片段首帧缩略图
├── log/                                # 日志（可按时间清理）
│   └── app_YYYYMMDD.log
├── db/                                 # 数据库（永久；业务删）
│   └── short_video_tools.db
├── material/                           # 素材（永久；业务删）
│   └── <uuid>.<ext>
├── create/                             # 创作中转（永久；业务删）
│   └── clip/<uuid>.mp4                 # 持久片段视频
├── finished/                           # 成品视频（永久；业务删）
│   └── <project_id>/<video_id>.<ext>
└── accounts/                           # 账号登录态（永久；业务删）
    └── <account_id>/
        ├── profile/                    # #452 chromium 持久化（cookies + IndexedDB + localStorage + SW）
        └── storage.json                # 备份（storage_state 同源快照）
```

## 账号登录态持久化（#452）

每个账号登录后会在 `accounts/<account_id>/` 下生成两个文件：

- `profile/` — chromium 完整 user data dir，由 `launch_persistent_context(user_data_dir=...)` 加载；持久化 cookies / Service Worker / IndexedDB / Cache / localStorage
- `storage.json` — `storage_state` 快照备份（与 profile 同源；profile 损坏可从此恢复）

**不要手动删除 accounts/<id>/profile/**——下次启动需重新登录；若 profile 与存储的账号列表不一致，会触发重新登录弹窗。

## 分组（#361）

### 应用目录（不可变更）

随程序所在目录，仅放程序自身需要的家目录。

- **config/** — 全局配置 `settings.json`（含 `data_dir` 指向数据目录）、加密密钥 `.key`
- **models/** — AI 模型（人脸检测模型等），首次使用从网络下载落地

> 用户不应直接修改、重定向或清理应用目录；变更需通过应用设置面板的"应用数据目录"操作（如未来提供导出/导入功能）。

### 本地数据目录（可变更）

业务数据落地，分两组：

#### 临时组（可清理）

| 子目录 | 清理方式 | 保留规则 |
|--------|----------|----------|
| `temp/` | 启动按 `temp_retention_days`（默认 7 天） | 超期删除 |
| `cache/` | 启动按 `temp_retention_days`（默认 7 天） | 超期删除；可重建 |
| `log/` | 启动按 `log_retention_days`（默认 30 天） | 超期删除 |

清理入口：
- 启动自动清理：`cleanup_startup_temp` + `notifier.cleanup_expired`
- 手动清理：设置页 → 「清理临时与缓存」（API `POST /settings/cleanup-temp-cache`）

#### 永久组（业务功能删除）

| 子目录 | 用途 | 删除方式 |
|--------|------|----------|
| `db/` | SQLite 数据库（含账号、素材元数据、任务记录、配置副本等） | 业务功能：账号/素材/任务/项目删除；卸载应用 |
| `material/` | 永久素材（视频、音乐） | 业务功能：素材库删除 |
| `create/` | 创作中转持久片段 | 业务功能：片段删除、组合释放 |
| `finished/` | 成品视频 | 业务功能：成品删除、组合释放 |
| `accounts/<id>/` | 账号登录态（chromium profile + storage.json 备份） | 业务功能：账号管理页删除账号 |

**永久组不在清理范围内**——只能通过业务功能操作删除，避免误删数据。

## 数据目录变更

用户在设置面板可重新指定 `data_dir`（保存即生效，下次启动时使用新路径）。

- 旧数据目录**不会自动迁移**——用户需手动复制（设置页提供"打开数据目录"按钮）。
- `data_dir` 路径写入应用目录 `config/settings.json`，固定不漂移。

## 备份恢复

备份范围（`POST /settings/backup`）：

- 应用目录 `config/`（除 `.key`）
- 数据目录 `db/`（SQLite 主库）
- 可选：数据目录 `material/`、`finished/`

> `create/` 当前不在备份范围（片段可由项目重新生成）。如需保留，可在备份时勾选扩展项。

恢复（`POST /settings/restore`）：

- 恢复前自动留当前状态快照（`config/.pre_restore_<ts>.zip`）
- 备份内路径前缀 `app/` 走应用目录、`data/` 走数据目录

## 旧版本迁移

v1.0.0 之前：

- 配置在 `data_dir/config/`
- 人脸模型在 `data_dir/models/face/`

启动 `init_data_dir` 时自动迁移：

- `data_dir/config/*` → `app_dir/config/*`（已存在则跳过）
- `data_dir/models/face/*` → `app_dir/models/face/*`

迁移后老目录保留空壳（避免误判用户数据），可手动删除。

## 路径相关常量（`app.services.setting_service`）

| 常量 | 含义 |
|------|------|
| `APP_DIR` | 应用目录（启动时 `Path.cwd()`） |
| `APP_CONFIG_DIR` | `APP_DIR/config` |
| `APP_MODELS_DIR` | `APP_DIR/models` |
| `DATA_DIR` | 本地数据目录（用户可改） |
| `DATA_TEMP_SUBDIRS` | `["temp", "cache", "log"]` |
| `DATA_PERM_SUBDIRS` | `["db", "material", "create", "finished"]` |
| `DATA_SUB_DIRS` | 上面两组合并 |

## 存储路径规则

所有业务文件路径在 DB 里以 **相对 data 根** 形式存储（如 `material/video/20260115/abc.mp4`），运行时拼接 `DATA_DIR` 取绝对路径。统一规则：分类与目录解耦，**改分类无需搬文件**；文件名只用 ID，**标题改动不影响路径**。

### 1. 素材（`material/`，#364 按 id 子目录聚合）

```
material/<type>/<yyyyMMdd>/<material_id>/
    ├── <material_id>.<ext>            # 视频/音频本体
    ├── <material_id>_cover.<ext>      # 封面
    └── <material_id>_avatar.<ext>     # 作者头像
```

| 字段 | 取值 |
|------|------|
| `type` | `video` / `music` |
| `yyyyMMdd` | 入库当天日期（`now_str()[:10]` 去 `-`） |
| `material_id` | 32 位 hex（应用层 `uuid4().hex`） |
| 视频 `ext` | 原文件后缀（含点），由探测或接口给 |
| 封面/头像 `ext` | 从 URL 后缀推断（`jpg/png/webp/gif/bmp`），无则 `.webp` |

示例：
```
material/video/20260115/3f2a8c…d4/3f2a8c…d4.mp4
material/video/20260115/3f2a8c…d4/3f2a8c…d4_cover.webp
material/video/20260115/3f2a8c…d4/3f2a8c…d4_avatar.webp
```

生成函数：
- `material_service._build_material_path(type, cat, id, title, ext)` — 视频本体
- `material_service._build_material_cover_path(type, id, ext)` — 封面
- `material_service._build_material_avatar_path(type, id, ext)` — 头像

**旧路径迁移**（v1.0.0 起启动自动跑一次，详见 `material_service.migrate_material_paths_to_id_subdir()`）：

- `material/<type>/<date>/<id>.<ext>` → `material/<type>/<date>/<id>/<id>.<ext>`
- `cache/cover/<id>*.webp` → `material/<type>/<date>/<id>/<id>_cover.<ext>`，DB `cover_url` 回写
- `cache/avatar/<key>.<ext>` → `material/<type>/<date>/<id>/<id>_avatar.<ext>`，DB `author_avatar` 回写

### 2. 项目片段（`project/<project_id>/clip/`，#363）

```
project/<project_id>/clip/<seq>_<clip_id>.mp4
project/<project_id>/cover/<seq>_<clip_id>.jpg      # 封面
```

| 字段 | 取值 |
|------|------|
| `project_id` | 32 位 hex（`project.id`） |
| `seq` | 项目内片段递增顺序号（1, 2, 3...） |
| `clip_id` | 32 位 hex（`project_shot_clip.id`） |
| 扩展名 | 恒为 `.mp4`（切割统一转码） |

示例：`project/4a2b…11/clip/3_9b1e0c…7a.mp4` + `project/4a2b…11/cover/3_9b1e0c…7a.jpg`

- `seq` 在 `add_clip` / `copy_project` 入口取 `_next_clip_seq(project_id)`（项目内 `MAX(seq)+1`）分配
- 文件命名按 `<seq>_<clip_id>` 排序，方便文件管理器按位次浏览
- 删除项目时 `shutil.rmtree(project/<project_id>)` 一次清完（中间产物一律清）

**旧路径迁移**（v1.0.0 起启动自动跑一次）：

- `create/clip/<id>.mp4` → `project/<pid>/clip/<seq>_<id>.mp4`
- `cache/clip/<id>.jpg` → `project/<pid>/cover/<seq>_<id>.jpg`
- `seq` 按项目内 `create_time` 升序分配 1, 2, 3...
- 详见 `creation_service.migrate_clip_paths_to_project()`

### 3. 创作成品（`finished/<project_id>/`）

```
finished/<project_id>/<seq>_<yyyyMMddHHMMSS>.mp4
```

| 字段 | 取值 |
|------|------|
| `project_id` | 32 位 hex（`project.id`） |
| `seq` | 本次生成内顺序号（从 1 开始递增） |
| `yyyyMMddHHMMSS` | 生成时间戳（紧凑无分隔符） |
| 扩展名 | 恒为 `.mp4` |

示例：`finished/4a2b…11/3_20260115142318.mp4`

清理：删除项目时 `shutil.rmtree(finished/<project_id>)`（行 132-135）。

### 4. 缓存（`cache/`）— 可清理

| 相对路径 | 用途 | 命名 |
|----------|------|------|
| ~~`cache/cover/<id>...`~~ | （#364 起迁到 `material/<type>/<date>/<id>/<id>_cover.<ext>`） | 旧路径废弃 |
| `cache/avatar/<key>.<ext>` | 账号头像（account_service，#364 范围外保留） | key = douyin_id |
| `cache/inspect/<material_id>/*.jpg` | 字幕/人脸抽帧中间产物 | 一个素材一个子目录 |
| ~~`cache/clip/<clip_id>.jpg`~~ | （#363 起迁到 `project/<pid>/cover/`） | 旧路径已废弃 |

### 5. 临时（`temp/`）— 可清理

| 相对路径 | 用途 |
|----------|------|
| `temp/gen_<generate_task_id[:8]>/c<i>_<j>.mp4` | 成品生成中：每轮即时切割的兜底片段 |
| `temp/gen_<…>/concat_<i>.mp4` | 拼接中转 |
| `temp/gen_<…>/bgm_<i>.mp4` | 混音中转 |
| `create/clip/.tmp_<clip_id>.mp4` | 片段切割中转（落在 `create/clip/` 同目录的隐藏文件） |

`temp/gen_*` 目录在 `do_generate` 末尾清理（行 1146 注释）；`create/clip/.tmp_*` 在切割失败/校验失败时 `unlink` 兜底。

### 6. 数据库（`db/`）

```
db/short_video_tools.db
db/short_video_tools.db-wal   # WAL 模式临时日志（启动 checkpoint 截断）
db/short_video_tools.db-shm   # WAL 共享内存映射
```

### 7. 日志（`log/`）

```
log/app_YYYYMMDD.log
```

按天切（loguru 时间格式化）；按 `log_retention_days` 自动清理。

## 路径与 DB 字段映射

| 相对路径模板 | DB 表 | 字段 |
|--------------|-------|------|
| `material/<type>/<date>/<id>/<id>.<ext>` | `material` | `file_path` |
| `material/<type>/<date>/<id>/<id>_cover.<ext>` | `material` | `cover_url`（本地缓存路径） |
| `material/<type>/<date>/<id>/<id>_avatar.<ext>` | `material` | `author_avatar`（本地缓存路径） |
| `project/<project_id>/clip/<seq>_<clip_id>.mp4` | `project_shot_clip` | `file_path` |
| `project/<project_id>/cover/<seq>_<clip_id>.jpg` | `project_shot_clip` | `thumb_path` |
| `project_shot_clip.seq` | `project_shot_clip` | `seq`（项目内片段顺序号） |
| `finished/<project_id>/<seq>_<ts>.mp4` | `generated_video` | `file_path` |

> DB 路径全部以正斜杠存（统一 `str(p.relative_to(DATA_DIR)).replace("\\", "/")`），跨平台兼容。
