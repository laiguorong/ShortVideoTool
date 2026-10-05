# -*- coding: utf-8 -*-
"""FFmpeg / FFprobe 封装（参考 VideoMatrix 同名模块模式）。

职责：
- 工具查找（打包目录 / 开发目录 / PATH）；
- 子进程执行（Popen+kill 主动回收，无 zombie；CPU 监控兜底）；
- 元数据探测 probe_media；
- h264 编码器自检（启动期一次性 5s 黑盒实测 + 运行期失败熔断）；
- M3/M4 使用的切割/拼接/去重命令构造在本模块按需追加。
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger


def _find_tool(name: str) -> str:
    """查找可执行工具（参考 ms-playwright 复制风格，ffmpeg 由 electron-builder extraResources 复制到 <install>/resources/backend/assets/ffmpeg/）：
    - 打包：_MEIPASS → exe 同目录 → exe 同目录/assets/ffmpeg（extraResources）→ PATH
    - 开发：脚本目录 → backend/assets/ffmpeg → PATH
    """
    if getattr(sys, "frozen", False):
        candidates = [
            Path(sys._MEIPASS) / name,
            Path(sys.executable).parent / name,
            # extraResources 复制目标：<install>/resources/backend/assets/ffmpeg/
            Path(sys.executable).parent / "assets" / "ffmpeg" / name,
        ]
    else:
        candidates = [
            Path(sys.argv[0]).parent / name,
            Path(__file__).resolve().parent.parent.parent / "assets" / "ffmpeg" / name,
        ]
    for c in candidates:
        if c.exists():
            return str(c)
    return name  # 退回 PATH 裸名


FFMPEG = _find_tool("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
FFPROBE = _find_tool("ffprobe.exe" if sys.platform == "win32" else "ffprobe")

# 抽帧参数常量（首帧/缩略图统一配置，避免多处不同步）
# -q:v 是 libjpeg 量化值（1=最高/最大，10+ 不可用），数值越大文件越小
THUMB_WIDTH = 240
THUMB_JPEG_Q = 5  # 平衡清晰度与体积；与 #504 480p+q3 ~30-50KB 相比，240p+q5 ~10-20KB


# ---------- 进程级 CPU 监控（任务 #42：ffmpeg 死循环兜底）----------

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:  # noqa: BLE001 psutil 缺失时降级为不监控
    _HAS_PSUTIL = False


def _monitor_ffmpeg_proc(proc: subprocess.Popen, deadline_s: float = 30):
    """后台线程：监控 ffmpeg 子进程 CPU 占用，持续>90% 超过 deadline 自动 kill。

    解决 #369 衍生问题：ffmpeg 二进制与配置不一致时不会立即失败，但可能空转
    （如 NVENC 驱动不兼容时 ffmpeg 反复 init encoder 100% CPU）。
    仅对重型操作启用（timeout>=300），避免误杀短时高 CPU 的常规编码。
    """
    if not _HAS_PSUTIL:
        return
    try:
        p = psutil.Process(proc.pid)
        high_since: Optional[float] = None
        while proc.poll() is None:
            try:
                cpu = p.cpu_percent(interval=2)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            if cpu > 90:
                if high_since is None:
                    high_since = time.time()
                elif time.time() - high_since > deadline_s:
                    logger.warning(
                        "[ffmpeg] PID {} CPU>90% 持续>{}s,自动 kill",
                        proc.pid, deadline_s,
                    )
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
            else:
                high_since = None
    except Exception:  # noqa: BLE001 监控异常不影响主流程
        pass


def run_cmd(cmd: list[str], capture_output: bool = True, cwd: str | None = None,
            timeout: int | None = None, monitor: bool = False) -> Optional[subprocess.CompletedProcess]:
    """执行外部命令，超时主动 kill 子进程（避免 zombie，#369/#42 兜底）。

    参数:
        cmd: 完整命令参数列表
        capture_output: 是否捕获输出
        cwd: 工作目录
        timeout: 超时秒数
        monitor: 是否启用 CPU 监控（重型操作 timeout>=300 时建议 True）
    返回:
        CompletedProcess-like（.returncode/.stdout/.stderr），失败/超时返回 None

    Windows 文件锁根治（#380）：
        - communicate() 后立即 close stdio 句柄（Python GC 不会立即触发）
        - 配合 caller 端创建父目录，避免后续 os.replace 撞 WinError 3
    """
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    else:
        creationflags = 0
    start = time.perf_counter()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            stdin=subprocess.DEVNULL,  # 防止 ffmpeg 等待 stdin 输入卡住
            cwd=cwd, creationflags=creationflags,
            text=True, encoding="utf-8", errors="ignore",
        )
    except Exception as e:
        logger.warning("[命令] 启动异常 {cmd0}：{e}", cmd0=cmd[0] if cmd else "?", e=e)
        return None

    # 重型操作开启 CPU 监控（#42 兜底）
    if monitor and timeout and timeout >= 300:
        threading.Thread(
            target=_monitor_ffmpeg_proc, args=(proc, 30),
            daemon=True, name=f"ffmpeg-monitor-{proc.pid}",
        ).start()

    stdout_data: str | None = None
    stderr_data: str | None = None
    try:
        stdout_data, stderr_data = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # 超时：先 kill，关闭句柄，回收进程
        try:
            proc.kill()
        except Exception:
            pass
        _force_close_proc_io(proc)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        cost = (time.perf_counter() - start) * 1000
        tool = Path(cmd[0]).name if cmd else "?"
        logger.warning("[{tool}] 超时（>{timeout}s）已 kill 耗时{cost:.0f}ms",
                       tool=tool, timeout=timeout, cost=cost)
        return None
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        _force_close_proc_io(proc)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        logger.warning("[命令] 执行异常 {cmd0}：{e}", cmd0=cmd[0] if cmd else "?", e=e)
        return None

    # #380：communicate 之后立即关闭 stdio 句柄，避免 Python 侧持有导致 OS 延迟释放
    _force_close_proc_io(proc)

    cp = subprocess.CompletedProcess(cmd, proc.returncode, stdout_data, stderr_data)
    cost = (time.perf_counter() - start) * 1000
    tool = Path(cmd[0]).name if cmd else "?"
    if cp.returncode != 0:
        # 任务 #64/#71：ffmpeg stderr 第一行通常是关键错误（"Stream specifier..."、
        # "Invalid argument" 等），后面是 filtergraph 描述（巨长但信息密度低）。
        # 任务 #71：单纯截首行[:200] 会切掉末尾"matches no streams"等关键短语。
        # 改为保留首 150 + 尾 80，确保关键错误关键词完整。
        raw_err = (cp.stderr or "").strip()
        first_line = raw_err.split("\n", 1)[0] if raw_err else ""
        if len(first_line) > 240:
            err_brief = f"{first_line[:150]}…{first_line[-80:]}（stderr 共 {len(raw_err)} 字符）"
        elif raw_err:
            err_brief = first_line
        else:
            err_brief = "(无 stderr)"
        logger.warning("[{tool}] 失败 rc={rc} 耗时{cost:.0f}ms：{err}",
                       tool=tool, rc=cp.returncode, cost=cost, err=err_brief)
    # 成功分支不再输出 debug：ffmpeg/ffprobe 高频调用（每次 cut/probe 都跑），
    # "完成 耗时 NNNms" 控制台噪音大且无定位价值；失败 WARNING 已足够排查。
    return cp


def _force_close_proc_io(proc: subprocess.Popen) -> None:
    """强制关闭子进程 stdio 句柄（#380 根治 Windows 文件锁）。

    communicate() 返回后 Python 仍持有子进程的 stdout/stderr pipe，
    OS 视为主进程在用子进程资源 → 文件句柄延迟释放。
    显式 close 触发 OS 立即回收。
    """
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def ffmpeg_available() -> bool:
    """FFmpeg 是否可用。"""
    result = run_cmd([FFMPEG, "-version"])
    return result is not None and result.returncode == 0


def probe_media(file_path: str) -> Optional[dict]:
    """探测媒体元数据（ffprobe -print_format json）。

    参数:
        file_path: 媒体文件路径
    返回:
        ffprobe 输出的 dict（format/streams），失败返回 None
    """
    cmd = [
        FFPROBE, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", file_path,
    ]
    result = run_cmd(cmd)
    if result is None or result.returncode != 0 or not result.stdout:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def extract_media_info(info: dict) -> dict:
    """从 probe_media 结果提取常用字段。

    iPhone MOV / Android 部分视频会把 tkhd Display Matrix 旋转元数据写在
    stream.side_data_list（side_data_type="Display Matrix", rotation=±90/±270）。
    此时 stream.width/height 是 sensor 原始维度（横向录制），需按 rotation 对调
    后才是用户视觉方向——否则 upload 竖屏视频被误判为横屏，封面 object-contain
    黑边，分类按 orientation 过滤时被排掉（fix #590/#591）。

    参数:
        info: probe_media 返回的 dict
    返回:
        {"duration_ms": int, "width": int|None, "height": int|None,
         "orientation": "vertical|horizontal|None", "bit_rate": str|None}
    """
    out = {"duration_ms": 0, "width": None, "height": None, "orientation": None, "bit_rate": None}
    if not info:
        return out
    fmt = info.get("format", {})
    try:
        out["duration_ms"] = int(float(fmt.get("duration", 0)) * 1000)
    except (TypeError, ValueError):
        pass
    out["bit_rate"] = fmt.get("bit_rate")
    # 取第一个视频流
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            try:
                out["width"] = int(stream.get("width", 0)) or None
                out["height"] = int(stream.get("height", 0)) or None
            except (TypeError, ValueError):
                pass
            # 读 side_data_list 中的 Display Matrix rotation（iPhone MOV / 部分安卓）
            rotation = 0
            for sd in stream.get("side_data_list", []) or []:
                if sd.get("side_data_type") == "Display Matrix":
                    try:
                        rotation = int(sd.get("rotation", 0))
                    except (TypeError, ValueError):
                        rotation = 0
                    break
            # ±90 / ±270 时 sensor w/h 与视觉 w/h 对调
            if abs(rotation) in (90, 270) and out["width"] and out["height"]:
                out["width"], out["height"] = out["height"], out["width"]
            if out["width"] and out["height"]:
                # 宽高比 > 1 为横屏（需求文档 F-03-R2）
                out["orientation"] = "horizontal" if out["width"] > out["height"] else "vertical"
            break
    return out


# ---------- h264 编码器检查（任务 #381：固定 libx264，启动期检查）----------

# 工具依赖 H.264 编码。默认使用 libx264，移除运行期降级逻辑。
# 启动期通过 check_ffmpeg_env() 实测 libx264 可用，不可用直接抛 RuntimeError 中断应用。
H264_ENCODER = "libx264"


def check_ffmpeg_env() -> None:
    """启动期检查：ffmpeg 可执行 + libx264 编码器可用（#381）。

    不可用直接抛 RuntimeError，中断 FastAPI 启动。
    调用方应在应用 on_startup 事件中调用，确保环境问题在用户操作前暴露。
    """
    import shutil
    # 1. ffmpeg 可执行文件存在
    if not shutil.which(FFMPEG) and not Path(FFMPEG).exists():
        raise RuntimeError(
            f"[启动检查] 未找到 ffmpeg 可执行文件：{FFMPEG}\n"
            "请确认 backend/assets/ffmpeg/ffmpeg.exe 存在，或将 ffmpeg 加入 PATH。"
        )

    # 2. ffmpeg 能跑起来
    ver = run_cmd([FFMPEG, "-hide_banner", "-version"], timeout=10)
    if ver is None or ver.returncode != 0:
        raise RuntimeError(
            f"[启动检查] ffmpeg 无法执行（rc={ver.returncode if ver else 'None'}）\n"
            "请检查 ffmpeg 二进制是否损坏或被杀毒软件拦截。"
        )

    # 3. libx264 编码器实测：5s 黑盒编一帧，rc=0 才算通过
    test_cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.04",
        "-c:v", "libx264", "-frames:v", "1", "-f", "null", "-",
    ]
    test = run_cmd(test_cmd, timeout=10)
    if test is None or test.returncode != 0:
        raise RuntimeError(
            "[启动检查] ffmpeg 不支持 libx264 编码器\n"
            "本工具依赖 H.264 编码，请重新安装带 libx264 的 ffmpeg。"
        )

    logger.info("[启动检查] ffmpeg + libx264 实测通过")


def get_h264_encoder() -> str:
    """返回固定 H.264 编码器名称（#381：移除降级，统一 libx264）。"""
    return H264_ENCODER


