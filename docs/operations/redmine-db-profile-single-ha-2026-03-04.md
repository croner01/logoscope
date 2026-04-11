# Redmine 发布记录：ClickHouse SINGLE/HA + Kafka 队列替换说明（2026-03-15）

> 版本: v3.23.1  
> 更新: 2026-03-15  
> 状态: 待发布 / 可直接用于 Redmine 更新

---

## 1. 目标与背景

本次交付将消息队列从 Redis 迁移为 Kafka，并保持数据库 profile 双模式能力：

1. ClickHouse 继续支持 `single`（开发）与 `ha`（生产）双 profile。
2. 队列后端统一为 Kafka（`QUEUE_BACKEND/QUEUE_TYPE=kafka`）。
3. 文档与参数口径统一为 Kafka 优先，`REDIS_*` 仅保留为历史回滚兼容说明。

---

## 2. 变更范围（交付清单）

### 2.1 部署清单（K8s Manifest）

- ClickHouse
  - `deploy/clickhouse-single.yaml`
  - `deploy/clickhouse-ha.yaml`
- Kafka
  - `deploy/kafka-single.yaml`

### 2.2 Schema 与初始化

- `deploy/clickhouse-init-single.sql`（`MergeTree*`）
- `deploy/clickhouse-init-replicated.sql`（`Replicated*MergeTree`）

### 2.3 运维脚本与入口

- `scripts/clickhouse-ha-control.sh`（ClickHouse HA 校验/同步）
- `deploy.sh`（`DB_PROFILE=single|ha` 作用于 ClickHouse）
- Kafka 当前通过 manifest 直接部署：`kubectl apply -f deploy/kafka-single.yaml`

说明：`deploy.sh` 当前未提供 `kafka` 子命令；`./deploy.sh all` 仍包含 Redis 流程，不作为 Kafka-only 推荐入口。

### 2.4 Helm 分支

- values：
  - `charts/logoscope/values.yaml`
  - `charts/logoscope/values-prod.yaml`（生产默认：`components.kafka.enabled=true`、`components.redis.enabled=false`）
- manifests：
  - `charts/logoscope/files/manifests/clickhouse-single.yaml`
  - `charts/logoscope/files/manifests/clickhouse-ha.yaml`
  - `charts/logoscope/files/manifests/kafka.yaml`

---

## 3. 组件变量参数（Redis -> Kafka）

### 3.1 参数映射（用于 Redmine 变更说明）

| 旧参数（Redis） | 新参数（Kafka） | 说明 |
| --- | --- | --- |
| `REDIS_HOST/REDIS_PORT/REDIS_DB` | `KAFKA_BROKERS` | 由 Redis 连接改为 Kafka broker 列表 |
| `REDIS_STREAM` / `REDIS_STREAM_LOGS` / `REDIS_STREAM_METRICS` / `REDIS_STREAM_TRACES` | Kafka Topics（`logs.raw` / `metrics.raw` / `traces.raw`） | Topic 名保持与原流名一致 |
| 旧的 Redis 批量大小参数 | `KAFKA_BATCH_SIZE`（生产端）/ `KAFKA_MAX_BATCH_SIZE`（消费端） | 批量策略拆分为生产与消费两侧 |
| 旧的 Redis 轮询/阻塞参数 | `KAFKA_BATCH_TIMEOUT_MS`（生产端）/ `KAFKA_POLL_TIMEOUT_MS`（消费端） | 轮询/刷盘等待迁移到 Kafka 参数 |
| `REDIS_PENDING_*` | Consumer Lag 监控口径 | 由 pending 监控转为消费组 lag 监控 |

### 3.2 Ingest Service（生产 Kafka 写入）

基线参数（来源：`deploy/ingest-service.yaml` 与 `charts/logoscope/values.yaml`）：

- `QUEUE_BACKEND=kafka`
- `KAFKA_BROKERS=kafka:9092`
- `KAFKA_REQUIRED_ACKS=all`
- `KAFKA_WRITE_MODE=async`
- `KAFKA_DIAL_TIMEOUT_SEC=3`
- `KAFKA_WRITE_TIMEOUT_SEC=5`
- `KAFKA_PING_INTERVAL_SEC=5`
- `KAFKA_RECONNECT_INTERVAL_SEC=5`
- `KAFKA_MAX_RECONNECT_ATTEMPTS=3`
- `KAFKA_BATCH_SIZE=500`
- `KAFKA_BATCH_BYTES=1048576`
- `KAFKA_BATCH_TIMEOUT_MS=20`
- `QUEUE_FLUSH_INTERVAL_MS=50`

### 3.3 Semantic Engine（消费）

基线参数（`deploy/semantic-engine.yaml`）：

