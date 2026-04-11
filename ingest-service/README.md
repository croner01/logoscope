# Ingest Service

Logoscope 数据摄入服务。当前仓库只保留 Go 实现，消息队列统一为 Kafka。

## 路径分层

- `cmd/ingest/`：Go 服务入口
- `internal/ingest/`：Go 业务实现（HTTP、解析、转换、Kafka 队列/WAL）
- `go.mod` / `go.sum`：Go 依赖
- `Dockerfile`：Go 运行镜像构建文件

## Go 服务能力

- 接收 OTLP 数据：
  - `POST /v1/logs`
  - `POST /v1/metrics`
  - `POST /v1/traces`
- 健康检查：
  - `GET /health`
  - `GET /ready`
- 队列观测：
  - `GET /api/v1/queue/stats`
  - `GET /metrics`

## 本地开发（Go）

```bash
cd ingest-service
go test ./...
go test -race ./...
go run ./cmd/ingest
```

## 容器构建（Go）

注意：`Dockerfile` 仅复制 Go 路径（`cmd/`、`internal/`、`go.mod`、`go.sum`）。

### 从仓库根目录构建

```bash
docker build -f ingest-service/Dockerfile -t ingest-service-go:latest .
```

### 从 `ingest-service` 目录构建

```bash
cd ingest-service
docker build -t ingest-service-go:latest .
```

`ingest-service/.dockerignore` 会在第二种构建方式下排除测试缓存等无关文件。
