# logoscope Helm Chart

## 打包

```bash
docker run --rm -v $(pwd):/work -w /work alpine/helm:3.15.4 lint charts/logoscope
docker run --rm -v $(pwd):/work -w /work alpine/helm:3.15.4 package charts/logoscope -d dist/helm
```

## 安装

```bash
helm upgrade --install logoscope charts/logoscope -n islap --create-namespace
```

## 与 Grafana 官方 chart 对齐的参数风格

本 chart 参数结构对齐了 Grafana 官方 chart 常用组织方式（`image` / `service` / `ingress` / `env` / `autoscaling`）：
- 全局：`global.*`
- 组件：`components.<name>.*`
- 组件镜像：`components.<name>.image`（优先）+ `images.<name>`（默认）
- 组件环境变量：`components.<name>.env`
- frontend 暴露：`components.frontend.service` + `components.frontend.ingress`
- 清单渲染：全部内联在 `templates/*.yaml`，不再依赖 `files/manifests/*.yaml`

## `values.yaml` 参数说明与建议值

### 顶层参数

| 参数 | 说明 | 建议值（开发） | 建议值（生产） |
|---|---|---|---|
| `namespace.nameOverride` / `ns` | 部署命名空间覆盖 | `dev-obsv`（按环境隔离） | `islap` 或平台规范命名 |
| `global.imageRegistry` | 全局镜像仓库前缀 | 内网测试仓库 | 生产制品仓库（不可用 `latest`） |
| `global.imageTag` | 全局镜像 tag 兜底 | 可空（组件单独设） | 建议固定版本号 |
| `global.storageClass` | 全局存储类兜底 | `local-path` / 环境默认 | `fast-ssd` / 平台标准存储类 |
| `global.nodeSelector` | 全局节点调度 | 可空 | 建议按节点池隔离 |
| `deployment.mode` | 全局部署模式（影响 clickhouse/redis） | `single` | `ha` |

### 组件通用参数（`components.<name>.*`）

| 参数 | 说明 | 建议值 |
|---|---|---|
| `enabled` | 组件开关 | 生产默认 `true`，仅在明确裁剪时关闭 |
| `image.registry/repository/tag/full` | 组件镜像覆盖 | 生产建议固定 `tag`，避免 `latest` |
| `nodeSelector` | 组件节点调度 | 有性能/隔离需求的服务单独配置 |
| `replicaCount` | 工作负载副本数 | API 类服务建议 `>=2`（生产） |
| `hpa.minReplicas/maxReplicas` | HPA 边界 | `min` 与 `replicaCount` 保持一致，`max` 根据容量上限设定 |
| `storageClass` | 持久化组件存储类 | DB 组件明确指定，避免依赖默认类 |
| `env` | 组件业务 env 覆盖 | 仅覆盖业务调优项，不覆盖基础运行常量 |

### frontend 专属参数

| 参数 | 说明 | 建议值（生产） |
|---|---|---|
| `components.frontend.service.type` | Service 类型 | `ClusterIP`（配 ingress） |
| `components.frontend.service.nodePort` | NodePort 端口 | 仅 `NodePort` 时设置固定值 |
| `components.frontend.ingress.enabled` | 是否创建 ingress | `true`（有统一入口时） |
| `components.frontend.ingress.className` | ingress 类 | `nginx` / 平台 IngressClass |
| `components.frontend.ingress.hosts` | 域名与路径 | 生产必须配置真实域名 |
| `components.frontend.ingress.tls` | TLS 配置 | 生产建议开启 |

### 建议保留默认（不建议频繁改）

- `APP_NAME`, `APP_VERSION`, `HOST`, `PORT`
- `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `PYTHONFAULTHANDLER`, `PYTHONPATH`, `TZ`
- `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`

这些属于进程身份或运行时基线，变更价值低且容易引入行为漂移。

## 部署方式（single/ha）

> 之前只有一个 `ha` 开关会造成覆盖不完整，现在改为“全局模式 + 组件覆盖”。

- 全局模式：`deployment.mode`，可选 `single | ha`
- 组件覆盖：
  - `components.clickhouse.mode`: `single | ha`
  - `components.redis.mode`: `single | ha`
  - `components.neo4j.mode`: 当前仅支持 `single`（若设 `ha` 会 fail）

说明：当前仓库仅包含 `neo4j` 单实例清单，没有官方 HA 拓扑清单，因此无法通过 chart 参数直接切到 neo4j HA。

## 环境变量治理结论（哪些该放 values，哪些不该）

### 建议放到 `values.yaml`（已接入）

- 外部依赖连接：`CLICKHOUSE_*`, `NEO4J_*`, `REDIS_*`, `AI_SERVICE_BASE_URL`
- 可调业务参数：批处理、超时、窗口、并发、队列、DLQ、同步周期等
- 可调观测参数：`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_ENABLED`
- AI 相关运行参数：`LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_BASE` 等
- 数据库运行参数：`NEO4J_*`（堆内存等）、`CLICKHOUSE_*`

### 建议保持清单固定（不放可调项）

- 进程基础信息：`APP_NAME`, `APP_VERSION`, `HOST`, `PORT`
- Python 运行基础项：`PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `PYTHONFAULTHANDLER`, `PYTHONPATH`, `TZ`
- OTel 资源标识常量：`OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`
- Secret 引用型变量（`valueFrom`）的具体值本身不放明文（保持 secret 管理）

