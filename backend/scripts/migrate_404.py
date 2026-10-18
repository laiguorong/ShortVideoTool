"""临时脚本：把 service 层所有 'raise ValueError("xxx不存在")' 改成 raise HTTPException(404, "xxx不存在")。"""
import re
from pathlib import Path

services = [
    "app/services/account_service.py",
    "app/services/creation_service.py",
    "app/services/material_service.py",
    "app/services/selection_service.py",
    "app/services/publish_intro_service.py",
]

# 匹配 raise ValueError("xxx不存在") 或 raise ValueError(f"...不存在")
PATTERN = re.compile(r'raise ValueError\((\s*(?:f)?"[^"]*不存在"\s*)\)')
REPLACEMENT = r'raise HTTPException(status_code=404, detail=\1)'

for sf in services:
    p = Path(sf)
    if not p.exists():
        print(f"  {sf} 不存在，跳过")
        continue
    src = p.read_text(encoding="utf-8")

    # 如果没有 HTTPException import，先加
    if "HTTPException" not in src.split("from fastapi")[0]:  # 简化判断
        # 在 loguru import 之后插入
        if "from loguru import logger" in src:
            src = src.replace(
                "from loguru import logger",
                "from fastapi import HTTPException\nfrom loguru import logger",
                1,
            )

    new_text, n = PATTERN.subn(REPLACEMENT, src)
    if n == 0:
        print(f"  {sf}: 0 处")
        continue
    p.write_text(new_text, encoding="utf-8")
    print(f"  {sf}: 改 {n} 处")
