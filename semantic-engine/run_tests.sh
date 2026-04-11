#!/bin/bash
# 运行测试脚本

set -e

echo "========================================="
echo "  Semantic Engine 测试套件"
echo "========================================="
echo ""

# 切换到脚本所在目录
cd "$(dirname "$0")"

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo "❌ 虚拟环境不存在，请先创建: python3 -m venv venv"
    exit 1
fi

# 激活虚拟环境
echo "📦 激活虚拟环境..."
source venv/bin/activate

# 安装运行时与测试依赖
echo "📥 安装运行时与测试依赖..."
pip install -q -r requirements-runtime.txt -r requirements-test.txt

# 运行测试
echo ""
echo "🧪 运行测试..."
echo "========================================="
pytest tests/ -v --tb=short "$@"

# 显示测试结果
TEST_EXIT_CODE=$?

echo ""
echo "========================================="
if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo "✅ 所有测试通过！"
else
    echo "❌ 测试失败，退出码: $TEST_EXIT_CODE"
fi
echo "========================================="

# 生成覆盖率报告
if [ -d "htmlcov" ]; then
    echo ""
    echo "📊 覆盖率报告已生成: htmlcov/index.html"
fi

exit $TEST_EXIT_CODE
