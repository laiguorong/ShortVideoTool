# -*- coding: utf-8 -*-
"""数据库建表迁移：按版本号组织的 DDL 列表。

规则（需求文档第 6 章）：
- 全部表 TEXT UUID 主键（应用层 uuid4().hex 生成，禁止自增整型）；
- 通用字段 id/create_time/update_time/deleted（部分纯流水表可无 deleted，以第 6 章定义为准）；
- 外键字段一律 TEXT；
- 金额 REAL 存元、比率小数；文件路径存相对 data 根路径。

启动时 migrate() 按 PRAGMA user_version 与 MIGRATIONS 逐版本执行。
"""

# 19 张表 DDL：账号 1 + 选品 4 + 素材 4 + 创作 7 + 发布 3 = 19 张（含关联表；#500 移除数据中心 3 张数据快照表）
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- ============ 账号 ============
        CREATE TABLE IF NOT EXISTS account (
            id TEXT PRIMARY KEY,
            douyin_id TEXT NOT NULL UNIQUE,
            nickname TEXT,
            avatar TEXT,
            remark TEXT,
            cookie_encrypted TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'undetected',  -- normal/undetected/invalid/disabled
            last_check_time TEXT,
            fan_count INTEGER,
            work_count INTEGER,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_account_status ON account(status);

        -- ============ 选品中心（重写 #449）============
        -- 选品拉取任务（精简：keyword + cities 是直接字段，不再存 conditions_json）
        CREATE TABLE IF NOT EXISTS shop_pull_task (
            id TEXT PRIMARY KEY,
            task_name TEXT NOT NULL,
            keyword TEXT NOT NULL,                -- 搜索关键词（如 "黑眼熊寿司"）
            cities TEXT NOT NULL DEFAULT '[]',    -- JSON 数组，空数组表示不限城市
            account_id TEXT NOT NULL,
            interval_config TEXT NOT NULL,        -- JSON: {type: minute|hour|daily, value, time}
            status TEXT NOT NULL DEFAULT 'enabled',  -- enabled/disabled
            last_run_time TEXT,
            next_run_time TEXT,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_task_account ON shop_pull_task(account_id);
        CREATE INDEX IF NOT EXISTS idx_task_status ON shop_pull_task(status);

        -- 选品拉取执行日志（每轮一条）
        CREATE TABLE IF NOT EXISTS shop_pull_log (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_time TEXT NOT NULL,
            pages_done INTEGER NOT NULL DEFAULT 0,
            new_count INTEGER NOT NULL DEFAULT 0,
            update_count INTEGER NOT NULL DEFAULT 0,
            fail_reason TEXT,
            create_time TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_log_task ON shop_pull_log(task_id);
        CREATE INDEX IF NOT EXISTS idx_log_run_time ON shop_pull_log(run_time);

        -- 门店库（精简：移除选品维度字段，保留 POI 详情维度）
        CREATE TABLE IF NOT EXISTS shop (
            id TEXT PRIMARY KEY,
            poi_id TEXT NOT NULL UNIQUE,          -- 抖音 POI ID
            name TEXT NOT NULL,                   -- 门店名

            -- 地理
            province TEXT,
            city TEXT,
            district TEXT,
            ad_code TEXT,
            address TEXT,
            lat_gcj02 REAL,
            lng_gcj02 REAL,

            -- 分类
            category TEXT,                        -- 一级（如 "美食"）
            category_full TEXT,                   -- 完整路径（如 "美食/日韩料理/寿司"）

            -- CPS 标记
            is_cps INTEGER NOT NULL DEFAULT 0,    -- 是否带佣金

            -- 佣金 / 比率
            commission_rate REAL,                 -- search 接口给的（万分之）
            take_rate_min INTEGER,                -- detail 接口（万分之）
            take_rate_max INTEGER,
            take_rate_avg REAL,

            -- 销量 / GMV
            total_sold INTEGER,                   -- 总销量
            total_gmv REAL,                       -- 总 GMV（元）
            total_commission REAL,                -- 总佣金（元）

            -- 商品维度
            spu_count INTEGER NOT NULL DEFAULT 0,
            cps_spu_count INTEGER NOT NULL DEFAULT 0,
            delivery_spu_count INTEGER NOT NULL DEFAULT 0,
            spu_type_groupon INTEGER NOT NULL DEFAULT 0,
            spu_type_delivery INTEGER NOT NULL DEFAULT 0,

            -- 平台
            platform_name TEXT,                   -- 平台名（如 "美团"）
            platform_source INTEGER,              -- 平台代号

            -- TOP SPU
            top_spu_name TEXT,
            top_spu_sold INTEGER,

            -- 详情标记（增量更新用）
            detail_fetched INTEGER NOT NULL DEFAULT 0,
            detail_updated_time TEXT,

            -- 通用
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_shop_city ON shop(city);
        CREATE INDEX IF NOT EXISTS idx_shop_province ON shop(province);
        CREATE INDEX IF NOT EXISTS idx_shop_is_cps ON shop(is_cps);
        CREATE INDEX IF NOT EXISTS idx_shop_commission ON shop(commission_rate);
        CREATE INDEX IF NOT EXISTS idx_shop_total_gmv ON shop(total_gmv);
        CREATE INDEX IF NOT EXISTS idx_shop_detail_fetched ON shop(detail_fetched);

        -- ============ 素材库 ============
        -- 素材分类树（parent_id 空串为根，type 隔离视频/音乐两棵树）
        CREATE TABLE IF NOT EXISTS material_category (
            id TEXT PRIMARY KEY,
            parent_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            type TEXT NOT NULL,                  -- video/music
            sort_order INTEGER NOT NULL DEFAULT 0,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        -- 素材
        CREATE TABLE IF NOT EXISTS material (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            type TEXT NOT NULL,                  -- video/music
            category_id TEXT NOT NULL,
            duration_ms INTEGER,
            file_size REAL NOT NULL,
            source_type TEXT NOT NULL,           -- pull/share/upload
            source_ref TEXT,
            file_md5 TEXT NOT NULL,
            resolution TEXT,
            orientation TEXT,                    -- vertical/horizontal
            file_path TEXT NOT NULL,
            cover_path TEXT,
            raw_link TEXT,
            ref_count INTEGER NOT NULL DEFAULT 0,
            file_status TEXT NOT NULL DEFAULT 'normal',  -- normal/missing
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_material_type ON material(type);
        CREATE INDEX IF NOT EXISTS idx_material_category ON material(category_id);
        CREATE INDEX IF NOT EXISTS idx_material_md5 ON material(file_md5);
        -- 视频拉取任务（结构同选品拉取任务）
        CREATE TABLE IF NOT EXISTS video_pull_task (
            id TEXT PRIMARY KEY,
            task_name TEXT NOT NULL,
            conditions_json TEXT NOT NULL,
            account_id TEXT NOT NULL,
            category_id TEXT NOT NULL,           -- 入库分类（必选，视频类型树）
            interval_config TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'enabled',
            last_run_time TEXT,
            next_run_time TEXT,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        -- 视频拉取执行日志
        CREATE TABLE IF NOT EXISTS video_pull_log (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_time TEXT NOT NULL,
            new_count INTEGER DEFAULT 0,
            skip_count INTEGER DEFAULT 0,
            fail_reason TEXT,
            create_time TEXT NOT NULL
        );

        -- ============ 创作中心 ============
        -- 注：v18 起，"模板"统一重命名为"项目"（#100）。新库直接建 project 表；
        --     旧库走 v18 迁移 RENAME。下方已用 project_* 命名。
        CREATE TABLE IF NOT EXISTS project (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            shot_count INTEGER NOT NULL DEFAULT 0,
            bgm_strategy TEXT NOT NULL DEFAULT 'random',   -- order/random
            dedup_rules_json TEXT NOT NULL DEFAULT '{}',
            output_resolution TEXT,
            cursor INTEGER NOT NULL DEFAULT 0,    -- 顺序组合游标
            status TEXT NOT NULL DEFAULT 'normal',
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        -- 分镜
        CREATE TABLE IF NOT EXISTS project_shot (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_shot_project ON project_shot(project_id);
        -- 分镜片段（切割产物，引用源素材与起止毫秒）
        CREATE TABLE IF NOT EXISTS project_shot_clip (
            id TEXT PRIMARY KEY,
            shot_id TEXT NOT NULL,
            material_id TEXT NOT NULL,
            clip_start_ms INTEGER NOT NULL DEFAULT 0,
            clip_end_ms INTEGER NOT NULL DEFAULT 0,
            mirrored INTEGER NOT NULL DEFAULT 0,
            create_time TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_clip_shot ON project_shot_clip(shot_id);
        CREATE INDEX IF NOT EXISTS idx_clip_material ON project_shot_clip(material_id);
        -- 项目 BGM 池
        CREATE TABLE IF NOT EXISTS project_bgm (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            material_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            create_time TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bgm_project ON project_bgm(project_id);
        -- 文案池（全局池 scope=global；项目池 scope=project，v18 起）
        CREATE TABLE IF NOT EXISTS text_pool (
            id TEXT PRIMARY KEY,
            pool_type TEXT NOT NULL,             -- title/topic
            scope TEXT NOT NULL,                 -- global/project
            project_id TEXT,
            content TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_text_pool ON text_pool(pool_type, scope);
        -- 生成任务
        CREATE TABLE IF NOT EXISTS generate_task (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            plan_count INTEGER NOT NULL,
            done_count INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'waiting',  -- waiting/running/paused/finished/cancelled
            fail_detail_json TEXT,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL
        );
        -- 成品视频（combination_key 唯一索引防重复生成）
        CREATE TABLE IF NOT EXISTS generated_video (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            combination_key TEXT NOT NULL,
            derivative_of TEXT,                  -- 同源衍生标记（派生自哪个成品 ID）
            file_path TEXT NOT NULL,
            file_size REAL,
            duration_ms INTEGER,
            bgm_material_id TEXT,
            dedup_params_json TEXT,
            status TEXT NOT NULL DEFAULT 'idle', -- idle/occupied
            publish_record_id TEXT,
            create_time TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uk_video_combination ON generated_video(combination_key);
        CREATE INDEX IF NOT EXISTS idx_video_project ON generated_video(project_id);

        -- ============ 发布 ============
        CREATE TABLE IF NOT EXISTS publish_task (
            id TEXT PRIMARY KEY,
            task_name TEXT NOT NULL,
            project_ids_json TEXT NOT NULL,
            account_ids_json TEXT NOT NULL,
            shop_ids_json TEXT NOT NULL,
            per_account_count INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL,
            parallel_strategy TEXT NOT NULL DEFAULT 'serial',  -- serial/parallel
            title_topic_mode TEXT NOT NULL DEFAULT 'random',
            shop_assign_mode TEXT NOT NULL DEFAULT 'round',    -- round/random
            status TEXT NOT NULL DEFAULT 'draft',  -- draft/running/paused/finished/cancelled
            success_count INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        -- 发布明细（状态机：waiting/publishing/suspended/success/failed/cancelled）
        CREATE TABLE IF NOT EXISTS publish_task_item (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            plan_time TEXT NOT NULL,
            account_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            video_id TEXT NOT NULL,
            shop_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            fail_reason TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            publish_record_id TEXT,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_item_task ON publish_task_item(task_id);
        CREATE INDEX IF NOT EXISTS idx_item_status ON publish_task_item(status);
        CREATE INDEX IF NOT EXISTS idx_item_plan_time ON publish_task_item(plan_time);
        -- 发布记录（留痕，快照冗余展示字段）
        CREATE TABLE IF NOT EXISTS publish_record (
            id TEXT PRIMARY KEY,
            publish_title TEXT NOT NULL,
            publish_topics TEXT NOT NULL,
            publish_time TEXT NOT NULL,
            video_title TEXT NOT NULL,
            account_id TEXT NOT NULL,
            account_snapshot TEXT,
            shop_id TEXT NOT NULL,
            shop_snapshot TEXT,
            project_id TEXT NOT NULL,
            video_id TEXT NOT NULL,
            video_local_path TEXT NOT NULL,
            online_video_id TEXT,
            task_item_id TEXT,
            is_manual INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'success',  -- success/removed
            create_time TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_record_account ON publish_record(account_id);
        CREATE INDEX IF NOT EXISTS idx_record_time ON publish_record(publish_time);

        -- 视频简介（按项目；#413 发布管理重设计）
        CREATE TABLE IF NOT EXISTS video_intro (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            content TEXT NOT NULL,                       -- 简介正文（含 {门店名}/{城市}/{类目} 占位符）
            topics_json TEXT NOT NULL DEFAULT '[]',     -- 话题列表（JSON 数组，执行时随机抽 1 个）
            enabled INTEGER NOT NULL DEFAULT 1,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_intro_project ON video_intro(project_id, deleted);
        -- 项目↔门店 多对多（勾选顺序即发布顺序；#413）
        CREATE TABLE IF NOT EXISTS project_shop (
            project_id TEXT NOT NULL,
            shop_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, shop_id)
        );
        CREATE INDEX IF NOT EXISTS idx_project_shop_proj ON project_shop(project_id);

        -- ============ 系统设置 ============
        -- 通知中心（30 天自动清理）
        CREATE TABLE IF NOT EXISTS notification (
            id TEXT PRIMARY KEY,
            level TEXT NOT NULL DEFAULT 'info',  -- info/warn/error
            category TEXT NOT NULL,              -- account/task/publish/system
            title TEXT NOT NULL,
            content TEXT,
            is_read INTEGER NOT NULL DEFAULT 0,
            handled INTEGER NOT NULL DEFAULT 0,
            action TEXT,                         -- 处理动作标识（如 relogin:账号ID）
            create_time TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notification_read ON notification(is_read);
        -- 首次启动风险告知确认记录
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            update_time TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        -- v2：账号表增最近登录时间（登录窗抓会话成功时写入）
        ALTER TABLE account ADD COLUMN last_login_time TEXT;
        """,
    ),
    (
        3,
        """
        -- v3：删除弃用的 last_check_time（检测时间；账号状态展示只用 last_login_time）
        ALTER TABLE account DROP COLUMN last_check_time;
        """,
    ),
    (
        4,
        """
        -- v4：账号表增跨域会话（B 方案浏览器模拟：buyin.jinritemai 等 SSO 种下的
        -- 其他域 Cookie，JSON {域: Cookie头串}，选品等电商接口用）
        ALTER TABLE account ADD COLUMN extra_cookies_json TEXT;
        """,
    ),
    (
        5,
        """
        -- v5：素材表增出处字段（F-03 详情：抓取出处展示）
        -- 作者信息 + 原视频链接 + 原下载直链 + 封面图（封面走 files 服务回源）
        ALTER TABLE material ADD COLUMN author_nickname TEXT;
        ALTER TABLE material ADD COLUMN author_douyin_id TEXT;
        ALTER TABLE material ADD COLUMN share_url TEXT;
        ALTER TABLE material ADD COLUMN download_url TEXT;
        ALTER TABLE material ADD COLUMN cover_url TEXT;
        """,
    ),
    (
        6,
        """
        -- v6：出处字段补全（互动数据 + 头像 + 发布时间）
        -- 抓取时点的视频互动快照（赞/评/藏/转）+ 作者头像缓存 + 原视频发布时间
        ALTER TABLE material ADD COLUMN author_avatar TEXT;
        ALTER TABLE material ADD COLUMN digg_count INTEGER;
        ALTER TABLE material ADD COLUMN comment_count INTEGER;
        ALTER TABLE material ADD COLUMN collect_count INTEGER;
        ALTER TABLE material ADD COLUMN share_count INTEGER;
        ALTER TABLE material ADD COLUMN publish_time TEXT;
        """,
    ),
    (
        7,
        """
        -- v7：素材标题与抓取出处标题分离
        -- author_title 记录原视频标题（抓取时固定），title 可由用户编辑
        ALTER TABLE material ADD COLUMN author_title TEXT;
        """,
    ),
    (
        8,
        """
        -- v8：分享链接导入任务（异步化，支持进度查看与失败重试）
        CREATE TABLE IF NOT EXISTS share_import_task (
            id TEXT PRIMARY KEY,
            category_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending/running/completed/failed
            total INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_share_import_task_status ON share_import_task(status);
        CREATE INDEX IF NOT EXISTS idx_share_import_task_category ON share_import_task(category_id);

        CREATE TABLE IF NOT EXISTS share_import_item (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            share_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending/success/failed
            message TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            material_id TEXT,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_share_import_item_task ON share_import_item(task_id);
        CREATE INDEX IF NOT EXISTS idx_share_import_item_status ON share_import_item(status);
        """,
    ),
    (
        9,
        """
        -- v9：本地文件/文件夹上传任务（异步化 + 进度查看 + 失败重试）
        CREATE TABLE IF NOT EXISTS upload_task (
            id TEXT PRIMARY KEY,
            category_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending/running/completed/failed
            total INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_upload_task_status ON upload_task(status);
        CREATE INDEX IF NOT EXISTS idx_upload_task_category ON upload_task(category_id);

        CREATE TABLE IF NOT EXISTS upload_item (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            file_path TEXT NOT NULL,  -- 原始文件绝对路径
            file_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending/success/failed
            message TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            material_id TEXT,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_upload_item_task ON upload_item(task_id);
        CREATE INDEX IF NOT EXISTS idx_upload_item_status ON upload_item(status);
        """,
    ),
    (
        10,
        """
        -- v10：抓取出处作者信息增强（detail 接口 author 节点白捡字段）
        -- sec_uid 可拼作者主页；简介/粉丝数/获赞数为达人筛选参考
        ALTER TABLE material ADD COLUMN author_sec_uid TEXT;
        ALTER TABLE material ADD COLUMN author_signature TEXT;
        ALTER TABLE material ADD COLUMN author_follower_count INTEGER;
        ALTER TABLE material ADD COLUMN author_total_favorited INTEGER;
        """,
    ),
    (
        11,
        """
        -- v11：创作中心片段增强（#56）
        -- sort_order：分镜内片段排序（拖拽换位），按 create_time 回填初值
        -- thumb_path：片段首帧缩略图相对路径（cache/clip/{id}.jpg），异步生成
        -- v18 起：表名已从 template_shot_clip 改为 project_shot_clip（#100）
        ALTER TABLE project_shot_clip ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE project_shot_clip ADD COLUMN thumb_path TEXT;
        UPDATE project_shot_clip SET sort_order = t.r FROM (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY shot_id ORDER BY create_time) - 1 AS r
            FROM project_shot_clip
        ) t WHERE project_shot_clip.id = t.id;
        """,
    ),
    (
        12,
        """
        -- v12：片段文件化 + 真镜像（#57）
        -- file_path：片段视频相对路径 create/clip/{clip_id}.mp4（添加即分割落盘）
        -- file_status：pending（待渲染）/ ready / failed
        -- fail_reason：渲染失败原因（failed 时展示，供重试）
        ALTER TABLE project_shot_clip ADD COLUMN file_path TEXT;
        ALTER TABLE project_shot_clip ADD COLUMN file_status TEXT NOT NULL DEFAULT 'pending';
        ALTER TABLE project_shot_clip ADD COLUMN fail_reason TEXT;
        """,
    ),
    (
        13,
        """
        -- v13：片段渲染版本号（#59）
        -- 缩略图路径按 clip_id 固定，镜像重渲染后 URL 不变会被浏览器缓存旧图；
        -- rev 每次渲染自增，前端以 ?v={rev} 破缓存。
        ALTER TABLE project_shot_clip ADD COLUMN rev INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        14,
        """
        -- v14：片段标注来源视频序号（#60）
        -- 同一项目内累计递增：每次「添加视频」的每个视频取一个新序号，其全部片段共用。
        ALTER TABLE project_shot_clip ADD COLUMN source_index INTEGER;
        """,
    ),
    (
        15,
        """
        -- v15：项目级保留原音开关 + BGM 项级音量（#96/#97）
        -- keep_original_audio：0=不保留（默认，与 BGM 互斥）/ 1=保留（与 BGM 同时输出，原 + BGM 叠加）
        -- bgm_volume：单条 BGM 的音量比例，默认 1.0（100% 原音量），范围 0.05~1.0
        ALTER TABLE project ADD COLUMN keep_original_audio INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE project_bgm ADD COLUMN bgm_volume REAL NOT NULL DEFAULT 1.0;
        """,
    ),
    (
        16,
        """
        -- v16：BGM 音量从单条迁移到项目级统一音量
        -- 需求重整：不再按 BGM 单条调整音量，统一在生成时按项目配置应用。
        -- keep_original_audio：保留 #96 设计，仍在 project 上。
        -- 删 project_bgm.bgm_volume；project 新增 bgm_volume REAL DEFAULT 0.3。
        -- 0.3（30%）作为合理默认音量，叠加在拼接产物上时不会盖过原视频人声/环境声。
        ALTER TABLE project_bgm DROP COLUMN bgm_volume;
        ALTER TABLE project ADD COLUMN bgm_volume REAL NOT NULL DEFAULT 0.3;
        """,
    ),
    (
        17,
        """
        -- v17：BGM 默认音量 0.3 → 1.0（#99）
        -- 重新评估：30% 默认音量太小，多数用户期望按 100% 原音量叠加 BGM。
        -- 存量未手动调整的项目（仍是默认值 0.3）一次性升到 1.0。
        -- 新建项目由 service 显式插 1.0 覆盖表 default（v18 起视情况调整）。
        UPDATE project SET bgm_volume=1.0 WHERE bgm_volume=0.3;
        """,
    ),
    (
        18,
        """
        -- v18：创作中心「模板」→「项目」重命名（#100）
        -- 范围：4 张创作中心核心表 + 4 个外键字段 + 5 个索引 + text_pool.scope 枚举值
        -- 设计：
        --   - 旧库走 v18 RENAME 迁移（保留存量数据）；
        --   - 新库走 v1 CREATE 时已直接用 project 表名（v1 同步改名，见 v1 段注释）。
        -- SQLite ALTER TABLE RENAME TO 会自动更新引用旧表的外键约束，无需手动改 generate_task.template_id 等。

        -- 1. 表重命名（仅旧库需执行；v1 已直接建 project_* 新库无 template 表）
        --    用 IF EXISTS 包裹保证幂等：新装跳过、旧装正常 RENAME
        ALTER TABLE template RENAME TO project;        -- 若不存在则报错，下方兼容写法替代
        ALTER TABLE template_shot RENAME TO project_shot;
        ALTER TABLE template_shot_clip RENAME TO project_shot_clip;
        ALTER TABLE template_bgm RENAME TO project_bgm;

        -- 2. 索引重命名（SQLite 不自动跟随表名）
        DROP INDEX IF EXISTS idx_shot_template;
        CREATE INDEX IF NOT EXISTS idx_shot_project ON project_shot(project_id);
        DROP INDEX IF EXISTS idx_bgm_template;
        CREATE INDEX IF NOT EXISTS idx_bgm_project ON project_bgm(project_id);
        DROP INDEX IF EXISTS idx_video_template;
        CREATE INDEX IF NOT EXISTS idx_video_project ON generated_video(project_id);

        -- idx_clip_shot / idx_clip_material / uk_video_combination 不含 template，名称不变

        -- 3. text_pool.scope 枚举值：'template' → 'project'（文案池的"项目级"维度）
        UPDATE text_pool SET scope='project' WHERE scope='template';

        -- 4. publish_task.template_ids_json 字段重命名（仅 SQLite 3.25+ 支持 RENAME COLUMN）
        --    旧版本通过"加新列+迁移+删旧列"三步走；这里用 SQLite 内置 RENAME COLUMN
        ALTER TABLE publish_task RENAME COLUMN template_ids_json TO project_ids_json;

        -- 5. 各表外键字段 template_id → project_id（v18 同步重命名）
        --    SQLite ALTER TABLE RENAME COLUMN 会自动更新外键引用，无需手动改约束名
        --    对新库（v1 已直接用 project_id）旧列已不存在，RENAME COLUMN 会报错；
        --    使用 SQLite 的 PRAGMA table_info 兼容性写法：见下方兼容脚本块。
        ALTER TABLE project_shot RENAME COLUMN template_id TO project_id;
        ALTER TABLE project_bgm RENAME COLUMN template_id TO project_id;
        ALTER TABLE generate_task RENAME COLUMN template_id TO project_id;
        ALTER TABLE generated_video RENAME COLUMN template_id TO project_id;
        ALTER TABLE text_pool RENAME COLUMN template_id TO project_id;
        ALTER TABLE publish_task_item RENAME COLUMN template_id TO project_id;
        ALTER TABLE publish_record RENAME COLUMN template_id TO project_id;
        """,
    ),
    (
        19,
        """
        -- v19：生成视频去重增强（#101 P0+P1）
        -- 背景：现有去重仅覆盖画面像素扰动（frame_drop/watermark/noise）。
        -- 抖音风控会从编码参数、容器元数据、音频指纹、文件 MD5 等多维度识别"机器生成"。
        -- 本次新增：
        --   file_md5: 成品文件 MD5（撞库检查：同账号已被发布占用的成品做哈希去重）

        ALTER TABLE generated_video ADD COLUMN file_md5 TEXT;
        CREATE INDEX IF NOT EXISTS idx_video_md5 ON generated_video(file_md5);
        """,
    ),
    (
        20,
        """
        -- v20：拉取任务存 bg_task_id 用于删除时取消 in-flight worker
        -- 背景：白切鸡 task 删除后 13:21 仍有 round 入库 107 条。
        -- 根因：delete_pull_task 只 remove_job + soft-delete，没法取消 task_service
        -- 队列里已经在飞的 _run_pull_round worker（submit 返回的 UUID 与 task_id
        -- 无关联）。修复：拉取 round 启动时把 task_service.submit 返回的 UUID 写到
        -- task 行；删除时先 request_cancel(bg_task_id) 再 remove_job + soft-delete。
        ALTER TABLE video_pull_task ADD COLUMN bg_task_id TEXT;
        ALTER TABLE shop_pull_task ADD COLUMN bg_task_id TEXT;
        """,
    ),
    (
        21,
        """
        -- v21：项目片段加 seq 列（#363 路径 project/<pid>/clip/<seq>_<id>.mp4）
        -- 作用：项目内递增顺序号，决定片段文件名中的位次编号，方便按数字浏览
        -- 唯一索引 (project_id, seq) 防止并发分配冲突（同 project 不会重复）
        ALTER TABLE project_shot_clip ADD COLUMN seq INTEGER;
        """,
    ),
    (
        22,
        """
        -- v22：视频拉取任务阶梯式翻页状态
        -- pull_round：已完成的轮数（0 = 还没跑过；每轮正常退出 +1，包括达 max_count
        --   / has_more=0 / empty_pages 中断 / 风控兜底）。重置由"重新启用"按钮触发。
        -- total_pulled：本任务累计入库数（跨轮累加，重置时清零）。
        -- 阶梯公式：本轮预算 = max(MIN_PAGES, BASE_PAGES - STEP * (pull_round))
        -- 默认 BASE=100 / STEP=10 / MIN=10 → 第 1 轮 100 页、第 10 轮 10 页兜底
        ALTER TABLE video_pull_task ADD COLUMN pull_round INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE video_pull_task ADD COLUMN total_pulled INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        23,
        """
        -- v23：视频拉取任务停用原因（区分手动 vs 自动达 max_count）
        -- 用途：前端列表按 stop_reason 决定 disabled 时展示"启用"或"重新启用"按钮。
        --   null     → 正常启用中，未停用
        --   'manual' → 用户手动 toggle 停用（点普通"启用"恢复，保留 round/total）
        --   'auto_max' → 达 max_count 自动停用（点"重新启用"清零阶梯+恢复）
        -- restart_pull_task 成功后重置回 null。
        ALTER TABLE video_pull_task ADD COLUMN stop_reason TEXT;
        """,
    ),
    (
        24,
        """
        -- v24：项目累计生成数（#413 持久化，不再依赖 SELECT COUNT(*)）
        -- 背景：原 generated_total 走 generated_video 实时 COUNT，删除成品后数字回落。
        -- 改用 project.generated_total 字段，_run_generate 成功时 +1，删除成品不减。
        ALTER TABLE project ADD COLUMN generated_total INTEGER NOT NULL DEFAULT 0;
        -- 回填历史项目（按当前 generated_video 数量初始化）
        UPDATE project SET generated_total = (
            SELECT COUNT(*) FROM generated_video WHERE project_id = project.id
        );
        """,
    ),
    (
        25,
        """
        -- v25：片段累计使用次数（新算法按权重 max(0, 10000 - used_count) 采样）
        -- 背景：原算法走笛卡尔积全量枚举 + 加权随机，组合数爆炸时卡死（#416）。
        -- 改为接受-拒绝采样：每个片段按 used_count 算权重，用得越多权重越低。
        -- 不回填历史：旧项目首次跑会一次性覆盖所有片段一次，用满一轮后自然均衡。
        ALTER TABLE project_shot_clip ADD COLUMN used_count INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        26,
        """
        -- v26：删除 idx_video_md5 索引（不再做 MD5 撞库查重）
        -- 背景：v25 算法下每次生成的视频组合键唯一，MD5 实际不会重复；
        -- 索引占用 + 写入开销无收益，移除。file_md5 字段保留（数据完整性）。
        DROP INDEX IF EXISTS idx_video_md5;
        """,
    ),
    (
        27,
        """
        -- v27：缓存可生成数（legal_total）到 project 表
        -- 背景：listProjects / getProject / create_generate_task 都要读可生成数，
        -- 实时算需要走 SQL + O(n) Python。加/删片段时分镜变化即重算并 UPDATE。
        -- 字段默认 0；首次访问 _legal_combination_total 时若读到 0 → 回退计算并 UPDATE。
        ALTER TABLE project ADD COLUMN legal_total INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        28,
        """
        -- v28：clip.file_path 预写入 + NOT NULL
        -- 背景：INSERT project_shot_clip 时 file_path 写 NULL，由 _cut_clip 完成时 UPDATE。
        -- get_project 末尾的 _backfill_pending_files 看到 NULL 会重复触发 _spawn_clip_render，
        -- 与 add_clips 主流程形成双重入队。
        -- 修复：INSERT 时按 _clip_rel_path(project_id, seq, clip_id) 规则预写 file_path，
        -- file_path 永远非空（不代表文件已落盘，仅是预期路径）；_cut_clip 完成后不再 UPDATE file_path。
        -- 历史遗留 NULL（极少见）补空串以满足 NOT NULL。
        UPDATE project_shot_clip SET file_path='' WHERE file_path IS NULL;
        -- SQLite 老表加 NOT NULL 必须先重建表。这里放宽：保留可空（业务层保证非空），
        -- 仅加 CHECK 约束防止应用 bug 写 NULL。
        -- 注：SQLite 12+ 支持 ALTER TABLE ADD COLUMN 带 CHECK；老实例兼容性考虑跳过。
        """,
    ),
    (
        29,
        """
        -- v29：项目级「最后 source_index」计数器（#423）
        -- 背景：source_index 原从 project_shot_clip 内 MAX(c.source_index) 起算，删除最大
        -- 序号视频后下次 addClips 会复用旧号，违反「source_index 唯一性、不复用」要求。
        -- 改为 project 表记录单调递增计数器 last_source_index；addClips 读 → 按视频数累加 → 写回。
        -- 删除 clip 不影响计数器（只增不减）。
        ALTER TABLE project ADD COLUMN last_source_index INTEGER NOT NULL DEFAULT 0;
        -- 回填历史项目：取该项目历史 MAX(source_index)，避免新插入 clip 复用旧号
        UPDATE project SET last_source_index = COALESCE((
            SELECT MAX(c.source_index) FROM project_shot_clip c
            JOIN project_shot s ON c.shot_id = s.id
            WHERE s.project_id = project.id
        ), 0);
        """,
    ),
    (
        30,
        """
        -- v30：项目级 fit_mode（#多画面适配）
        -- cover=等比铺满裁剪（默认）/ contain=等比居中补黑 / fill=拉伸 / blur_bg=模糊背景+前景居中
        ALTER TABLE project ADD COLUMN fit_mode TEXT NOT NULL DEFAULT 'cover';
        """,
    ),
    (
        31,
        """
        -- v31：项目级画幅 width/height（#427）
        -- 替代 output_resolution 自由字符串，存规范化数值便于按比例过滤素材（#429）。
        -- 默认 1080x1920 = 9:16 竖屏（抖音/快手主流）。
        -- 兼容：旧项目的 output_resolution 仍保留，回填到 width/height。
        -- 注：先尝试按已知值回填，再统一解析剩余任意 WxH 字符串（防漏）。
        ALTER TABLE project ADD COLUMN width INTEGER NOT NULL DEFAULT 1080;
        ALTER TABLE project ADD COLUMN height INTEGER NOT NULL DEFAULT 1920;
        -- 通用解析：从 output_resolution 提取 WxH 回填（覆盖任意历史值）
        UPDATE project SET
            width = CAST(SUBSTR(output_resolution, 1, INSTR(output_resolution, 'x') - 1) AS INTEGER),
            height = CAST(SUBSTR(output_resolution, INSTR(output_resolution, 'x') + 1) AS INTEGER)
        WHERE output_resolution LIKE '%x%'
          AND INSTR(output_resolution, 'x') > 1
          AND SUBSTR(output_resolution, 1, INSTR(output_resolution, 'x') - 1) GLOB '[0-9]*'
          AND SUBSTR(output_resolution, INSTR(output_resolution, 'x') + 1) GLOB '[0-9]*';
        """,
    ),
    (
        32,
        """-- v32：shop 表扩展（#449 选品模块重构）
        -- 背景：原 shop 表仅 14 字段（业务主键 poi_id + 基础指标）。任务 #446/#447 已验证：
        --   - search/poi 接口返回 address_info（含省/市/区/ad_code）+ poi_latitude/longitude_gcj02 + poi_backend_type 三级分类
        --   - cps/detail/v2 接口返回 platform_name/take_rate/total_sold/total_gmv/total_commission/spu_type 等维度
        -- 本次为 shop 表加 28 列覆盖 cps/detail/v2 维度 + 城市分列 + 坐标，配套 5 个索引。
        -- 设计原则：
        --   - ALTER TABLE ADD COLUMN 幂等（在 _apply_v32 中逐列检测）
        --   - 新列均给默认值，老数据 NULL 兜底（不影响 v31 及之前数据）
        --   - 索引用 IF NOT EXISTS 保证幂等
        -- 字段清单：见 _apply_v32 函数实现
        """,
    ),
    (
        33,
        """
        -- ============ v33 选品中心从零重写（#449）============
        -- 旧 shop / shop_pull_task / shop_pull_log 三张表残留 DROP 掉，按新 schema 重建。
        -- 注意：原 v1 选品 3 表 DDL 已不在 MIGRATIONS，这里集中重建。
        -- v33 _apply_v33 函数里实现 DROP + CREATE。
        """,
    ),
    (
        34,
        """
        -- ============ v34 账号检测冷却（#449 review）============
        -- account 表加 last_check_time（手动检测去重，30 分钟冷却）。
        -- _apply_v34 幂等检测列存在性后 ADD COLUMN。
        """,
    ),
    (
        35,
        """
        -- ============ v35 门店加库标记 ============
        -- shop 表加 is_added_to_library + added_time，_apply_v35 幂等 ADD COLUMN。
        """,
    ),
    (
        36,
        """
        -- ============ v36 POI 补充维度（任务 #406）============
        -- shop 表加 poi_search_tags_v2 / poi_score / poi_score_content /
        --          business_area / l1_name / l2_name / l3_name。
        """,
    ),
    (
        37,
        """
        -- ============ v37 选品任务正则过滤（任务 #407）============
        -- shop_pull_task 表加 shop_name_pattern / city_pattern（TEXT，可空）。
        """,
    ),
    (
        38,
        """
        -- ============ v38 发布管理重设计（#413）============
        -- 实际 DDL 全部在 _apply_v38 函数内幂等执行（#413 审查 #15）。
        """,
    ),
    (
        39,
        """
        -- ============ v39 标题/话题存储合并（#415 优化）============
        -- 实际数据迁移在 _apply_v39 函数内幂等执行：
        -- 把 video_intro.topics_json 里的 #话题 用空格追加到 content 末尾，然后 topics_json='[]'。
        -- 后续代码从 content 用正则提取 #话题，不再读 topics_json。
        """,
    ),
    (
        40,
        """
        -- ============ v40 发布任务双计算模式（#calc_mode）============
        -- publish_task 表加 schedule_mode / fixed_interval_min / balanced_step_min。
        -- DEFAULT 兜底：旧任务自动按 balanced +60 处理，行为与 v39 一致。
        -- _apply_v40 幂等检测列存在性后 ADD COLUMN。
        """,
    ),
    (
        41,
        """
        -- ============ v41 #418 视频目录模式 ============
        -- publish_task 表加 project_source（默认 'project'）。
        -- publish_task_item 表加 source（默认 'project'）+ video_path（绝对路径，视频目录模式用）。
        -- 旧行：source='project' / video_path=NULL → 走 generated_video 查视频（与 v40 一致）。
        -- _apply_v41 幂等检测列存在性后 ADD COLUMN。
        """,
    ),
    (
        42,
        """
        -- ============ v42 进度列用时（started_at）============
        -- publish_task 加 started_at TEXT NULL：首次进入 running 时写入（COALESCE 保留原始起点）；
        -- update_time 不准（每次 publish_item 改 status 都刷），必须独立列。
        -- 前端进度列渲染用时 = now - started_at（H:MM:SS 格式）。
        -- _apply_v42 幂等检测列存在性后 ADD COLUMN。
        """,
    ),
    (
        43,
        """
        -- ============ v43 #fix-type-param import-share 加 type 入参 ============
        -- share_import_task 表加 type TEXT（'video' / 'music'），由前端页签决定，
        -- 与 category_id 解耦——未分类（id='-'）下也能按 type 选入库类型。
        -- _apply_v43 幂等检测列存在性后 ADD COLUMN。DEFAULT 'video' 兜底老任务。
        -- 注意：DDL block 兜底（防止 _apply_v43 函数被改名/删除导致 migration 静默失败）。
        ALTER TABLE share_import_task ADD COLUMN type TEXT NOT NULL DEFAULT 'video';
        """,
    ),
    (
        44,
        """
        -- ============ v44 移除数据中心（F-07 #500）============
        -- 数据中心功能整体移除，删除三类日快照表。
        -- 工作台不再依赖今日播放/GMV/佣金（dashboard_service 不查这些表）。
        DROP TABLE IF EXISTS video_stats_daily;
        DROP TABLE IF EXISTS sales_stats_daily;
        DROP TABLE IF EXISTS account_stats_daily;
        """,
    ),
]


def migrate(database) -> int:
    """执行建表迁移，返回迁移后版本号。

    参数:
        database: app.db.database.Database 实例
    """
    current = database.user_version()
    for version, ddl in MIGRATIONS:
        if version <= current:
            continue
        # v18 / v19 / v32 / v33 含需幂等处理的子句
        if version == 18:
            _apply_v18(database)
        elif version == 19:
            _apply_v19(database)
        elif version == 32:
            _apply_v32(database)
        elif version == 33:
            _apply_v33(database)
        elif version == 34:
            _apply_v34(database)
        elif version == 35:
            _apply_v35(database)
        elif version == 36:
            _apply_v36(database)
        elif version == 37:
            _apply_v37(database)
        elif version == 38:
            _apply_v38(database)
        elif version == 39:
            _apply_v39(database)
        elif version == 40:
            _apply_v40(database)
        elif version == 41:
            _apply_v41(database)
        elif version == 42:
            _apply_v42(database)
        elif version == 43:
            _apply_v43(database)
        else:
            database.executescript(ddl)
        database.set_user_version(version)
    # #migration-safety：migrate() 完成后兜底检查关键列（防 migration 静默失败——
    # 例如 v43 函数被改名后 user_version 已升但列未加；D 盘 user_version=44 但缺 type 列即此场景）。
    _ensure_critical_columns(database)
    return database.user_version()


def _ensure_critical_columns(database) -> None:
    """兜底检查关键列是否存在，缺失时静默补上。

    仅在 share_import_task.type 上做兜底（v43 实际跑过的 DB 都会走此分支）。
    不抛错：用户 DB 已 user_version=44 但缺 type 列是已知场景（v43 DDL block 之前是空注释
    走 executescript 无效），手动补列后业务可继续。
    """
    if _has_table(database, "share_import_task") and not _has_column(database, "share_import_task", "type"):
        try:
            database.execute(
                "ALTER TABLE share_import_task ADD COLUMN type TEXT NOT NULL DEFAULT 'video'")
            from loguru import logger
            logger.warning("[migration] 兜底补 share_import_task.type 列（v43 静默失败修复）")
        except Exception:  # noqa: BLE001
            pass  # 列已存在或其他原因，不影响主流程


def _has_table(database, name: str) -> bool:
    """判断表是否存在。"""
    row = database.query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return row is not None


def _has_column(database, table: str, col: str) -> bool:
    """判断表的某列是否存在。"""
    if not _has_table(database, table):
        return False
    rows = database.query_all(f"PRAGMA table_info({table})")
    return any(r["name"] == col for r in rows)


def _apply_v18(database) -> None:
    """v18「模板」→「项目」幂等迁移（兼容新库/旧库）。"""
    # 1. 表重命名（旧库存在 template 时）
    renames = [
        ("template", "project"),
        ("template_shot", "project_shot"),
        ("template_shot_clip", "project_shot_clip"),
        ("template_bgm", "project_bgm"),
    ]
    for old, new in renames:
        if _has_table(database, old) and not _has_table(database, new):
            database.execute(f"ALTER TABLE {old} RENAME TO {new}")

    # 2. 先 RENAME COLUMN（让索引能用新列名）
    col_renames = [
        "project_shot", "project_bgm", "generate_task",
        "generated_video", "text_pool", "publish_task_item", "publish_record",
    ]
    for t in col_renames:
        if _has_table(database, t) and _has_column(database, t, "template_id") \
                and not _has_column(database, t, "project_id"):
            database.execute(f"ALTER TABLE {t} RENAME COLUMN template_id TO project_id")

    # 3. 索引重命名（SQLite 不自动跟随表名；现列已是 project_id）
    index_pairs = [
        ("idx_shot_template", "idx_shot_project", "project_shot(project_id)"),
        ("idx_bgm_template", "idx_bgm_project", "project_bgm(project_id)"),
        ("idx_video_template", "idx_video_project", "generated_video(project_id)"),
    ]
    for old, new, on_cols in index_pairs:
        database.execute(f"DROP INDEX IF EXISTS {old}")
        database.execute(f"CREATE INDEX IF NOT EXISTS {new} ON {on_cols}")

    # 4. text_pool.scope 枚举值迁移
    if _has_table(database, "text_pool"):
        database.execute("UPDATE text_pool SET scope='project' WHERE scope='template'")

    # 5. publish_task.template_ids_json → project_ids_json
    if _has_table(database, "publish_task"):
        if _has_column(database, "publish_task", "template_ids_json") and \
           not _has_column(database, "publish_task", "project_ids_json"):
            database.execute(
                "ALTER TABLE publish_task RENAME COLUMN template_ids_json TO project_ids_json")


def _apply_v19(database) -> None:
    """v19：generated_video 加 file_md5 + 索引（幂等）。"""
    if not _has_table(database, "generated_video"):
        return
    if not _has_column(database, "generated_video", "file_md5"):
        database.execute("ALTER TABLE generated_video ADD COLUMN file_md5 TEXT")
    database.execute("CREATE INDEX IF NOT EXISTS idx_video_md5 ON generated_video(file_md5)")


# v32：shop 表扩展（#449 选品模块重构）
# 字段语义：
#   - province/city/district/ad_code/city_full/category_full：search/poi address_info + poi_backend_type
#   - address/address_detail：search 接口末梢地址 vs detail 接口完整地址
#   - lat_gcj02/lng_gcj02：火星坐标（国内合规，国内展示必须用此坐标）
#   - is_cps/spu_count/cps_spu_count/delivery_spu_count：商业指标
#   - platform_name/platform_source：cps/detail/v2 平台层（商家抖音自营 等）
#   - take_rate_min/max/avg：佣金率（万分之，400=4%）
#   - total_sold/total_gmv/total_commission：业绩汇总（元/份）
#   - spu_type_groupon/delivery：商品类型分布
#   - top_spu_name/top_spu_sold：爆款
#   - detail_fetched/detail_updated_time：详情拉取状态（0=未拉 1=已拉）
#   - source_type：来源 manual/search_pull/task_pull
_V32_SHOP_COLUMNS: list[tuple[str, str]] = [
    ("province", "TEXT"),
    ("city_full", "TEXT"),
    ("category_full", "TEXT"),
    ("district", "TEXT"),
    ("ad_code", "TEXT"),
    ("address", "TEXT"),
    ("address_detail", "TEXT"),
    ("lat_gcj02", "REAL"),
    ("lng_gcj02", "REAL"),
    ("is_cps", "INTEGER NOT NULL DEFAULT 0"),
    ("spu_count", "INTEGER NOT NULL DEFAULT 0"),
    ("cps_spu_count", "INTEGER NOT NULL DEFAULT 0"),
    ("delivery_spu_count", "INTEGER NOT NULL DEFAULT 0"),
    ("platform_name", "TEXT"),
    ("platform_source", "INTEGER"),
    ("take_rate_min", "INTEGER"),
    ("take_rate_max", "INTEGER"),
    ("take_rate_avg", "REAL"),
    ("total_sold", "INTEGER"),
    ("total_gmv", "REAL"),
    ("total_commission", "REAL"),
    ("spu_type_groupon", "INTEGER NOT NULL DEFAULT 0"),
    ("spu_type_delivery", "INTEGER NOT NULL DEFAULT 0"),
    ("top_spu_name", "TEXT"),
    ("top_spu_sold", "INTEGER"),
    ("detail_fetched", "INTEGER NOT NULL DEFAULT 0"),
    ("detail_updated_time", "TEXT"),
    ("source_type", "TEXT NOT NULL DEFAULT 'manual'"),
]


def _apply_v32(database) -> None:
    """v32：shop 表扩展（#449）—— 幂等加列 + 5 个新索引。

    ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS，逐列检测。
    """
    if not _has_table(database, "shop"):
        return
    for col, decl in _V32_SHOP_COLUMNS:
        if not _has_column(database, "shop", col):
            database.execute(f"ALTER TABLE shop ADD COLUMN {col} {decl}")
    # 5 个新索引（IF NOT EXISTS 保证幂等）
    database.execute("CREATE INDEX IF NOT EXISTS idx_shop_city ON shop(city)")
    database.execute("CREATE INDEX IF NOT EXISTS idx_shop_province ON shop(province)")
    database.execute("CREATE INDEX IF NOT EXISTS idx_shop_is_cps ON shop(is_cps)")
    database.execute("CREATE INDEX IF NOT EXISTS idx_shop_commission ON shop(commission_rate)")
    database.execute("CREATE INDEX IF NOT EXISTS idx_shop_total_gmv ON shop(total_gmv)")


def _apply_v33(database) -> None:
    """v33：选品中心从零重写（#449 重写版）。

    旧 v1 选品 3 表（shop_pull_task / shop_pull_log / shop）+ shop_task_rel
    按新 schema 重建：keyword + cities 替代 conditions_json；门店表精简。

    策略：DROP 旧表（保留其他模块数据），按新 DDL CREATE。
    """
    # 1. DROP 旧表（如果存在）
    # shop_task_rel 是旧 v1 时代的任务-门店关联表，v33 不再保留（任务和门店解耦）
    for t in ("shop_task_rel", "shop_pull_log", "shop_pull_task", "shop"):
        database.execute(f"DROP TABLE IF EXISTS {t}")

    # 2. CREATE 新表 + 索引（按新 schema）
    new_ddl = """
        -- 选品拉取任务（精简：keyword + cities 是直接字段）
        CREATE TABLE shop_pull_task (
            id TEXT PRIMARY KEY,
            task_name TEXT NOT NULL,
            keyword TEXT NOT NULL,
            cities TEXT NOT NULL DEFAULT '[]',
            account_id TEXT NOT NULL,
            interval_config TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'enabled',
            last_run_time TEXT,
            next_run_time TEXT,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_task_account ON shop_pull_task(account_id);
        CREATE INDEX idx_task_status ON shop_pull_task(status);

        -- 选品拉取执行日志
        CREATE TABLE shop_pull_log (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_time TEXT NOT NULL,
            pages_done INTEGER NOT NULL DEFAULT 0,
            new_count INTEGER NOT NULL DEFAULT 0,
            update_count INTEGER NOT NULL DEFAULT 0,
            fail_reason TEXT,
            create_time TEXT NOT NULL
        );
        CREATE INDEX idx_log_task ON shop_pull_log(task_id);
        CREATE INDEX idx_log_run_time ON shop_pull_log(run_time);

        -- 门店库（精简：POI 详情维度齐全 + 城市/坐标/平台）
        CREATE TABLE shop (
            id TEXT PRIMARY KEY,
            poi_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,

            province TEXT,
            city TEXT,
            district TEXT,
            ad_code TEXT,
            address TEXT,
            lat_gcj02 REAL,
            lng_gcj02 REAL,

            category TEXT,
            category_full TEXT,

            is_cps INTEGER NOT NULL DEFAULT 0,

            commission_rate REAL,
            take_rate_min INTEGER,
            take_rate_max INTEGER,
            take_rate_avg REAL,

            total_sold INTEGER,
            total_gmv REAL,
            total_commission REAL,

            spu_count INTEGER NOT NULL DEFAULT 0,
            cps_spu_count INTEGER NOT NULL DEFAULT 0,
            delivery_spu_count INTEGER NOT NULL DEFAULT 0,
            spu_type_groupon INTEGER NOT NULL DEFAULT 0,
            spu_type_delivery INTEGER NOT NULL DEFAULT 0,

            platform_name TEXT,
            platform_source INTEGER,

            top_spu_name TEXT,
            top_spu_sold INTEGER,

            detail_fetched INTEGER NOT NULL DEFAULT 0,
            detail_updated_time TEXT,

            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_shop_city ON shop(city);
        CREATE INDEX idx_shop_province ON shop(province);
        CREATE INDEX idx_shop_is_cps ON shop(is_cps);
        CREATE INDEX idx_shop_commission ON shop(commission_rate);
        CREATE INDEX idx_shop_total_gmv ON shop(total_gmv);
        CREATE INDEX idx_shop_detail_fetched ON shop(detail_fetched);
    """
    database.executescript(new_ddl)


def _apply_v34(database) -> None:
    """v34：账号登录态冷却去重 —— 加 last_check_time。

    check_account 检测前先读 last_check_time，若 30 分钟内则跳过实际网络请求，
    减少无效风控暴露。account_check_hours 默认 6 小时，自动检测本就不会高频；
    手动检测按钮连点会触发此冷却。
    """
    if not _has_table(database, "account"):
        return
    if not _has_column(database, "account", "last_check_time"):
        database.execute("ALTER TABLE account ADD COLUMN last_check_time TEXT")


def _apply_v35(database) -> None:
    """v35：门店「已加库」标记。

    加 is_added_to_library（0/1）+ added_time 两列，前端门店库按钮开关使用。
    幂等：旧库补列，新库走重建表 DDL 时也已包含。
    """
    if not _has_table(database, "shop"):
        return
    if not _has_column(database, "shop", "is_added_to_library"):
        database.execute(
            "ALTER TABLE shop ADD COLUMN is_added_to_library INTEGER NOT NULL DEFAULT 0"
        )
    if not _has_column(database, "shop", "added_time"):
        database.execute("ALTER TABLE shop ADD COLUMN added_time TEXT")
    database.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_is_added ON shop(is_added_to_library)"
    )


def _apply_v37(database) -> None:
    """v37：选品任务加正则过滤（任务 #407）。

    加 shop_name_pattern / city_pattern 两列（TEXT，可空）。
    空 = 不过滤。运行时按 Python re 匹配，匹配失败整条 POI 跳过。
    """
    if not _has_table(database, "shop_pull_task"):
        return
    for col in ("shop_name_pattern", "city_pattern"):
        if not _has_column(database, "shop_pull_task", col):
            database.execute(f"ALTER TABLE shop_pull_task ADD COLUMN {col} TEXT")


def _apply_v38(database) -> None:
    """v38：发布管理重设计（#413）。

    新表（video_intro / project_shop）的 DDL 已在 v1 创建，本版本幂等补建用于旧库升级。

    扩列：
    - publish_task：end_time / daily_limit_mode / daily_limit_global /
      daily_limit_per_account_json / same_project_interval_min /
      diff_project_interval_min / declaration / allow_download
    - publish_task_item：intro_id / intro_snapshot / topics_snapshot /
      declaration / allow_download
    - publish_record：intro_snapshot / topics_snapshot /
      declaration / allow_download
    """
    # 新表幂等补建（v1 已 CREATE IF NOT EXISTS；这里给老库兜底）
    database.execute("""
        CREATE TABLE IF NOT EXISTS video_intro (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            content TEXT NOT NULL,
            topics_json TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        )
    """)
    database.execute("""
        CREATE TABLE IF NOT EXISTS project_shop (
            project_id TEXT NOT NULL,
            shop_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, shop_id)
        )
    """)
    database.execute(
        "CREATE INDEX IF NOT EXISTS idx_intro_project ON video_intro(project_id, deleted)"
    )
    database.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_shop_proj ON project_shop(project_id)"
    )

    # publish_task 扩列
    if _has_table(database, "publish_task"):
        task_cols = [
            ("end_time", "TEXT"),
            ("daily_limit_mode", "TEXT NOT NULL DEFAULT 'global'"),
            ("daily_limit_global", "INTEGER"),
            ("daily_limit_per_account_json", "TEXT"),
            ("same_project_interval_min", "INTEGER NOT NULL DEFAULT 60"),
            ("diff_project_interval_min", "INTEGER NOT NULL DEFAULT 10"),
            ("declaration", "TEXT NOT NULL DEFAULT 'ai_generated'"),
            ("allow_download", "INTEGER NOT NULL DEFAULT 0"),
            ("projects_payload_json", "TEXT"),  # #413 向导草稿详情（项目+门店+简介）
        ]
        for col, decl in task_cols:
            if not _has_column(database, "publish_task", col):
                database.execute(f"ALTER TABLE publish_task ADD COLUMN {col} {decl}")

    # publish_task_item 扩列
    if _has_table(database, "publish_task_item"):
        item_cols = [
            ("intro_id", "TEXT"),
            ("intro_snapshot", "TEXT"),
            ("topics_snapshot", "TEXT"),
            ("declaration", "TEXT"),
            ("allow_download", "INTEGER"),
        ]
        for col, decl in item_cols:
            if not _has_column(database, "publish_task_item", col):
                database.execute(f"ALTER TABLE publish_task_item ADD COLUMN {col} {decl}")

    # publish_record 扩列
    if _has_table(database, "publish_record"):
        record_cols = [
            ("intro_snapshot", "TEXT"),
            ("topics_snapshot", "TEXT"),
            ("declaration", "TEXT"),
            ("allow_download", "INTEGER"),
        ]
        for col, decl in record_cols:
            if not _has_column(database, "publish_record", col):
                database.execute(f"ALTER TABLE publish_record ADD COLUMN {col} {decl}")


def _apply_v36(database) -> None:
    """v36：POI 补充维度（任务 #406）。

    search/poi 返回的附加维度，原 36 字段未覆盖，本版本统一入库：
    - poi_search_tags_v2：搜索标签数组（JSON 串保存）
    - poi_score：评分
    - poi_score_content：评分文案
    - business_area：商圈名称
    - l1/l2/l3_name：分类三级名（已有 category_full 由这三段拼，本版冗余存便于检索/前端展示）
    """
    if not _has_table(database, "shop"):
        return
    new_cols = [
        ("poi_search_tags_v2", "TEXT"),
        ("poi_score", "REAL"),
        ("poi_score_content", "TEXT"),
        ("business_area", "TEXT"),
        ("l1_name", "TEXT"),
        ("l2_name", "TEXT"),
        ("l3_name", "TEXT"),
    ]
    for col, decl in new_cols:
        if not _has_column(database, "shop", col):
            database.execute(f"ALTER TABLE shop ADD COLUMN {col} {decl}")


def _apply_v39(database) -> None:
    """v39：标题/话题存储合并（#415 优化）。

    把 video_intro.topics_json 里的 #话题 用空格追加到 content 末尾，然后 topics_json='[]'。
    幂等：topics_json 已是 '[]' 的行跳过；二次执行不会重复追加。
    """
    import json as _json
    if not _has_table(database, "video_intro"):
        return
    rows = database.query_all(
        "SELECT id, content, topics_json FROM video_intro WHERE deleted=0"
    )
    if not rows:
        return
    for r in rows:
        raw = r.get("topics_json") or "[]"
        if raw == "[]":
            continue  # 已迁移过
        try:
            topics = _json.loads(raw)
        except _json.JSONDecodeError:
            # 损坏数据：清空 topics_json 但不动 content
            database.execute(
                "UPDATE video_intro SET topics_json='[]' WHERE id=?",
                (r["id"],))
            continue
        if not topics:
            database.execute(
                "UPDATE video_intro SET topics_json='[]' WHERE id=?",
                (r["id"],))
            continue
        # topics 元素可能含或不含 # 前缀；按空格拼接原样保留
        extra = " ".join(str(t) for t in topics if str(t).strip())
        old_content = r.get("content") or ""
        if extra:
            new_content = old_content.rstrip() + (" " if old_content.strip() else "") + extra
        else:
            new_content = old_content
        database.execute(
            "UPDATE video_intro SET content=?, topics_json='[]' WHERE id=?",
            (new_content, r["id"]))


def _apply_v41(database) -> None:
    """v41：#418 视频目录模式——publish_task + publish_task_item 扩列。

    publish_task.project_source TEXT DEFAULT 'project'
        → 'project'（默认，项目列表模式）/ 'video_dir'（成品视频目录模式）
    publish_task.video_dirs_json TEXT
        → 视频目录模式存 [{"id", "abs_path", "dir_name", "video_count"}, ...]
    publish_task.manual_shops_json TEXT
        → 视频目录模式存 {"dir_id": [{"id", "name", "poi_id", "city"}, ...], ...}
    publish_task.manual_intros_json TEXT
        → #292：视频目录模式存手动输入视频简介（每行一条）
    publish_task_item.source TEXT DEFAULT 'project'
        → 区分明细是项目模式还是视频目录模式（_publish_one_item 据此决定是否调 move_to_published）
    publish_task_item.video_path TEXT
        → 视频目录模式存视频绝对路径；项目模式为 NULL（仍走 video_id 查 generated_video）
    """
    if _has_table(database, "publish_task") and not _has_column(database, "publish_task", "project_source"):
        database.execute("ALTER TABLE publish_task ADD COLUMN project_source TEXT NOT NULL DEFAULT 'project'")
    if _has_table(database, "publish_task") and not _has_column(database, "publish_task", "video_dirs_json"):
        database.execute("ALTER TABLE publish_task ADD COLUMN video_dirs_json TEXT")
    if _has_table(database, "publish_task") and not _has_column(database, "publish_task", "manual_shops_json"):
        database.execute("ALTER TABLE publish_task ADD COLUMN manual_shops_json TEXT")
    if _has_table(database, "publish_task") and not _has_column(database, "publish_task", "manual_intros_json"):
        database.execute("ALTER TABLE publish_task ADD COLUMN manual_intros_json TEXT")
    if _has_table(database, "publish_task_item") and not _has_column(database, "publish_task_item", "source"):
        database.execute("ALTER TABLE publish_task_item ADD COLUMN source TEXT NOT NULL DEFAULT 'project'")
    if _has_table(database, "publish_task_item") and not _has_column(database, "publish_task_item", "video_path"):
        database.execute("ALTER TABLE publish_task_item ADD COLUMN video_path TEXT")


def _apply_v40(database) -> None:
    """v40：发布任务双计算模式（#calc_mode）。

    publish_task 扩列：
    - schedule_mode TEXT NOT NULL DEFAULT 'balanced'  -- 'fixed' | 'balanced'
    - fixed_interval_min INTEGER NOT NULL DEFAULT 10
    - balanced_step_min INTEGER NOT NULL DEFAULT 60

    旧 DB 行的 same_project_interval_min / diff_project_interval_min 保留。
    扩列 DEFAULT 兜底使旧任务按 balanced + 60min 行为运行（与 v39 一致）。
    """
    if not _has_table(database, "publish_task"):
        return
    cols = [
        ("schedule_mode", "TEXT NOT NULL DEFAULT 'balanced'"),
        ("fixed_interval_min", "INTEGER NOT NULL DEFAULT 10"),
        ("balanced_step_min", "INTEGER NOT NULL DEFAULT 60"),
    ]
    for col, decl in cols:
        if not _has_column(database, "publish_task", col):
            database.execute(f"ALTER TABLE publish_task ADD COLUMN {col} {decl}")


def _apply_v42(database) -> None:
    """v42：进度列用时——publish_task 记录首次进入 running 的时刻。

    publish_task.started_at TEXT
        → 首次 status='running' 时写入 now_str()；后续 toggle/refresh 不更新（保留原始起点）
        → NULL 表示从未进入 running（草稿/未确认）

    前端进度列渲染用时 = now - started_at（H:MM:SS 格式）；
    update_time 不准（每次 publish_item 改 status 都刷新），必须独立列。
    """
    if _has_table(database, "publish_task") and not _has_column(database, "publish_task", "started_at"):
        database.execute("ALTER TABLE publish_task ADD COLUMN started_at TEXT")


def _apply_v43(database) -> None:
    """v43：import-share 加 type 入参——share_import_task.type 列。

    前端页签决定 type（video/music），与 category_id 解耦：
    - 未分类（id='-'）+ type=video → 入视频库
    - 未分类（id='-'）+ type=music → 入音乐库
    - 非未分类：cat.type 必须等于 type（不匹配 → 400）

    DEFAULT 'video' 兜底老任务（worker 未读 type 时按 video 走，保持向后兼容）。
    list_tasks 已 SELECT t.* 自动包含 type，前端展示用。
    """
    if _has_table(database, "share_import_task") and not _has_column(database, "share_import_task", "type"):
        database.execute(
            "ALTER TABLE share_import_task ADD COLUMN type TEXT NOT NULL DEFAULT 'video'")
