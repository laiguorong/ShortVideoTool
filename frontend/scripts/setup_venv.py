#!/usr/bin/env python3
"""跨平台 venv 创建 + 装依赖.

#fix-venv-onboarding-cross-platform:
- Windows: .venv/Scripts/python.exe
- macOS/Linux: .venv/bin/python
- 自动检测 Python 3.12.x, 创建 venv, 装 requirements + pyinstaller
- WorkBuddy / 360 杀软环境: 必须用 `python -E scripts/setup_venv.py` 屏蔽 shim

用法:
  python scripts/setup_venv.py                    # 自动检测
  python -E scripts/setup_venv.py                # 防 360 shim (Windows + WorkBuddy)
  python scripts/setup_venv.py --check           # 只验证 venv 状态, 不装
"""
import argparse
import platform
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent.parent / 'backend'
IS_WINDOWS = platform.system() == 'Windows'
VENV_DIR = BACKEND / '.venv'
VENV_BIN = VENV_DIR / ('Scripts' if IS_WINDOWS else 'bin')
VENV_PY = VENV_BIN / ('python.exe' if IS_WINDOWS else 'python')
REQ_FILE = BACKEND / 'requirements.txt'


def run(cmd, **kwargs):
    if isinstance(cmd, list):
        print('+ ' + ' '.join(str(c) for c in cmd))
    else:
        print('+ ' + cmd)
    subprocess.run(cmd, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description='跨平台 venv 创建 + 装依赖')
    parser.add_argument('--check', action='store_true', help='只验证 venv 状态, 不创建/装')
    args = parser.parse_args()

    # 1. 验证 Python 版本 (3.12.x)
    major, minor = sys.version_info[:2]
    if major != 3 or minor != 12:
        print(f'ERROR: 当前 Python {major}.{minor}, 项目需要 3.12.10 (python.org/downloads/release/python-31210/)')
        sys.exit(1)
    print(f'Python {sys.version.split()[0]} (OK)')

    # 2. check mode: 只验证
    if args.check:
        if VENV_PY.exists():
            out = subprocess.run([str(VENV_PY), '-m', 'pip', 'list'], capture_output=True, text=True)
            pkgs = [l for l in out.stdout.splitlines() if l and not l.startswith('-') and not l.startswith('Package')]
            print(f'venv 存在: {VENV_DIR}, 装 {len(pkgs)} 个包 (期望 ~80)')
            return 0
        else:
            print(f'ERROR: venv 不存在, 跑 python scripts/setup_venv.py 建')
            return 1

    # 3. 创建 venv (如果不存在)
    if not VENV_PY.exists():
        print(f'创建 venv: {VENV_DIR}')
        run([sys.executable, '-m', 'venv', str(VENV_DIR)])
    else:
        print(f'venv 已存在: {VENV_DIR} (跳过创建)')

    # 4. 装 requirements
    print('装 requirements.txt ...')
    run([str(VENV_PY), '-m', 'pip', 'install', '-r', str(REQ_FILE)])

    # 5. 装 pyinstaller + pyinstaller-hooks-contrib (不在 requirements.txt)
    print('装 pyinstaller + pyinstaller-hooks-contrib ...')
    run([str(VENV_PY), '-m', 'pip', 'install', 'pyinstaller==6.22.3', 'pyinstaller-hooks-contrib'])

    # 6. 验证
    print('验证 venv 包数 ...')
    out = subprocess.run([str(VENV_PY), '-m', 'pip', 'list'], capture_output=True, text=True)
    pkgs = [l for l in out.stdout.splitlines() if l and not l.startswith('-') and not l.startswith('Package')]
    print(f'venv 装 {len(pkgs)} 个包 (期望 ~80)')

    # 7. 跨平台提示
    if IS_WINDOWS:
        print('Windows 提示: 装过 360 杀软或 WorkBuddy 时, pip install 会被 safe-delete shim 拦')
        print('  解决: 用 `python -E scripts/setup_venv.py` 屏蔽 PYTHONPATH 避开 shim')
        print('  或: build 时用 venv python + `-E` 标志 (package.json build:backend 已加)')

    print()
    print('=' * 60)
    print('venv 创建完成! 下一步:')
    print(f'  跑后端:  cd {BACKEND} && {VENV_PY} -m uvicorn app.main:app --host 127.0.0.1 --port 8765')
    print(f'  打包:    cd frontend && npm run dist')
    print('=' * 60)


if __name__ == '__main__':
    main()
