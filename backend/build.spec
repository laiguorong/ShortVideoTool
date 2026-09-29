# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：后端单 exe + 捆绑 FFmpeg（assets/ffmpeg/）
# 构建：cd backend && pyinstaller build.spec（产物 dist/shortvideo-backend.exe）
# 任务 #48（1.x）：人脸检测改用 MediaPipe Tasks API，BlazeFace 模型 .tflite
# 捆绑于 app/core/models/face/（首次运行即离线可用）。

import os
from glob import glob

block_cipher = None

# FFmpeg 二进制（本地 assets/ffmpeg/，构建前需就位）
ffmpeg_dir = os.path.join(SPECPATH, 'assets', 'ffmpeg')
binaries = []
for name in ('ffmpeg.exe', 'ffprobe.exe', 'ffmpeg', 'ffprobe'):
    p = os.path.join(ffmpeg_dir, name)
    if os.path.exists(p):
        binaries.append((p, '.'))

# MediaPipe Tasks API .tflite 模型（捆绑到 sys._MEIPASS/app/core/models/face/）
face_model_dir = os.path.join(SPECPATH, 'app', 'core', 'models', 'face')
face_model_datas = []
if os.path.isdir(face_model_dir):
    face_model_datas = [
        (p, os.path.join('app', 'core', 'models', 'face'))
        for p in glob(os.path.join(face_model_dir, '*.tflite'))
    ]

a = Analysis(
    ['run_backend.py'],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=face_model_datas,
    hiddenimports=[
        # uvicorn 动态导入
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio',
        'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.auto',
        # fastapi / pydantic
        'fastapi', 'pydantic',
        # apscheduler
        'apscheduler.schedulers.background', 'apscheduler.triggers.interval', 'apscheduler.triggers.cron',
        # 应用包
        'app', 'app.api', 'app.models', 'app.services', 'app.core', 'app.core.douyin', 'app.db',
        # MediaPipe Tasks API（PyInstaller 默认不扫到 tasks.* 子模块）
        'mediapipe.tasks',
        'mediapipe.tasks.python',
        'mediapipe.tasks.python.vision',
        'mediapipe.tasks.python.core',
        'mediapipe.tasks.cc',
        'mediapipe.framework',
        'mediapipe.framework.formats',
        'mediapipe.framework.tool',
        'mediapipe.python._framework_bindings',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib'],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='shortvideo-backend',
    debug=False,
    strip=False,
    upx=False,
    console=True,  # 保留控制台便于排查
)