"""临时脚本：扫后端 Pydantic model 死代码（model 定义后无任何非 models/ 文件引用）。"""
import ast
from pathlib import Path

models = []
for f in Path("app/models").glob("*.py"):
    src = f.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id == "BaseModel":
                    models.append((f.name, node.name))

total_dead = 0
for file, model in models:
    if model.startswith("_"):
        continue
    refs = 0
    for py in Path("app").rglob("*.py"):
        if "models" in str(py):
            continue
        if model in py.read_text(encoding="utf-8", errors="ignore"):
            refs += 1
    if refs == 0:
        print(f"  {file}: {model} (非 models 文件无引用)")
        total_dead += 1
print(f"\n死 Pydantic model 候选: {total_dead}")
