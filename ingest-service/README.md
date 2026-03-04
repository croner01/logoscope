# Ingest Service

Logoscope 数据摄入服务 - 专注于 OpenTelemetry OTLP 协议数据接收。

## 职责

- ✅ 接收 OTLP 日志数据 (`/v1/logs`)
- ✅ 接收 OTLP 指标数据 (`/v1/metrics`)
- ✅ 接收 OTLP 追踪数据 (`/v1/traces`)
- ✅ 数据验证和格式化
- ✅ 写入 Redis Stream (`logs.raw`)
- ✅ 健康检查 (`/health`)

## 技术栈

- **框架**: FastAPI 0.104
- **服务器**: Uvicorn
- **队列**: Redis Stream
- **Python**: 3.11

## 快速开始

### 本地开发

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
python main.py
```

### 容器化

```bash
# 构建镜像
docker build -t ingest-service:1.0.0 .

# 运行容器
docker run -p 8080:8080 \
  -e REDIS_HOST=redis \
  -e REDIS_PORT=6379 \
  ingest-service:1.0.0
```

### K8s 部署

```bash
# 部署服务
kubectl apply -f deploy/ingest-service.yaml

# 查看状态
kubectl get pods -n islap -l app=ingest-service

# 查看日志
kubectl logs -n islap -l app=ingest-service --tail 100
```

## API 端点

| 端点 | 方法 | 功能 |
|------|------|------|
| /v1/logs | POST | 接收 OTLP 日志 |
| /v1/metrics | POST | 接收 OTLP 指标 |
| /v1/traces | POST | 接收 OTLP 追踪 |
| /health | GET | 健康检查 |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|----------|------|
| APP_NAME | ingest-service | 应用名称 |
| APP_VERSION | 1.0.0 | 应用版本 |
| HOST | 0.0.0.0 | 监听地址 |
| PORT | 8080 | 监听端口 |
| REDIS_HOST | 10.43.13.32 | Redis 主机 |
| REDIS_PORT | 6379 | Redis 端口 |
| REDIS_DB | 0 | Redis 数据库 |
| REDIS_STREAM | logs.raw | Redis Stream 名称 |
| BATCH_SIZE | 100 | 批处理大小 |
| BATCH_TIMEOUT | 5 | 批处理超时（秒） |
| LOG_LEVEL | info | 日志级别 |

## 测试

```bash
# 运行单元测试
pytest tests/

# 运行特定测试
pytest tests/test_ingest.py

# 生成覆盖率报告
pytest --cov=. tests/
```

## 监控

### 健康检查

```bash
curl http://ingest-service:8080/health
```

### 指标

服务会通过 OpenTelemetry 导出指标：
- `http_requests_total` - 总请求数
- `http_request_duration_seconds` - 请求延迟
- `ingested_logs_total` - 接收日志总数
- `ingested_metrics_total` - 接收指标总数
- `ingested_traces_total` - 接收追踪总数
