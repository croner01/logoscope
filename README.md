# Logoscope

Logoscope 是一个基于 Kubernetes 的可观测性平台，提供日志采集、处理、存储、AI 分析和拓扑构建功能。

## 架构

```
Fluent Bit → OTel Collector → Ingest Service → Semantic Engine → Query Service/Topology Service → Frontend
                                          ↘
                                           AI Service
                                                         ↓
                                                    ClickHouse/Neo4j
```

### 组件

- **Fluent Bit**: 边缘采集层，负责日志采集和轻量字段补充
- **OTel Collector**: 管道层，负责数据路由和基础处理
- **Ingest Service**: 数据摄入服务，接收 OTLP 数据并写入队列
- **Semantic Engine**: 核心语义组件，负责告警管理、标签发现与语义治理能力
- **AI Service**: AI 分析服务，负责 LLM 分析、会话历史、案例库与 follow-up
- **Query Service**: 查询服务，提供日志、事件、追踪查询 API
- **Topology Service**: 拓扑服务，提供拓扑查询和管理 API
- **Frontend**: React + TypeScript + Vite 仪表板
- **ClickHouse**: 时序数据库，存储日志、事件、追踪和指标
- **Neo4j**: 图数据库，存储服务图、依赖图和拓扑
- **Redis**: 缓存和消息队列

## 快速开始

### 前置要求

- Kubernetes 1.20+
- kubectl 配置正确
- 至少 10GB 可用存储空间
- K3s（推荐）或其他 Kubernetes 发行版

### 部署

1. 克隆仓库：
```bash
git clone https://github.com/your-org/logoscope.git
cd logoscope
```

2. 运行部署脚本：
```bash
chmod +x deploy.sh
./deploy.sh all
```

3. 初始化数据库：
```bash
./deploy.sh init-db
```

4. 验证部署：
```bash
./deploy.sh status
./deploy.sh health
```

### 镜像构建与发布（K8s）

固定约束：

- 命名空间：`islap`
- 镜像仓库：`localhost:5000/logoscope/`
- 部署清单：`deploy/`

推荐使用一键脚本：

```bash
# 构建并推送全部核心服务
scripts/k8s-image-ops.sh build-push all latest

# 更新 Deployment 镜像并滚动发布
scripts/k8s-image-ops.sh set-image all latest
scripts/k8s-image-ops.sh rollout-status all
```

完整手册见：

- `docs/k8s-image-ops.md`
- `docs/operations/int03-release-notes-2026-02-27.md`（本轮迭代发布说明与验证结果）

### 手动部署

如果需要手动部署，请按照以下顺序执行：

1. 创建命名空间：
```bash
kubectl apply -f deploy/namespace.yaml
```

2. 部署基础设施：
```bash
kubectl apply -f deploy/clickhouse.yaml
kubectl apply -f deploy/neo4j.yaml
kubectl apply -f deploy/redis.yaml
```

3. 部署核心服务：
```bash
kubectl apply -f deploy/ingest-service.yaml
kubectl apply -f deploy/semantic-engine.yaml
kubectl apply -f deploy/ai-service.yaml
kubectl apply -f deploy/semantic-engine-worker.yaml
kubectl apply -f deploy/query-service.yaml
kubectl apply -f deploy/topology-service.yaml
```

4. 部署采集组件：
```bash
kubectl apply -f deploy/otel-collector.yaml
kubectl apply -f deploy/otel-gateway.yaml
kubectl apply -f deploy/fluent-bit.yaml
```

5. 部署前端：
```bash
kubectl apply -f deploy/frontend.yaml
```

## 使用

### 查看状态
```bash
./deploy.sh status
```

### 健康检查
```bash
./deploy.sh health
```

### 查看日志
```bash
# 查看所有组件日志
./deploy.sh logs semantic-engine
./deploy.sh logs query-service
./deploy.sh logs frontend

# 查看指定行数的日志
./deploy.sh logs semantic-engine 200
```

### 重启组件
```bash
./deploy.sh restart semantic-engine
./deploy.sh restart frontend
```

### 访问服务
```bash
# Frontend
kubectl port-forward -n islap svc/logoscope-frontend 3000:80

# Query Service
kubectl port-forward -n islap svc/query-service 8081:8080

# Topology Service
kubectl port-forward -n islap svc/topology-service 8082:8080

# Semantic Engine
kubectl port-forward -n islap svc/semantic-engine 8080:8080

# AI Service
kubectl port-forward -n islap svc/ai-service 8090:8090

# ClickHouse
kubectl port-forward -n islap svc/clickhouse 8123:8123

# Neo4j
kubectl port-forward -n islap svc/neo4j 7474:7474
```

## API 端点

### Query Service
- `GET /health` - 健康检查
- `GET /api/v1/logs` - 获取日志列表
- `GET /api/v1/logs/context` - 获取日志上下文
- `GET /api/v1/events` - 获取事件列表
- `GET /api/v1/traces` - 获取追踪列表

### Topology Service
- `GET /health` - 健康检查
- `GET /api/v1/topology/hybrid` - 获取混合拓扑
- `GET /api/v1/topology/enhanced` - 获取增强拓扑
- `GET /api/v1/topology/stats` - 获取拓扑统计
- `GET /api/v1/monitor/topology` - 获取监控拓扑
- `GET /api/v1/topology/snapshots` - 获取拓扑快照列表
- `POST /api/v1/topology/snapshots` - 创建拓扑快照
- `WS /ws/topology` - WebSocket 实时拓扑（双向）
- `WS /api/v1/topology/subscribe` - WebSocket 拓扑订阅（被动推送）

