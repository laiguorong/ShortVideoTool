# -*- coding: utf-8 -*-
"""视频编辑核心（M4）：切割、拼接、镜像、BGM、去重处理的 FFmpeg 命令构造与执行。

设计（需求文档 F-04）：
- 切割：按固定时长等分 / 去头尾后等分（第一版场景切割用固定时长近似，TBD 简化项）；
- 拼接：各分镜片段 concat（统一转码对齐参数后 concat demuxer）；
- 去重（#101 增强）：
    * L1 画面像素：抽帧扰动 / 动态水印 / 动态干扰（缩放/亮度/裁边）；
    * L2 编码参数：码率 ±15%、profile 随机、GOP 随机、B 帧数随机；
    * L3 色域参数：color_primaries / color_trc / color_range 微扰；
    * L4 容器元数据：comment/title/encoder 随机化（去工具指纹）；
    * L5 时间戳：creation_time 写入过去 7 天内的随机时刻（多成品时间戳不再雷同）；
    * L6 文件 MD5：成品入库前比对同账号已占用成品哈希（撞库拦截）；
    * #106：移除 L7 BGM 起点偏移与音量微扰（效果不明显且影响听感，留项目级 bgm_volume 即可）。
- 输出统一 H.264 + AAC + MP4（F-04-R12 规范）。
"""

import os
import random
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from app.core.ffmpeg import (
    FFMPEG, get_h264_encoder, run_cmd, probe_media, extract_media_info,
)


def cut_clip(src: str, start_ms: int, end_ms: str | int, out_path: str) -> bool:
    """切割单片段（-ss/-to 精确切割）。

    镜像已拆分（#380），如需镜像请在 cut 之后调用 mirror_clip_file。

    任务 #75：源无音频时追加 lavfi anullsrc 作为 input 1 + -map 1:a，让输出 mp4
    保证 v+a 双流；下游 render_video_full 不必再补救（避免 ffmpeg 二次 EINVAL）。

    参数:
        src: 源素材绝对路径
        start_ms / end_ms: 起止毫秒
        out_path: 输出绝对路径
    返回:
        是否成功
    """
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start_ms / 1000:.3f}", "-to", f"{end_ms / 1000:.3f}",
           "-i", src]
    # 任务 #75：探测源音频；缺则追加 lavfi anullsrc（保证输出有 a 流）
    streams = _probe_clip_streams(src)
    need_audio_pad = not streams["has_audio"]
    if need_audio_pad:
        dur_s = max(streams["duration_ms"], 1) / 1000
        cmd.extend([
            "-f", "lavfi", "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", f"{dur_s:.3f}",
            "-map", "0:v", "-map", "1:a",
        ])
    enc = get_h264_encoder()
    cmd += ["-c:v", enc, "-b:v", "4000k",
            "-c:a", "aac", "-ac", "2", "-ar", "44100", out_path]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    result = run_cmd(cmd, timeout=300, monitor=True)
    ok = result is not None and result.returncode == 0 and Path(out_path).exists()
    return ok


def mirror_clip_file(src_path: str, out_path: str) -> bool:
    """对已生成的片段 mp4 做水平镜像（#380：不再重新切原视频）。

    参数:
        src_path: 片段 mp4 绝对路径
        out_path: 输出 mp4 绝对路径（建议与 src_path 不同，调用方原子替换）
    返回:
        是否成功
    """
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-i", src_path, "-vf", "hflip"]
    enc = get_h264_encoder()
    cmd += ["-c:v", enc, "-b:v", "4000k",
            "-c:a", "aac", "-ac", "2", "-ar", "44100", out_path]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    result = run_cmd(cmd, timeout=300, monitor=True)
    ok = result is not None and result.returncode == 0 and Path(out_path).exists()
    return ok


