# K8s 镜像构建与发布手册

本项目固定运维约束（已固化）：

- K8s 环境：服务运行在 Pod 中
- 命名空间：`islap`
- 部署清单目录：`deploy/`
- 本地镜像仓库：`localhost:5000/logoscope/`

## 1. 可构建服务与 Docker 上下文

- `semantic-engine` -> `semantic-engine/`
- `ingest-service` -> `ingest-service/`
- `query-service` -> `query-service/`
- `topology-service` -> `topology-service/`
- `frontend` -> `frontend/`

说明：

- `semantic-engine-worker` 使用 `semantic-engine` 同一镜像（`localhost:5000/logoscope/semantic-engine:<tag>`）。

## 2. 直接命令（手工执行）

可先设定变量：

```bash
export NS=islap
export REG=localhost:5000/logoscope
export TAG=latest
# 或者使用时间戳：export TAG=$(date +%Y%m%d-%H%M%S)
```

### 2.1 构建镜像

```bash
docker build -t $REG/semantic-engine:$TAG semantic-engine
docker build -t $REG/ingest-service:$TAG ingest-service
docker build -t $REG/query-service:$TAG query-service
docker build -t $REG/topology-service:$TAG topology-service
docker build -t $REG/frontend:$TAG frontend
```

### 2.2 推送镜像

```bash
docker push $REG/semantic-engine:$TAG
docker push $REG/ingest-service:$TAG
docker push $REG/query-service:$TAG
docker push $REG/topology-service:$TAG
docker push $REG/frontend:$TAG
```

### 2.3 重新应用服务（manifest）

```bash
kubectl apply -f deploy/semantic-engine.yaml
kubectl apply -f deploy/semantic-engine-worker.yaml
kubectl apply -f deploy/ingest-service.yaml
kubectl apply -f deploy/query-service.yaml
kubectl apply -f deploy/topology-service.yaml
kubectl apply -f deploy/frontend.yaml
```

### 2.4 更新 Deployment 镜像并触发滚动发布

```bash
kubectl -n $NS set image deployment/semantic-engine semantic-engine=$REG/semantic-engine:$TAG
kubectl -n $NS set image deployment/semantic-engine-worker worker=$REG/semantic-engine:$TAG
kubectl -n $NS set image deployment/ingest-service ingest-service=$REG/ingest-service:$TAG
kubectl -n $NS set image deployment/query-service query-service=$REG/query-service:$TAG
kubectl -n $NS set image deployment/topology-service topology-service=$REG/topology-service:$TAG
kubectl -n $NS set image deployment/frontend frontend=$REG/frontend:$TAG
```

说明：

- `ingest-service` 仅负责 OTLP 接入并写入 Redis Stream，不再包含 `trace-processor` 直写链路。

### 2.5 重启 Pod（滚动重启）

```bash
kubectl -n $NS rollout restart deployment/semantic-engine
kubectl -n $NS rollout restart deployment/semantic-engine-worker
kubectl -n $NS rollout restart deployment/ingest-service
kubectl -n $NS rollout restart deployment/query-service
kubectl -n $NS rollout restart deployment/topology-service
kubectl -n $NS rollout restart deployment/frontend
```

### 2.6 查看发布状态

```bash
kubectl -n $NS rollout status deployment/semantic-engine
kubectl -n $NS rollout status deployment/semantic-engine-worker
kubectl -n $NS rollout status deployment/ingest-service
kubectl -n $NS rollout status deployment/query-service
kubectl -n $NS rollout status deployment/topology-service
kubectl -n $NS rollout status deployment/frontend
```

### 2.7 镜像一致性核对（建议）

```bash
kubectl -n $NS get deploy semantic-engine semantic-engine-worker ingest-service query-service topology-service frontend \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .spec.template.spec.containers[*]}{.name}{"="}{.image}{" "}{end}{"\n"}{end}'
```

## 3. 一键脚本（推荐）

仓库已提供脚本：

- `scripts/k8s-image-ops.sh`

### 3.1 示例

```bash
# 构建并推送所有核心服务
scripts/k8s-image-ops.sh build-push all latest

# 仅执行发布门禁（trace smoke + ai contract + query contract，失败返回非0）
scripts/k8s-image-ops.sh gate all latest

# 一步发布（build/push/set-image/rollout + gate）
scripts/k8s-image-ops.sh release all latest

# 用新 tag 更新镜像
scripts/k8s-image-ops.sh set-image all latest

# 重新应用 deploy 清单
scripts/k8s-image-ops.sh apply all

# 重启并检查
scripts/k8s-image-ops.sh restart all
scripts/k8s-image-ops.sh rollout-status all
```

## 4. 建议发布流程

```bash
export TAG=$(date +%Y%m%d-%H%M%S)
scripts/k8s-image-ops.sh release all $TAG
```

说明：

