# -*- coding: utf-8 -*-
"""一次性脚本：往 SQLite 注入截图用的演示数据。

只用于本地推广文案截图——所有数据是测试用虚拟账号/门店/素材，
不会污染生产（脚本调用前需手动停止后端，避免 WAL 锁竞争）。
"""
import sys
import uuid
import json
import pathlib
import datetime as dt

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "db" / "short_video_tools.db"

NOW = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
TODAY = dt.date.today().strftime("%Y-%m-%dT%H:%M:%S")


def gen_id():
    return uuid.uuid4().hex


def main() -> int:
    import sqlite3
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")

    # -------- 清空演示用账号/门店/素材/项目（保留 settings 等系统表）--------
    for tbl in ("account", "shop", "material", "material_category",
                "project", "project_shot", "project_shop",
                "publish_task", "publish_record", "publish_task_intro"):
        try:
            cur.execute(f"DELETE FROM {tbl}")
        except Exception as e:
            print(f"[skip] DELETE {tbl}: {e}")

    # -------- 注入 5 个账号（不同登录状态各 1-2 个）--------
    accounts = [
        ("测试号-A", "normal", 12000, 45),
        ("测试号-B", "normal", 8500, 28),
        ("测试号-C", "invalid", 0, 0),     # 失效（演示）
        ("测试号-D", "undetected", 22000, 60),
        ("测试号-E", "normal", 5400, 15),
    ]
    for nick, status, fans, works in accounts:
        aid = gen_id()
        cur.execute(
            """INSERT INTO account (
                id, douyin_id, nickname, avatar, remark,
                cookie_encrypted, status, last_check_time,
                fan_count, work_count, create_time, update_time, deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (aid, f"dy_{aid[:16]}", nick, None, nick + "（演示）",
             "PLACEHOLDER_ENCRYPTED", status, NOW, fans, works, NOW, NOW),
        )
        print(f"[account] {nick} status={status}")

    # -------- 注入 12 个门店（5 个已标记，is_added_to_library=1）--------
    shops = [
        ("鱼公元鱼生", "广州", "天河区", "美食/日韩料理/寿司", 1, 850, 3, 1200, 5),
        ("黑眼熊寿司", "深圳", "南山区", "美食/日韩料理/寿司", 1, 1200, 5, 980, 3),
        ("巷子口咖啡", "广州", "越秀区", "美食/咖啡", 1, 600, 2, 540, 2),
        ("蜀香小厨", "成都", "锦江区", "美食/川菜", 1, 950, 4, 1100, 0),
        ("小巷子私房菜", "杭州", "西湖区", "美食/私房菜", 1, 720, 3, 680, 1),
        ("甜心烘焙坊", "广州", "番禺区", "美食/烘焙", 0, 580, 1, 420, 0),
        ("海底捞火锅", "上海", "徐汇区", "美食/火锅", 0, 1500, 8, 2200, 0),
        ("一点点奶茶", "深圳", "福田区", "美食/奶茶", 0, 480, 1, 380, 0),
        ("明治居酒屋", "北京", "朝阳区", "美食/日料", 0, 1100, 4, 950, 0),
        ("云海肴", "昆明", "五华区", "美食/云南菜", 0, 680, 2, 520, 0),
        ("老街坊面馆", "苏州", "姑苏区", "美食/面馆", 0, 420, 1, 320, 0),
        ("麦当当", "广州", "白云区", "美食/快餐", 0, 320, 1, 260, 0),
    ]
    for (name, city, dist, cat, is_added, comm, spu, gmv, published) in shops:
        sid = gen_id()
        cur.execute(
            """INSERT INTO shop (
                id, poi_id, name, province, city, district,
                category, category_full,
                is_cps, commission_rate, spu_count, cps_spu_count,
                total_gmv, publish_count, is_added_to_library, added_time,
                platform_name, detail_fetched,
                create_time, update_time, deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (sid, f"poi_{sid[:16]}", name, "广东" if city in ("广州", "深圳") else city,
             city, dist, cat.split("/")[0], cat,
             1, float(comm), spu, spu, float(gmv * 1000), published,
             is_added, NOW if is_added else None,
             "抖音", 1, NOW, NOW),
        )
        print(f"[shop] {name} is_added={is_added} published={published}")

    # -------- 注入素材分类树（3 层）--------
    cat_root = gen_id()
    cur.execute(
        """INSERT INTO material_category (id, parent_id, name, type, sort_order, create_time, update_time)
           VALUES (?, '', '短视频素材', 'video', 0, ?, ?)""",
        (cat_root, NOW, NOW),
    )
    cat_l1_food = gen_id()
    cat_l1_tech = gen_id()
    for (cid, parent, name) in [
        (cat_l1_food, cat_root, "美食探店"),
        (cat_l1_tech, cat_root, "科技数码"),
    ]:
        cur.execute(
            """INSERT INTO material_category (id, parent_id, name, type, sort_order, create_time, update_time)
               VALUES (?, ?, ?, 'video', 0, ?, ?)""",
            (cid, parent, name, NOW, NOW),
        )
    cat_l2_sushi = gen_id()
    cat_l2_coffee = gen_id()
    for (cid, parent, name) in [
        (cat_l2_sushi, cat_l1_food, "日料寿司"),
        (cat_l2_coffee, cat_l1_food, "咖啡甜品"),
    ]:
        cur.execute(
            """INSERT INTO material_category (id, parent_id, name, type, sort_order, create_time, update_time)
               VALUES (?, ?, ?, 'video', 0, ?, ?)""",
            (cid, parent, name, NOW, NOW),
        )
    cat_l3_omakase = gen_id()
    cat_l3_latte = gen_id()
    for (cid, parent, name) in [
        (cat_l3_omakase, cat_l2_sushi, "Omakase 实拍"),
        (cat_l3_latte, cat_l2_coffee, "拿铁拉花"),
    ]:
        cur.execute(
            """INSERT INTO material_category (id, parent_id, name, type, sort_order, create_time, update_time)
               VALUES (?, ?, ?, 'video', 0, ?, ?)""",
            (cid, parent, name, NOW, NOW),
        )

    # -------- 注入 16 个素材 --------
    for i in range(16):
        mid = gen_id()
        # 在叶子分类间循环
        cat_id = [cat_l3_omakase, cat_l3_latte][i % 2]
        # 文件路径是占位符（前端不实际播放）
        cur.execute(
            """INSERT INTO material (
                id, title, type, category_id, duration_ms, file_size,
                source_type, source_ref, file_md5, resolution, orientation,
                file_path, cover_path, ref_count, file_status,
                create_time, update_time, deleted
            ) VALUES (?, ?, 'video', ?, ?, ?, 'pull', ?, ?, '1080x1920', 'vertical',
                      ?, ?, 0, 'normal', ?, ?, 0)""",
            (mid, f"演示素材-{i+1:02d}", cat_id, 15000 + i * 500,
             12.5 + i, f"https://example.com/source/{i}",
             f"md5_{mid[:24]}",
             f"H:/ShortVideoToolData/material/{mid}.mp4",
             f"H:/ShortVideoToolData/material/{mid}_cover.jpg",
             NOW, NOW),
        )

    # -------- 注入 1 个项目 + 3 个分镜 + 2 个挂载门店 --------
    proj_id = gen_id()
    cur.execute(
        """INSERT INTO project (
            id, title, shot_count, bgm_strategy, dedup_rules_json,
            output_resolution, cursor, status, create_time, update_time, deleted
        ) VALUES (?, ?, 3, 'random', ?, '1080x1920', 0, 'normal', ?, ?, 0)""",
        (proj_id, "鱼公元鱼生探店（演示）",
         '{"frame_drop": true, "watermark": false, "noise": false}',
         NOW, NOW),
    )
    shot_ids = []
    for i, sname in enumerate(["开场", "菜品特写", "结尾 CTA"]):
        sid = gen_id()
        shot_ids.append(sid)
        cur.execute(
            """INSERT INTO project_shot (id, project_id, name, sort_order, create_time, update_time)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sid, proj_id, sname, i, NOW, NOW),
        )
    # 挂两个门店（project_shop 复合主键 project_id+shop_id，无 id 列）
    cur.execute("SELECT id FROM shop WHERE is_added_to_library=1 LIMIT 2")
    shop_rows = cur.fetchall()
    for i, (sid,) in enumerate(shop_rows):
        cur.execute(
            "INSERT INTO project_shop (project_id, shop_id, sort_order) VALUES (?, ?, ?)",
            (proj_id, sid, i),
        )

    # -------- 注入 3 条视频简介（#publish-contract 简介文案 + 话题） --------
    intros = [
        ("🍣 {门店名} 探店 | 食材新鲜、口感惊艳，强烈推荐！",
         '["#日料探店", "#广州美食", "#鱼生"]'),
        ("📍 来{城市}必吃的{类目}店 | {门店名} | 性价比超高",
         '["#探店推荐", "#美食分享"]'),
        ("💯 {门店名} 全网最真实测评 | 服务/口味/环境全打分",
         '["#真实测评", "#探店vlog"]'),
    ]
    for content, topics in intros:
        iid = gen_id()
        cur.execute(
            """INSERT INTO video_intro (id, project_id, content, topics_json, enabled, create_time, update_time)
               VALUES (?, ?, ?, ?, 1, ?, ?)""",
            (iid, proj_id, content, topics, NOW, NOW),
        )
    print(f"[intro] 3 video intros")

    # -------- 注入 project_shot_clip（每个分镜 2-3 个素材片段） --------
    cur.execute("SELECT id FROM material WHERE type='video' AND deleted=0 LIMIT 6")
    mat_rows = cur.fetchall()
    if mat_rows:
        for sid in shot_ids:
            for j, (mid,) in enumerate(mat_rows[:3]):  # 每个分镜 3 个片段
                start_ms = j * 5000  # 0, 5s, 10s
                end_ms = start_ms + 5000  # 5s, 10s, 15s
                cur.execute(
                    """INSERT INTO project_shot_clip (id, shot_id, material_id, clip_start_ms, clip_end_ms, mirrored, create_time)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (gen_id(), sid, mid, start_ms, end_ms, j % 2, NOW),
                )
        print(f"[shot_clip] {len(shot_ids) * 3} clip fragments")

    # -------- 注入 project_bgm（背景音乐） --------
    cur.execute("SELECT id FROM material WHERE type='music' AND deleted=0 LIMIT 1")
    music_rows = cur.fetchall()
    if not music_rows:
        # 没有 music 类型素材 → 创建一个虚拟 BGM
        cur.execute(
            """INSERT INTO material (
                id, title, type, category_id, duration_ms, file_size,
                source_type, source_ref, file_md5, file_path, orientation,
                ref_count, file_status, create_time, update_time, deleted
            ) VALUES (?, '演示 BGM 素材', 'music', ?, 120000, 4.2,
                      'upload', '', ?, 'H:/ShortVideoToolData/material/demo_bgm.mp3',
                      'horizontal', 0, 'normal', ?, ?, 0)""",
            (gen_id(), gen_id(), f"md5_bgm_{gen_id()[:16]}", NOW, NOW),
        )
        cur.execute("SELECT id FROM material WHERE type='music' AND deleted=0 LIMIT 2")
        music_rows = cur.fetchall()
    for k, (mid,) in enumerate(music_rows[:2]):
        cur.execute(
            """INSERT INTO project_bgm (id, project_id, material_id, sort_order, create_time)
               VALUES (?, ?, ?, ?, ?)""",
            (gen_id(), proj_id, mid, k, NOW),
        )
    print(f"[bgm] {min(len(music_rows), 2)} bgm tracks")

    # -------- 注入 generated_video（成品视频） --------
    cur.execute("SELECT id FROM material WHERE type='video' AND deleted=0 LIMIT 2")
    bgm_for_done = cur.fetchall()
    for k in range(4):
        cur.execute(
            """INSERT INTO generated_video (
                id, project_id, combination_key, derivative_of, file_path, file_size,
                duration_ms, bgm_material_id, dedup_params_json, status, create_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (gen_id(), proj_id, f"comb-{k:03d}", None,
             f"H:/ShortVideoToolData/finished/{proj_id}/成品-{k+1:02d}.mp4",
             18.5 + k, 15000 + k * 500,
             bgm_for_done[k % len(bgm_for_done)][0] if bgm_for_done else None,
             '{"frame_drop": true, "watermark": false, "noise": false}',
             "idle" if k < 3 else "occupied",
             NOW),
        )
    print(f"[generated_video] 4 finished videos")

    # -------- 注入 2 个发布任务（演示用：1 个草稿 + 1 个运行中）--------
    cur.execute("SELECT id FROM account WHERE status='normal' LIMIT 1")
    acc_row = cur.fetchone()
    acc_id = acc_row[0] if acc_row else None

    task1 = gen_id()
    cur.execute(
        """INSERT INTO publish_task (
            id, task_name, project_ids_json, account_ids_json, shop_ids_json,
            per_account_count, start_time, interval_minutes,
            parallel_strategy, title_topic_mode, shop_assign_mode,
            status, success_count, fail_count, create_time, update_time, deleted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (task1, "鱼公元鱼生探店（演示）",
         f'["{proj_id}"]', f'["{acc_id}"]',
         json.dumps([s for (s,) in shop_rows]),
         15, NOW, 60,
         "serial", "random", "round",
         "draft", 0, 0, NOW, NOW),
    )
    task2 = gen_id()
    cur.execute(
        """INSERT INTO publish_task (
            id, task_name, project_ids_json, account_ids_json, shop_ids_json,
            per_account_count, start_time, interval_minutes,
            parallel_strategy, title_topic_mode, shop_assign_mode,
            status, success_count, fail_count, create_time, update_time, deleted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (task2, "黑眼熊寿司矩阵任务",
         f'["{proj_id}"]', f'["{acc_id}"]',
         json.dumps([s for (s,) in shop_rows]),
         20, NOW, 45,
         "serial", "random", "round",
         "running", 7, 1, NOW, NOW),
    )

    # -------- 注入 5 条发布记录（演示历史）--------
    # account_snapshot = 单字符串（昵称，与 _write_publish_record 一致）
    # shop_snapshot = "门店名|类目"（| 分隔；前端 split('|')[0] 渲染门店名）
    statuses = ["success", "success", "success", "success", "removed"]
    cur.execute("SELECT name, category FROM shop ORDER BY publish_count DESC LIMIT 5")
    shop_with_cat = cur.fetchall()
    for i in range(5):
        rid = gen_id()
        sid_idx = i % len(shop_rows)
        shop_name, shop_cat = shop_with_cat[i] if i < len(shop_with_cat) else (shop_rows[sid_idx][0], "美食")
        cur.execute(
            """INSERT INTO publish_record (
                id, publish_title, publish_topics, publish_time, video_title,
                account_id, account_snapshot, shop_id, shop_snapshot,
                project_id, video_id, video_local_path, online_video_id,
                task_item_id, is_manual, status, create_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (rid,
             f"演示视频-{i+1:02d} | 探店 #演示",
             "#日料探店,#广州美食",
             NOW, f"成品-{i+1:02d}",
             acc_id, "测试号-A（演示）",
             shop_rows[sid_idx][0], f"{shop_name}|{shop_cat}",
             proj_id, f"video_demo_{i}", f"H:/ShortVideoToolData/finished/{proj_id}/demo_{i}.mp4",
             f"online_{rid[:16]}", None,
             statuses[i], NOW),
        )

    conn.commit()
    print(f"\n[OK] 注入完成：5 账号 + 12 门店 + 16 素材 + 1 项目 + 2 任务 + 5 记录")
    print(f"     DB: {DB}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