def ensure_clip_has_audio(src: str, out_path: str) -> bool:
    """确保 clip mp4 含音频流（任务 #76：源头补音频）。

    用途：单视频直进 render_video_full 时（不经过 cut_clip），源 mp4 缺音频会触发
    render 内部追加 lavfi input slot 逻辑。让调用方先调用本函数把源 mp4 转成双流
    mp4 写临时文件，render_video_full 就不用再追加 slot。

    行为：
      - 源已含音频 → 原子复制 src 到 out_path（避免 ffmpeg 重编码浪费时间）
      - 源缺音频 → ffmpeg `-c:v copy` 不重编码视频，仅追加 lavfi anullsrc 静音流
    返回:
        成功 True（out_path 文件存在）；失败 False
    """
    streams = _probe_clip_streams(src)
    if not streams["has_video"]:
        logger.error("[合成] clip {} 无视频流，无法补音频", Path(src))
        return False
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if streams["has_audio"]:
        # 已含音频：直接复制，避免 ffmpeg 浪费时间
        try:
            Path(out_path).write_bytes(Path(src).read_bytes())
            return True
        except OSError as exc:
            logger.error("[合成] 复制 {} → {} 失败：{}", src, out_path, exc)
            return False
    # 缺音频：ffmpeg -c:v copy + 追加 lavfi anullsrc（极快，不重编码视频）
    dur_s = max(streams["duration_ms"], 1) / 1000
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-f", "lavfi", "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", f"{dur_s:.3f}",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac", "-ac", "2", "-ar", "44100",
        out_path,
    ]
    result = run_cmd(cmd, timeout=300, monitor=True)
    ok = result is not None and result.returncode == 0 and Path(out_path).exists()
    return ok


def cut_clips_segment(src: str, segments: list[tuple[int, int]],
                       out_dir: str) -> list[bool]:
    """一次 ffmpeg 切多片段（segment muxer，#380 模式 B）。

    同素材的 N 个片段在单次 ffmpeg 调用内完成切割，避免 Windows 下多次 ffmpeg
    句柄延迟释放导致的 WinError 32（实测 30s 重试仍偶发）。

    镜像已拆分（#380），如需镜像请在 cut 之后调用 mirror_clip_file。

    参数:
        src: 源素材绝对路径
        segments: [(start_ms, end_ms), ...]  按时间顺序递增排列
        out_dir: 输出目录；输出文件名 clip_000.mp4 / clip_001.mp4 ...
    返回:
        每片段是否成功（顺序对应输入 segments）。ffmpeg 整批失败时全 False。
    """
    if not segments:
        return []

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out_pattern = os.path.join(out_dir, "clip_%03d.mp4")

    # segment_times 接收**累积**时间戳（秒），N 个切点产出 N 个文件
    # 第 0 段起点 0 隐含（不传）。最后一段终点 = 源时长，需要显式传入才能切出。
    segment_times_sec = [s / 1000 for s, _ in segments]
    # 强制每个切点出 IDR 帧（#380 模式 B 关键：避免 muxer 漏写后续段）
    if len(segment_times_sec) > 1:
        diffs = [segment_times_sec[i + 1] - segment_times_sec[i]
                 for i in range(len(segment_times_sec) - 1)]
        min_gap = max(0.1, min(diffs))
        force_expr = f"expr:gte(t,n_forced*{min_gap:.3f})"
    else:
        force_expr = "expr:gte(t,n_forced*2)"

    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    # 任务 #75：探测源音频；缺则追加 lavfi anullsrc（保证每个分段 mp4 都有 a 流）
    streams = _probe_clip_streams(src)
    need_audio_pad = not streams["has_audio"]
    if need_audio_pad:
        dur_s = max(streams["duration_ms"], 1) / 1000
        cmd.extend([
            "-f", "lavfi", "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", f"{dur_s:.3f}",
            "-map", "0:v", "-map", "1:a",
        ])
    enc = get_h264_encoder()
    cmd += [
        "-c:v", enc, "-b:v", "4000k",
        "-c:a", "aac", "-ac", "2", "-ar", "44100",
        "-force_key_frames", force_expr,
        "-f", "segment",
        # 关键：传全部 N 个时间戳（含最后一段终点），否则最后一段不会被写出
        "-segment_times", ",".join(f"{t:.3f}" for t in segment_times_sec),
        "-reset_timestamps", "1",
        out_pattern,
    ]

    result = run_cmd(cmd, timeout=600, monitor=True)
    if result is None or result.returncode != 0:
        # 整批失败 → 全部 False；清理可能残缺的临时文件
        for i in range(len(segments)):
            p = Path(out_dir) / f"clip_{i:03d}.mp4"
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        return [False] * len(segments)

    # 校验：文件存在 + 时长近似（精度 ±5 秒，segment muxer 按 IDR 切点切允许偏差）
    results: list[bool] = []
    for i, (start, end) in enumerate(segments):
        p = Path(out_dir) / f"clip_{i:03d}.mp4"
        if not p.exists():
            results.append(False)
            continue
        actual_ms = probe_duration_ms(str(p))
        expected_ms = end - start
        # 允许 ±5 秒误差；只要 ffmpeg 切出了文件（actual>0）即视为成功
        results.append(actual_ms > 0 and abs(actual_ms - expected_ms) < 5000)
    return results