def extract_frame(video_path: str, out_path: str, seek_seconds: float = 1.0) -> bool:
    """从视频抽取一帧保存为图片（缩放到指定宽 + 指定 JPEG 质量，减小文件体积）。

    沿革：
    - 原生 + q:v 2 → 200-500KB
    - #504 480p + q:v 3 → 30-50KB
    - 当前 240p + q:v 5 → ~10-20KB（适合缩略图/列表预览，前端 CSS object-fit 适配）

    参数:
        video_path: 视频文件路径
        out_path: 输出图片路径（格式随扩展名，如 .jpg）
        seek_seconds: 定位时间点（秒），须小于视频时长
    返回:
        是否成功生成文件
    """
    cmd = [
        FFMPEG, "-y",
        "-ss", f"{max(0.0, seek_seconds):.2f}",
        "-i", video_path,
        "-frames:v", "1",
        "-vf", f"scale={THUMB_WIDTH}:-2",
        "-q:v", str(THUMB_JPEG_Q),
        out_path,
    ]
    result = run_cmd(cmd)
    if result is not None and result.returncode == 0 and Path(out_path).exists():
        return True
    # 失败：把 ffmpeg stderr 末尾摘要暴露到日志（之前完全吞，生产看不见原因）
    if result is not None and result.stderr:
        tail_lines = [ln for ln in result.stderr.strip().splitlines() if ln.strip()][-6:]
        logger.warning(
            "[抽帧] 失败 seek={seek}s video={video} → ffmpeg 输出末 6 行:\n{tail}",
            seek=seek_seconds, video=Path(video_path).name, tail="\n".join(tail_lines),
        )
    else:
        logger.warning(
            "[抽帧] 失败 seek={seek}s video={video} → 无 ffmpeg 输出（result={}）",
            seek=seek_seconds, video=Path(video_path).name,
            result="None" if result is None else f"rc={result.returncode}",
        )
    return False