- `USE_QUEUE=true`
- `QUEUE_TYPE=kafka`
- `KAFKA_BROKERS=kafka:9092`
- `KAFKA_GROUP_ID=log-workers`
- `KAFKA_CLIENT_ID=semantic-engine`
- `KAFKA_AUTO_OFFSET_RESET=earliest`
- `KAFKA_POLL_TIMEOUT_MS=1000`
- `KAFKA_MAX_BATCH_SIZE=200`
- `KAFKA_MAX_RETRY_ATTEMPTS=3`
- `KAFKA_RETRY_DELAY_SECONDS=2`

### 3.4 Semantic Engine Worker（高吞吐消费）

基线参数（`deploy/semantic-engine-worker.yaml`）：

- `QUEUE_TYPE=kafka`
- `KAFKA_BROKERS=kafka:9092`
- `KAFKA_GROUP_ID=log-workers`
- `KAFKA_CLIENT_ID=semantic-engine-worker`
- `KAFKA_AUTO_OFFSET_RESET=earliest`
- `KAFKA_POLL_TIMEOUT_MS=1000`
- `KAFKA_MAX_POLL_INTERVAL_MS=600000`
- `KAFKA_SESSION_TIMEOUT_MS=45000`
- `KAFKA_HEARTBEAT_INTERVAL_MS=3000`
- `KAFKA_GROUP_PER_STREAM=true`
- `KAFKA_CALLBACK_OFFLOAD=true`
- `KAFKA_FLUSH_OFFLOAD=true`
- `KAFKA_COMMIT_ERROR_AS_WARNING=true`
- `KAFKA_MAX_BATCH_SIZE=80`
- `KAFKA_MAX_RETRY_ATTEMPTS=5`
- `KAFKA_RETRY_DELAY_SECONDS=2`

### 3.5 Kafka Broker（集群内服务）

基线参数（`deploy/kafka-single.yaml`）：

- `KAFKA_ENABLE_KRAFT=yes`
- `KAFKA_CFG_PROCESS_ROLES=broker,controller`
- `KAFKA_CFG_NODE_ID=1`
- `KAFKA_CFG_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093`
- `KAFKA_CFG_ADVERTISED_LISTENERS=PLAINTEXT://kafka:9092`
- `KAFKA_CFG_AUTO_CREATE_TOPICS_ENABLE=true`

---

## 4. 使用步骤（可直接执行）

### 4.1 开发环境（SINGLE）

```bash
# 1) 部署 ClickHouse（单副本）
DB_PROFILE=single ./deploy.sh clickhouse

# 2) 部署 Kafka（单副本）
kubectl apply -f deploy/kafka-single.yaml
kubectl rollout status deployment/kafka -n islap --timeout=300s

# 3) 初始化 schema（自动选择 single 版本 SQL）
DB_PROFILE=single ./deploy.sh init-db

# 4) ClickHouse 一致性检查
DB_PROFILE=single NAMESPACE=islap scripts/clickhouse-ha-control.sh check
```

### 4.2 生产环境（HA）

```bash
# 1) 部署 ClickHouse（HA）
DB_PROFILE=ha ./deploy.sh clickhouse

# 2) 部署 Kafka（当前为 single broker）
kubectl apply -f deploy/kafka-single.yaml
kubectl rollout status deployment/kafka -n islap --timeout=300s

# 3) 初始化 schema（自动选择 replicated 版本 SQL）
DB_PROFILE=ha ./deploy.sh init-db

# 4) ClickHouse 一致性检查
DB_PROFILE=ha NAMESPACE=islap scripts/clickhouse-ha-control.sh check
```

### 4.3 Kafka 变量生效校验（建议纳入验收）

```bash
kubectl -n islap get deploy ingest-service \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  | rg 'QUEUE_BACKEND|KAFKA_'

kubectl -n islap get deploy semantic-engine \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  | rg 'QUEUE_TYPE|KAFKA_'

kubectl -n islap get deploy semantic-engine-worker \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  | rg 'QUEUE_TYPE|KAFKA_'
```

### 4.4 Helm 安装方式

```bash
# 开发/测试（single）
helm upgrade --install logoscope charts/logoscope -n islap --create-namespace \
  --set database.profile=single \
  --set components.kafka.enabled=true \
  --set components.redis.enabled=false

# 生产（ha，推荐使用 values-prod 默认开关）
helm upgrade --install logoscope charts/logoscope -n islap --create-namespace \
  -f charts/logoscope/values-prod.yaml
```

---

## 5. 一致性与重试策略（生产关注）

### 5.1 ClickHouse（HA）

1. Keeper 与 ClickHouse 按 StatefulSet 顺序滚动。
2. `bootstrap` 使用 `Replicated*MergeTree` 引擎建表，避免单机引擎误用。
3. `sync` 内置重试：
   - `SYNC_RETRIES`（默认 `3`）
   - `SYNC_RETRY_SLEEP`（默认 `3s`）
4. `check` 重点校验：
   - `is_readonly = 0`
   - `queue_size` 阈值
   - `absolute_delay` 阈值

### 5.2 Kafka（当前部署边界）