def build_dedup_params(dedup_rules: dict, project_title: str = "",
                       generate_time_str: str = "") -> dict:
    """按去重规则开关随机生成扰动参数（F-04-R6，#101 增强，参数记录入 dedup_params_json）。

    参数:
        dedup_rules: {
            "frame_drop": bool, "watermark": bool, "noise": bool, "strength": "low|mid|high",
            # #111：水印内容来源
            "watermark_text_mode": "fixed|timestamp|title|custom",  # 默认 timestamp
            "watermark_text_custom": str,  # 仅 custom 时使用，最长 16 字
        }
        project_title: 项目名称（watermark_text_mode=title 时用）
        generate_time_str: 生成时间字符串（watermark_text_mode=timestamp 时用，如 "09-13 10:30"）
    返回:
        随机参数 dict（含 strength 决定的扰动幅度 + #101 增强：编码/色域/元数据/时间戳/音频指纹）
    """
    strength = dedup_rules.get("strength", "mid")
    # 各强度扰动幅度
    ranges = {
        "low": {"frame": (1, 2), "wm_opacity": (0.10, 0.18), "scale": (0.99, 1.01),
                "bright": (-3, 3), "crop": (0.01, 0.015)},
        "mid": {"frame": (2, 4), "wm_opacity": (0.15, 0.25), "scale": (0.98, 1.02),
                "bright": (-5, 5), "crop": (0.015, 0.025)},
        "high": {"frame": (3, 6), "wm_opacity": (0.20, 0.30), "scale": (0.97, 1.03),
                 "bright": (-8, 8), "crop": (0.02, 0.03)},
    }[strength]

    params: dict = {"strength": strength}

    # L1 画面像素扰动（F-04-R6 原生）
    if dedup_rules.get("frame_drop"):
        # #101 P1：抽帧位置随机化（不再固定间隔）
        # 选 N 个不连续帧位置（30fps 下覆盖 ~30 秒）
        drop_count = random.randint(*ranges["frame"])
        drop_positions = sorted(random.sample(range(30, 900), drop_count))
        params["frame_drop"] = {"count": drop_count, "positions": drop_positions}
    if dedup_rules.get("watermark"):
        # #111：水印内容来源（timestamp/title/custom，#114 移除 fixed「抖音」）
        wm_mode = dedup_rules.get("watermark_text_mode", "timestamp")
        if wm_mode == "title" and project_title:
            # 项目名裁剪到 16 字，避免水印超长
            wm_text = (project_title or "")[:16]
        elif wm_mode == "custom":
            custom = (dedup_rules.get("watermark_text_custom") or "").strip()[:16]
            wm_text = custom if custom else "抖音"
        else:
            # 默认时间戳（含空值兜底，时间戳始终显示当前生成时刻）
            wm_text = generate_time_str or "抖音"
        # 动态水印：随机位置（归一化坐标）与透明度
        params["watermark"] = {
            "text": wm_text,
            "x": round(random.uniform(0.05, 0.75), 3),
            "y": round(random.uniform(0.05, 0.85), 3),
            "opacity": round(random.uniform(*ranges["wm_opacity"]), 3),
        }
    if dedup_rules.get("noise"):
        # 动态干扰：缩放/亮度/裁边随机组合
        params["noise"] = {
            "scale": round(random.uniform(*ranges["scale"]), 4),
            "brightness": random.randint(*ranges["bright"]),
            "crop": round(random.uniform(*ranges["crop"]), 4),
        }

    # #101 P0：编码参数扰动（每个成品一组，避免编码指纹一致）
    params["encoder"] = {
        "bitrate_scale": round(random.uniform(0.85, 1.15), 3),  # 码率 ±15%
        "profile": random.choice(["high", "main", "baseline"]),
        "gop_size": random.choice([30, 60, 90, 120]),
        "bframes": random.randint(0, 4),
    }

    # #101 P1：色域参数微扰（避开色彩空间指纹一致）
    params["color"] = {
        "primaries": random.choice(["bt709", "bt470bg", "smpte170m"]),
        "trc": random.choice(["bt709", "smpte170m"]),
        "range": random.choice(["tv", "pc"]),
    }

    # #101 P0：容器元数据扰动（去工具指纹）
    # comment/encoder/title 都随机化，避免暴露"FFmpeg/Libx264"特征
    rand_text = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8))
    params["metadata"] = {
        "comment": rand_text,
        "title": rand_text[:6],
        # 显式置空 encoder（FFmpeg 默认会写 libx264）
        "encoder": "",
    }

    # #101 P0：时间戳扰动（写入过去 7 天内的随机时刻，多成品时间戳不再雷同）
    # 留负偏移给 ffmpeg -metadata creation_time 用
    params["timestamp_offset_s"] = random.randint(-7 * 86400, 0)

    return params


