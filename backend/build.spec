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

# RapidOCR 数据文件（config.yaml + models/*.onnx）
# PyInstaller onedir 模式下默认不收包内的非 .py 文件，但 RapidOCR.main 用
# `Path(__file__).parent / 'config.yaml'` 读配置、读 models/*.onnx 加载权重，
# 所以必须显式声明。不声明时 RapidOCR() 抛 FileNotFoundError → 字幕检测失效。
# 路径解析：优先从已 import 的包定位 site-packages；dev 环境没装则回退到 venv 路径。
try:
    import rapidocr_onnxruntime as _rapidocr_mod
    _rapidocr_dir = os.path.dirname(_rapidocr_mod.__file__)
except ImportError:
    _rapidocr_dir = os.path.join(
        SPECPATH, 'venv', 'Lib', 'site-packages', 'rapidocr_onnxruntime'
    )

rapidocr_datas = []
_rapidocr_yaml = os.path.join(_rapidocr_dir, 'config.yaml')
if os.path.isfile(_rapidocr_yaml):
    rapidocr_datas.append((_rapidocr_yaml, 'rapidocr_onnxruntime'))
_rapidocr_models_dir = os.path.join(_rapidocr_dir, 'models')
if os.path.isdir(_rapidocr_models_dir):
    for _f in os.listdir(_rapidocr_models_dir):
        if _f.endswith('.onnx'):
            rapidocr_datas.append(
                (os.path.join(_rapidocr_models_dir, _f),
                 os.path.join('rapidocr_onnxruntime', 'models'))
            )

# MediaPipe Tasks API 原生 dll（libmediapipe.dll, ~55 MB）
# mediapipe.tasks.python.core.mediapipe_c_bindings 用
# `importlib.resources.files('mediapipe.tasks.c')` 解析 dll 路径，
# onedir 下 dll 必须位于 _internal/mediapipe/tasks/c/libmediapipe.dll。
# PyInstaller 默认会按扩展名收 .dll，但前提是 Analysis 扫到 mediapipe.tasks.c
# 这个子包——所以 hiddenimports 里必须显式声明 'mediapipe.tasks.c'。
try:
    import mediapipe.tasks.c as _mp_c_mod
    _mp_c_dir = os.path.dirname(_mp_c_mod.__file__)
except ImportError:
    # mediapipe.tasks.c 不是 mediapipe.tasks.python.c——
    # mediapipe 1.0.1 把 libmediapipe.dll 直接放在 mediapipe/tasks/c/，
    # 跟 mediapipe.tasks.python 平级，不是 python 包下的子模块
    try:
        import mediapipe as _mp
        _mp_c_dir = os.path.join(os.path.dirname(_mp.__file__), 'tasks', 'c')
    except ImportError:
        _mp_c_dir = os.path.join(SPECPATH, 'venv', 'Lib', 'site-packages', 'mediapipe', 'tasks', 'c')

mediapipe_c_binaries = []
if os.path.isdir(_mp_c_dir):
    for _f in os.listdir(_mp_c_dir):
        if _f.endswith(('.dll', '.so', '.dylib')):
            mediapipe_c_binaries.append(
                (os.path.join(_mp_c_dir, _f),
                 os.path.join('mediapipe', 'tasks', 'c'))
            )

a = Analysis(
    ['run_backend.py'],
    pathex=[SPECPATH],
    binaries=mediapipe_c_binaries,
    datas=face_model_datas + rapidocr_datas,
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
        # MediaPipe Tasks API 1.0.1（PyInstaller 默认不扫到 tasks.* 子模块）
        # 1.0.1 用 ctypes + importlib.resources 加载 libmediapipe.dll，
        # 老的 mediapipe.framework / mediapipe.python._framework_bindings / mediapipe.tasks.cc
        # 这 5 条都是 0.10.x 时代的死路径，1.0.1 已删除，PyInstaller 找不到会默默跳过。
        # 必须显式列出 mediapipe.tasks.c 让 Analysis 扫到那个目录的 binaries，
        # 否则 ~55MB libmediapipe.dll 不会被收集 → ctypes.CDLL 加载失败 → import mediapipe 抛错。
        'mediapipe.tasks',
        'mediapipe.tasks.python',
        'mediapipe.tasks.python.vision',
        'mediapipe.tasks.python.core',
        'mediapipe.tasks.c',
        'mediapipe.tasks.python.vision.face_detector',
        'mediapipe.tasks.python.vision.core.image',
    ],
    hookspath=[],
    runtime_hooks=[],
    # 只排 tkinter，**不要**排 matplotlib。
    # 排 matplotlib 会静默打死 mediapipe（不是「人脸检测降级」，是整条链路 ImportError）：
    #   mediapipe/__init__ → mediapipe.tasks.python → vision/__init__ →
    #   drawing_styles → drawing_utils → `import matplotlib.pyplot`（模块级，非 lazy）
    # drawing_utils 只用于可视化绘制，本项目不画图，但 import 语句照样在模块顶层执行。
    # 实测（PyInstaller 6.22.3 + mediapipe 1.0.1，excludes 带/不带 matplotlib 的 A/B）：
    #   带 matplotlib → ModuleNotFoundError: No module named 'matplotlib' → 人脸检测全失效
    #   不带        → MP_OK 1.0.1（matplotlib.pyplot 不依赖 tkinter，tkinter 仍可安全排除）
    # 代价：_internal 多 ~33MB matplotlib（安装包已 500MB+，可接受）。
    # 症状对照：启动日志刷「[人脸检测] mediapipe 未安装」×2（short/full 各一次）。
    excludes=['tkinter'],
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
