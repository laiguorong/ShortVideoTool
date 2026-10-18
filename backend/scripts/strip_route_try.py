"""临时脚本：删除所有路由层 try/except ValueError → raise HTTPException(400, ...) 块。

service raise HTTPException(404) / ValueError(业务校验) 都由 main.py 全局 handler
统一处理为 ApiResp 信封，路由层 try/except 已冗余。
"""
import re
from pathlib import Path

# 模式 A: try: return ok(...); except ValueError: raise HTTPException(400)
# 模式 B: try: info = svc(); return ok(info); except ValueError: raise HTTPException(400)
# 两种都支持；统一把 try/except 整块删除，保留 return ok(...) 或 value = svc(); return ok(value)

PATTERNS = [
    # 模式 A: try 直接包 return ok
    (
        re.compile(
            r'    try:\n'
            r'        return ok\(([^\n]+)\)\n'
            r'    except ValueError as e:\n'
            r'        raise HTTPException\(status_code=400, detail=str\(e\)\) from e\n',
            re.MULTILINE,
        ),
        r'    return ok(\1)\n',
    ),
    # 模式 B-1: try 包 var=svc(); except; return ok(var)
    (
        re.compile(
            r'    try:\n'
            r'        (\w+) = ([^\n]+)\n'
            r'    except ValueError as e:\n'
            r'        raise HTTPException\(status_code=400, detail=str\(e\)\) from e\n'
            r'    return ok\(\1\)\n',
            re.MULTILINE,
        ),
        r'    \1 = \2\n    return ok(\1)\n',
    ),
    # 模式 B-2: try 包 var = svc(); except; return ok(var) （4 空格缩进的返回值在函数内）
    # 已包含在 B-1
]

# 简化的 ValueError 路径（不一定是 status_code=400，可能是其他）
PATTERN_400 = re.compile(
    r'    try:\n'
    r'((?:        (?!return ok\()\S[^\n]*\n)+?)'  # 任意 4 空格缩进行（避免 return ok 在 try 内）
    r'    except ValueError as e:\n'
    r'        raise HTTPException\((?:status_code=)?400,? (?:detail=)?str\(e\)\) from e\n',
    re.MULTILINE,
)

# 更宽泛的 try/except ValueError 兜底
PATTERN_LOOSE = re.compile(
    r'    try:\n'
    r'        ([^\n]+)\n'
    r'    except ValueError as e:\n'
    r'        raise HTTPException\((?:status_code=)?400,?(?:\s+)?(?:detail=)?str\(e\)\) from e\n',
    re.MULTILINE,
)

# 用更精准的：A 只删 return ok 的；B 用 4 空格缩进 + return ok 后续
PATTERN_A = re.compile(
    r'    try:\n'
    r'        (return ok\([^\n]+\))\n'
    r'    except ValueError as e:\n'
    r'        raise HTTPException\((?:status_code=)?\d+,? (?:detail=)?str\(e\)\) from e\n',
    re.MULTILINE,
)

PATTERN_B = re.compile(
    r'    try:\n'
    r'        (\w+) = ([^\n]+)\n'
    r'    except ValueError as e:\n'
    r'        raise HTTPException\((?:status_code=)?\d+,? (?:detail=)?str\(e\)\) from e\n'
    r'    return ok\(\1\)\n',
    re.MULTILINE,
)

# Pattern C: try 包 多行语句（任意）+ return ok(...) 跨多行
# 匹配 try: ... (任意内容含 return ok(...)) ... except ValueError
# 用 [^\n]* 包括 1+ 行 + 1 个 return ok(行，可能跨行)
PATTERN_C = re.compile(
    r'    try:\n'
    r'((?:        [^\n]*\n)+?)'  # 1+ 行 8 空格缩进任意
    r'(        return ok\(.*?\n        \))'  # return ok() 跨多行（直到最右 ")"）
    r'\n    except ValueError as e:\n'
    r'        raise HTTPException\((?:status_code=)?\d+,? (?:detail=)?str\(e\)\) from e\n',
    re.MULTILINE | re.DOTALL,
)

total = 0
for f in sorted(Path("app/api").glob("*.py")):
    if f.name == "__init__.py":
        continue
    text = f.read_text(encoding="utf-8")
    n_total = 0
    while True:
        # 模式 A
        n_a = len(PATTERN_A.findall(text))
        text = PATTERN_A.sub(r'    \1\n', text)
        # 模式 B
        n_b = len(PATTERN_B.findall(text))
        text = PATTERN_B.sub(r'    \1 = \2\n    return ok(\1)\n', text)
        # 模式 C（最严格，先匹配；A/B 已无重叠）
        n_c = len(PATTERN_C.findall(text))
        text = PATTERN_C.sub(r'\1        \2\n', text)
        if n_a + n_b + n_c == 0:
            break
        n_total += n_a + n_b + n_c
    if n_total > 0:
        f.write_text(text, encoding="utf-8")
        total += n_total
        print(f"  {f.name}: 删 {n_total} 个")

print(f"\n总删除: {total}")
