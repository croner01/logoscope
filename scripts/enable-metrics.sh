#!/bin/bash
# 为应用启用 Metrics 导出

set -e

echo "=== 启用 OpenTelemetry Metrics 导出 ==="

# 1. semantic-engine 添加 Metrics 导出
echo "1. 为 semantic-engine 启用 Metrics..."
kubectl patch deployment -n islap semantic-engine --type=json -p='[
  {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "OTEL_METRICS_EXPORTER", "value": "otlp"}},
  {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "OTEL_METRICS_EXPORTER_PERIOD", "value": "60"}}
]' 2>/dev/null && echo "   ✅ semantic-engine Metrics 已启用" || echo "   ⚠️ semantic-engine 可能已配置"

# 2. log-generator 添加完整 OTEL 配置
echo "2. 为 log-generator 启用 OTEL（包括 Metrics）..."
kubectl patch deployment -n islap log-generator --type=json -p='[
  {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "OTEL_SERVICE_NAME", "value": "log-generator"}},
  {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": "http://otel-collector.islap.svc:4317"}},
  {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "OTEL_METRICS_EXPORTER", "value": "otlp"}}
]' 2>/dev/null && echo "   ✅ log-generator Metrics 已启用" || echo "   ⚠️ log-generator 可能已配置"

echo ""
echo "=== 等待 Pod 重启 ==="
echo "监控 Pod 状态: kubectl get pods -n islap -l app=semantic-engine,app=log-generator -w"
echo ""
echo "预计等待时间: 30-60 秒"
sleep 5

# 显示新 Pod 状态
kubectl get pods -n islap -l app=semantic-engine -l app=log-generator

echo ""
echo "=== 验证步骤 ==="
echo "1. 等待 2-3 分钟让应用启动并开始发送 Metrics"
echo "2. 检查 otel-collector 日志: kubectl logs -n islap -l app=opentelemetry,component=otel-collector --tail=50 | grep -i metric"
echo "3. 验证 ClickHouse: curl -s 'http://10.43.71.7:8123/?database=logs&query=SELECT+COUNT(*)+FROM+metrics+WHERE+timestamp+%3E+now()+-+INTERVAL+5+MINUTE+FORMAT+JSON'"
