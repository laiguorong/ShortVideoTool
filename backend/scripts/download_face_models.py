# -*- coding: utf-8 -*-
"""下载 MediaPipe BlazeFace 模型到 app/core/models/face/。

任务 #48（1.x）：MediaPipe Tasks API 的 FaceDetector 实际接受 .tflite 文件
（Google Storage 没有发布 .task 打包版本，.task 仅用于 Pose/LLM 等
需要元数据或多文件 bundle 的场景）。

首次执行一次性下载两个 .tflite 模型到源码目录，
后续构建/打包直接复用（PyInstaller 通过 build.spec 捆绑）。
"""
import urllib.request
from pathlib import Path

BASE = "https://storage.googleapis.com/mediapipe-models/face_detector"
FILES = {
    "blaze_face_short_range.tflite":
        f"{BASE}/blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
    "blaze_face_full_range.tflite":
        f"{BASE}/blaze_face_full_range/float16/1/blaze_face_full_range.tflite",
}

out = Path(__file__).resolve().parent.parent / "app" / "core" / "models" / "face"
out.mkdir(parents=True, exist_ok=True)
for name, url in FILES.items():
    dst = out / name
    if dst.exists() and dst.stat().st_size > 100_000:
        print(f"已存在：{dst}")
        continue
    print(f"下载 {url} → {dst}")
    urllib.request.urlretrieve(url, dst)
    assert dst.stat().st_size > 100_000, f"下载失败：{dst}"
print("完成")