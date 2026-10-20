# -*- coding: utf-8 -*-
"""用 PowerShell 启动资源管理器 + System.Drawing 截屏。

目标：截取 Windows 资源管理器显示 backend/data/ 目录的窗口。
"""
import subprocess
import sys
import time
import pathlib
import os

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT / "docs" / "marketing" / "screenshots"
OUT_DIR.mkdir(parents=True, exist_ok=True)

DATA_DIR = ROOT / "backend" / "data"
DATA_DIR_WIN = str(DATA_DIR).replace("/", "\\")

# PowerShell 脚本：启动资源管理器 + 截屏到文件
PS_SCRIPT = f"""
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

# 启动资源管理器指向目标目录
Start-Process explorer.exe -ArgumentList '"{DATA_DIR_WIN}"' -WindowStyle Normal

# 等窗口出来
Start-Sleep -Seconds 3

# 截屏（多屏全屏拼接 → 当前主屏）
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$out = "{str(OUT_DIR / "06-datadir-v1.0.4-raw.png").replace("/", chr(92))}"
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bmp.Dispose()

Write-Output "[OK] saved to $out  ($($bounds.Width)x$($bounds.Height))"
"""

# 写 ps1 文件 + 用 PowerShell tool 调用
ps_file = ROOT / "backend" / "tests" / "_ps_screenshot.ps1"
ps_file.write_text(PS_SCRIPT, encoding="utf-8")
print(f"[ps1] {ps_file}")
print(f"[out]  {OUT_DIR / '06-datadir-v1.0.4-raw.png'}")