1. 当前 manifest 为单副本 Kafka（KRaft 单节点），用于现阶段队列承载。
2. 写入可靠性通过 `acks=all` 与重连/批量参数保障。
3. 消费稳定性通过 `KAFKA_MAX_POLL_INTERVAL_MS`、`KAFKA_SESSION_TIMEOUT_MS`、`KAFKA_HEARTBEAT_INTERVAL_MS` 控制。
4. 异步积压监控口径从 Redis pending 迁移为 Kafka consumer lag。

---

## 6. 对页面展示与数据准确性的影响评估

结论：

1. ClickHouse `single/ha` 切换不改变 API 返回结构。
2. Redis -> Kafka 迁移不改变日志/指标/链路三类原始 topic 命名语义（`logs.raw`/`metrics.raw`/`traces.raw`）。
3. 若出现队列配置不一致（例如服务仍为 `QUEUE_TYPE=redis`），会导致消费中断，需通过第 4.3 节变量校验阻断发布。

---

## 7. 生产发布最小命令清单（含回滚）

### 7.1 发布前检查

```bash
# 1) 集群与命名空间
kubectl config current-context
kubectl -n islap get pods

# 2) 核心配置检查（生产默认应为 kafka on / redis off）
rg -n "components:\\s*$|kafka:|redis:|enabled:" charts/logoscope/values-prod.yaml
```

### 7.2 生产发布（Kafka 主路径）

```bash
# 1) 使用生产覆盖文件发布
helm upgrade --install logoscope charts/logoscope -n islap --create-namespace \
  -f charts/logoscope/values-prod.yaml

# 2) 等待关键工作负载完成 rollout
kubectl -n islap rollout status deployment/kafka --timeout=300s
kubectl -n islap rollout status statefulset/clickhouse --timeout=600s
kubectl -n islap rollout status deployment/ingest-service --timeout=300s
kubectl -n islap rollout status deployment/semantic-engine --timeout=300s
kubectl -n islap rollout status deployment/semantic-engine-worker --timeout=300s
```

### 7.3 发布后验证

```bash
# 1) 队列参数校验
kubectl -n islap get deploy ingest-service \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  | rg 'QUEUE_BACKEND|KAFKA_'

kubectl -n islap get deploy semantic-engine \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  | rg 'QUEUE_TYPE|KAFKA_'

kubectl -n islap get deploy semantic-engine-worker \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  | rg 'QUEUE_TYPE|KAFKA_'

# 2) 数据层检查
DB_PROFILE=ha NAMESPACE=islap scripts/clickhouse-ha-control.sh check
```

### 7.4 一键回滚（首选）

```bash
# 1) 查看历史版本
helm history logoscope -n islap

# 2) 回滚到上一个稳定 revision
helm rollback logoscope <revision> -n islap

# 3) 回滚后确认
kubectl -n islap get pods
DB_PROFILE=ha NAMESPACE=islap scripts/clickhouse-ha-control.sh check
```

### 7.5 队列故障应急回退（Kafka -> Redis）

```bash
# 仅在 Kafka 故障且确认需要切回 Redis 时执行
helm upgrade --install logoscope charts/logoscope -n islap --create-namespace \
  -f charts/logoscope/values-prod.yaml \
  --set components.kafka.enabled=false \
  --set components.redis.enabled=true \
  --set components.redis.mode=ha
```

---

## 8. 验收标准（Redmine 可勾选）

- [ ] `DB_PROFILE=single` + Kafka 可完成部署、初始化、检查。
- [ ] `DB_PROFILE=ha` + Kafka 可完成部署、初始化、检查。
- [ ] `ingest-service/semantic-engine/semantic-engine-worker` 均生效 `KAFKA_*` 参数。
- [ ] Kafka rollout 正常，关键服务无队列连接错误日志。
- [ ] 文档与 Helm values 已同步，开发/生产可按参数切换。

---

## 9. 回滚方案

### 9.1 紧急回退到旧队列实现（仅故障处置）

```bash
# 1) 下线 Kafka
kubectl -n islap delete -f deploy/kafka-single.yaml

# 2) 回退 Redis（若使用历史回滚预案）
DB_PROFILE=single ./deploy.sh redis
```

### 9.2 Helm 回滚

```bash
helm rollback logoscope <revision> -n islap
```

### 9.3 回滚后检查

```bash
DB_PROFILE=single NAMESPACE=islap scripts/clickhouse-ha-control.sh check
```

---

## 10. Redmine 更新建议模板

可在 Redmine 任务备注中直接使用：

1. 消息队列已由 Redis 切换为 Kafka，组件参数统一到 `QUEUE_BACKEND/QUEUE_TYPE + KAFKA_*`。
2. ClickHouse 双 profile（`single/ha`）能力保持不变，部署与初始化流程已验证。
3. 文档已补充 Redis->Kafka 参数映射、组件变量基线与校验命令。
4. Helm 发布建议显式开启 Kafka 并关闭 Redis，避免混合配置造成消费异常。
