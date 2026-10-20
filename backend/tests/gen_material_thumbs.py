# -*- coding: utf-8 -*-
"""生成素材真实预览图（电影海报风格）→ backend/data/material/<mid>_cover.jpg

风格：1080x1920 竖屏视频缩略图（接近抖音/小红书真实风格）
- 渐变背景（深色 + 主体色）
- 大号白色标题（接近真实视频文字层）
- 副标题 + 装饰元素
- 不直接是"截图"——但是设计成"看起来像真实场景的截图"
"""
import os
import sys
import pathlib
import sqlite3
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
MATERIAL_DIR = ROOT / "backend" / "data" / "material"
DB = ROOT / "backend" / "data" / "db" / "short_video_tools.db"

# 寻找系统字体（Windows 自带）
FONT_DIRS = [
    pathlib.Path("C:/Windows/Fonts"),
    pathlib.Path("C:/Windows/Fonts/zh-CN"),
]

def find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | None:
    """找一个能用的中文字体（思源黑体或 fallback 到微软雅黑）。"""
    candidates = [
        ("msyh.ttc", "微软雅黑"),
        ("msyhbd.ttc", "微软雅黑 Bold"),
        ("simhei.ttf", "黑体"),
        ("simsun.ttc", "宋体"),
        ("arial.ttf", "Arial"),
        ("arialbd.ttf", "Arial Bold"),
    ] if bold else [
        ("msyh.ttc", "微软雅黑"),
        ("simhei.ttf", "黑体"),
        ("arial.ttf", "Arial"),
    ]
    for fname, _ in candidates:
        for d in FONT_DIRS:
            f = d / fname
            if f.exists():
                try:
                    return ImageFont.truetype(str(f), size)
                except Exception:
                    continue
    # 兜底：默认字体（仅英文）
    return ImageFont.load_default()


# 视频缩略图风格（接近抖音真实视频开场 1 秒的氛围）
DESIGNS = [
    {  # 0: 日料寿司
        "bg_dark": "#0F1419", "bg_accent": "#E91E63",
        "title_main": "日料寿司", "title_sub": "OMAKASE 实拍",
        "badge": "探店", "stars": 5, "bottom": "广州市天河区",
        "badge_color": "#FF4D6D",
    },
    {  # 1: 咖啡甜品
        "bg_dark": "#1A0F08", "bg_accent": "#FF9800",
        "title_main": "咖啡甜品", "title_sub": "手冲+拿铁拉花",
        "badge": "种草", "stars": 5, "bottom": "深圳市南山区",
        "badge_color": "#FFB74D",
    },
    {  # 2: 健康轻食
        "bg_dark": "#0F1A0F", "bg_accent": "#4CAF50",
        "title_main": "健康轻食", "title_sub": "沙拉+鲜果碗",
        "badge": "推荐", "stars": 4, "bottom": "广州市番禺区",
        "badge_color": "#66BB6A",
    },
    {  # 3: 海鲜火锅
        "bg_dark": "#0A1A2A", "bg_accent": "#2196F3",
        "title_main": "海鲜火锅", "title_sub": "深海鲜活·现场捞",
        "badge": "必吃", "stars": 5, "bottom": "上海市徐汇区",
        "badge_color": "#42A5F5",
    },
    {  # 4: 下午茶
        "bg_dark": "#1A0F1F", "bg_accent": "#9C27B0",
        "title_main": "下午茶", "title_sub": "三层塔·甜咸搭配",
        "badge": "精选", "stars": 4, "bottom": "成都市锦江区",
        "badge_color": "#AB47BC",
    },
    {  # 5: 私房菜
        "bg_dark": "#1F140A", "bg_accent": "#795548",
        "title_main": "私房菜", "title_sub": "主厨私房·限量",
        "badge": "私藏", "stars": 5, "bottom": "杭州市西湖区",
        "badge_color": "#A1887F",
    },
    {  # 6: 科技数码
        "bg_dark": "#0A1822", "bg_accent": "#607D8B",
        "title_main": "科技数码", "title_sub": "新品开箱·评测",
        "badge": "测评", "stars": 4, "bottom": "iPhone / 安卓",
        "badge_color": "#90A4AE",
    },
    {  # 7: 川湘菜
        "bg_dark": "#1F0808", "bg_accent": "#F44336",
        "title_main": "川湘菜", "title_sub": "麻辣鲜香·地道",
        "badge": "爆款", "stars": 5, "bottom": "广州市白云区",
        "badge_color": "#EF5350",
    },
]


