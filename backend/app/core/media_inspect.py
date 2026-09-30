# -*- coding: utf-8 -*-
"""视频内容检测模块（F-03 拉取条件增强）。

职责：
- 从视频文件均匀抽取若干帧（每 0.5s 一帧，约 60-120 帧/视频）
- 用 pHash 帧间对比 + 相邻相似帧合并，得到代表帧列表
- 字幕检测：对代表帧做 OCR，识别到任意文字即视为「有字幕」
- 主播人脸检测：用 MediaPipe Tasks API 检测代表帧中的人脸

设计原则：
- 抽帧复用 `app.core.ffmpeg` 的子进程封装（保持原生分辨率）
- 依赖懒加载：imagehash / mediapipe 在首次调用时导入，启动期不阻塞
- 检测依赖缺失或异常时一律视为「未检出」（默认放行，避免阻断正常入库流程）
- 检测结果只影响拉取入库过滤，不修改视频文件本身
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

from app.core.ffmpeg import FFMPEG, THUMB_JPEG_Q, THUMB_WIDTH
from app.services.setting_service import get_data_dir


# ---------- 抽帧（任务 #127：均匀抽帧 + 帧间合并） ----------

# 抽帧时间间隔（秒）：每 0.5s 一帧，60s 视频约 120 帧
_FRAME_INTERVAL_S = 0.5

# 抽帧上限：超长视频封顶 120 帧（约 60s 视频），防止 10 分钟视频抽 1200 帧爆磁盘
_MAX_FRAMES = 120

# 任务 #128 优化6：流式分批 + 命中早停。每 5s 一批：短视频 1 批可拦；长视频平均砍半耗时。
_STREAM_BATCH_S = 5.0


class VideoCorruptedError(Exception):
    """视频文件损坏（ffprobe 探测失败：moov atom not found / Invalid data 等）。

    inspect_video 在抽帧前用 ffprobe 探测时长，探测失败即抛本异常。
    调用方收到后应视为「文件不可用」拒绝入库，不进入抽帧循环（避免无效 ffmpeg 调用）。
    """


class VideoNoStreamError(Exception):
    """视频文件无视频流（纯音频伪装 mp4 / m4a 误标 .mp4 等）。

    ffprobe 探测成功且有时长字段，但 streams 全是 audio / subtitle / data，
    没有 codec_type=video。抽帧必然全部 -22 失败（image2 muxer 无 stream 可写）。

    inspect_video 在抽帧前识别抛本异常，跳过抽帧循环直接拒绝入库，
    节省 100+ 次无效 ffmpeg 调用，同时避免 retry stderr 日志刷屏。
    """

# 单次抽帧超时（秒）
_FRAME_TIMEOUT = 15

# 抽帧缓存目录（data/cache/inspect/{material_id}/）
_INSPECT_DIR = Path("cache") / "inspect"

# pHash 哈希尺寸：8 → 64-bit；越大越精细但计算越慢
_PHASH_HASH_SIZE = 8

# 相邻帧相似合并的汉明距阈值（64-bit 上 ≤5 表示高度相似；经验值）
_PHASH_HAMMING_THRESHOLD = 5

# 自适应阈值开关：True 时根据本批帧间汉明距 P25 动态收紧阈值
# （场景切换频繁时硬阈值易误合并，短视频镜头变化大时可关闭）
_PHASH_ADAPTIVE_THRESHOLD = True

# 抽帧并发度上限：min(8, os.cpu_count())；ffmpeg 是 IO 密集型，8 够用
_FRAME_CONCURRENCY = max(1, min(8, (os.cpu_count() or 4)))

# 进度日志步长：每 N 帧打印一次，避免长任务日志刷屏
_PROGRESS_STEP = 20

# pHash 磁盘缓存文件名（与 frame_*.jpg 同目录）
_PHASH_CACHE_NAME = "phash_cache.json"


def _inspect_dir(material_id: str) -> Path:
    """获取检测缓存目录（用于缓存抽帧结果，便于多次检测复用）。"""
    base = get_data_dir() / _INSPECT_DIR / material_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _probe_video_metadata(video_path: str) -> Tuple[float, bool]:
    """探测视频元数据（时长 + 是否含视频流），用于推导抽帧总数 + 抽帧前置检查。

    一次 ffprobe 同时校验时长与视频流，避免纯音频伪装 mp4 触发 100+ 次 ffmpeg 失败。

    返回:
        (duration_s, has_video_stream) — 都成功才返回。
    异常:
        VideoCorruptedError - ffprobe 失败 / 无 metadata 时长
            （moov atom not found / Invalid data / metadata 不完整）
        VideoNoStreamError - 有时长但无 video 流（纯音频 m4a 伪装 .mp4 等）。
            抽帧必然 -22 失败，应拒绝入库而非进入抽帧循环。

    调用方应在抽帧前识别两个异常，跳过循环直接拒绝入库，节省 100+ 次 ffmpeg 调用。
    """
    from app.core.ffmpeg import probe_media, extract_media_info
    info = probe_media(video_path)
    if not info:
        raise VideoCorruptedError(f"ffprobe 探测失败（文件可能损坏）：{video_path}")
    duration_ms = extract_media_info(info).get("duration_ms") or 0
    if duration_ms <= 0:
        # ffprobe 探测成功但没读到时长：视频缺 metadata/时长字段，
        # 视为损坏（无法推导抽帧位置，强行抽会全部失败）
        raise VideoCorruptedError(f"ffprobe 无时长字段（文件 metadata 不完整）：{video_path}")
    # 校验视频流：纯音频伪装 mp4（如 m4a 误标 .mp4）无 -frames:v 可处理的 stream，
    # 抽帧必然 rc=-22「Output file does not contain any stream」。
    has_video = any(s.get("codec_type") == "video" for s in info.get("streams", []))
    if not has_video:
        raise VideoNoStreamError(f"ffprobe 无视频流（纯音频伪装？）：{video_path}")
    return duration_ms / 1000.0, True


def _frame_seek_points(duration_s: float) -> List[float]:
    """根据视频时长推导均匀抽帧时间点（秒）。

    策略：每 `_FRAME_INTERVAL_S` 秒一个时间点，从 0s 开始，封顶 `_MAX_FRAMES`。
    极短视频（< 0.5s）至少返回 1 个点；时长探测失败时退化为单点 [0.0]（开头帧
    100% 安全，避免旧版 [1.0, 5.0, 10.0] 在短于 1s 的视频上 -ss 越界 rc=-22）。
    """
    if duration_s <= 0:
        # 探测失败退化：单点开头帧；批量检测对单帧也能跑（OCR/人脸无帧对比依赖）
        return [0.0]
    n = max(1, math.ceil(duration_s / _FRAME_INTERVAL_S))
    n = min(n, _MAX_FRAMES)
    pts: List[float] = []
    for i in range(n):
        t = round(i * _FRAME_INTERVAL_S, 2)
        # 防止 ffmpeg -ss 落空：最后一个时间点不超过 duration - 0.3s
        if t >= duration_s - 0.3:
            t = max(0.0, duration_s - 0.3)
        pts.append(t)
    return pts


def _frame_batches(seek_points: List[float], batch_s: float = _STREAM_BATCH_S
                   ) -> List[Tuple[int, List[float]]]:
    """按时间窗口把抽帧点切分为多个批次（任务 #128 优化6：流式早停）。

    每个批次的帧在同一时间窗口 `[batch_idx*batch_s, (batch_idx+1)*batch_s)` 内。
    返回: [(batch_idx, [seek_points...]), ...] 按 batch_idx 升序。
    """
    if not seek_points:
        return []
    batches_dict: Dict[int, List[float]] = {}
    for p in seek_points:
        b_idx = int(p // batch_s)
        batches_dict.setdefault(b_idx, []).append(p)
    return sorted(batches_dict.items())


def _extract_batch_frames(video_path: str, out_dir: Path,
                          batch_idx: int, batch_seeks: List[float]) -> List[Path]:
    """抽一个批次的帧到磁盘（带缓存复用），返回按 seek 顺序的帧路径列表。

    与 `extract_inspect_frames` 区别：仅处理传入的 batch_seeks（不遍历整段视频），
    用于流式场景。
    """
    frames: List[Optional[Path]] = []
    tasks: List[Tuple[str, Path, float, int]] = []
    for local_idx, seek in enumerate(batch_seeks):
        frame_path = out_dir / f"b{batch_idx:02d}_{local_idx:02d}_{int(seek*1000):06d}ms.jpg"
        if frame_path.exists() and frame_path.stat().st_size > 0:
            frames.append(frame_path)
        else:
            frames.append(None)
            tasks.append((video_path, frame_path, seek, len(frames) - 1))

    if tasks:
        # 复用线程池并发抽帧
        with ThreadPoolExecutor(max_workers=_FRAME_CONCURRENCY) as pool:
            future_map = {pool.submit(_run_extract_frame, t): t for t in tasks}
            from concurrent.futures import as_completed
            done = 0
            for fut in as_completed(future_map):
                _vp, frame_path, _seek, idx = future_map[fut]
                if fut.result():
                    frames[idx] = frame_path
                done += 1
                if done % _PROGRESS_STEP == 0 or done == len(tasks):
                    logger.debug("[抽帧] 批次 {} 进度 {}/{}", batch_idx, done, len(tasks))

    return [p for p in frames if p is not None]


def _run_extract_frame(args: Tuple[str, Path, float, int]) -> bool:
    """ffmpeg 抽单帧子任务（线程池 worker）。

    用 Popen 显式管理子进程（审查修复 #10）：超时主动 kill + 二次 communicate 回收，
    避免 subprocess.run + TimeoutExpired 后子进程 zombie 占用。

    任务 #129 修复：失败时捕获 stderr 写入 WARNING，便于诊断「所有批次均未产出帧」
    的根因（编码异常 / 文件被删 / 权限不足）。不再用 DEVNULL 吞错。

    参数: (video_path, frame_path, seek, ordered_idx)
    返回: True=成功生成文件（调用方负责写回 ordered_paths[ordered_idx]）。
    """
    video_path, frame_path, seek, _idx = args
    import subprocess

    def _build_cmd(seek_s: float) -> list:
        return [
            FFMPEG, "-y",
            "-ss", f"{seek_s:.2f}",
            "-i", video_path,
            "-frames:v", "1",
            # ffmpeg 7.x image2 muxer 默认非「单帧覆盖写入」模式，
            # 必须显式 -update 1 才能把 .jpg 写为单帧文件；否则报
            # "Output file does not contain any stream" / rc=-22 (EINVAL)
            "-update", "1",
            "-vf", f"scale={THUMB_WIDTH}:-2",
            "-q:v", str(THUMB_JPEG_Q),
            str(frame_path),
        ]
    # 主路径用入参 seek；若 -ss 越界（短视频等）失败，退到开头帧 0.0 再试一次。
    # 兜底机制：开头帧永远在文件内（即使 < 1s），保证至少 1 帧产出。
    cmd = _build_cmd(seek)
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[抽帧] ffmpeg 启动异常：{}", e)
        return False
    try:
        stdout_b, stderr_b = proc.communicate(timeout=_FRAME_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "[抽帧] ffmpeg 超时（>{}s）已 kill：seek={} 文件={}",
            _FRAME_TIMEOUT, seek, video_path,
        )
        return False
    except Exception as e:  # noqa: BLE001
        logger.debug("[抽帧] ffmpeg communicate 异常：{}", e)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return False
    # 失败时把 ffmpeg stderr 抽出来（截末 1500 字符，_stderr_tail helper），
# 覆盖 input 探测 + 流映射 + image2 muxer 错，便于诊断真因。
    if proc.returncode != 0 or not frame_path.exists():
        # 兜底：主 seek 失败时重试一次开头帧 0.0（解决 -ss 越界 rc=-22）。
        # 仅在主 seek > 0 时退避，避免对已经是 0.0 的 seek 死循环。
        # 改用 subprocess.run + capture_output：run 内置封装 communicate + returncode，
        # 避免 Popen + communicate 的 BrokenPipeError / 子进程 zombie 等 corner case
        # 让 retry 走 except 路径导致 retry_stderr_b 保持 b""（日志 retry=(empty) 误导）。
        # 细化异常：TimeoutExpired / Exception 分别记录到 retry_stderr_b，
        # 让日志 retry 字段永远有内容（真无 stderr 时显式 [empty]，异常时 [exception: ...]）。
        retry_stderr_b: bytes = b""
        if seek > 0:
            retry_cmd = _build_cmd(0.0)
            try:
                retry_result = subprocess.run(
                    retry_cmd, capture_output=True, timeout=_FRAME_TIMEOUT,
                    creationflags=creationflags,
                )
                retry_stderr_b = retry_result.stderr or b""
                if retry_result.returncode == 0 and frame_path.exists():
                    return True
            except subprocess.TimeoutExpired:
                retry_stderr_b = b"[timeout]"
            except Exception as e:  # noqa: BLE001
                retry_stderr_b = f"[retry exception: {e!r}]".encode("utf-8", errors="ignore")
        # 主失败 + retry 失败时日志同时带两者 stderr，便于区分 root cause：
        # - 主 stderr 是 -ss 越界/文件损坏 → 重试也失败同一根因
        # - 主 stderr 空但 retry 有 stderr → 主进程异常但 retry 真因不同
        main_tail = _stderr_tail(stderr_b)
        retry_tail = _stderr_tail(retry_stderr_b)
        logger.warning(
            "[抽帧] ffmpeg 失败 rc={} seek={} 文件={}：主 stderr={} | retry stderr={}",
            proc.returncode, seek, video_path, main_tail, retry_tail,
        )
        return False
    return True


def _stderr_tail(b: bytes) -> str:
    """截 stderr bytes 末尾 1500 字符；空时返 (empty) 占位，保持日志格式统一。

    1500 而非 300：ffmpeg stderr 通常包含 input 探测（~500）+ 流映射（~500）+ 末尾错
    （~200）。300 只看到末尾 image2 muxer 错，看不到上游 decoder / demuxer 错，掩盖真因。
    """
    s = (b or b"").decode("utf-8", errors="ignore").strip()[-1500:]
    return s or "(empty)"


def extract_inspect_frames(video_path: str, material_id: str) -> List[Path]:
    """抽帧到 data/cache/inspect/{material_id}/；返回帧文件路径列表。

    抽帧策略（任务 #127）：每 0.5s 抽一帧，约 60-120 帧/视频，封顶 120 帧。
    并发：未命中磁盘缓存的帧用 ThreadPoolExecutor 并行 ffmpeg 调用；
    已存在帧直接复用，不进 worker。进度每 `_PROGRESS_STEP` 帧打印一次。
    缓存命名 `frame_{idx}_{ms}ms.jpg`：同一 material 多次检测复用抽帧结果。
    任何抽帧失败静默跳过（视频极短/ffmpeg 缺系统库等场景）。
    """
    out_dir = _inspect_dir(material_id)
    try:
        duration_s, _has_video = _probe_video_metadata(video_path)
    except Exception as e:  # noqa: BLE001
        # VideoNoStreamError / VideoCorruptedError 都被兜底为 duration_s=0.0
        # → _frame_seek_points(0) 退化为 [0.0] 单帧，worker 跑完发现失败即可。
        # 此函数无「拒绝入库」职责（只抽帧），不抛异常上抛。
        logger.debug("[抽帧] 探测时长失败 {}：{}", video_path, e)
        duration_s = 0.0

    # 1) 分离缓存命中与待抽帧，记录 idx 以便 worker 回写结果
    tasks: List[Tuple[str, Path, float, int]] = []
    ordered_paths: List[Optional[Path]] = []
    seek_points = _frame_seek_points(duration_s)
    for idx, seek in enumerate(seek_points):
        frame_path = out_dir / f"frame_{idx}_{int(seek*1000):06d}ms.jpg"
        if frame_path.exists() and frame_path.stat().st_size > 0:
            ordered_paths.append(frame_path)
        else:
            ordered_paths.append(None)
            tasks.append((video_path, frame_path, seek, idx))

    # 2) 并发抽未命中帧（worker 完成后回写 ordered_paths）
    if tasks:
        logger.info(
            "[抽帧] 待抽 {} 帧（总 {} 帧，缓存命中 {}），并发度 {}",
            len(tasks), len(seek_points),
            len(seek_points) - len(tasks), _FRAME_CONCURRENCY,
        )
        with ThreadPoolExecutor(max_workers=_FRAME_CONCURRENCY) as pool:
            future_map = {pool.submit(_run_extract_frame, t): t for t in tasks}
            from concurrent.futures import as_completed
            done = 0
            for fut in as_completed(future_map):
                _video_path, frame_path, _seek, idx = future_map[fut]
                ok = fut.result()
                if ok:
                    ordered_paths[idx] = frame_path
                done += 1
                if done % _PROGRESS_STEP == 0 or done == len(tasks):
                    logger.info("[抽帧] 进度 {}/{}", done, len(tasks))

    # 3) 按原顺序收集结果（失败的保持 None，最后过滤）
    frames: List[Path] = [p for p in ordered_paths if p is not None]
    if not frames:
        logger.warning(
            "[抽帧] 未生成任何帧文件，请检查 ffmpeg 是否可执行（path={}）或换用完整版 ffmpeg",
            FFMPEG,
        )
    return frames


# ---------- 帧间相似度对比（pHash） ----------

_PHASH_LOCK = threading.Lock()
_PHASH_CACHE_LOCK = threading.Lock()  # 保护磁盘缓存 JSON 读改写（审查修复 #1）
_PHASH_LIB_OK: Optional[bool] = None  # 懒探测 imagehash / PIL 可用性


def _ensure_phash_lib() -> bool:
    """探测 imagehash + Pillow 是否可用（一次探测，进程内缓存）。

    返回:
        True=可用；False=缺失或导入失败（合并流程将跳过）。
    """
    global _PHASH_LIB_OK
    if _PHASH_LIB_OK is not None:
        return _PHASH_LIB_OK
    with _PHASH_LOCK:
        if _PHASH_LIB_OK is not None:
            return _PHASH_LIB_OK
        try:
            import imagehash  # noqa: F401
            from PIL import Image  # noqa: F401
            _PHASH_LIB_OK = True
            logger.info("[pHash] imagehash + Pillow 可用，启用帧间合并")
        except ImportError as e:
            logger.warning("[pHash] 依赖缺失，跳过帧间合并（pip install imagehash Pillow）：{}", e)
            _PHASH_LIB_OK = False
        except Exception as e:  # noqa: BLE001
            logger.warning("[pHash] 依赖初始化失败，跳过帧间合并：{}", e)
            _PHASH_LIB_OK = False
        return _PHASH_LIB_OK


def _phash_cache_path(out_dir: Path) -> Path:
    """pHash 磁盘缓存文件路径：与 frame_*.jpg 同目录。"""
    return out_dir / _PHASH_CACHE_NAME


def _load_phash_cache(out_dir: Path) -> Dict[str, dict]:
    """加载 pHash 磁盘缓存。文件不存在/解析失败返回空 dict。"""
    cache_file = _phash_cache_path(out_dir)
    if not cache_file.exists():
        return {}
    try:
        with cache_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.debug("[pHash] 缓存读取失败 {}：{}", cache_file, e)
        return {}


def _save_phash_cache(out_dir: Path, cache: Dict[str, dict]) -> None:
    """写入 pHash 磁盘缓存（原子：先写 .tmp 再 rename）。失败仅 DEBUG，不阻断。"""
    cache_file = _phash_cache_path(out_dir)
    tmp = cache_file.with_suffix(".json.tmp")
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        tmp.replace(cache_file)
    except Exception as e:  # noqa: BLE001
        logger.debug("[pHash] 缓存写入失败 {}：{}", cache_file, e)


def _compute_phash(frame_path: Path) -> Optional["object"]:
    """计算单帧的 64-bit pHash；依赖缺失/读图失败返回 None。

    纯计算函数（无 IO）。缓存逻辑上移到 _merge_similar_frames（批级 load/save），
    避免逐帧 load+save 的 IO 浪费与并发读改写竞争。

    返回:
        imagehash.ImageHash 对象；调用方可用 `h1 - h2` 求汉明距。
    """
    if not _ensure_phash_lib():
        return None
    try:
        from PIL import Image
        import imagehash
        with Image.open(str(frame_path)) as img:
            return imagehash.phash(img.convert("L"), hash_size=_PHASH_HASH_SIZE)
    except Exception as e:  # noqa: BLE001
        logger.debug("[pHash] 计算失败 {}：{}", frame_path.name, e)
        return None


def _compute_phashes_with_cache(frames: List[Path], material_id: Optional[str] = None
                                 ) -> List[Optional["object"]]:
    """批级 pHash 计算 + 磁盘缓存（任务 #127 优化2 + 审查修复 #1/#4）。

    流程（线程安全）：
    1) 一次性加载整批共享的磁盘缓存（_PHASH_CACHE_LOCK 保护读改写）
    2) 逐帧查缓存：命中且 mtime+size 校验通过 → 反序列化；未命中 → 调 _compute_phash
    3) 一次性原子写回整个缓存（rename .tmp → 正式）

    相对逐帧 load/save 节省 N-1 次 JSON 序列化，并发调用同 material_id 安全。

    参数:
        frames: 帧路径列表
        material_id: 传则启用磁盘缓存；None 则纯计算无缓存
    返回:
        与 frames 等长的 ImageHash 列表；失败的位为 None
    """
    if not frames:
        return []

    cache: Dict[str, dict] = {}
    out_dir: Optional[Path] = None
    if material_id:
        out_dir = _inspect_dir(material_id)

    # 1) 一次性 load（在锁内做完整读改写，避免 N 次独立锁开销）
    with _PHASH_CACHE_LOCK:
        if out_dir is not None:
            cache = _load_phash_cache(out_dir)

        # 2) 逐帧：查缓存 → 未命中则计算
        results: List[Optional["object"]] = []
        dirty = False
        import imagehash  # 局部导入
        for fp in frames:
            entry = cache.get(fp.name)
            hit = False
            if entry:
                try:
                    stat = fp.stat()
                    if entry.get("mtime") == stat.st_mtime and entry.get("size") == stat.st_size:
                        results.append(imagehash.hex_to_hash(entry["hash"]))
                        hit = True
                except Exception:  # noqa: BLE001
                    pass
            if hit:
                continue
            h = _compute_phash(fp)
            results.append(h)
            if h is not None and out_dir is not None:
                try:
                    stat = fp.stat()
                    cache[fp.name] = {
                        "mtime": stat.st_mtime,
                        "size": stat.st_size,
                        "hash": str(h),
                    }
                    dirty = True
                except Exception:  # noqa: BLE001
                    pass

        # 3) 一次性写回
        if dirty and out_dir is not None:
            _save_phash_cache(out_dir, cache)
    return results


def _phash_hamming(h1, h2) -> int:
    """计算两个 pHash 之间的汉明距（不同位数）。"""
    try:
        return int(h1 - h2)
    except Exception:  # noqa: BLE001
        return _PHASH_HASH_SIZE * _PHASH_HASH_SIZE  # 异常视为最不相似


def _adaptive_threshold(distances: List[int]) -> int:
    """根据相邻帧汉明距分布推导自适应阈值。

    取 P25（25%分位数），夹在 [3, _PHASH_HAMMING_THRESHOLD] 之间。
    - 场景切换快（汉明距普遍大）：P25 偏高 → 维持硬阈值
    - 场景稳定（汉明距普遍小）：P25 偏低 → 阈值收紧，合并更激进
    - 样本不足（<5 帧）：直接返回硬阈值
    """
    if not distances or len(distances) < 5:
        return _PHASH_HAMMING_THRESHOLD
    sorted_d = sorted(distances)
    p25_idx = max(0, len(sorted_d) // 4)
    p25 = sorted_d[p25_idx]
    return max(3, min(_PHASH_HAMMING_THRESHOLD, int(p25)))


def _merge_similar_frames(frames: List[Path], material_id: Optional[str] = None) -> List[Path]:
    """按时间序合并相邻相似帧，返回代表帧列表（每组保留首帧）。

    算法（任务 #127 优化3）：
    - 先批量算所有帧 pHash（启用 material_id 时走磁盘缓存）
    - 统计相邻汉明距分布，按 P25 自适应阈值（_PHASH_ADAPTIVE_THRESHOLD 控制开关）
    - 与当前组首帧比汉明距：≤ 阈值归入当前组；> 阈值开新组
    - 每组保留首帧为代表帧

    异常兜底：imagehash 缺失/单帧计算失败 → 返回原列表（不合并，保持兼容）。

    参数:
        frames: 按时间序排列的帧路径列表
        material_id: 传则启用 pHash 磁盘缓存（同一 material 二次检测 0 成本）
    返回:
        代表帧路径列表（长度 ≤ 原列表）
    """
    if not frames:
        return frames
    if not _ensure_phash_lib():
        return frames
    if len(frames) == 1:
        return frames

    # 1) 批量算 pHash（批级缓存读改写一次，线程安全）
    hashes = _compute_phashes_with_cache(frames, material_id)
    if hashes[0] is None:
        # 第一帧失败：放弃合并，保持兼容
        return frames

    # 2) 统计相邻汉明距 + 自适应阈值
    distances: List[int] = []
    for i in range(1, len(hashes)):
        if hashes[i] is not None and hashes[i - 1] is not None:
            distances.append(_phash_hamming(hashes[i - 1], hashes[i]))
    threshold = (
        _adaptive_threshold(distances) if _PHASH_ADAPTIVE_THRESHOLD
        else _PHASH_HAMMING_THRESHOLD
    )

    # 3) 单遍合并
    representatives: List[Path] = [frames[0]]
    cur_hash = hashes[0]
    for fp, h in zip(frames[1:], hashes[1:]):
        if h is None:
            representatives.append(fp)
            cur_hash = None
            continue
        if cur_hash is None:
            representatives.append(fp)
            cur_hash = h
            continue
        if _phash_hamming(cur_hash, h) > threshold:
            representatives.append(fp)
            cur_hash = h

    raw_n = len(frames)
    merged_n = len(representatives)
    ratio = merged_n / raw_n if raw_n else 1.0
    adaptive_tag = "自适应" if _PHASH_ADAPTIVE_THRESHOLD else "硬阈值"
    logger.info(
        "[pHash] 相邻帧合并：原始 {} 帧 → 代表 {} 帧（保留 {:.1%}，{}≤{}，avg 距离 {:.1f}）",
        raw_n, merged_n, ratio, adaptive_tag, threshold,
        (sum(distances) / len(distances)) if distances else 0.0,
    )
    return representatives


# ---------- 字幕检测（OCR） ----------

_OCR_LOCK = threading.Lock()
_OCR_READER = None  # 懒加载 RapidOCR 引擎
_OCR_LANG = ("ch", "en")  # 中文 + 英文


def _get_ocr_reader():
    """懒加载 RapidOCR；首次调用初始化，进程内单例。

    返回:
        RapidOCR 实例；依赖缺失/初始化失败返回 None。
    选型说明：rapidocr-onnxruntime 基于 ONNX Runtime，体积小、不依赖 torch，
    避开 easyocr 在 Windows 上 c10.dll 初始化的兼容问题。
    """
    global _OCR_READER
    if _OCR_READER is not None:
        return _OCR_READER
    with _OCR_LOCK:
        if _OCR_READER is not None:
            return _OCR_READER
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except ImportError:
            logger.warning("[字幕检测] rapidocr-onnxruntime 未安装，跳过字幕检测（pip install rapidocr-onnxruntime 可启用）")
            return None
        try:
            _OCR_READER = RapidOCR()
            logger.info("[字幕检测] RapidOCR 初始化完成（lang={}）", _OCR_LANG)
            return _OCR_READER
        except Exception as e:  # noqa: BLE001
            logger.warning("[字幕检测] RapidOCR 初始化失败：{}", e)
            return None


def detect_subtitle(frames: List[Path], min_confidence: float = 0.4) -> Tuple[bool, List[str]]:
    """字幕检测：对画面帧 OCR，识别到任意文字即视为「有字幕」。

    参数:
        frames: 抽帧结果路径列表
        min_confidence: 置信度阈值（识别结果 >= 该值算有效文字）
    返回:
        (是否命中字幕, 识别到的文本列表)。命中时返回该帧去重+截断后的字幕文本
        （用于排障日志），无字幕或依赖缺失时返回 (False, [])。
    """
    reader = _get_ocr_reader()
    if reader is None or not frames:
        return False, []
    try:
        for fp in frames:
            try:
                # RapidOCR 返回 ([(bbox, text, conf), ...],) 或 None
                result, _elapsed = reader(str(fp))
            except Exception as e:  # noqa: BLE001
                logger.debug("[字幕检测] 单帧 OCR 异常：{}", e)
                continue
            if not result:
                continue
            texts: List[str] = []
            for item in result:
                # 兼容两种格式：3 元组 (bbox, text, conf) 或带 score 的 dict
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    conf, text = float(item[2] or 0), (item[1] or "")
                elif isinstance(item, dict):
                    conf = float(item.get("score", 0) or 0)
                    text = item.get("text", "") or ""
                else:
                    continue
                if conf >= min_confidence and text.strip():
                    texts.append(text.strip())
            # 单帧有任意字幕文本即命中,返回该帧文本（早停避免后续帧重复 OCR）
            if texts:
                return True, _dedup_and_trim_texts(texts)
    except Exception as e:  # noqa: BLE001
        logger.warning("[字幕检测] 异常，默认放行：{}", e)
    return False, []


def _dedup_and_trim_texts(
    texts: List[str], max_each: int = 50, max_total: int = 200
) -> List[str]:
    """字幕文本去重 + 截断每条 + 整体截断,避免日志刷屏。

    每条最长 max_each 字符,累计达到 max_total 后追加 "..." 并停止。
    """
    seen, out, total = set(), [], 0
    for t in texts:
        s = t.strip()[:max_each]
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        total += len(s)
        if total >= max_total:
            out.append("...")
            break
    return out


# ---------- 主播人脸检测（任务 #48 → 1.x：MediaPipe Tasks API 主检测器） ----------

_FACE_LOCK = threading.Lock()
_FACE_DETECTOR_SHORT = None  # MediaPipe FaceDetector 短距（<2m）
_FACE_DETECTOR_FULL = None   # MediaPipe FaceDetector 全距（>2m）

# MediaPipe Tasks API 模型本地路径（Google Storage 实际发布 .tflite，
# 未发布 .task 打包版——.task 仅用于 Pose/LLM 等需要元数据 bundle 的场景）
# 项目内置 / PyInstaller 打包 / 用户缓存，三级查找
_FACE_MODEL_SHORT = "blaze_face_short_range.tflite"
_FACE_MODEL_FULL = "blaze_face_full_range.tflite"

# Google Storage 模型下载源（在线 fallback）
_FACE_MODEL_URLS = {
    "blaze_face_short_range.tflite":
        "https://storage.googleapis.com/mediapipe-models/face_detector/"
        "blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
    "blaze_face_full_range.tflite":
        "https://storage.googleapis.com/mediapipe-models/face_detector/"
        "blaze_face_full_range/float16/1/blaze_face_full_range.tflite",
}

# 最小置信度阈值（MediaPipe 返回 [0,1]，默认 0.5）
_FACE_MIN_CONFIDENCE = 0.5

# 最小人脸框像素（短边）；低于此尺寸视为误检或过远，过滤掉
_FACE_MIN_SIZE = 40

# 任务 #128 优化5：Full Range 远距检测器误报率显著高于 Short Range。
# 收紧其置信度与最小尺寸，仅在 Short Range 完全无命中时才启用 Full Range。
_FACE_FULL_MIN_CONFIDENCE = 0.6   # Short 0.5；Full 远景更高门槛
_FACE_FULL_MIN_SIZE = 60          # Short 40；Full 砍掉远景小误报
_FACE_FULL_CONFIRM_SIZE = 100     # ≥ 100px 直接确认（强信号）；60-99px 走跨帧累计
# 任务 #129 复盘修复 7685 食物误识别：Full 强命中除 bbox≥100px 外再要求 score≥0.7。
# 实测：MediaPipe 退化 bbox score 通常 ≤ 0.7（7685 食物=0.6521），
# 真脸 score 通常 ≥ 0.7（短视频小框远景脸在 0.62-0.77 之间，但小框不走强命中阈值）。
_FACE_FULL_CONFIRM_SCORE = 0.7    # Full 强命中须 score ≥ 0.7（按 score 区分退化 vs 真脸）

# 任务 #129 复盘修复 48849a/69c29d/95a5f5/d5c7be/5765ab 5 个食物误识别：
# Short Range 也会给低分退化 bbox（score 0.5-0.65，bbox 100-200px 食物），原来
# "命中即返回 True" 太宽松。加 score 强信号阈值：short 命中 score ≥ 0.7 直接确认；
# score < 0.7 走跨帧累计（≥ 2 帧），单帧退化不会翻车。
_FACE_SHORT_CONFIRM_SCORE = 0.7   # Short 强命中须 score ≥ 0.7
_FACE_SHORT_PENDING_FRAMES = 3    # Short 弱命中跨帧累计阈值（任务 #129 复盘 48849a：2 帧仍误判食物）

# 任务 #129 复盘修复 7a9fcd0a（烤串）/d447e9c9（五花肉）误识别：
# Full Range 在食物上偶发「bbox≥100px + score≥0.7 强命中」误报。分析 keypoints
# 发现：真脸的 6 个 keypoint 在 bbox 内呈左右对称分布（左眼/左耳在左半，
# 右眼/右耳在右半，x 跨度 > 0.4）；食物误报的关键点全挤在 bbox 一侧（7a9 全
# 在左侧 x<0.2，d447 全在右侧 x>0.65），完全无左右结构。加 keypoint 左右分布
# 验证：要求至少 1 个 kp 在 bbox 左半（x≤0.45）且至少 1 个 kp 在 bbox 右半
# （x≥0.55）。仅在 Full 强命中分支验证——Short Range 与 Full 弱命中累计分支
# 不查（避免假阳性）。
_FACE_KP_LEFT_MAX = 0.45           # kp 视为「左」的上限
_FACE_KP_RIGHT_MIN = 0.55          # kp 视为「右」的下限

# 任务 #129 复盘修复 7a9fcd0a（烤串）/d447e9c9（五花肉）v2：keypoint 左右分布
# 验证拦不住 MediaPipe 的「食物上幻觉人脸」（模型硬挤出面部几何）。
# 实测 bbox 内纹理特征：
#   真脸 12b23    : 灰度 std=59, sat=76, edge=0.063
#   误判 7a9 烤串: std=20, sat=41, edge=0.006（烤焦肉颜色均匀）
#   误判 d447 五花: std=30, sat=185, edge=0.235（红肉高饱和）
#   真脸 000519 远: std=35-47, sat=78-104（小但五官仍有对比）
# 规则：Full Range 强命中额外做三重过滤——
#   sat_mean > SAT_MAX（红肉/高饱和食物）→ reject
#   或 std < STD_MIN 且 edge_density < EDGE_MIN（颜色均匀+无纹理）→ reject
# 仅作用于 Full 强命中分支（Short Range 命中即返回；弱命中累计不影响）
_FACE_BBOX_GRAY_STD_MIN = 30.0     # bbox 内灰度标准差下限
_FACE_BBOX_EDGE_DENSITY_MIN = 0.05  # bbox 内 Canny 边缘密度下限
_FACE_BBOX_SAT_MAX = 150.0         # bbox 内 HSV 饱和度均值上限

# 任务 #129 复盘修复 7671 误识别：bbox 占画面比例上限（防止 detector 把整块
# 图案当脸）。实测：真实抖音人脸宽度通常 ≤ 33% 画面宽（人不会占满整屏），
# 超过说明 detector 在不确定时输出退化 bbox（7671 frame_15 误识别 249/720=35%）。
# Short Range / Full Range 共用。
_FACE_BBOX_MAX_RATIO_W = 0.33     # bbox.width ≤ 画面宽 33%
_FACE_BBOX_MAX_RATIO_H = 0.33     # bbox.height ≤ 画面高 33%

# Full Range 弱命中跨帧累计阈值：单帧 bbox<100px 走投票，≥ N 帧累计才确认。
# 真远景人脸跨帧稳定（占视频时长多帧），误识别单帧偶发但通常 ≤ 2 帧。
_FACE_FULL_PENDING_FRAMES = 3     # 累计 ≥ 3 帧弱命中即确认（7671 误识别仅 2 帧）

# 任务 #129 复盘修复 7651 烧烤视频：人脸命中时把可疑帧复制 + 画 bbox 框存到
# data/cache/face_evidence/{video_id}/，便于人工复检（误判/漏判直接看图）。
# 放在 data/cache/ 下（DATA_TEMP_SUBDIRS）由 cleanup_startup_temp 按
# temp_retention_days 自动清理，无需用户手动删；用户也可在设置里点清理缓存按钮删。
_FACE_EVIDENCE_DIR = "face_evidence"   # data/cache/ 下的子目录名（英文避免编码问题）

# 肤色预筛阈值：单帧肤色像素占比 < 该值则直接判无人脸（跳过检测，砍烤肉/风景）
_SKIN_MIN_RATIO = 0.005  # 0.5%

# HSV 肤色范围（宽松，覆盖黄/白/黑肤色；过滤非肤色背景）
_SKIN_HSV_LOWER = (0, 30, 60)
_SKIN_HSV_UPPER = (25, 200, 255)


def _bundled_face_models_dir() -> Path | None:
    """查找打包内置 MediaPipe 模型目录。

    优先级：
    1. `app/core/models/face/`（源代码内置路径）
    2. PyInstaller 解包目录 `sys._MEIPASS/app/core/models/face/`

    返回:
        目录 Path（None = 未找到）。
    """
    try:
        here = Path(__file__).resolve().parent
        candidate = here / "models" / "face"
        if candidate.exists():
            return candidate
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = Path(meipass) / "app" / "core" / "models" / "face"
            if candidate.exists():
                return candidate
    except Exception:  # noqa: BLE001
        pass
    return None


def _download_face_model(filename: str, dst_dir: Path, timeout: int = 30) -> Path | None:
    """从 Google Storage 下载缺失的模型文件（fallback）。

    仅在源码内置与用户缓存均未命中时触发；任何异常静默失败，返回 None。
    文件 < 100KB 视为下载失败（避免空文件被误用）。
    """
    url = _FACE_MODEL_URLS.get(filename)
    if not url:
        return None
    dst = dst_dir / filename
    if dst.exists() and dst.stat().st_size > 100_000:
        return dst
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
        if len(data) < 100_000:
            return None
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        logger.info("[人脸检测] 模型下载完成 {} ({} KB)", filename, len(data) // 1024)
        return dst
    except Exception as e:  # noqa: BLE001
        logger.warning("[人脸检测] 模型下载失败 {}：{}", filename, e)
        return None


def _resolve_face_model_path(filename: str) -> Path | None:
    """查找模型文件路径（三级回退；#361：用户缓存目录改用应用目录 models/face/）。

    1. 源码内置 / PyInstaller 内置 `app/core/models/face/`
    2. 应用目录 `get_models_dir()/face/`（用户首次下载后落地）
    3. 在线下载到应用目录（fallback）

    返回:
        Path（None = 找不到且下载失败）。
    """
    bundled = _bundled_face_models_dir()
    if bundled and (bundled / filename).exists():
        return bundled / filename
    try:
        user_dir = get_models_dir() / "face"
    except Exception:
        user_dir = None
    if user_dir:
        user_path = user_dir / filename
        if user_path.exists() and user_path.stat().st_size > 100_000:
            return user_path
        downloaded = _download_face_model(filename, user_dir)
        if downloaded:
            return downloaded
    return None


def _init_face_detector(model_name: str):
    """构造单个 MediaPipe FaceDetector（内部用）。不持锁，调用方需在 _FACE_LOCK 内。

    任务 #48（1.x）：从 0.10.x Solutions API 迁移到 1.0.1 Tasks API。
    - 不再有 model_selection 概念，改用不同模型文件（短距/全距）
    - 必须显式提供 model_asset_path，Tasks API 不会自动下载
    - 模型文件由 `_resolve_face_model_path` 三级回退获取

    任务 #128：full 距检测器用更严的 min_detection_confidence（_FACE_FULL_MIN_CONFIDENCE）
    砍远景误报；short 保持原阈值。

    参数:
        model_name: 'short'（近距离 ≤2m）或 'full'（远距离）
    返回:
        mediapipe.tasks.vision.FaceDetector 实例；依赖缺失/模型缺失/初始化失败返回 None。
    """
    try:
        import mediapipe as mp
    except ImportError:
        logger.warning("[人脸检测] mediapipe 未安装，跳过人脸检测（pip install mediapipe==1.0.1 可启用）")
        return None
    try:
        filename = _FACE_MODEL_SHORT if model_name == "short" else _FACE_MODEL_FULL
        model_path = _resolve_face_model_path(filename)
        if model_path is None:
            logger.warning("[人脸检测] {} 模型文件未找到且下载失败，跳过该检测器", filename)
            return None

        BaseOptions = mp.tasks.BaseOptions
        FaceDetector = mp.tasks.vision.FaceDetector
        FaceDetectorOptions = mp.tasks.vision.FaceDetectorOptions
        VisionRunningMode = mp.tasks.vision.RunningMode

        # 任务 #128：full 距用更严的置信度阈值
        conf = _FACE_MIN_CONFIDENCE if model_name == "short" else _FACE_FULL_MIN_CONFIDENCE
        options = FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=VisionRunningMode.IMAGE,
            min_detection_confidence=conf,
        )
        return FaceDetector.create_from_options(options)
    except Exception as e:  # noqa: BLE001
        logger.warning("[人脸检测] MediaPipe Tasks API 初始化失败（{}）：{}", model_name, e)
        return None


def _get_face_detectors() -> tuple:
    """懒加载 MediaPipe 双距检测器：短距 + 全距（审查修复 #5/#8）。

    整体流程在 _FACE_LOCK 内一次性完成（import mediapipe + 模型路径解析 +
    构造 detector），避免多线程首次并发触发重复初始化或重复下载模型。

    返回:
        (short_detector, full_detector) 元组；任一缺失对应位为 None。
    """
    global _FACE_DETECTOR_SHORT, _FACE_DETECTOR_FULL
    with _FACE_LOCK:
        if _FACE_DETECTOR_SHORT is None:
            _FACE_DETECTOR_SHORT = _init_face_detector("short")
        if _FACE_DETECTOR_FULL is None:
            _FACE_DETECTOR_FULL = _init_face_detector("full")
        return _FACE_DETECTOR_SHORT, _FACE_DETECTOR_FULL


def _skin_pixel_ratio(img) -> float:
    """计算单帧肤色像素占比（HSV 阈值），用于过滤非人脸场景（烤肉/风景/字幕）。

    返回:
        肤色像素占总像素的比例（0-1）；cv2 不可用返回 0。
    """
    try:
        import cv2
        import numpy as np
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv,
                           np.array(_SKIN_HSV_LOWER, dtype=np.uint8),
                           np.array(_SKIN_HSV_UPPER, dtype=np.uint8))
        total = mask.size
        if total == 0:
            return 0.0
        return float((mask > 0).sum()) / float(total)
    except Exception:  # noqa: BLE001
        return 0.0


def _detect_face_mediapipe(img, detector, min_size: int = _FACE_MIN_SIZE) -> list:
    """MediaPipe Tasks API 单帧人脸检测：返回 [{x, y, w, h, score, kps}, ...]。

    任务 #48（1.x）：从 Solutions API 迁移到 Tasks API 的关键差异：
    - bounding_box 字段为绝对像素坐标（origin_x/origin_y/width/height），
      不再是 0.10.x 的相对归一化坐标，无需再乘图像宽高
    - 调用方式：detector.detect(mp.Image(SRGB, data=rgb))，
      不再是 detector.process(rgb)

    任务 #128：min_size 参数化，full 距检测器传入更大阈值（_FACE_FULL_MIN_SIZE）
    砍远景小误报。内部已 min_detection_confidence 过滤。
    任务 #129 复盘修复（7685 食物误识别）：返回带 score 的字典，调用方按 score
    二次过滤。MediaPipe 退化 bbox 的 score 通常 ≤ 0.7（7685 食物 score=0.6521），
    真脸 score 通常 ≥ 0.7（000519 真脸 score 0.618-0.770，但 000519 是远景小框，
    不走强命中阈值；12b23 短距命中 score 通常 ≥ 0.8）。

    任务 #129 复盘修复（7a9fcd0a 烤串 / d447e9c9 五花肉）：返回 keypoints
    列表 [(x_norm, y_norm), ...]（归一化到 [0,1] 图像坐标），调用方按 keypoint
    左右分布过滤强命中（食物误报的关键点全挤在 bbox 一侧）。

    返回空列表 = 无脸或检测器不可用。
    """
    if detector is None:
        return []
    try:
        import cv2
        import mediapipe as mp
        # MediaPipe 接受 RGB，BGR→RGB
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = detector.detect(mp_image)
        if not result or not result.detections:
            return []
        img_h, img_w = img.shape[:2]
        boxes = []
        for det in result.detections:
            # Tasks API：bounding_box 已是绝对像素坐标
            bbox = det.bounding_box
            x1 = int(bbox.origin_x)
            y1 = int(bbox.origin_y)
            bw = int(bbox.width)
            bh = int(bbox.height)
            score = float(det.categories[0].score)
            if bw < min_size or bh < min_size:
                continue
            # bbox 占比上限（任务 #129 复盘修复 7671 误识别）：
            # MediaPipe Tasks API 在不确定时会给低分 bbox + 全 0 关键点退化输出，
            # 单纯靠 conf+min_size 拦不住。增加「bbox 占画面宽/高比例」上限：
            # 真实抖音视频人脸宽度通常 ≤ 33% 画面宽（人不会占满整屏）；
            # 超过说明 detector 把整块图案（食物/海报/手）当成脸。
            max_w = int(img_w * _FACE_BBOX_MAX_RATIO_W)
            max_h = int(img_h * _FACE_BBOX_MAX_RATIO_H)
            if bw > max_w or bh > max_h:
                logger.debug("[人脸检测] bbox({},{},{},{}) 超过占比上限 {}x{}，丢弃",
                             x1, y1, bw, bh, max_w, max_h)
                continue
            x1 = max(0, x1); y1 = max(0, y1)
            x2 = min(img_w, x1 + bw); y2 = min(img_h, y1 + bh)
            bw, bh = x2 - x1, y2 - y1
            if bw > 0 and bh > 0:
                # 关键点（图像归一化坐标 [0,1]）
                kps = [(float(kp.x), float(kp.y)) for kp in det.keypoints] if det.keypoints else []
                boxes.append({
                    "x": x1, "y": y1, "w": bw, "h": bh,
                    "score": score, "kps": kps,
                })
        return boxes
    except Exception as e:  # noqa: BLE001
        logger.debug("[人脸检测] MediaPipe Tasks 单帧异常：{}", e)
        return []


def _save_face_evidence(frame_path: Path, bbox: Tuple[int, int, int, int],
                         detector_tag: str, video_id: str = "") -> None:
    """人脸命中时把可疑帧复制 + 画红框存到 cache/face_evidence 目录（任务 #129 复盘修复）。

    误判/漏判排查时直接看图：data/cache/face_evidence/{video_id}/frame_X_bbox.jpg。
    路径位于 DATA_TEMP_SUBDIRS（cache）下，由 cleanup_startup_temp 按
    temp_retention_days 自动清理；用户也可通过"清理缓存"按钮手动删除。
    video_id 为空或 frame 不存在时静默跳过（避免污染无主目录）。

    参数:
        frame_path: 原始抽帧文件路径（inspect 缓存目录下的 jpg）
        bbox: (x, y, w, h) MediaPipe 检测框（绝对像素）
        detector_tag: 哪个检测器命中 "short" / "full"
        video_id: 视频/素材 ID，用于子目录名
    """
    if not video_id:
        return
    if not frame_path or not frame_path.exists():
        return
    try:
        from PIL import Image, ImageDraw
        from app.services.setting_service import get_data_dir
        qdir = get_data_dir() / "cache" / _FACE_EVIDENCE_DIR / video_id
        qdir.mkdir(parents=True, exist_ok=True)
        # 复制 + 画红框（不引用 inspect 缓存，inspect 缓存可能被后续清理）
        img = Image.open(frame_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        x, y, w, h = bbox
        for off in range(3):
            draw.rectangle(
                [x - off, y - off, x + w + off, y + h + off],
                outline=(255, 0, 0),
            )
        # 标注：左上角贴检测器名 + bbox 尺寸
        draw.text(
            (max(0, x), max(0, y - 14)),
            f"{detector_tag} {w}x{h}",
            fill=(255, 0, 0),
        )
        out_path = qdir / frame_path.name
        img.save(out_path, "JPEG", quality=85)
        logger.info("[人脸证据] 命中截图已保存 {}（{} {}x{}）",
                    out_path, detector_tag, w, h)
    except Exception as e:  # noqa: BLE001
        logger.debug("[人脸证据] 保存截图异常 {}：{}", frame_path, e)


def _keypoints_have_left_right(kps_in_bbox: list,
                                left_max: float = _FACE_KP_LEFT_MAX,
                                right_min: float = _FACE_KP_RIGHT_MIN) -> bool:
    """检测 MediaPipe 关键点在 bbox 内是否呈左右对称分布。

    任务 #129 复盘修复 7a9fcd0a（烤串）/d447e9c9（五花肉）：食物误识别时关键点
    全挤在 bbox 一侧（无面部几何结构）。真脸的 6 个关键点（两眼+鼻+嘴+两耳）
    在 bbox 左半（rel_x<=left_max）和右半（rel_x>=right_min）各至少有 1 个。
    输入空 kp 视为不可验证，返回 False（保守走累计分支而非强命中）。

    参数:
        kps_in_bbox: 已投影到 bbox 相对坐标 [(rel_x, rel_y), ...]，
                     0.0=bbox 左/上边，1.0=bbox 右/下边（可越界，耳会到外）。
        left_max / right_min: 归一化阈值
    返回:
        True=左右分布合理（看起来像脸），False=单侧聚集或无 kp。
    """
    if not kps_in_bbox:
        return False
    has_left = any(rx <= left_max for (rx, _ry) in kps_in_bbox)
    has_right = any(rx >= right_min for (rx, _ry) in kps_in_bbox)
    return has_left and has_right


def _bbox_texture_is_face(img, x: int, y: int, w: int, h: int) -> bool:
    """检测 bbox 区域纹理是否符合人脸特征（任务 #129 复盘 7a9fcd0a/d447e9c9 修复）。

    真脸：眼/嘴/皮肤对比 → 灰度 std 较高（≥30），Canny 边缘密度 ≥0.05，
         HSV 饱和度均值较低（人脸肤色不饱和 ≤150）。
    食物误判：
      - 烤焦肉（7a9 烤串）：颜色均匀 → std<30 且 edge<0.05
      - 红肉（d447 五花肉）：高饱和 → sat_mean>150

    规则（三选一 reject）：
      - sat_mean > SAT_MAX  -> 食物
      - std < STD_MIN AND edge_density < EDGE_MIN  -> 均匀无纹理食物
    不依赖关键点（关键点会被幻觉），仅用原始纹理统计。

    参数:
        img: BGR numpy 数组（cv2.imdecode 结果）
        x, y, w, h: bbox 绝对像素坐标
    返回:
        True=像脸（保留），False=不像脸（reject）
    """
    try:
        crop = img[y:y + h, x:x + w]
        if crop is None or crop.size == 0 or crop.shape[0] < 4 or crop.shape[1] < 4:
            return True  # 太小无法判断，保守放行（走累计分支兜底）
        import cv2
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        gray_std = float(gray.std())
        sat_mean = float(hsv[:, :, 1].mean())
        edges = cv2.Canny(crop, 50, 150)
        edge_density = float((edges > 0).mean())
        # 高饱和 -> 直接 reject（红肉/鲜艳食物）
        if sat_mean > _FACE_BBOX_SAT_MAX:
            return False
        # 颜色均匀且无纹理 -> reject（烤焦食物/纯背景）
        if gray_std < _FACE_BBOX_GRAY_STD_MIN and edge_density < _FACE_BBOX_EDGE_DENSITY_MIN:
            return False
        return True
    except Exception:  # noqa: BLE001
        return True  # 异常保守放行（不阻断主流程）


def detect_face(frames: List[Path], video_id: str = "") -> bool:
    """主播人脸检测：MediaPipe 主检测器（任务 #48 -> 1.x Tasks API）。

    策略（任务 #128 优化5 + 任务 #129 复盘修复）：
    1. HSV 肤色预筛：肤色像素占比 < 0.5% 直接判 False（砍烤肉/风景/字幕）
    2. MediaPipe FaceDetector 短距（blaze_face_short_range，2m 内准）
       - bbox 占比 > 33% 画面视为退化丢弃（任务 #129 修复 7671）
       - score >= 0.7 强命中直接确认；score < 0.7 跨帧累计 >= 3 帧确认
         （任务 #129 复盘 48849a/69c29d 等 5 个食物误识别）
    3. MediaPipe FaceDetector 全距（blaze_face_full_range，远景）
       - 仅在短距 0 命中时启用
       - 置信度阈值 0.6 + 最小尺寸 60px
       - bbox >= 100px + score >= 0.7 强命中额外两重验证：
         (a) 关键点左右分布（防单侧聚集退化）
         (b) bbox 纹理（灰度 std/HSV sat/Canny 边缘密度）像脸
         （任务 #129 复盘 7a9fcd0a 烤串 std=20 / d447e9c9 五花 sat=185
          全距食物幻觉人脸）
       - 60-99px 弱命中跨帧累计 >= 3 帧，确认前对累计列表中 score 最高的帧
         做同样的纹理检查（避免 7a9 烤串累计 4 帧弱命中绕过强命中过滤）

    命中证据（任务 #129 复盘修复）：命中时把可疑帧复制 + 画红框存到
    data/cache/face_evidence/{video_id}/frame_X_bbox.jpg（DATA_TEMP_SUBDIRS
    cache 子目录，由 cleanup_startup_temp 按 temp_retention_days 自动清理）。
    video_id 为空不存。

    判定：任一检测器任一帧命中即返回 True。
    依赖缺失或全无可用检测器 -> False（默认放行，避免阻断主流程）。

    参数:
        frames: 抽帧结果路径列表
        video_id: 视频/素材 ID；非空时命中帧会存到 cache/face_evidence 目录
    返回:
        是否检测到人脸。
    """
    short_det, full_det = _get_face_detectors()
    if short_det is None and full_det is None:
        return False
    if not frames:
        return False
    pending_hits = []     # Full Range 弱命中累计
    short_pending = []    # Short Range 弱命中累计
    try:
        import cv2
        import numpy as np
        for fp_inner in frames:
            try:
                buf = np.fromfile(str(fp_inner), dtype=np.uint8)
                img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
                if img is None:
                    continue

                # 策略1：肤色预筛
                skin_ratio = _skin_pixel_ratio(img)
                if skin_ratio < _SKIN_MIN_RATIO:
                    logger.debug("[人脸检测] 帧 {} 肤色占比 {:.3%} < 阈值 {:.3%}，跳过",
                                 fp_inner.name, skin_ratio, _SKIN_MIN_RATIO)
                    continue

                # 策略2：短距检测
                # 任务 #129 复盘修复 48849a/69c29d/95a5f5/d5c7be/5765ab 5 个食物误识别：
                # Short Range score >= 0.7 强信号直接确认；< 0.7 跨帧累计 >= 3 帧确认。
                short_boxes = _detect_face_mediapipe(img, short_det, min_size=_FACE_MIN_SIZE)
                for box in short_boxes:
                    x, y, w, h, sc = box["x"], box["y"], box["w"], box["h"], box["score"]
                    if sc >= _FACE_SHORT_CONFIRM_SCORE:
                        logger.debug("[人脸检测] Short 强命中 bbox({},{},{},{}) score {:.3f} >= 0.7，确认",
                                     x, y, w, h, sc)
                        _save_face_evidence(fp_inner, (x, y, w, h), "short", video_id)
                        return True
                    short_pending.append((fp_inner, x, y, w, h, sc))
                    if len(short_pending) >= _FACE_SHORT_PENDING_FRAMES:
                        logger.debug("[人脸检测] Short 累计 {} 帧弱命中，确认人脸",
                                     len(short_pending))
                        last_fp, lx, ly, lw, lh, _lsc = short_pending[-1]
                        _save_face_evidence(last_fp, (lx, ly, lw, lh), "short_vote", video_id)
                        return True

                # 策略3：全距检测（仅 short 无命中时启用）
                # 任务 #129 复盘修复 7a9fcd0a（烤串）/d447e9c9（五花肉）：
                # Full Range 强命中（bbox>=100 + score>=0.7）在食物上偶发；分析关键点
                # 发现误报 kp 全挤 bbox 一侧，加左右分布验证。
                if full_det is not None:
                    full_boxes = _detect_face_mediapipe(img, full_det, min_size=_FACE_FULL_MIN_SIZE)
                    img_h, img_w = img.shape[:2]
                    for box in full_boxes:
                        x, y, w, h, sc = box["x"], box["y"], box["w"], box["h"], box["score"]
                        is_big = (w >= _FACE_FULL_CONFIRM_SIZE and h >= _FACE_FULL_CONFIRM_SIZE)
                        is_big_strong = is_big and sc >= _FACE_FULL_CONFIRM_SCORE
                        if is_big_strong:
                            # 强命中额外做两重验证：
                            # 1) 关键点左右分布（防单侧聚集退化）
                            # 2) bbox 纹理特征（防 7a9 烤串 std=20 / d447 五花 sat=185
                            #    这类 MediaPipe 在食物上「幻觉人脸」的场景）
                            kps_rel = [
                                ((kp[0] * img_w - x) / max(w, 1),
                                 (kp[1] * img_h - y) / max(h, 1))
                                for kp in box["kps"]
                            ]
                            if not _keypoints_have_left_right(kps_rel):
                                logger.debug(
                                    "[人脸检测] Full 强命中但关键点单侧聚集 bbox({},{},{},{}) "
                                    "score {:.3f} -> 视为退化跳过",
                                    x, y, w, h, sc,
                                )
                                continue
                            if not _bbox_texture_is_face(img, x, y, w, h):
                                logger.debug(
                                    "[人脸检测] Full 强命中但 bbox 纹理不像脸 "
                                    "bbox({},{},{},{}) score {:.3f} -> 视为食物跳过",
                                    x, y, w, h, sc,
                                )
                                continue
                            logger.debug(
                                "[人脸检测] Full 强命中 bbox({},{},{},{}) >= 100px + score {:.3f} >= 0.7，"
                                "关键点分布合理 + 纹理像脸，确认",
                                x, y, w, h, sc,
                            )
                            _save_face_evidence(fp_inner, (x, y, w, h), "full", video_id)
                            return True
                        if is_big:
                            # bbox 大但 score 低 -> 弱命中累计（典型退化）
                            logger.debug(
                                "[人脸检测] Full bbox 大但 score 低 bbox({},{},{},{}) score={:.3f} "
                                "-> 弱命中累计",
                                x, y, w, h, sc,
                            )
                        pending_hits.append((fp_inner, x, y, w, h, sc))
                        if len(pending_hits) >= _FACE_FULL_PENDING_FRAMES:
                            # 累计达阈值，确认前取累计列表中 score 最高的帧做纹理检查
                            # ——避免 7a9 烤串累计 4 帧（每帧都过线）误判食物。
                            best = max(pending_hits, key=lambda h: h[5])
                            best_fp, bx, by, bw, bh, bsc = best
                            try:
                                import cv2
                                import numpy as np
                                best_buf = np.fromfile(str(best_fp), dtype=np.uint8)
                                best_img = cv2.imdecode(best_buf, cv2.IMREAD_COLOR)
                                texture_ok = _bbox_texture_is_face(
                                    best_img, bx, by, bw, bh,
                                ) if best_img is not None else False
                            except Exception:  # noqa: BLE001
                                texture_ok = True  # 异常保守放行
                            if not texture_ok:
                                logger.debug(
                                    "[人脸检测] Full 累计 {} 帧但最高分帧 score={:.3f} "
                                    "bbox({},{},{},{}) 纹理不像脸 -> 拒绝",
                                    len(pending_hits), bsc, bx, by, bw, bh,
                                )
                                pending_hits.clear()
                                continue
                            logger.debug(
                                "[人脸检测] Full 累计 {} 帧弱命中（最高分 {:.3f} 纹理通过），确认人脸",
                                len(pending_hits), bsc,
                            )
                            last_fp, lx, ly, lw, lh, _lsc = pending_hits[-1]
                            _save_face_evidence(last_fp, (lx, ly, lw, lh), "full_vote", video_id)
                            return True
            except Exception as e:  # noqa: BLE001
                logger.debug("[人脸检测] 单帧检测异常：{}", e)
                continue
    except Exception as e:  # noqa: BLE001
        logger.warning("[人脸检测] 异常，默认放行：{}", e)
    return False


def inspect_video(video_path: str, material_id: str,
                  need_subtitle: bool, need_face: bool
                  ) -> Tuple[Optional[bool], Optional[bool], List[str], str]:
    """视频内容检测高级封装（任务 #128 优化6：流式分批 + 命中早停）。

    流程：每 `_STREAM_BATCH_S` 秒一个批次，依次抽帧 → pHash 合并 → OCR/人脸。
    任一项命中即早停：另一项标记 miss（不再跑），避免上游把「未跑」当成「抽帧失败」返 None。
    全部批次跑完仍未命中 → 返回 (False, False)。
    所有批次均未产出帧（抽帧完全失败）→ 返回 (None, None)（v126 语义）。

    参数:
        video_path: 视频文件绝对路径
        material_id: 入库后素材 ID（用于缓存抽帧）
        need_subtitle: 是否需要字幕检测
        need_face: 是否需要人脸检测
    返回:
        (has_subtitle, has_face, sub_texts, inspect_reason) 四元组：
        - has_subtitle/has_face 三态：
          - True  = 命中
          - False = 未命中（流式跑完所有批次都未命中，或早停时另一项的状态）
          - None  = 抽帧失败无法判定（v126 语义）
        - sub_texts: 字幕命中时识别到的文本（向上传日志使用）
        - inspect_reason: 抽帧失败的具体子原因字符串，便于上游精准日志：
          - ""                  = 正常（命中 / 未命中 / 不需要检测）
          - "no_video_stream"   = ffprobe 无 video 流（纯音频伪装 mp4 / 图声视频）
          - "file_corrupted"    = ffprobe 失败 / metadata 不完整
          - "probe_error"       = ffprobe 探测未知异常
          - "no_frames"         = 所有批次均未产出帧（ffmpeg 抽帧全失败）
    """
    if not need_subtitle and not need_face:
        return False, False, [], ""
    try:
        duration_s, _has_video = _probe_video_metadata(video_path)
    except VideoNoStreamError as e:
        # 抽帧前完整性探测：纯音频伪装 mp4 / m4a 误标 .mp4。
        # 抽帧必然全部 -22 失败（image2 muxer 无 stream 可写），
        # 不进入抽帧循环，直接返回 (None, None) 让上游严格拒绝入库。
        logger.warning(
            "[视频检测] 无视频流跳过抽帧 {}：{}", video_path, e,
        )
        return None, None, [], "no_video_stream"
    except VideoCorruptedError as e:
        # 抽帧前完整性探测：文件损坏 / moov atom not found / Invalid data 等。
        # 不进入抽帧循环，直接返回 (None, None) 让上游拒绝入库，
        # 节省 100+ 次 ffmpeg 调用（避免无效抽帧跑完所有批次才发现）。
        logger.warning(
            "[视频检测] 文件受损跳过抽帧 {}：{}", video_path, e,
        )
        return None, None, [], "file_corrupted"
    except Exception as e:  # noqa: BLE001
        logger.debug("[视频检测] 探测时长失败 {}：{}", video_path, e)
        duration_s = 0.0

    seek_points = _frame_seek_points(duration_s)
    batches = _frame_batches(seek_points, batch_s=_STREAM_BATCH_S)
    if not batches:
        logger.warning("[视频检测] 未产出任何抽帧点 {}：视为无法判定", video_path)
        return None, None, [], "probe_error"

    out_dir = _inspect_dir(material_id)
    # 三态独立追踪：'pending' 未跑完 / 'hit' 命中 / 'miss' 跑完未命中
    sub_state = "pending" if need_subtitle else "miss"
    face_state = "pending" if need_face else "miss"
    total_frames_produced = 0  # 审查修复 #3：区分「从未抽到帧」与「抽到帧但未命中」
    sub_texts: List[str] = []  # 字幕命中时识别到的文本,向上传给调用方写日志

    t_start = time.time()
    for batch_idx, batch_seeks in batches:
        # 1) 抽这批帧（带缓存复用）
        try:
            batch_frames = _extract_batch_frames(video_path, out_dir, batch_idx, batch_seeks)
        except Exception as e:  # noqa: BLE001
            logger.warning("[视频检测] 批次 {} 抽帧异常：{}", batch_idx, e)
            batch_frames = []

        if not batch_frames:
            # 该批次无帧（ffmpeg 失败等），继续下一批；最终若全部空批 → None
            continue

        total_frames_produced += len(batch_frames)

        # 2) pHash 合并相似帧
        try:
            merged = _merge_similar_frames(batch_frames, material_id=material_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[视频检测] pHash 合并异常，沿用原始帧列表：{}", e)
            merged = batch_frames

        # 3) 字幕检测（命中即早停；审查修复 #2：另一项显式置 miss 避免 None 歧义）
        if sub_state == "pending":
            hit, texts = detect_subtitle(merged)
            if hit:
                sub_state = "hit"
                sub_texts = texts  # 缓存命中帧文本,函数末尾返回
                if face_state == "pending":
                    face_state = "miss"  # 早停，人脸不再验
                logger.info(
                    "[视频检测] 字幕命中 @ {}-{}s（已用 {:.1f}s，早停）",
                    batch_idx * _STREAM_BATCH_S,
                    (batch_idx + 1) * _STREAM_BATCH_S,
                    time.time() - t_start,
                )
                return True, False, sub_texts, ""  # 字幕文本向上传,排障日志输出

        # 4) 人脸检测（命中即早停；任务 #129 复盘修复：传 video_id 让命中帧存到 cache/face_evidence）
        if face_state == "pending":
            if detect_face(merged, video_id=material_id):
                face_state = "hit"
                if sub_state == "pending":
                    sub_state = "miss"  # 早停，字幕不再验
                logger.info(
                    "[视频检测] 人脸命中 @ {}-{}s（已用 {:.1f}s，早停）",
                    batch_idx * _STREAM_BATCH_S,
                    (batch_idx + 1) * _STREAM_BATCH_S,
                    time.time() - t_start,
                )
                return False, True, sub_texts, ""  # 人脸命中时字幕未跑,sub_texts 为空

        # 5) 本批跑过且未命中 → 标记 miss
        if sub_state == "pending":
            sub_state = "miss"
        if face_state == "pending":
            face_state = "miss"

        # 两项都已确定 miss，提前退出
        if sub_state == "miss" and face_state == "miss":
            break

    # 审查修复 #3：所有批次都没产出帧 → 抽帧失败，返 None（v126 语义）
    if total_frames_produced == 0:
        logger.warning("[视频检测] 所有批次均未产出帧 {}：视为无法判定", video_path)
        return None, None, [], "no_frames"

    # 流式跑完未命中
    logger.info(
        "[视频检测] 全部分支跑完未命中（总耗时 {:.1f}s，共 {} 批，产出 {} 帧）",
        time.time() - t_start, len(batches), total_frames_produced,
    )
    return _state_to_out(sub_state), _state_to_out(face_state), sub_texts, ""


def _state_to_out(state: str) -> Optional[bool]:
    """内部状态转三态返回值：hit=True / miss=False / pending=None（仅保留兼容）。"""
    if state == "hit":
        return True
    if state == "miss":
        return False
    return None
