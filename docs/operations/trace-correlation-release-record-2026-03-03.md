# 链路追踪优化发布记录（2026-03-03）

> 版本: v3.22.0  
> 更新: 2026-03-03  
> 状态: 已发布

---

## 1. 目标与范围

本次发布目标：

1. 修复应用日志 `trace/span` 丢失，提升日志-链路关联能力。  
2. 明确 `trace_id` 来源（`otlp/missing/synthetic`），关闭伪 trace 回退对 KPI 的污染。  
3. 增加可重复的验证脚本与发布后证据，形成可归档发布记录。

发布服务范围（namespace=`islap`）：

1. `query-service`
2. `semantic-engine`
3. `semantic-engine-worker`
4. `topology-service`
5. `ai-service`

---

## 2. 代码变更摘要

### 2.1 OTel 初始化与时序修复

核心修复：将 OTel 初始化改为对具体 FastAPI `app` 执行 `instrument_app(...)`，并在应用初始化阶段完成，避免 `startup/lifespan` 时序导致上下文不可见。

- `shared_src/utils/otel_init.py`
  - 支持 `init_otel(..., app=app)`
  - 默认排除健康检查路径（`/health /ready /live` 等）不建 span
  - 增加重复注入保护标记 `_logoscope_otel_instrumented`
- `query-service/main.py`
- `semantic-engine/main.py`
- `topology-service/main.py`
- `ai-service/main.py`

### 2.2 日志提取与来源标记补齐

- `semantic-engine/normalize/normalizer.py`
  - 新增正文 fallback 提取：支持从文本日志 `trace=<32hex> span=<16hex>` 解析
  - `trace_id_source` 与真实提取来源对齐，避免 `_raw_attributes` 中残留 `missing`
- `ENABLE_PSEUDO_TRACE_ID_FALLBACK=false` 保持开启（部署中已生效），缺失 trace 不再伪造

### 2.3 验证与守护能力

- `scripts/trace-e2e-smoke.sh`
  - 默认 `TRACE_ID/SPAN_ID` 改为标准 hex（32/16）
  - 增加格式校验，避免注入非法 trace id 导致误报
- `scripts/trace-correlation-check.sh`
  - 输出 logs/traces/join 统计，写入 `reports/trace-correlation-check/`

### 2.4 测试补充

- `semantic-engine/tests/test_otel_init_shared.py`
- `semantic-engine/tests/test_normalizer.py`

---

## 3. 发布信息

### 3.1 镜像 Tag

- `tracefix-20260303213938`

### 3.2 发布命令（执行时间：2026-03-03）

```bash
scripts/k8s-image-ops.sh build-push query-service tracefix-20260303213938
scripts/k8s-image-ops.sh build-push semantic-engine tracefix-20260303213938
scripts/k8s-image-ops.sh build-push topology-service tracefix-20260303213938
scripts/k8s-image-ops.sh build-push ai-service tracefix-20260303213938

scripts/k8s-image-ops.sh set-image query-service tracefix-20260303213938
scripts/k8s-image-ops.sh set-image semantic-engine tracefix-20260303213938
scripts/k8s-image-ops.sh set-image topology-service tracefix-20260303213938
scripts/k8s-image-ops.sh set-image ai-service tracefix-20260303213938

scripts/k8s-image-ops.sh rollout-status query-service
scripts/k8s-image-ops.sh rollout-status semantic-engine
scripts/k8s-image-ops.sh rollout-status topology-service
scripts/k8s-image-ops.sh rollout-status ai-service
```

### 3.3 部署结果

所有目标 deployment rollout 成功，线上镜像已切换到同一 tag：

1. `query-service -> localhost:5000/logoscope/query-service:tracefix-20260303213938`
2. `semantic-engine -> localhost:5000/logoscope/semantic-engine:tracefix-20260303213938`
3. `semantic-engine-worker -> localhost:5000/logoscope/semantic-engine:tracefix-20260303213938`
4. `topology-service -> localhost:5000/logoscope/topology-service:tracefix-20260303213938`
5. `ai-service -> localhost:5000/logoscope/ai-service:tracefix-20260303213938`

---

## 4. 验证证据

### 4.1 E2E Trace 冒烟（通过）

执行：

```bash
ATTEMPTS=12 SLEEP_SECONDS=2 scripts/trace-e2e-smoke.sh
```

结果（样例）：

1. `trace_id=64eaf8b5c8f242c8bef0809ef7205ac7`
2. ingest `/v1/traces` 返回 `200`
3. ClickHouse `logs.traces` 首轮查询命中
4. query-service `/api/v1/traces`、`/spans`、`/stats` 返回正常
5. Kafka `traces.raw` 消费组 lag=`0`（与原 pending 口径等价迁移）

### 4.2 相关率检查（通过）

执行：

```bash
bash scripts/trace-correlation-check.sh --namespace islap --window-hours 1
```

最新报告：

- `reports/trace-correlation-check/latest.json`
- `run_id=trace-correlation-20260303-134226-14345`
- `generated_at=2026-03-03T13:42:31.723Z`

关键指标（1h 窗口）：

1. `logs.total=3611`
2. `logs.with_trace_id=262`
3. `logs.with_span_id=262`
4. `logs.with_trace_and_span=262`
5. `logs.with_otel_trace_and_span=112`
6. `logs.kpi_correlation_rate=0.072556`
7. `join.join_rate_trace_span=1.000000`
8. `traces.total=5578`

与发布前问题对比（历史基线）：

1. 发布前曾出现 `with_span_id=0`、`kpi_correlation_rate=0`
2. 本次发布后恢复为 `with_span_id>0` 且 `join_rate_trace_span` 达到 `100%`（候选样本内）

---

## 5. 风险与后续计划

### 5.1 当前剩余风险

1. `trace_id_source=missing` 仍占较大比例，说明仍有日志不在请求上下文或未携带传播头。
2. 覆盖率提升依赖各服务（含异步消费者/定时任务）持续接入 OTel context 与传播链路。

### 5.2 后续优化建议

1. 将 `trace-correlation-check` 纳入 release gate，设置最低阈值（如 `with_span_id` 与 `kpi_correlation_rate`）。
2. 对 `trace_id_source=missing` 做按服务 TopN 归因，逐服务消缺。
3. 背景任务链路统一手动建 span 并注入日志上下文，提升非 HTTP 路径可关联率。

---

## 6. 回滚说明

如需回滚，执行同批服务镜像回退到上一个稳定 tag，并逐个确认 rollout：

```bash
scripts/k8s-image-ops.sh set-image <service> <previous-tag>
scripts/k8s-image-ops.sh rollout-status <service>
```

本次发布过程未触发回滚。