### Ingest Service
- `GET /health` - 健康检查
- `POST /api/v1/ingest` - 接收 OTLP 日志数据

### Semantic Engine
- `GET /health` - 健康检查
- `GET /api/v1/cache/stats` - 缓存统计
- `DELETE /api/v1/cache` - 清理缓存（推荐）
- `POST /api/v1/cache/clear` - 清理缓存（兼容）
- `GET /api/v1/deduplication/stats` - 去重统计
- `POST /api/v1/deduplication/clear-cache` - 清理去重缓存

### AI Service
- `GET /health` - 健康检查
- `GET /api/v1/ai/health` - AI 分析健康检查
- `POST /api/v1/ai/analyze-log` - 日志分析
- `POST /api/v1/ai/analyze-trace` - Trace 分析（使用 `trace_id`）
- `POST /api/v1/ai/analyze-trace-llm` - Trace LLM 分析（使用 `trace_id`）

## 配置

### 环境变量

各服务支持以下环境变量（详见各服务的 config.py）：

**通用配置：**
- `APP_NAME`: 应用名称
- `APP_VERSION`: 应用版本
- `HOST`: 监听地址
- `PORT`: 监听端口
- `DEBUG`: 调试模式
- `LOG_LEVEL`: 日志级别

**ClickHouse 配置：**
- `CLICKHOUSE_HOST`: ClickHouse 主机
- `CLICKHOUSE_PORT`: ClickHouse 端口
- `CLICKHOUSE_DATABASE`: ClickHouse 数据库
- `CLICKHOUSE_USER`: ClickHouse 用户
- `CLICKHOUSE_PASSWORD`: ClickHouse 密码

**Neo4j 配置：**
- `NEO4J_HOST`: Neo4j 主机
- `NEO4J_PORT`: Neo4j 端口
- `NEO4J_USER`: Neo4j 用户
- `NEO4J_PASSWORD`: Neo4j 密码
- `NEO4J_DATABASE`: Neo4j 数据库

**Redis 配置：**
- `REDIS_HOST`: Redis 主机
- `REDIS_PORT`: Redis 端口
- `REDIS_PASSWORD`: Redis 密码

## 故障排除

### 使用 deploy.sh 工具
```bash
# 查看状态
./deploy.sh status

# 健康检查
./deploy.sh health

# 查看日志
./deploy.sh logs <component>

# 重启组件
./deploy.sh restart <component>
```

### Pod 无法启动
1. 检查 Pod 状态：
```bash
kubectl describe pod <pod-name> -n islap
```

2. 查看 Pod 日志：
```bash
kubectl logs <pod-name> -n islap
```

3. 检查资源限制：
```bash
kubectl get pod <pod-name> -n islap -o yaml | grep -A 10 resources
```

### 没有日志数据
1. 检查 Fluent Bit 状态：
```bash
./deploy.sh logs fluent-bit
```

2. 检查 OTel Collector 状态：
```bash
./deploy.sh logs otel-collector
./deploy.sh logs otel-gateway
```

3. 检查 Ingest Service：
```bash
./deploy.sh logs ingest-service
```

### 存储问题
1. 检查 PVC 状态：
```bash
kubectl get pvc -n islap
```

2. 检查存储容量：
```bash
kubectl exec -n islap -it <clickhouse-pod> -- df -h
kubectl exec -n islap -it <neo4j-pod> -- df -h
```

## 卸载

```bash
# 使用 deploy.sh 清理（如果支持）
./deploy.sh clean clickhouse
./deploy.sh clean neo4j
./deploy.sh clean redis

# 或者手动删除
kubectl delete -f deploy/frontend.yaml
kubectl delete -f deploy/fluent-bit.yaml
kubectl delete -f deploy/otel-collector.yaml
kubectl delete -f deploy/otel-gateway.yaml
kubectl delete -f deploy/topology-service.yaml
kubectl delete -f deploy/query-service.yaml
kubectl delete -f deploy/semantic-engine-worker.yaml
kubectl delete -f deploy/semantic-engine.yaml
kubectl delete -f deploy/ingest-service.yaml
kubectl delete -f deploy/redis.yaml
kubectl delete -f deploy/neo4j.yaml
kubectl delete -f deploy/clickhouse.yaml
kubectl delete -f deploy/namespace.yaml
```

## 开发

### 前端开发
```bash
cd frontend
npm install
npm run dev
```

### 后端服务开发
```bash
# Semantic Engine
cd semantic-engine
pip install -r requirements.txt
python main.py

# Ingest Service
cd ingest-service
pip install -r requirements.txt
python main.py

# Query Service
cd query-service
pip install -r requirements.txt
python main.py

# Topology Service
cd topology-service
pip install -r requirements.txt
python main.py
```

### 测试
```bash
# 运行所有测试
cd semantic-engine && pytest

# 运行特定测试
cd semantic-engine && pytest tests/test_normalizer.py
```

## 贡献

欢迎贡献！请提交 Pull Request。

## 许可证

MIT License
