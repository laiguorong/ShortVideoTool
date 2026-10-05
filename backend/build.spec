# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：后端 onedir 模式（exe + _internal/），ffmpeg 不进 exe
# 构建：cd backend && pyinstaller build.spec
# 产物：dist/shortvideo-backend/shortvideo-backend.exe + _internal/
# ffmpeg/ffprobe 由 frontend electron-builder extraResources 复制到
# resources/backend/ffmpeg/（参考 ms-playwright 风格）。
# 任务 #48（1.x）：人脸检测改用 MediaPipe Tasks API，BlazeFace 模型 .tflite
# 捆绑于 app/core/models/face/（首次运行即离线可用）。

import os
from glob import glob

block_cipher = None

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
    binaries=[],
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

# 限制 locale：保留 zh_CN，剔除其他语言包（gettext .mo）
# 后端不用 gettext/i18n，全语种 .mo 是死体积；保留 zh_CN 防止 `_locale` 内置 C 扩展依赖链路出问题
def _keep_zh_locale_only(toc):
    """剔除非 zh_CN 的 gettext locale .mo 文件（路径形如 .../locale/<lang>/LC_MESSAGES/<file>.mo）。"""
    out = []
    for entry in toc:
        if isinstance(entry, tuple) and len(entry) >= 2:
            dest = entry[1] if len(entry) >= 2 else ''
            if '/locale/' in dest and dest.endswith('.mo') and 'zh_CN' not in dest:
                continue
        out.append(entry)
    return out

a.datas = _keep_zh_locale_only(a.datas)
a.binaries = _keep_zh_locale_only(a.binaries)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir 关键：exe 内不嵌二进制
    name='shortvideo-backend',
    debug=False,
    strip=False,
    upx=False,
    console=True,  # 保留控制台便于排查
)

# onedir 模式：COLLECT 把 binaries/datas 收集到 exe 同目录的 _internal/
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='shortvideo-backend',
)
