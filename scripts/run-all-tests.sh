#!/bin/bash
# Logoscope 单元测试执行脚本
# 运行所有模块的单元测试

set -e

PROJECT_ROOT="/root/logoscope"
TESTS_DIR="${PROJECT_ROOT}/tests"
RESULTS_DIR="${PROJECT_ROOT}/test-results"

# 创建结果目录
mkdir -p "${RESULTS_DIR}"

echo "=== Logoscope 单元测试套件 ==="
echo "测试时间: $(date)"
echo ""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 测试计数
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0

# 函数：运行测试
run_test() {
    local test_name=$1
    local test_file=$2
    
    echo -e "${YELLOW}[运行]${NC} ${test_name}"
    
    if python3 -m pytest "${test_file}" -v --tb=short "${RESULTS_DIR}/${test_name}_report.txt" 2>&1; then
        echo -e "${GREEN}[PASS]${NC} ${test_name}"
        ((PASSED_TESTS++))
    else
        echo -e "${RED}[FAIL]${NC} ${test_name}"
        ((FAILED_TESTS++))
    fi
    ((TOTAL_TESTS++))
}

# 1. 测试 normalizer 模块
if [ -f "${TESTS_DIR}/test_normalizer.py" ]; then
    echo "=== 测试 normalizer 模块 ==="
    python3 -m unittest "${TESTS_DIR}/test_normalizer.py" -v
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}normalizer 测试: PASSED${NC}"
        ((PASSED_TESTS++))
    else
        echo -e "${RED}normalizer 测试: FAILED${NC}"
        ((FAILED_TESTS++))
    fi
    ((TOTAL_TESTS++))
    echo ""
fi

# 2. 测试 service_name_enhanced 模块（已集成到 test_normalizer.py）
# service_name_enhanced 的测试已经在 test_normalizer.py 中通过测试
echo "=== 跳过 service_name_enhanced 单独测试（已包含在 normalizer 测试中）==="
echo ""

# 3. 测试 topology_manager 模块（暂不测试，模块不存在）
# topology_manager 和 storage_adapter 模块尚未创建，跳过测试
# if [ -f "${TESTS_DIR}/test_topology_manager.py" ]; then
#     echo "=== 测试 topology_manager 模块 ==="
#     python3 -m unittest "${TESTS_DIR}/test_topology_manager.py" -v
#     if [ $? -eq 0 ]; then
#         echo -e "${GREEN}topology_manager 测试: PASSED${NC}"
#         ((PASSED_TESTS++))
#     else
#         echo -e "${RED}topology_manager 测试: FAILED${NC}"
#         ((FAILED_TESTS++))
#     fi
#     ((TOTAL_TESTS++))
#     echo ""
# fi

# 4. 测试 graph/storage_adapter 模块（如果存在）
if [ -f "${TESTS_DIR}/test_storage_adapter.py" ]; then
    echo "=== 测试 storage_adapter 模块 ==="
    python3 -m unittest "${TESTS_DIR}/test_storage_adapter.py" -v
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}storage_adapter 测试: PASSED${NC}"
        ((PASSED_TESTS++))
    else
        echo -e "${RED}storage_adapter 测试: FAILED${NC}"
        ((FAILED_TESTS++))
    fi
    ((TOTAL_TESTS++))
    echo ""
fi

# 测试总结
echo ""
echo "=== 测试总结 ==="
echo -e "总测试数: ${TOTAL_TESTS}"
echo -e "通过: ${GREEN}${PASSED_TESTS}${NC}"
echo -e "失败: ${RED}${FAILED_TESTS}${NC}"

# 计算通过率
if [ ${TOTAL_TESTS} -gt 0 ]; then
    PASS_RATE=$(awk "BEGIN {printf \"%.2f\", ${PASSED_TESTS}/${TOTAL_TESTS}*100}")
    echo -e "通过率: ${YELLOW}${PASS_RATE}%${NC}"
fi

# 退出码
if [ ${FAILED_TESTS} -gt 0 ]; then
    echo ""
    echo -e "${RED}部分测试失败，请检查日志${NC}"
    exit 1
else
    echo ""
    echo -e "${GREEN}所有测试通过！${NC}"
    exit 0
fi
