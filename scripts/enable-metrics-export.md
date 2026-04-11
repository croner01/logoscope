# 启用 OpenTelemetry Metrics 导出

## 问题诊断

当前 Metrics 表无数据写入（最新数据：2026-02-07），原因是：

1. ✅ otel-collector Metrics Pipeline 已配置并运行
2. ❌ 应用程序未启用 Metrics 导出

### 检查结果

**semantic-engine**:
- `OTEL_TRACES_EXPORTER=otlp` ✅
- `OTEL_METRICS_EXPORTER` ❌ **未配置**

**log-generator**:
- 无任何 OTEL 环境变量 ❌

---

## 解决方案

### 方案 A: 修改 semantic-engine Deployment

在 semantic-engine deployment 中添加环境变量：

```yaml
env:
  # 现有配置
  - name: OTEL_TRACES_EXPORTER
    value: otlp
  
  # ⭐ 新增：启用 Metrics 导出
  - name: OTEL_METRICS_EXPORTER
    value: otlp
  
  - name: OTEL_METRICS_EXPORTER_PERIOD
    value: "60"  # 每 60 秒导出一次 metrics
  
  # ⭐ 新增：自动运行时指标
  - name: OTEL_RUNTIME_METRICS
    value: "all"
  
  - name: OTEL_PYTHON_EXCLUDED_INSTRUMENTATIONS
    value: "httpx"  # 排除不需要的库
```

**应用命令**:
```bash
kubectl patch deployment -n islap semantic-engine --type=json -p='[
  {
    "op": "add",
    "path": "/spec/template/spec/containers/0/env/-",
    "value": {
      "name": "OTEL_METRICS_EXPORTER",
      "value": "otlp"
    }
  },
  {
    "op": "add",
    "path": "/spec/template/spec/containers/0/env/-",
    "value": {
      "name": "OTEL_METRICS_EXPORTER_PERIOD",
      "value": "60"
    }
  }
]'
```

### 方案 B: 为 log-generator 添加完整 OTEL 配置

如果 log-generator 是 Python 应用，添加完整配置：

```yaml
env:
  - name: OTEL_SERVICE_NAME
    value: log-generator
  
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: http://otel-collector.islap.svc:4317
  
  - name: OTEL_TRACES_EXPORTER
    value: otlp
  
  - name: OTEL_METRICS_EXPORTER
    value: otlp
  
  - name: OTEL_METRICS_EXPORTER_PERIOD
    value: "60"
  
  - name: OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED
    value: "true"
```

**应用命令**:
```bash
kubectl patch deployment -n islap log-generator --type=json -p='[
  {
    "op": "add",
    "path": "/spec/template/spec/containers/0/env/-",
    "value": {"name": "OTEL_SERVICE_NAME", "value": "log-generator"}
  },
  {
    "op": "add",
    "path": "/spec/template/spec/containers/0/env/-",
    "value": {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": "http://otel-collector.islab.svc:4317"}
  },
  {
    "op": "add",
    "path": "/spec/template/spec/containers/0/env/-",
    "value": {"name": "OTEL_METRICS_EXPORTER", "value": "otlp"}
  }
]'
```

---

## 验证步骤

### 1. 应用配置后，等待 Pod 重启

```bash
kubectl get pods -n islap -l app=semantic-engine -w
```

### 2. 检查 otel-collector 是否接收 Metrics

```bash
kubectl logs -n islap -l app=opentelemetry,component=otel-collector --tail=100 | grep -i metric
```

### 3. 验证 ClickHouse Metrics 表

```bash
# 等待 2-3 分钟后检查
curl -s "http://10.43.71.7:8123/?database=logs&query=SELECT+COUNT(*)+FROM+metrics+WHERE+timestamp+%3E+now()+-+INTERVAL+5+MINUTE+FORMAT+JSON"
```

**预期结果**: 应该有新的 metrics 记录写入

---

## 自动化脚本

创建 `enable-metrics.sh` 脚本自动化此过程：

```bash
#!/bin/bash
# 为所有应用启用 Metrics 导出

# 1. semantic-engine
echo "启用 semantic-engine Metrics..."
kubectl patch deployment -n islap semantic-engine --type=json -p='[
  {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "OTEL_METRICS_EXPORTER", "value": "otlp"}},
  {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "OTEL_METRICS_EXPORTER_PERIOD", "value": "60"}}
]'

# 2. log-generator
echo "启用 log-generator Metrics..."
kubectl patch deployment -n islap log-generator --type=json -p='[
  {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "OTEL_SERVICE_NAME", "value": "log-generator"}},
  {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": "http://otel-collector.islap.svc:4317"}},
  {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "OTEL_METRICS_EXPORTER", "value": "otlp"}}
]'

echo "✅ Metrics 导出已启用，等待 Pod 重启..."
```

---

## Python 应用特殊说明

如果应用是 Python，确保安装了以下包：

```bash
pip install opentelemetry-api
pip install opentelemetry-sdk
pip install opentelemetry-auto-instrumentation
```

并在代码中启用自动 metrics：

```python
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

# 配置 metrics exporter
exporter = OTLPMetricExporter(endpoint="otel-collector.islap.svc:4317", insecure=True)
reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60000)
metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
```

---

**注意**: 环境变量方式（推荐）无需修改代码，只需配置 OTel SDK 自动检测。
