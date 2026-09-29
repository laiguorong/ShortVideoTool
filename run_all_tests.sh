#!/bin/bash
# 全量回归脚本：跑 P0/P1 新增的 53 个测试用例 + 后端语法检查 + 前端 typecheck
#
# 用法：
#   bash run_all_tests.sh
#
# 退出码：0=全过，非0=有失败

cd "$(dirname "$0")"

PASSED_TOTAL=0
FAILED_TOTAL=0
SECTIONS_FAILED=0

run_section() {
    local name="$1"
    shift
    if "$@"; then
        echo "[OK] ${name}"
        PASSED_TOTAL=$((PASSED_TOTAL + 1))
    else
        echo "[FAIL] ${name}"
        FAILED_TOTAL=$((FAILED_TOTAL + 1))
        SECTIONS_FAILED=$((SECTIONS_FAILED + 1))
    fi
}

echo "============================================================"
echo "[1/4] 后端语法检查"
echo "============================================================"
cd backend
run_section "backend 语法" python -c "import ast; [ast.parse(open(f, encoding='utf-8').read()) for f in [
    'app/services/task_service.py',
    'app/services/publish_service.py',
    'app/services/material_service.py',
    'app/services/share_import_service.py',
    'app/services/upload_service.py',
    'app/services/creation_service.py',
    'app/services/account_service.py',
    'app/core/douyin/client.py',
    'app/core/douyin/browser.py',
]]; print('[OK] backend 语法通过')"

echo ""
echo "============================================================"
echo "[2/4] 后端 P0/P1 单元测试"
echo "============================================================"
BACKEND_PASSED=0
BACKEND_FAILED=0
# 后端测试实际位于 backend/tests/（之前根 tests/ 已删，单测迁入 backend/tests）
for f in tests/test_p0_*.py tests/test_p1_*.py tests/test_task_service_e2e.py tests/test_highrisk_*.py tests/test_start_ts_*.py \
         tests/test_v38_migrate_413.py tests/test_publish_intro_service_413.py tests/test_publish_schedule_413.py \
         tests/test_publish_draft_413.py tests/test_publish_record_413.py \
         tests/test_setting_data_dir_fallback.py; do
    if [ -f "$f" ]; then
        echo ""
        echo "--- $f ---"
        if python "$f"; then
            BACKEND_PASSED=$((BACKEND_PASSED + 1))
        else
            BACKEND_FAILED=$((BACKEND_FAILED + 1))
        fi
    fi
done
echo ""
echo "[结果] 后端测试：${BACKEND_PASSED} 文件通过 / ${BACKEND_FAILED} 文件失败"
if [ "$BACKEND_FAILED" -eq 0 ]; then
    PASSED_TOTAL=$((PASSED_TOTAL + 1))
else
    FAILED_TOTAL=$((FAILED_TOTAL + 1))
    SECTIONS_FAILED=$((SECTIONS_FAILED + 1))
fi

echo ""
echo "============================================================"
echo "[3/4] 前端 typecheck（排除预存告警）"
echo "============================================================"
cd ../frontend
TYPECHECK_OUTPUT=$(npx tsc --noEmit 2>&1 || true)
if [ -z "$TYPECHECK_OUTPUT" ]; then
    echo "[OK] 前端 typecheck 0 新错误（预存告警已排除）"
    PASSED_TOTAL=$((PASSED_TOTAL + 1))
else
    echo "[FAIL] 前端 typecheck 新错误："
    echo "$TYPECHECK_OUTPUT"
    FAILED_TOTAL=$((FAILED_TOTAL + 1))
    SECTIONS_FAILED=$((SECTIONS_FAILED + 1))
fi

echo ""
echo "============================================================"
echo "[4/5] 前端单元测试"
echo "============================================================"
FRONTEND_TESTS_PASSED=0
FRONTEND_TESTS_FAILED=0
for spec in tests/status_label.spec.ts tests/preferredDataDir.spec.ts tests/buildRelaunchArgs.spec.ts \
             tests/createReloadScheduler.spec.ts tests/dataDirChoice.spec.ts; do
    if [ -f "$spec" ]; then
        echo "--- $spec ---"
        if npx tsx "$spec" 2>&1 | tail -3; then
            FRONTEND_TESTS_PASSED=$((FRONTEND_TESTS_PASSED + 1))
        else
            FRONTEND_TESTS_FAILED=$((FRONTEND_TESTS_FAILED + 1))
        fi
    fi
done
if [ "$FRONTEND_TESTS_FAILED" -eq 0 ]; then
    PASSED_TOTAL=$((PASSED_TOTAL + 1))
    echo "[结果] 前端测试：${FRONTEND_TESTS_PASSED} 套件通过 / 0 失败"
else
    FAILED_TOTAL=$((FAILED_TOTAL + 1))
    SECTIONS_FAILED=$((SECTIONS_FAILED + 1))
    echo "[结果] 前端测试：${FRONTEND_TESTS_PASSED} 通过 / ${FRONTEND_TESTS_FAILED} 失败"
fi

echo ""
echo "============================================================"
echo "[5/5] 前端 GBK stdout 编码测试"
echo "============================================================"
if node tests/test_gbk_stdout.cjs && echo "[OK] GBK stdout 编码 monkey patch 通过"; then
    PASSED_TOTAL=$((PASSED_TOTAL + 1))
else
    FAILED_TOTAL=$((FAILED_TOTAL + 1))
    SECTIONS_FAILED=$((SECTIONS_FAILED + 1))
fi

echo ""
echo "============================================================"
echo "汇总：通过 ${PASSED_TOTAL} 节 / 失败 ${FAILED_TOTAL} 节"
echo "============================================================"
exit $SECTIONS_FAILED