def build_dedup_filter(params: dict, resolution: str) -> str:
    """去重参数 → ffmpeg -vf 滤镜链（仅画面 L1 扰动）。"""
    w, h = (int(x) for x in resolution.lower().split("x"))
    chain: list[str] = []
    if "noise" in params:
        n = params["noise"]
        crop_w = int(w * (1 - 2 * n["crop"]))
        crop_h = int(h * (1 - 2 * n["crop"]))
        chain.append(f"crop=iw*{1 - 2 * n['crop']:.4f}:ih*{1 - 2 * n['crop']:.4f}")
        chain.append(f"scale={n['scale'] * crop_w:.0f}:{n['scale'] * crop_h:.0f}")
        chain.append(f"scale={w}:{h}")
        chain.append(f"eq=brightness={n['brightness'] / 100:.3f}")
    if "watermark" in params:
        wm = params["watermark"]
        x_px = int(w * wm["x"])
        y_px = int(h * wm["y"])
        # #111：自定义水印文本含特殊字符（冒号/单引号/反斜杠/逗号）需转义，避免 ffmpeg drawtext 解析报错
        wm_text_safe = (
            str(wm["text"])
            .replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
            .replace(",", "\\,")
        )
        chain.append(
            f"drawtext=text='{wm_text_safe}':x={x_px}:y={y_px}:"
            f"fontsize={h // 30}:fontcolor=white@{wm['opacity']}")
    # #101 P1：抽帧位置随机化（frame_drop 从 int 变成 {count, positions}）
    if "frame_drop" in params and isinstance(params["frame_drop"], dict) and params["frame_drop"]["count"] > 0:
        # 用 select 表达式按位置删帧（positions 列表）
        # 表达式：'eq(n,X)+eq(n,Y)+...' 即"当前帧是任意丢帧位置"则丢弃
        positions = params["frame_drop"]["positions"]
        if positions:
            expr = "+".join(f"eq(n\\,{p})" for p in positions)
            chain.append(f"select='not({expr})',setpts=N/(25*TB)")
    return ",".join(chain) if chain else "null"