## 组件参数盘点（来自 manifests）

| 组件 | 部署方式 | 可更新参数 |
|---|---|---|
| aiService | Deployment | `enabled`, `replicaCount`, `image`, `nodeSelector`, `env` |
| clickhouse | single/ha | `mode`, `replicaCount`, `image`, `nodeSelector`, `storageClass`, `env` |
| fluentBit | DaemonSet | `enabled`, `image`, `nodeSelector` |
| frontend | Deployment | `enabled`, `replicaCount`, `image`, `nodeSelector`, `service.*`, `ingress.*` |
| ingestService | Deployment + HPA | `enabled`, `replicaCount`, `hpa.min/max`, `image`, `nodeSelector`, `env` |
| neo4j | single only | `mode(single)`, `replicaCount`, `image`, `nodeSelector`, `storageClass`, `env` |
| otelCollector | DaemonSet | `enabled`, `image`, `nodeSelector` |
| otelGateway | Deployment + HPA | `enabled`, `replicaCount`, `hpa.min/max`, `image`, `nodeSelector` |
| queryService | Deployment + HPA | `enabled`, `replicaCount`, `hpa.min/max`, `image`, `nodeSelector`, `env` |
| redis | single/ha | `mode`, `replicaCount`, `image`, `nodeSelector`, `storageClass` |
| semanticEngine | Deployment + HPA | `enabled`, `replicaCount`, `hpa.min/max`, `image`, `nodeSelector`, `env` |
| semanticEngineWorker | Deployment + HPA | `enabled`, `replicaCount`, `hpa.min/max`, `image`, `nodeSelector`, `env` |
| topologyService | Deployment | `enabled`, `replicaCount`, `image`, `nodeSelector`, `env` |
| valueKpiCronjob | CronJob | `enabled`, `schedule`, `image`, `nodeSelector` |

## 常用示例

### 1) 全局 HA + neo4j 保持 single

```bash
helm upgrade --install logoscope charts/logoscope -n islap \
  --set deployment.mode=ha \
  --set components.neo4j.mode=single
```

### 2) 不同组件使用不同镜像仓库

```bash
helm upgrade --install logoscope charts/logoscope -n islap \
  --set components.queryService.image.registry=registry-a.example.com \
  --set components.queryService.image.repository=logoscope/query-service \
  --set components.queryService.image.tag=v20260307 \
  --set components.semanticEngineWorker.image.registry=registry-b.example.com \
  --set components.semanticEngineWorker.image.repository=team-ai/semantic-worker \
  --set components.semanticEngineWorker.image.tag=v2
```

### 3) 组件级 nodeSelector / storageClass

```bash
helm upgrade --install logoscope charts/logoscope -n islap \
  --set components.queryService.nodeSelector.nodepool=query \
  --set components.clickhouse.storageClass=ultra-ssd \
  --set components.redis.storageClass=standard
```

### 4) 仅 frontend 开启 ingress

```bash
helm upgrade --install logoscope charts/logoscope -n islap \
  --set components.frontend.service.type=ClusterIP \
  --set components.frontend.ingress.enabled=true \
  --set components.frontend.ingress.className=nginx \
  --set components.frontend.ingress.hosts[0].host=logoscope.example.com \
  --set components.frontend.ingress.hosts[0].paths[0].path=/ \
  --set components.frontend.ingress.hosts[0].paths[0].pathType=Prefix
```

### 5) 通过 values 覆盖业务 env

```bash
helm upgrade --install logoscope charts/logoscope -n islap \
  --set components.queryService.env.QUERY_LOGS_DEFAULT_TIME_WINDOW="6 HOUR" \
  --set components.queryService.env.QUERY_LOGS_MAX_THREADS=8 \
  --set components.semanticEngine.env.KAFKA_MAX_BATCH_SIZE=200
```

## 其他可优化点（建议后续）

1. 增加 `values.schema.json`：对 `deployment.mode`、`components.*.mode`、`frontend.ingress` 做强校验，提前阻断错误值。
2. 增加 `resources` 参数化：当前资源仍主要在清单内，建议逐步迁移到 `components.<name>.resources`。
3. 增加 `envFromSecret/envValueFrom`：避免敏感配置通过明文 values 下发。
4. 增加渲染单测：对关键开关（single/ha、frontend ingress、env 覆盖）做 `helm template` 快照测试。
