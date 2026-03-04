# 追踪页面时间与 Span 时长修复交付方案（2026-03-04）

## 1. 交付目标

本次交付覆盖以下两项需求：

1. 追踪页面时间展示取消 `CST` 字样，仅显示本地化时间值。
2. 追踪详情 `Span` 时间线“请求块普遍显示 0ms”问题修复，并补齐旧数据回退策略。

## 2. 变更范围

### 2.1 前端展示层

- 移除追踪页面用户可见文案中的 `（CST）/(CST)`。
- 保持时间按 `Asia/Shanghai` 时区显示，但不再在 UI 显示时区缩写。

涉及路径（核心）：

- `frontend/src/utils/formatters.ts`
- `frontend/src/pages/TracesExplorer.tsx`
- `frontend/src/components/traces/TraceTimeline.tsx`
- `frontend/src/components/traces/SpanDetailPanel.tsx`

### 2.2 Trace 写入链路（实时数据）

- `semantic-engine` 在解析 trace span 时：
  - 从 `startTimeUnixNano/endTimeUnixNano` 计算 `duration_ns/duration_ms`。
  - 新增兼容 `start_time/end_time`（ISO 与 epoch）字段。
  - 写入 tags 作为下游回退数据来源。

涉及路径：

- `semantic-engine/msgqueue/signal_parser.py`
- `semantic-engine/tests/test_signal_parser.py`

### 2.3 Trace 查询链路（历史数据）

- `query-service` 在 `/api/v1/traces/{trace_id}/spans` 查询时：
  - 当全量 span 时长均为 0，启用时间线推断回退。
  - 起始时间解析增强：支持 ISO、`yyyy-mm-dd hh:mm:ss`、纯数字 epoch（s/ms/us/ns）。

涉及路径：

- `query-service/api/query_observability_service.py`
- `query-service/tests/test_trace_routes.py`

## 3. 版本与镜像

本次发布镜像：

- `localhost:5000/logoscope/query-service:20260304-spanfix3`
- `localhost:5000/logoscope/semantic-engine:20260304-spanfix3`
- `localhost:5000/logoscope/frontend:20260304-spanfix2`

对应工作负载：

- `deployment/query-service`
- `deployment/semantic-engine`
- `deployment/semantic-engine-worker`
- `deployment/frontend`

## 4. 验证口径

### 4.1 功能口径

1. 追踪页面不出现 `CST` 文案。
2. 新产生 trace 的 span `duration_ms` 非 0。
3. 历史全 0 trace 经查询层回退后，大多数父/中间 span 变为非 0。

### 4.2 已执行样例（线上）

- 最近 trace 抽样：`span_count=5, non_zero=5`。
- 历史 trace `5d44cab64229489fad7fdf9d7a12cb0f`：`span_count=37, non_zero=36, zero=1`。

说明：极少数尾部叶子 span 若缺失可推断结束点，仍可能保留 `0ms`。

## 5. 回滚方案

1. 镜像回滚：
   - `query-service` 回滚到 `20260304-spanfix2`。
   - `semantic-engine` 与 `semantic-engine-worker` 回滚到 `20260304-spanfix2`。
2. Helm 回滚：
   - `helm rollback <release> <revision> -n <namespace>`。

## 6. Helm 安装包交付

### 6.1 Chart 路径

- `charts/logoscope`

### 6.2 组件说明

Chart 默认包含：

- clickhouse
- redis
- neo4j
- otel-gateway
- otel-collector
- fluent-bit
- ingest-service
- semantic-engine
- semantic-engine-worker
- query-service
- topology-service
- ai-service
- frontend
- value-kpi-cronjob

### 6.3 安装命令

```bash
helm upgrade --install logoscope charts/logoscope -n islap --create-namespace
```

### 6.4 包产物

- `dist/helm/logoscope-0.1.0.tgz`

