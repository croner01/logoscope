# Logoscope 设计实施指南

> 版本: v3.21.0  
> 更新: 2026-02-11  
> 状态: 发布

---

## 📋 目录

- [1. 部署准备](#1-部署准备)
- [2. 部署步骤](#2-部署步骤)
- [3. 配置管理](#3-配置管理)
- [4. 验证测试](#4-验证测试)
- [5. 监控运维](#5-监控运维)
- [6. 故障排查](#6-故障排查)
- [7. 优化调整](#7-优化调整)

---

## 1. 部署准备

### 1.1 环境要求

#### 硬件要求

**最小配置** (开发/测试):
```yaml
CPU: 4 cores
内存: 16 GB
磁盘: 200 GB SSD
网络: 1 Gbps
```

**推荐配置** (生产):
```yaml
CPU: 16+ cores
内存: 64+ GB
磁盘: 1 TB+ NVMe SSD
网络: 10 Gbps
```

**组件资源分配**:
| 组件 | CPU | 内存 | 存储 |
|------|------|------|------|
| Fluent Bit (每节点) | 100m | 128Mi | - |
| OTel Collector (每节点) | 200m | 300Mi | - |
| OTel Gateway | 500m | 512Mi | 10Gi |
| Semantic Engine | 500m | 1Gi | - |
| ClickHouse | 4c | 8Gi | 500Gi |
| Neo4j | 2c | 4Gi | 100Gi |
| Redis | 500m | 256Mi | 10Gi |
| 前端 | 100m | 128Mi | - |

#### 软件要求

| 软件 | 版本 | 说明 |
|------|------|------|
| **Kubernetes** | v1.24+ | 容器编排 |
| **Helm** | v3.0+ | 包管理 (可选) |
| **Docker** | v20.10+ | 容器运行时 |
| **Python** | v3.12+ | 运行时 |
| **ClickHouse** | v23.3+ | 数据库 |
| **Neo4j** | v5.0+ | 图数据库 |
| **Redis** | v7.0+ | 缓存 |

### 1.2 依赖服务

#### 外部依赖

```yaml
必需:
  - Kubernetes API Server
  - 容器镜像仓库 (Docker Registry/ Harbor)
  
可选:
  - GitOps 工具 (ArgoCD/Flux)
  - 监控系统 (Prometheus/Grafana)
  - 日志转发 (外部 SIEM)
```

### 1.3 网络规划

#### 端口分配

| 组件 | 服务端口 | 监控端口 | 协议 |
|------|----------|----------|------|
| Fluent Bit | - | 2020 | HTTP |
| OTel Collector | 4317, 4318 | 8888, 13133 | gRPC, HTTP |
| OTel Gateway | 4317, 4318 | 8888, 13133 | gRPC, HTTP |
| Semantic Engine | 8080 | 8080 | HTTP |
| ClickHouse | 8123, 9000 | 8123 | HTTP, Native |
| Neo4j | 7474, 7687 | 7474 | HTTP, Bolt |
| Redis | 6379 | 6379 | Redis |
| 前端 | 3000 | 3000 | HTTP |

#### 网络策略

```yaml
# 命名空间隔离
namespaces:
  - islap:     # 基础设施组件
  - logoscope:  # 应用组件

# 网络策略
NetworkPolicies:
  - allow-ingress-from-external:     # 允许外部访问
  - allow-ingress-from-otel:        # OTel → Semantic Engine
  - allow-egress-to-databases:      # 应用 → 数据库
```

---

## 2. 部署步骤

### 2.1 使用 Helm 部署 (推荐)

#### 2.1.1 添加仓库

```bash
# 添加 Logoscope Helm 仓库
helm repo add logoscope https://charts.logoscope.io

# 更新仓库
helm repo update
```

#### 2.1.2 创建命名空间

```bash
# 创建命名空间
kubectl create namespace islap
kubectl create namespace logoscope
```

#### 2.1.3 部署基础设施

```bash
# 部署 ClickHouse
helm install clickhouse logoscope/clickhouse \
  --namespace islap \
  --set persistence.size=500Gi \
  --set replicas=3

# 部署 Neo4j
helm install neo4j logoscope/neo4j \
  --namespace islap \
  --set persistence.size=100Gi \
  --set passwords.neo4j="your-password"

# 部署 Redis
helm install redis logoscope/redis \
  --namespace islap \
  --set persistence.size=10Gi
```

#### 2.1.4 部署采集层

```bash
# 部署 OTel Gateway
helm install otel-gateway logoscope/otel-gateway \
  --namespace islap \
  --set replicas=3

# 部署 OTel Collector (DaemonSet)
helm install otel-collector logoscope/otel-collector \
  --namespace islap \
  --set daemonSet.enabled=true

# 部署 Fluent Bit
helm install fluent-bit logoscope/fluent-bit \
  --namespace islap
```

#### 2.1.5 部署应用层

```bash
# 部署 Semantic Engine
helm install semantic-engine logoscope/semantic-engine \
  --namespace logoscope \
  --set image.tag=v3.21.0 \
  --set replicas=2 \
  --set resources.limits.memory=2Gi \
  --set env.CLICKHOUSE_HOST=clickhouse.islap.svc \
  --set env.NEO4J_HOST=neo4j.islap.svc \
  --set env.REDIS_HOST=redis.islap.svc

# 部署前端
helm install frontend logoscope/frontend \
  --namespace logoscope \
  --set image.tag=v3.21.0
```

### 2.2 使用 kubectl 部署

#### 2.2.1 准备部署清单

```bash
# 克隆仓库
git clone https://github.com/your-org/logoscope.git
cd logoscope/deploy
```

#### 2.2.2 部署顺序

```bash
# Step 1: 基础设施 (按顺序)
kubectl apply -f 00-namespace.yaml
kubectl apply -f 01-clickhouse.yaml
kubectl apply -f 02-neo4j.yaml
kubectl apply -f 03-redis.yaml

# Step 2: 采集层
kubectl apply -f 10-otel-gateway.yaml
kubectl apply -f 11-otel-collector.yaml
kubectl apply -f 12-fluent-bit.yaml

# Step 3: 应用层
kubectl apply -f 20-semantic-engine.yaml
kubectl apply -f 21-frontend.yaml

# Step 4: 网络策略
kubectl apply -f 30-network-policies.yaml
```

### 2.3 验证部署

```bash
# 检查所有 Pod 状态
kubectl get pods -A | grep logoscope

# 预期输出
# islap         clickhouse-0                    1/1     Running
# islap         neo4j-0                         1/1     Running
# islap         redis-0                         1/1     Running
# islap         otel-gateway-0                  1/1     Running
# islap         otel-collector-xxx              1/1     Running
# logoscope      semantic-engine-xxx             2/2     Running
# logoscope      frontend-xxx                     1/1     Running

# 检查 PVC 绑定
kubectl get pvc -n islap

# 检查服务端点
kubectl get svc -A
```

---

## 3. 配置管理

### 3.1 环境变量配置

#### Semantic Engine 核心配置

```yaml
# deployment/semantic-engine.yaml
env:
  # 应用配置
  - name: APP_NAME
    value: "semantic-engine"
  - name: APP_VERSION
    value: "v3.21.0"
  - name: HOST
    value: "0.0.0.0"
  - name: PORT
    value: "8080"
  
  # ClickHouse 配置
  - name: CLICKHOUSE_HOST
    value: "clickhouse.islap.svc"
  - name: CLICKHOUSE_PORT
    value: "9000"
  - name: CLICKHOUSE_DATABASE
    value: "logs"
  - name: CLICKHOUSE_USER
    value: "default"
  - name: CLICKHOUSE_PASSWORD
    valueFrom:
      secretKeyRef:
        name: clickhouse-credentials
        key: password
  
  # Neo4j 配置
  - name: NEO4J_HOST
    value: "neo4j.islap.svc"
  - name: NEO4J_PORT
    value: "7687"
  - name: NEO4J_USER
    value: "neo4j"
  - name: NEO4J_PASSWORD
    valueFrom:
      secretKeyRef:
        name: neo4j-credentials
        key: password
  - name: NEO4J_DATABASE
    value: "neo4j"
  
  # Redis 配置
  - name: REDIS_HOST
    value: "redis.islap.svc"
  - name: REDIS_PORT
    value: "6379"
  - name: REDIS_DB
    value: "0"
  
  # OpenTelemetry 配置
  - name: OTEL_SERVICE_NAME
    value: "semantic-engine"
  - name: OTEL_TRACES_EXPORTER
    value: "otlp"
  - name: OTEL_METRICS_EXPORTER          # ⭐ P0修复: 启用 Metrics
    value: "otlp"
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "http://otel-gateway.islap.svc:4318"
  - name: OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED
    value: "true"
  
  # 队列配置
  - name: USE_QUEUE
    value: "true"
  - name: QUEUE_TYPE
    value: "redis"
  - name: PROCESS_BATCH_SIZE
    value: "10"
  - name: PROCESS_TIMEOUT
    value: "30"
```

#### OTel Collector 配置

```yaml
# otel-collector ConfigMap
config: |
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318
  
  processors:
    batch:
      timeout: 5s
      send_batch_size: 1000        # ⭐ P0优化: 减小批量
      send_batch_max_size: 1200
    
    memory_limiter:
      limit_mib: 512
      spike_limit_mib: 128
      check_interval: 5s
  
  exporters:
    logging:
      loglevel: info
    
    otlp:
      endpoint: otel-gateway.islap.svc:4317
      tls:
        insecure: true
  
  service:
    pipelines:
      logs:
        receivers: [otlp]
        processors: [batch, memory_limiter, attributes]
        exporters: [logging, otlp]
      
      traces:
        receivers: [otlp]
        processors: [batch, memory_limiter, attributes]
        exporters: [logging, otlp]
      
      metrics:                     # ⭐ 关键: 添加 Metrics
        receivers: [otlp]
        processors: [batch, memory_limiter, attributes]
        exporters: [logging, otlp]
  
  extensions:
    health_check:
      endpoint: 0.0.0.0:13133
```

### 3.2 ConfigMap 配置

#### Fluent Bit ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: fluent-bit-config
  namespace: islap
data:
  fluent-bit.conf: |
    [SERVICE]
        Flush         1
        Daemon        off
        Log_Level     info
    
    [INPUT]
        Name              tail
        Path              /var/log/containers/*.log
        Parser            docker
        Tag               kube.*
        Refresh_Interval  5
        Mem_Buf_Limit    5MB
        Skip_Long_Lines   On
    
    [FILTER]
        Name                nest_parser
        Match               kube.*
        Parser              open_telemetry
    
    [OUTPUT]
        Name                otlp
        Match               kube.*
        Host                otel-collector.islap.svc
        Port                4318
        Protocol            http
        Batch              1000
        Compression        none
```

### 3.3 Secret 配置

```yaml
# 创建 ClickHouse Secret
kubectl create secret generic clickhouse-credentials \
  --from-literal=password='your-clickhouse-password' \
  -n islap

# 创建 Neo4j Secret
kubectl create secret generic neo4j-credentials \
  --from-literal=password='your-neo4j-password' \
  -n islap

# 创建 API Keys
kubectl create secret generic api-secrets \
  --from-literal=jwt-secret='your-jwt-secret' \
  --from-literal=encryption-key='your-encryption-key' \
  -n logoscope
```

---

## 4. 验证测试

### 4.1 健康检查

```bash
# Semantic Engine 健康检查
curl http://semantic-engine.logoscope.svc:8080/health

# 预期响应
{
  "status": "healthy",
  "version": "v3.21.0",
  "components": {
    "clickhouse": "ok",
    "neo4j": "ok",
    "redis": "ok"
  },
  "timestamp": "2026-02-11T10:00:00Z"
}

# OTel Collector 健康检查
curl http://otel-collector.islap.svc:13133/health

# ClickHouse 健康检查
curl http://clickhouse.islap.svc:8123/ping

# Neo4j 健康检查
curl http://neo4j.islap.svc:7474
```

### 4.2 数据流验证

#### 4.2.1 日志采集验证

```bash
# 生成测试日志
kubectl run test-logger --image=busybox --restart=Never -n logoscope -- \
  sh -c 'for i in {1..100}; do echo "Test log message $i" | tee /var/log/app.log; sleep 1; done'

# 检查 Fluent Bit 日志
kubectl logs -n islap -l app=fluent-bit --tail=50

# 检查 OTel Collector 日志
kubectl logs -n islap -l app=opentelemetry,component=otel-collector --tail=50 | grep LogsExporter
```

#### 4.2.2 数据写入验证

```bash
# ClickHouse 验证
curl -s "http://clickhouse.islap.svc:8123/?database=logs&query=SELECT+COUNT(*)+FROM+logs+FORMAT+JSON"

# 预期输出 (5分钟后)
# {"data":[{"count()":"100"}]}

# 验证最新记录
curl -s "http://clickhouse.islap.svc:8123/?database=logs&query=SELECT+*+FROM+logs+ORDER+BY+timestamp+DESC+LIMIT+1+FORMAT+JSON"
```

#### 4.2.3 拓扑生成验证

```bash
# 调用拓扑 API
curl http://semantic-engine.logoscope.svc:8080/api/v1/topology/hybrid?time_window=1+HOUR

# 预期响应
{
  "status": "success",
  "data": {
    "nodes": [...],
    "edges": [...]
  }
}

# 验证 Neo4j 数据
kubectl exec -it neo4j-0 -n islap -- cypher-shell -u neo4j -p password \
  "MATCH (n:Service) RETURN count(n) as service_count"
```

### 4.3 API 功能测试

```bash
# 运行集成测试套件
cd /root/logoscope
pytest tests/integration/ -v

# 或手动测试
# 1. 日志查询
curl "http://localhost:8080/api/v1/logs?limit=10&start_time=2026-02-11T00:00:00Z"

# 2. 分布式追踪
curl "http://localhost:8080/api/v1/traces?limit=10"

# 3. 服务拓扑
curl "http://localhost:8080/api/v1/topology/hybrid"

# 4. AI 分析
curl -X POST "http://localhost:8080/api/v1/ai/analyze-logs" \
  -H "Content-Type: application/json" \
  -d '{"logs": ["Error: Connection timeout", "Error: Database dead lock"], "analysis_type": "pattern"}'
```

### 4.4 性能基准测试

#### 4.4.1 写入性能

```bash
# 使用 log-generator 工具
kubectl run log-generator --image=logoscope/log-generator:v3.21.0 \
  --restart=Never -n logoscope \
  -- --logs-per-second=100 \
  -- --duration=60 \
  -- --target-otel=http://otel-collector.islap.svc:4317

# 预期结果
# 生成 6000 条日志 (100 logs/s * 60s)
# 验证 ClickHouse 中的数据量
```

#### 4.4.2 查询性能

```bash
# 安装 ClickHouse Benchmarker
# clickhouse-benchmark 工具或自定义脚本

# 测试查询性能
for i in {1..100}; do
  start=$(date +%s%N)
  curl -s "http://localhost:8080/api/v1/logs?limit=100&service=semantic-engine" > /dev/null
  end=$(date +%s%N)
  echo "$((end-start)) ms"
done

# 预期结果
# P50 < 50ms, P95 < 200ms
```

---

## 5. 监控运维

### 5.1 Prometheus 监控

#### 关键指标

**应用指标**:
```yaml
# Semantic Engine
semantic_engine_requests_total          # 总请求数
semantic_engine_request_duration_ms      # 请求延迟
semantic_engine_logs_ingested_total     # 日志摄入量
semantic_engine_errors_total            # 错误计数

# OTel Collector
otelcol_receiver_accepted_spans        # 接收的 span 数
otelcol_exporter_sent_spans           # 发送的 span 数
```

**数据库指标**:
```yaml
# ClickHouse
clickhouse_rows_read                 # 读取行数
clickhouse_rows_written               # 写入行数
clickhouse_query_duration_ms          # 查询延迟
clickhouse_memory_usage               # 内存使用

# Neo4j
neo4j_database_pool_size             # 连接池大小
neo4j_store_size_total              # 存储大小
neo4j_transaction_count             # 事务计数
```

#### Grafana Dashboard

导入预配置的 Dashboard:
- `dashboards/logoscope-overview.json`     # 系统概览
- `dashboards/clickhouse-performance.json`   # ClickHouse 性能
- `dashboards/neo4j-performance.json`      # Neo4j 性能

### 5.2 日志聚合

#### Fluent Bit 日志配置

```yaml
# 确保组件日志也发送到 OTel
[INPUT]
    Name              systemd
        Tag               host.*
        Read_From_Tail   true

[OUTPUT]
        Name                otlp
        Match               host.*
        Host                otel-collector.islap.svc
```

#### 日志级别配置

| 组件 | 日志级别 | 说明 |
|------|----------|------|
| Fluent Bit | info | 生产环境 |
| OTel Collector | info | 包含详细的处理日志 |
| Semantic Engine | info | 生产环境，调试用 debug |

### 5.3 告警配置

#### Prometheus AlertManager

```yaml
# alerts/logoscope-alerts.yaml
groups:
  - name: logoscope
    interval: 30s
    rules:
      # 日志摄入速率下降
      - alert: LowLogIngestRate
        expr: rate(semantic_engine_logs_ingested_total[5m]) < 100
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "日志摄入速率过低"
      
      # 查询延迟过高
      - alert: HighQueryLatency
        expr: histogram_quantile(0.95, semantic_engine_request_duration_ms) > 500
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "查询延迟过高 (P95 > 500ms)"
      
      # ClickHouse 写入失败
      - alert: ClickHouseWriteFailures
        expr: rate(clickhouse_write_failed_total[5m]) > 0.01
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "ClickHouse 写入失败率 > 1%"
```

---

## 6. 故障排查

### 6.1 常见问题

#### 问题 1: 日志未写入 ClickHouse

**症状**:
- 应用产生日志，但 ClickHouse logs 表无新数据

**诊断步骤**:
```bash
# 1. 检查 Fluent Bit 运行状态
kubectl get pods -n islap -l app=fluent-bit

# 2. 检查 OTel Collector 接收
kubectl logs -n islap otel-collector-xxx --tail=100 | grep LogsExporter

# 3. 检查 Semantic Engine 日志
kubectl logs -n logoscope semantic-engine-xxx --tail=100

# 4. 测试 ClickHouse 连接
kubectl exec -it semantic-engine-xxx -n logoscope -- \
  curl http://clickhouse.islap.svc:8123/ping
```

**解决方案**:
```yaml
可能原因:
  1. Fluent Bit 未配置正确的输出
  解决: 检查 fluent-bit.conf OUTPUT 配置
  
  2. OTel Collector Pipeline 未配置
  解决: 检查 otel-collector ConfigMap
  
  3. Semantic Engine ClickHouse 连接失败
  解决: 检查 CLICKHOUSE_HOST/PORT 环境变量
  
  4. ClickHouse 写入权限问题
  解决: 检查用户权限和表权限
```

#### 问题 2: Metrics 数据未写入

**症状**:
- metrics 表数据停滞 (最新数据是几天前)

**诊断步骤**:
```bash
# 1. 确认 Metrics Pipeline 已配置
kubectl get cm -n islap otel-collector-gateway-mode -o yaml | grep -A 5 "metrics:"

# 2. 确认应用已启用 Metrics 导出
kubectl get deployment -n islap semantic-engine -o yaml | grep OTEL_METRICS

# 3. 检查 OTel Collector 是否接收 Metrics
kubectl logs -n islap -l app=opentelemetry,component=otel-collector --tail=100 | grep -i metric
```

**解决方案**:
```bash
# 应用 Metrics 启用脚本
/root/logoscope/scripts/enable-metrics.sh

# 手动修复: 在应用 deployment 中添加
- name: OTEL_METRICS_EXPORTER
  value: otlp
- name: OTEL_METRICS_EXPORTER_PERIOD
  value: "60"
```

#### 问题 3: 拓扑节点缺失

**症状**:
- Neo4j 只有 7 个节点，但实际有 28+ 个服务

**解决方案**:
```bash
# 运行服务同步脚本
cd /root/logoscope/semantic-engine/graph
python3 service_sync.py

# 验证同步结果
kubectl exec -it neo4j-0 -n islap -- cypher-shell -u neo4j -p password \
  "MATCH (n:Service) RETURN count(n)"
```

#### 问题 4: Redis 连接拒绝

**症状**:
- Semantic Engine 无法连接 Redis
- 错误: "Error 111 connecting to ... :6379"

**解决方案**:
```bash
# 1. 检查 Redis Pod 状态
kubectl get pods -n islap -l app=redis

# 2. 检查 Redis Service
kubectl get svc -n islap redis

# 3. 测试 Redis 连接
kubectl exec -it semantic-engine-xxx -n logoscope -- \
  redis-cli -h redis.islap.svc -p 6379 PING

# 4. 检查 Redis 绑定配置
kubectl exec -it redis-0 -n islap -- \
  redis-cli CONFIG GET bind
# 应该返回 "0.0.0.0" 而非 "127.0.0.1"
```

### 6.2 调试技巧

#### 启用调试日志

```yaml
# Semantic Engine
env:
  - name: LOG_LEVEL
    value: "debug"

# OTel Collector
config:
  service:
    telemetry:
      logs:
        level: debug
```

#### 实时监控

```bash
# 实时日志流
stern -n logoscope semantic-engine

# 实时资源监控
kubectl top pods -n logoscope -l app=semantic-engine

# 网络流量
kubectl port-forward -n logoscope svc/semantic-engine 8080:8080
```

---

## 7. 优化调整

### 7.1 性能优化

#### P0: 批处理大小调整

**当前**: send_batch_size = 10000 (可能导致 gRPC ResourceExhausted)

**优化**: send_batch_size = 1000

**验证**: 无 ResourceExhausted 错误，日志延迟正常

#### P1: ClickHouse 分区优化

**当前**: PARTITION BY toYYYYMM(timestamp)  # 按月分区

**优化**: PARTITION BY toYYYYMMDD(timestamp)  # 按日分区

**收益**: 查询性能提升 30-50%

**实施**: 需要维护窗口，数据迁移

```sql
-- 创建优化后的新表
CREATE TABLE logs.logs_optimized (...) 
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, service_name, level);

-- 迁移数据
INSERT INTO logs.logs_optimized 
SELECT * FROM logs.logs 
WHERE timestamp >= now() - INTERVAL 30 DAY;

-- 交换表
RENAME TABLE logs.logs TO logs_old;
RENAME TABLE logs.logs_optimized TO logs.logs;
```

#### P2: 拓扑缓存策略

**当前**: 每次查询都重新计算

**优化**: Redis 缓存拓扑结果 (60秒 TTL)

**收益**: API 响应时间降低 80%

### 7.2 可靠性优化

#### 数据保留策略

```sql
-- 设置 TTL 自动清理
ALTER TABLE logs.logs 
MODIFY TTL timestamp + INTERVAL 30 DAY;

ALTER TABLE logs.traces 
MODIFY TTL timestamp + INTERVAL 30 DAY;

ALTER TABLE logs.events 
MODIFY TTL timestamp + INTERVAL 7 DAY;

ALTER TABLE logs.metrics 
MODIFY TTL timestamp + INTERVAL 3 DAY;
```

#### 备份策略

```bash
# 每日自动备份
# 添加到 crontab
0 2 * * * /root/logoscope/scripts/backup-databases.sh

# 验证备份
ls -lh /data/backups/logoscope/
```

### 7.3 扩缩容策略

#### 水平扩缩容

```yaml
# Semantic Engine HPA
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: semantic-engine-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: semantic-engine
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 80
```

#### 垂直扩缩容

```yaml
# 资源限制调整
resources:
  limits:
    cpu: "2000m"     # 从 500m 增加到 2000m
    memory: "4Gi"     # 从 1Gi 增加到 4Gi
  requests:
    cpu: "1000m"
    memory: "2Gi"
```

---

## 附录

### A. 部署检查清单

- [ ] 环境要求满足 (CPU, 内存, 磁盘)
- [ ] Kubernetes 版本 >= 1.24
- [ ] 所有必需端口已开放
- [ ] 存储类 (StorageClass) 已配置
- [ ] Secret 已创建 (数据库密码, API 密钥)
- [ ] 命名空间已创建
- [ ] 基础设施组件已部署 (ClickHouse, Neo4j, Redis)
- [ ] 采集层已部署 (Fluent Bit, OTel Collector/Gateway)
- [ ] 应用层已部署 (Semantic Engine, 前端)
- [ ] 网络策略已应用
- [ ] 健康检查全部通过
- [ ] 数据流验证通过 (日志 → ClickHouse)
- [ ] 拓扑生成验证通过
- [ ] API 功能测试通过
- [ ] 监控已配置 (Prometheus, Grafana)
- [ ] 告警已配置 (AlertManager)
- [ ] 备份任务已调度
- [ ] 文档已更新

### B. 相关文档

- [系统设计文档](SYSTEM_DESIGN.md)
- [API 参考手册](../api/reference.md)
- [架构文档](../architecture/)
- [开发指南](../development/)
- [运维手册](../operations/)

### C. 联系方式

- **项目**: https://github.com/your-org/logoscope
- **文档**: https://docs.logoscope.io
- **问题**: https://github.com/your-org/logoscope/issues

---

**文档版本**: v3.21.0  
**最后更新**: 2026-02-11  
**维护者**: Semantic Engine Team
