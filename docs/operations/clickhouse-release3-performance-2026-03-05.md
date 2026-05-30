# ClickHouse 性能优化发布记录（2026-03-05）

> 版本: v3.24.0  
> 更新: 2026-03-05  
> 状态: 已发布

---

## 1. 发布目标与范围

本次发布围绕 ClickHouse 查询与结构优化，目标如下：

1. 将边 RED 指标读取路径优先切换到 `logs.trace_edges_1m`，降低 `logs.traces` 自连接慢查询压力。
2. 增加 `trace_edges_1m` 增量滚动聚合链路，补齐持续刷新能力。
3. 调整 trace 列表/统计扫描阈值与 SQL 形态，减少 `LIMIT 1 BY` 高代价路径。
4. 统一日志子串检索表达式并将关键 DDL 收敛到基线。

发布服务范围（namespace=`islap`）：

1. `query-service`
2. `topology-service`
3. `clickhouse`（执行 release SQL）

---

## 2. 代码与 SQL 变更摘要

### 2.1 应用代码

- `shared_src/logoscope_storage/adapter.py`
  - `get_edge_red_metrics` 改为优先读取 `trace_edges_1m`，失败或无数据时回退 traces 自连接。
  - 新增 `trace_edges_1m` schema 探测与缓存，兼容 release-2 老结构与 release-3 扩展列。
- `query-service/api/query_observability_service.py`
  - 下调 trace 默认扫描阈值。
  - trace 列表查询由 `LIMIT 1 BY trace_id` 改为 `GROUP BY trace_id + max(trace_ts)`。
  - duration 抽样由 `LIMIT 1 BY` 改为 `argMax(trace_duration, trace_ts)`。
- `query-service/api/query_logs_service.py`
  - 子串检索统一为 `ILIKE concat('%', ..., '%')`。

### 2.2 SQL 与脚本

- 发布 SQL：
  - `deploy/sql/release-1-hotfix.sql`
  - `deploy/sql/release-2-structural.sql`（补充扩展列定义）
  - `deploy/sql/release-3-performance.sql`（本次执行）
- 增量聚合脚本：
  - `scripts/trace-edges-rollup.sh`
- 审计脚本：
  - `scripts/clickhouse-online-audit.sql`
  - `scripts/run-clickhouse-online-audit.sh`

### 2.3 基线 DDL 收敛

以下文件已同步 release 关键对象，避免新环境漂移：

1. `deploy/clickhouse-init-single.sql`
2. `deploy/clickhouse-init-replicated.sql`
3. `deploy/clickhouse-single.yaml`（内联 `init.sql`）
4. `deploy/clickhouse.yaml`（内联 `init.sql`）
5. `deploy/clickhouse-ha.yaml`（内联 `init.sql`）

---

## 3. 发布执行记录

### 3.1 镜像构建与发布

发布时间：2026-03-05

镜像 tag：

1. `query-service:20260305-r3perf`
2. `topology-service:20260305-r3perf`

执行命令：

```bash
scripts/k8s-image-ops.sh build-push query-service 20260305-r3perf
scripts/k8s-image-ops.sh build-push topology-service 20260305-r3perf

kubectl -n islap set image deployment/query-service \
  query-service=localhost:5000/logoscope/query-service:20260305-r3perf
kubectl -n islap set image deployment/topology-service \
  topology-service=localhost:5000/logoscope/topology-service:20260305-r3perf

kubectl -n islap rollout status deployment/query-service
kubectl -n islap rollout status deployment/topology-service
```

发布后 deployment 镜像确认：

1. `query-service=query-service:20260305-r3perf`
2. `topology-service=topology-service:20260305-r3perf`

### 3.2 数据库变更执行

执行命令：

```bash
kubectl -n islap exec -i clickhouse-6b4679c8bd-2g6xz \
  -- clickhouse-client --multiquery \
  < /root/logoscope/deploy/sql/release-3-performance.sql
```

核验结果：

1. `logs.trace_edges_1m` 扩展列存在（`namespace/timeout_count/retries_sum/pending_sum/dlq_sum/p95_ms/p99_ms/duration_sum_ms`）。
2. `logs.trace_edges_rollup_watermark` 表存在。
3. `logs.logs.idx_logs_message_ngram` 存在。
4. `logs.logs.proj_logs_pod_ns_time` 存在。

### 3.3 手动滚动聚合验证

执行命令：

```bash
MODE=kubectl NAMESPACE=islap CLICKHOUSE_POD=clickhouse-6b4679c8bd-2g6xz \
  ./scripts/trace-edges-rollup.sh
```

脚本输出：

1. `rolling up edge spans: [2026-03-05 02:09:00, 2026-03-05 02:29:00)`
2. `rollup done, watermark advanced to 2026-03-05 02:29:00`

watermark 校验：

1. `window_end=2026-03-05 02:29:00`
2. `updated_at=2026-03-05 03:05:04`

---

## 4. 发布后观察与说明

### 4.1 当前数据特征

本次滚动聚合脚本执行成功，但 `trace_edges_1m` 最新 minute 仍未前移，原因是当前窗口内跨服务父子 span 关系为 0：

1. `joined_all=3773`
2. `joined_cross_service=0`

当前聚合规则保留条件：`child.service_name != parent.service_name`，因此该窗口不产生新增 edge 行，行为符合设计预期。

### 4.2 风险与后续建议

1. 建议将 `scripts/trace-edges-rollup.sh` 以分钟级 Cron 形式常驻执行（或接入现有发布后定时任务）。
2. 若业务需要统计同服务内部调用边，可在变更评审后移除跨服务过滤条件。
3. 发布后继续跟踪 `system.query_log`：
   - traces 自连接模板 p95/read_rows 是否下降
   - `WITH recent_spans` 模板读放大是否下降
   - `FINAL` 占比是否持续维持低位

---

## 5. 相关材料

1. SQL 包：`deploy/sql/release-3-performance.sql`
2. 增量脚本：`scripts/trace-edges-rollup.sh`
3. 审计脚本：`scripts/run-clickhouse-online-audit.sh`
4. 审计结果目录：`reports/clickhouse-audit/`
