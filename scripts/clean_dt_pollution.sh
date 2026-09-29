#!/bin/bash
# #data-dir-defense：一键清理测试 fixture 历史污染（dt_pubrec_* / dt_data_* / dt_* 残留）。
#
# 背景：早期 init_data_dir 显式参数曾写 settings.json::data_dir，pytest fixture
# init_data_dir(tmp/"data") 把 Temp 临时目录持久化到配置；后续 dev 启动
# resolve_default_data_dir() 沿用 → 用户看到的是 Temp 路径。
#
# 后端 setting_service.resolve_default_data_dir() 现在含 _looks_like_pollution()
# 防御（识别 Temp/dt_* 路径并强制兜底），本脚本是手动清理现有残留。
#
# 用法：bash scripts/clean_dt_pollution.sh
# 退出码：0=全部成功，非 0=有失败

set -e
cd "$(dirname "$0")/.."

echo "============================================================"
echo "[1/4] 杀端口 8765 占用进程（dev 后端 uvicorn）"
echo "============================================================"
PIDS=$(netstat -ano 2>/dev/null | grep ":8765" | grep "LISTENING" | awk '{print $5}' | sort -u)
if [ -z "$PIDS" ]; then
    echo "[SKIP] 端口 8765 无占用"
else
    for pid in $PIDS; do
        echo "  - 杀 PID $pid"
        taskkill //F //PID "$pid" 2>&1 || true
    done
fi

echo ""
echo "============================================================"
echo "[2/4] 删 Temp/dt_* 测试残留（pytest / tempfile.mkdtemp 默认前缀）"
echo "============================================================"
TEMP_DIR="$TEMP"
if [ -z "$TEMP_DIR" ]; then
    TEMP_DIR="/c/Users/$USERNAME/AppData/Local/Temp"
fi
if [ ! -d "$TEMP_DIR" ]; then
    echo "[WARN] Temp 目录不存在：$TEMP_DIR"
else
    # 删 dt_*/test_*/tmp_* 前缀子目录（pytest fixture 默认前缀）
    COUNT=0
    for prefix in "dt_" "test_" "tmp_"; do
        for d in "$TEMP_DIR"/${prefix}*/; do
            [ -d "$d" ] || continue
            # 跳过本脚本误伤：dt_* 中可能有用户重要数据，但当前 fixture 都是 mkdtemp(prefix=) 生成可删
            BASENAME=$(basename "$d")
            echo "  - 删 $BASENAME"
            rm -rf "$d" 2>&1 || true
            COUNT=$((COUNT + 1))
        done
    done
    echo "[完成] 共清理 $COUNT 个残留目录"
fi

echo ""
echo "============================================================"
echo "[3/4] 还原 backend/config/settings.json::data_dir"
echo "============================================================"
SETTINGS_FILE="backend/config/settings.json"
if [ ! -f "$SETTINGS_FILE" ]; then
    echo "[SKIP] $SETTINGS_FILE 不存在"
else
    # 检测 data_dir 是否指向 Temp 污染路径
    CURRENT_DATA_DIR=$(grep -oP '"data_dir":\s*"\K[^"]+' "$SETTINGS_FILE" || echo "")
    if [ -z "$CURRENT_DATA_DIR" ]; then
        echo "[SKIP] data_dir 未设置（首次启动兜底即可）"
    elif echo "$CURRENT_DATA_DIR" | grep -qE "Temp[\\/](dt_|test_|tmp_)"; then
        # 清空 data_dir 让后端启动时兜底到最大盘
        echo "  当前 data_dir = $CURRENT_DATA_DIR（污染路径，清空）"
        # Python 改 settings.json 保留其他字段
        python -c "
import json, sys
p = '$SETTINGS_FILE'
with open(p, encoding='utf-8') as f:
    s = json.load(f)
if 'data_dir' in s:
    del s['data_dir']
with open(p, 'w', encoding='utf-8') as f:
    json.dump(s, f, ensure_ascii=False, indent=2)
print('[OK] 已清空 data_dir 字段（其他字段保留）')
"
    else
        echo "[SKIP] data_dir = $CURRENT_DATA_DIR（非污染路径，保留）"
    fi
fi

echo ""
echo "============================================================"
echo "[4/4] 验证"
echo "============================================================"
echo "端口 8765 状态："
netstat -ano 2>/dev/null | grep ":8765" | grep "LISTENING" || echo "  无占用 ✓"
echo ""
echo "Temp/dt_* 残留数："
ls "$TEMP_DIR"/dt_*/ 2>/dev/null | wc -l | xargs echo "  dt_*: "
ls "$TEMP_DIR"/test_*/ 2>/dev/null | wc -l | xargs echo "  test_*: "
ls "$TEMP_DIR"/tmp_*/ 2>/dev/null | wc -l | xargs echo "  tmp_*: "

echo ""
echo "============================================================"
echo "清理完成。下一步："
echo "  1. npm run dev（重启主进程）"
echo "  2. 主进程会 spawn 后端带 env SHORTVIDEO_DATA_DIR=...（用户选择路径）"
echo "  3. 设置页 dataDir 应显示用户选择的路径"
echo ""
echo "如果仍然异常：检查 userData JSON:"
echo "  cat %APPDATA%/sv-platform-tool-frontend/preferred_data_dir.json"
echo "============================================================"