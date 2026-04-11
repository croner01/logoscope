#!/bin/bash
# Semantic Engine 启动脚本（带 OpenTelemetry 自动插桩）

set -e

echo "Starting Semantic Engine with OpenTelemetry Auto-Instrumentation..."

# 检查是否启用了 OpenTelemetry
if [ "$OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED" = "true" ]; then
    echo "OpenTelemetry auto-instrumentation is enabled"

    # 使用 opentelemetry-instrument 启动应用
    # start.py 会自动启动 uvicorn
    exec opentelemetry-instrument python start.py api
else
    echo "OpenTelemetry auto-instrumentation is disabled"

    # 正常启动应用
    exec python start.py api
fi