def make_cover(idx: int, mid: str, out_path: pathlib.Path) -> bool:
    """画 1080x1920 电影海报风格封面。"""
    W, H = 1080, 1920
    design = DESIGNS[idx % len(DESIGNS)]
    accent = design["bg_accent"]
    dark = design["bg_dark"]

    # 画布：深色底
    img = Image.new("RGB", (W, H), dark)
    draw = ImageDraw.Draw(img, "RGBA")

    # 1. 顶部色块（径向渐变模拟：用同心圆叠加制造色彩层次）
    for r in range(600, 0, -50):
        alpha = max(0, int(80 - r / 8))
        # parse hex → rgb tuple
        rgb = tuple(int(accent[i:i+2], 16) for i in (1, 3, 5))
        draw.ellipse([W//2 - r, -r + 200, W//2 + r, r + 200], fill=(*rgb, alpha))

    # 2. 主区域（中间）：更大更亮的"主体色块"圆形
    for r in range(450, 0, -30):
        alpha = int(40 + (450 - r) * 0.4)
        rgb = tuple(int(accent[i:i+2], 16) for i in (1, 3, 5))
        draw.ellipse([W//2 - r, H//2 - r - 100, W//2 + r, H//2 + r - 100], fill=(*rgb, alpha))

    # 3. 顶部装饰线（横幅）
    draw.rectangle([60, 200, W - 60, 210], fill=accent)

    # 4. badge（圆角矩形 + 文字）
    badge = design["badge"]
    badge_color = design["badge_color"]
    badge_font = find_font(64, bold=True)
    # 估算文字宽度
    bbox = draw.textbbox((0, 0), badge, font=badge_font)
    bw = bbox[2] - bbox[0] + 60
    bh = 90
    bx, by = 60, 230
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=12, fill=badge_color)
    draw.text((bx + 30, by + 8), badge, fill="#FFFFFF", font=badge_font)

    # 5. 大字主标题（视频标题）
    main_font = find_font(180, bold=True)
    title_main = design["title_main"]
    bbox = draw.textbbox((0, 0), title_main, font=main_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (W - tw) // 2
    ty = 750
    # 阴影
    draw.text((tx + 6, ty + 6), title_main, fill=(0, 0, 0, 180), font=main_font)
    # 主文字
    draw.text((tx, ty), title_main, fill="#FFFFFF", font=main_font)

    # 6. 副标题（小字）
    sub_font = find_font(90, bold=False)
    title_sub = design["title_sub"]
    bbox = draw.textbbox((0, 0), title_sub, font=sub_font)
    sw = bbox[2] - bbox[0]
    sx = (W - sw) // 2
    sy = ty + th + 40
    draw.text((sx + 4, sy + 4), title_sub, fill=(0, 0, 0, 150), font=sub_font)
    draw.text((sx, sy), title_sub, fill="#FFE082", font=sub_font)

    # 7. 星级评分
    stars = design["stars"]
    star_y = sy + 180
    star_x_start = (W - stars * 60) // 2
    for i in range(stars):
        sx2 = star_x_start + i * 60
        # 五角星
        draw.text((sx2, star_y), "★", fill="#FFD700", font=find_font(60, bold=True))

    # 8. 底部位置信息（半透明背景条 + 文字）
    bottom = design["bottom"]
    bottom_font = find_font(60, bold=False)
    bbox = draw.textbbox((0, 0), bottom, font=bottom_font)
    btw = bbox[2] - bbox[0]
    bth = bbox[3] - bbox[1]
    btw_pad = 40
    bth_pad = 20
    bx2 = (W - btw - 2 * btw_pad) // 2
    by2 = H - 280
    draw.rounded_rectangle(
        [bx2, by2, bx2 + btw + 2 * btw_pad, by2 + bth + 2 * bth_pad],
        radius=10, fill=(255, 255, 255, 30)
    )
    draw.text((bx2 + btw_pad, by2 + bth_pad), bottom, fill="#FFFFFF", font=bottom_font)

    # 9. 底部"探店视频"水印
    watermark_font = find_font(48, bold=True)
    draw.text((60, H - 150), "📍 探店实拍", fill=(255, 255, 255, 200), font=watermark_font)
    draw.text((W - 240, H - 150), "🛍️ 演示素材", fill=(255, 255, 255, 200), font=watermark_font)

    # 保存
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), "JPEG", quality=88)
    return out_path.exists()


def update_db_cover_urls() -> int:
    """把素材的 cover_url 设为相对路径。"""
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    cur.execute("SELECT id FROM material WHERE deleted=0 AND type='video' ORDER BY create_time")
    mids = [r[0] for r in cur.fetchall()]
    n = 0
    for mid in mids:
        cover_url = f"material/{mid}_cover.jpg"
        cur.execute("UPDATE material SET cover_url=? WHERE id=?", (cover_url, mid))
        n += 1
    conn.commit()
    conn.close()
    return n


def main() -> int:
    MATERIAL_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    cur.execute("SELECT id FROM material WHERE deleted=0 AND type='video' ORDER BY create_time LIMIT 8")
    mids = [r[0] for r in cur.fetchall()]
    conn.close()

    print(f"[mids] {len(mids)} materials")
    ok = 0
    for idx, mid in enumerate(mids):
        out_path = MATERIAL_DIR / f"{mid}_cover.jpg"
        try:
            if make_cover(idx, mid, out_path):
                print(f"[gen] {out_path.name}  size={out_path.stat().st_size}")
                ok += 1
            else:
                print(f"[fail] {out_path.name}")
        except Exception as e:
            print(f"[err] {out_path.name}: {e}")

    updated = update_db_cover_urls()
    print(f"\n[db] updated {updated} cover_url")

    import urllib.request
    if mids:
        sample = f"http://127.0.0.1:8765/api/files/material/{mids[0]}_cover.jpg"
        try:
            r = urllib.request.urlopen(sample, timeout=3)
            print(f"[verify] {r.status}  content-type={r.headers.get('Content-Type')}")
        except Exception as e:
            print(f"[verify err] {e}")

    print(f"\n[done] {ok}/{len(mids)} covers generated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