- `release` 内置执行 `scripts/release-gate.sh`。
- gate 失败会阻断发布流程（命令返回非 0）。
- 每次 gate 会在 `reports/release-gate/` 写入 `*.json` + `*.smoke.log` + `*.ai.log` + `*.query.log` 报告，并尝试写入 `logs.release_gate_reports` 供 KPI 看板统计。
- gate 内置三个校验脚本：
  - `scripts/trace-e2e-smoke.sh`
  - `scripts/ai-contract-check.sh`
  - `scripts/query-contract-check.sh`

手工绕过（必须记录原因）：

```bash
scripts/release-gate.sh --candidate hotfix-2026Q2 --tag "$TAG" --target all --bypass-reason "emergency rollback window"
```

## 4.1 价值指标检查（M4-05）

```bash
# 评估价值 KPI 告警并写入一条快照（默认不因告警失败）
scripts/m4-value-kpi-check.sh

# 如需将 active alert 作为失败条件
FAIL_ON_ALERTS=true scripts/m4-value-kpi-check.sh
```

说明：

- 报告输出目录：`reports/value-kpi/*.json`
- 最新报告软链：`reports/value-kpi/latest.json`
- 脚本会调用：
  - `GET /api/v1/value/kpi/alerts`
  - `POST /api/v1/value/kpi/snapshots`

## 4.2 周期快照任务（M4-06）

```bash
# 部署 CronJob
kubectl apply -f deploy/value-kpi-cronjob.yaml

# 手工触发一次任务进行验收
JOB=value-kpi-weekly-manual-$(date +%H%M%S)
kubectl -n islap create job --from=cronjob/value-kpi-weekly "$JOB"
kubectl -n islap wait --for=condition=complete --timeout=180s job/"$JOB"
kubectl -n islap logs job/"$JOB"
```

## 4.3 CI 标准门禁入口（平台治理）

推荐在发布流水线中固定调用：

```bash
cd /root/logoscope
NAMESPACE=islap \
CANDIDATE="ci-${CI_PIPELINE_ID:-manual}" \
TAG="${CI_COMMIT_TAG:-${CI_COMMIT_SHORT_SHA:-unknown}}" \
TARGET=all \
COVERAGE_MIN_FLOOR=30 \
QUERY_COV_MIN=30 \
TOPOLOGY_COV_MIN=30 \
INGEST_COV_MIN=30 \
AI_COV_MIN=30 \
scripts/ci-release-gate.sh
```

说明：

- 脚本会调用 `scripts/release-gate.sh`，并透传覆盖率硬门槛参数。
- 产物目录：`reports/release-gate-ci/`（`latest.json` 为最新软链）。

## 4.4 每周自动提阈检查任务

建议每周一 UTC 执行一次：

```bash
cd /root/logoscope
FAIL_ON_RAISE_GAP=true \
RAISE_STEP=5 \
RAISE_BUFFER=3 \
scripts/coverage-threshold-weekly-check.sh
```

说明：

- 该脚本读取 `reports/backend-pytest/latest.json`，检查阈值是否落后于里程碑并给出提阈建议。
- 报告目录：`reports/coverage-threshold-weekly/`（`latest.json` 为最新软链）。
- 当 `FAIL_ON_RAISE_GAP=true` 且存在可提阈空间时，脚本返回非 0，可驱动 CI 创建后续提阈任务。

## 4.5 ClickHouse Release 3（性能优化）

当发布包含 ClickHouse 结构/SQL 优化（如 `release-3-performance.sql`）时，建议在应用 rollout 成功后按以下顺序执行：

```bash
# 1) 执行 ClickHouse 变更包
kubectl -n islap exec -i <clickhouse-pod> -- clickhouse-client --multiquery \
  < /root/logoscope/deploy/sql/release-3-performance.sql

# 2) 手动跑一次 trace edge 滚动聚合验证
MODE=kubectl NAMESPACE=islap CLICKHOUSE_POD=<clickhouse-pod> \
  /root/logoscope/scripts/trace-edges-rollup.sh
```

发布记录建议归档到：

- `docs/operations/clickhouse-release3-performance-2026-03-05.md`

若需持续滚动聚合，请将 `scripts/trace-edges-rollup.sh` 以分钟级 Cron 方式接入运维调度。

## 5. Trace E2E 回归脚本

脚本路径：`scripts/trace-e2e-smoke.sh`

能力覆盖：

- 注入一条 OTLP trace 到 `ingest-service`
- 验证 trace 已写入 ClickHouse `logs.traces`
- 验证 query-service API：
  - `/api/v1/traces?trace_id=...`
  - `/api/v1/traces/{trace_id}/spans`
  - `/api/v1/traces/stats`
- 校验 `traces.raw` 消费组 pending（默认 `MAX_PENDING=0`）

常用命令：

```bash
# 默认回归（严格校验 pending=0）
scripts/trace-e2e-smoke.sh

# 放宽 pending 校验（只观测，不作为失败条件）
MAX_PENDING=-1 scripts/trace-e2e-smoke.sh

# 增加轮询等待次数
ATTEMPTS=30 SLEEP_SECONDS=2 scripts/trace-e2e-smoke.sh
```