def render_video_full(
    clip_paths: list[str],
    out_path: str,
    resolution: str = "1080x1920",
    bgm_path: str | None = None,
    bgm_volume: float = 0.3,
    keep_original_audio: bool = False,
    dedup_params: dict | None = None,
    fit_mode: str = "cover",
) -> bool:
    """合并渲染：拼接 + BGM 混音 + 去重，单次 ffmpeg 调用（D 优化）。

    参数:
        fit_mode: 画面层显方式（#多画面适配）
          - cover：等比放大 + 居中裁剪，铺满无黑边（默认，抖音风格）
          - contain：等比缩小 + 居中补黑，全内容可见
          - fill：直接拉伸到目标（可能变形，慎用）
          - blur_bg：原画 contain 居中，背景高斯模糊铺满（高端抖音风）

    原 3 步（N+3 次 ffmpeg）合并为单次：
      - 每个 clip 独立 normalize（scale/pad/setsar/format）
      - concat N 段（含原音）
      - BGM 可选叠加（keep_original_audio 决定原音是否参与 amix）
      - 去重滤镜（hue/noise/drawtext/frame_drop）串到视频链尾部
      - 编码器/色域/容器 metadata 扰动

    参数:
        clip_paths: 片段绝对路径列表（按序，N>=1）
        out_path: 输出最终视频路径
        resolution: 输出分辨率 WxH（竖屏 1080x1920）
        bgm_path: BGM 路径，None 表示不混 BGM
        bgm_volume: BGM 音量比例（0.05~1.0）
        keep_original_audio: True=原音与 BGM 叠加；False=只留 BGM
        dedup_params: build_dedup_params 返回的扰动参数；None/空时不应用去重

    返回:
        成功 True（且 out_path 文件存在）；失败 False
    失败时不抛异常（沿用 run_cmd 返回），由调用方更新任务状态、用户重新触发。
    """
    n = len(clip_paths)
    if n == 0:
        return False
    w, h = resolution.lower().split("x")

    # 1. 构造输入：每个 clip 占 1 个 mp4 slot（提供 video）；缺音频时追加 lavfi anullsrc
    # 作为额外 input slot，filter_complex video/audio source 分别索引。
    # 任务 #74：之前缺音频 clip 用 lavfi anullsrc 单 input 替换 mp4，导致 [i:v] 引用
    # 不到该 input 的视频流（anullsrc 只有 a 流）。改为 mp4 永远占一个 slot 保留 v 流。
    inputs: list[str] = []
    v_input_idx: list[int] = []  # 第 i 个 clip 的 mp4 input index（提供 video）
    a_input_idx: list[int] = []  # 第 i 个 clip 的 audio input index
    current_idx = 0
    for clip_path in clip_paths:
        streams = _probe_clip_streams(clip_path)
        if not streams["has_video"]:
            # 缺视频无法合成（filtergraph [i:v] 必须存在）—— fail fast
            logger.error("[合成] clip {} 无视频流，跳过", Path(clip_path).name)
            Path(out_path).unlink(missing_ok=True)
            return False
        inputs.extend(["-i", clip_path])
        v_input_idx.append(current_idx)
        current_idx += 1
        if streams["has_audio"]:
            a_input_idx.append(current_idx - 1)
        else:
            dur_s = max(streams["duration_ms"], 1) / 1000
            inputs.extend([
                "-f", "lavfi", "-i",
                f"anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t", f"{dur_s:.3f}",
            ])
            a_input_idx.append(current_idx)
            current_idx += 1
            logger.info(
                "[合成] clip {} 缺音频 → 追加 lavfi anullsrc（input {}）",
                Path(clip_path).name, current_idx - 1,
            )
    bgm_index = current_idx  # BGM 紧跟所有 mp4/lavfi 之后的下一个 input slot
    if bgm_path:
        inputs.extend(["-stream_loop", "-1", "-i", bgm_path])

    # 2. filter_complex 拼装
    parts: list[str] = []

    # 2.1 每个 clip 独立 normalize → concat → dedup
    # 修复：原逻辑「先 concat 再 normalize」在片段分辨率不同时失败
    # （ffmpeg concat 要求所有输入 video stream 的 resolution/SAR/pix_fmt 完全一致）。
    # 改为：每段按 fit_mode 独立 normalize，再 concat。
    #   cover：scale-increase + crop（等比铺满，溢出裁剪，无黑边）
    #   contain：scale-decrease + pad（等比缩小，居中补黑，全可见）
    #   fill：scale（直接拉伸，可能变形）
    #   blur_bg：scale-increase + crop 拿背景，scale-decrease + pad 叠前景（双层）
    a_inputs = "".join(f"[{a_input_idx[i]}:a]" for i in range(n))
    if fit_mode == "blur_bg":
        # 双层：原视频缩放铺满做模糊背景，前景 contain 居中
        # 同一 input stream 需先 split 切分，否则 ffmpeg "Error reinitializing filters"
        for i in range(n):
            v_ref = f"[{v_input_idx[i]}:v]"
            parts.append(f"{v_ref}split=2[bg_src{i}][fg_src{i}]")
            # 背景层：等比放大铺满后裁剪到目标，再高斯模糊
            # 注意：gblur 输出的 SAR 可能微变（15360:15367），concat 要求严格一致，必须加 setsar=1
            parts.append(
                f"[bg_src{i}]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h}:(iw-ow)/2:(ih-oh)/2,"
                f"gblur=sigma=30,setsar=1[bg{i}]"
            )
            # 前景层：等比缩小居中（contain）
            parts.append(
                f"[fg_src{i}]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,format=yuv420p[fg{i}]"
            )
        # 复合：bg 在下，fg 在上（concat 已统一尺寸，可直接 overlay）
        bg_streams = "".join(f"[bg{i}]" for i in range(n))
        fg_streams = "".join(f"[fg{i}]" for i in range(n))
        parts.append(f"{bg_streams}concat=n={n}:v=1:a=0[bgcat]")
        parts.append(f"{fg_streams}concat=n={n}:v=1:a=0[fgcat]")
        parts.append("[bgcat][fgcat]overlay=0:0[concat_v]")
    else:
        for i in range(n):
            v_ref = f"[{v_input_idx[i]}:v]"
            if fit_mode == "contain":
                scale_filter = (
                    f"{v_ref}scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                    f"setsar=1,format=yuv420p[v{i}]"
                )
            elif fit_mode == "fill":
                scale_filter = (
                    f"{v_ref}scale={w}:{h},setsar=1,format=yuv420p[v{i}]"
                )
            else:  # cover（默认）
                scale_filter = (
                    f"{v_ref}scale={w}:{h}:force_original_aspect_ratio=increase,"
                    f"crop={w}:{h}:(iw-ow)/2:(ih-oh)/2,"
                    f"setsar=1,format=yuv420p[v{i}]"
                )
            parts.append(scale_filter)
        v_norm = "".join(f"[v{i}]" for i in range(n))
        parts.append(f"{v_norm}concat=n={n}:v=1:a=0[concat_v]")

    # 2.2 dedup 滤镜（concat 后已统一分辨率，无需再 scale/pad）
    dedup_params = dedup_params or {}
    vf_dedup = build_dedup_filter(dedup_params, resolution)
    if vf_dedup and vf_dedup != "null":
        parts.append(f"[concat_v]{vf_dedup}[v_out]")
    else:
        parts.append("[concat_v]copy[v_out]")

    # 2.3 audio：先 audio concat → 再 BGM 处理
    if bgm_path:
        # BGM 先调音量；keep_audio=True 时跟原音 amix，False 时只保留 BGM
        parts.append(f"[{bgm_index}:a]volume={bgm_volume}[bg]")
        if keep_original_audio:
            # 保留原音：audio concat 输出 ca → amix with bg
            parts.append(f"{a_inputs}concat=n={n}:v=0:a=1[ca]")
            parts.append("[ca][bg]amix=inputs=2:duration=first[a_out]")
        else:
            # 丢弃原音：不调 audio concat（原音轨直接 garbage collected）
            parts.append("[bg]anull[a_out]")
    else:
        # 无 BGM：audio concat → anull → a_out
        parts.append(f"{a_inputs}concat=n={n}:v=0:a=1[ca]")
        parts.append("[ca]anull[a_out]")

    filter_complex = ";".join(parts)

    # 3. 编码器 / 色域 / 容器 metadata 参数
    enc = dedup_params.get("encoder", {})
    color = dedup_params.get("color", {})
    meta = dedup_params.get("metadata", {})

    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[v_out]", "-map", "[a_out]",
        "-c:v", get_h264_encoder(),
        "-b:v", "4000k", "-r", "30", "-s", f"{w}x{h}",
        "-profile:v", enc.get("profile", "high"),
        "-preset", "medium",
        "-bf", str(enc.get("bframes", 2)),
        "-g", str(enc.get("gop_size", 60)),
        # #101 色域扰动（默认 bt709）
        "-color_primaries", color.get("primaries", "bt709"),
        "-color_trc", color.get("trc", "bt709"),
        "-color_range", color.get("range", "tv"),
        "-c:a", "aac", "-ar", "44100", "-ac", "2",
    ]
    # BGM stream_loop 无限输入：按非 loop 输入（视频流）截断，避免 hang。
    # 无 BGM 时不加 -shortest，否则按"任一输入最短"截断会把多段拼接成 1 段。
    if bgm_path:
        cmd.append("-shortest")

    # 4. 容器 metadata 扰动（#101 P0）
    ts_offset = dedup_params.get("timestamp_offset_s", 0)
    if ts_offset:
        ts = (datetime.now() + timedelta(seconds=ts_offset)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        cmd += ["-metadata", f"creation_time={ts}"]
    for k in ("comment", "title", "encoder"):
        if meta.get(k):
            cmd += ["-metadata", f"{k}={meta[k]}"]

    cmd.append(out_path)
    result = run_cmd(cmd, timeout=600, monitor=True)
    ok = result is not None and result.returncode == 0 and Path(out_path).exists()
    if not ok and Path(out_path).exists():
        # 半成品清理（ffmpeg 失败时可能留 0 字节文件）
        Path(out_path).unlink(missing_ok=True)
    return ok


def probe_duration_ms(path: str) -> int:
    """探测文件时长（毫秒），失败返回 0。"""
    info = extract_media_info(probe_media(path))
    return info.get("duration_ms") or 0


def _probe_clip_streams(path: str) -> dict:
    """探测文件含哪些流。

    用途：filter_complex 引用 [i:v]/[i:a] 时流缺失会报"Stream specifier matches no streams"
    （rc=-22 EINVAL），需要在 inputs 阶段补 lavfi 合成流。

    返回:
        {"has_video": bool, "has_audio": bool, "duration_ms": int}
        探测失败时 has_video/has_audio 都兜底为 True（走原 -i 路径，让 ffmpeg 自行报错）
    """
    info = probe_media(path)
    if not info:
        return {"has_video": True, "has_audio": True, "duration_ms": 0}
    has_video = has_audio = False
    for stream in info.get("streams") or []:
        ctype = stream.get("codec_type")
        if ctype == "video":
            has_video = True
        elif ctype == "audio":
            has_audio = True
    duration_ms = 0
    try:
        duration_ms = int(float(info.get("format", {}).get("duration", 0)) * 1000)
    except (TypeError, ValueError):
        pass
    return {"has_video": has_video, "has_audio": has_audio, "duration_ms": duration_ms}


def _has_audio_stream(path: str) -> bool:
    """探测文件是否含音频流（兼容旧调用方；新代码优先用 _probe_clip_streams）。"""
    return _probe_clip_streams(path)["has_audio"]
