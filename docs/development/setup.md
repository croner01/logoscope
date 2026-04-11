# Logoscope 开发环境搭建

> 版本: v3.21.0
> 更新: 2026-02-11
> 相关文档: [API 参考](../api/reference.md)

## 🎯 环境概览

### 开发栈

```
┌─────────────────────────────────────────────────────┐
│              开发环境                 │
│  ┌───────────────────────────────────────────┐ │
│  │  操作系统                    │ │
│  │  - Linux (Ubuntu 22.04+)        │ │
│  │  - macOS 12+                   │ │
│  │  - Windows 10+                  │ │
│  └───────────────────────────────────────┘ │
│                                        │
│  ┌───────────────────────────────────────────┐ │
│  │  运行时环境                   │ │
│  │  - Python 3.12+                 │ │
│  │  - Node.js 18+                   │ │
│  │  - Go 1.21+                     │ │
│  │  - Docker 20.10+                │ │
│  │  - kubectl 1.28+               │ │
│  └───────────────────────────────────────┘ │
│                                        │
│  ┌───────────────────────────────────────────┐ │
│  │  IDE                           │ │
│  │  - VS Code (推荐)              │ │
│  │  - PyCharm (可选)              │ │
│  │  - JetBrains Fleet (可选)       │ │
│  └───────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

### 核心依赖

| 组件 | 版本 | 用途 |
|------|------|------|
| Python | 3.12+ | 运行时、后端开发 |
| Go | 1.21+ | Fluent Bit、OTel Collector |
| Node.js | 18+ | 前端开发 |
| Docker | 20.10+ | 容器化部署 |
| kubectl | 1.28+ | Kubernetes 集群管理 |
| Git | 2.x+ | 版本控制 |

---

## 📦 本地开发环境

### 1. 克隆项目

```bash
# 克隆仓库
git clone https://github.com/your-org/logoscope.git

# 进入项目目录
cd logoscope
```

### 2. 后端开发环境

#### Python 环境准备

```bash
# 创建 Python 虚拟环境
cd semantic-engine
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 验证安装
python -c "import clickhouse_connect; print('ClickHouse OK')"
python -c "import neo4j; print('Neo4j OK')"
```

#### 配置 ClickHouse

```bash
# 使用 Docker 运行 ClickHouse
docker run -d \
  --name logoscope-clickhouse \
  -p 8123:8123 \
  --ulimit nofile=262144 \
  clickhouse/clickhouse-server

# 或连接到现有 ClickHouse
export CLICKHOUSE_HOST=10.43.71.7
export CLICKHOUSE_PORT=8123
export CLICKHOUSE_USER=default
export CLICKHOUSE_DATABASE=logs
export CLICKHOUSE_PASSWORD=
```

#### 配置 Neo4j

```bash
# 使用 Docker 运行 Neo4j
docker run -d \
  --name logoscope-neo4j \
  -p 7474:7474 \
  -e NEO4J_AUTH=neo4j/password \
  neo4j:5

# 或连接到现有 Neo4j
export NEO4J_HOST=10.43.215.51
export NEO4J_PORT=7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=your-password
export NEO4J_DATABASE=neo4j
```

#### 配置 Redis

```bash
# 使用 Docker 运行 Redis
docker run -d \
  --name logoscope-redis \
  -p 6379:6379 \
  redis:7

# 或连接到现有 Redis
export REDIS_HOST=10.43.215.51
export REDIS_PORT=6379
export REDIS_PASSWORD=
```

#### 环境变量

创建 `.env` 文件或使用 `direnv`：

```bash
# .env 文件示例
CLICKHOUSE_HOST=10.43.71.7
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=default
CLICKHOUSE_DATABASE=logs
CLICKHOUSE_PASSWORD=

NEO4J_HOST=10.43.215.51
NEO4J_PORT=7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=password
NEO4J_DATABASE=neo4j

REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=

OTEL_COLLECTOR_HOST=otel-collector.logoscope.svc
OTEL_GATEWAY_HOST=otel-gateway.logoscope.svc
```

### 3. 前端开发环境

#### Node.js 环境准备

```bash
# 进入前端目录
cd frontend

# 安装依赖
npm install

# 验证安装
npm list
```

#### 配置 API 端点

```bash
# 开发环境
export VITE_API_BASE_URL=http://localhost:8080/api/v1

# 生产环境
export VITE_API_BASE_URL=https://api.logoscope.example.com/api/v1
```

---

## 🐳 Kubernetes 本地开发

### 1. Kind 集群（推荐用于本地开发）

```bash
# 安装 Kind
curl -Lo https://kind.sigs.k8s.io/dl/v0.20.0/amd64 \
  -o kind-linux-amd64 \
  && chmod +x kind-linux-amd64 \
  && sudo mv kind-linux-amd64 /usr/local/bin/kind

# 创建集群
kind create cluster --name logoscope-dev

# 加载镜像
kind load docker-image nginx:latest
kind load docker-image redis:7-alpine
kind load docker-image clickhouse/clickhouse-server:23
kind load docker-image neo4j:5
```

### 2. 部署配置

#### 语义引擎部署

```yaml
# deploy/semantic-engine.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: semantic-engine
  namespace: logoscope
spec:
  replicas: 1  # 开发环境用 1
  selector:
    matchLabels:
      app: semantic-engine
  template:
    metadata:
      labels:
        app: semantic-engine
        version: v3.21.0
    spec:
      containers:
      - name: semantic-engine
        image: logoscope/semantic-engine:v3.21.0-dev
        imagePullPolicy: IfNotPresent  # 本地构建
        env:
          - name: CLICKHOUSE_HOST
            value: "10.43.71.7"
          - name: CLICKHOUSE_PORT
            value: "8123"
          - name: NEO4J_HOST
            value: "10.43.215.51"
          - name: NEO4J_PORT
            value: "7687"
          - name: REDIS_HOST
            value: "10.43.215.51"
          - name: REDIS_PORT
            value: "6379"
        ports:
          - containerPort: 8080
            protocol: TCP
        resources:
          requests:
            memory: "512Mi"
            cpu: "250m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
```

#### OTel Collector 部署

```yaml
# deploy/otel-collector/
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: otel-collector
  namespace: logoscope
spec:
  selector:
    matchLabels:
      app: otel-collector
  template:
    metadata:
      labels:
        app: otel-collector
        version: v3.21.0
    spec:
      containers:
      - name: otel-collector
        image: otel/opentelemetry-collector:0.108.0
        command:
          - /otelcolc
          - --config=/etc/otelcol/config.yaml
        volumeMounts:
          - name: config
            mountPath: /etc/otelcol
        env:
          - name: OTEL_EXPORTER_OTLP_ENDPOINT
            value: "http://otel-gateway.logoscope.svc:4317"
```

#### 数据库部署

```yaml
# deploy/databases/
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: clickhouse-config
data:
  clickhouse-config.xml: |
    <yandex>
      <clickhouse_replace_query>
        <database>logs</database>
        <table>logs</table>
        <conditions>timestamp > now() - INTERVAL 7 DAY</conditions>
      </clickhouse_replace_query>
    </yandex>
```

---

## 🏃 运行和调试

### 后端调试

```bash
# 运行语义引擎
cd semantic-engine
python3 -m uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# 调试模式
python3 -m pdb main:app

# 日志级别
export LOG_LEVEL=DEBUG

# 性能分析
python3 -m cProfile -o output.prof main:app
```

### 前端调试

```bash
# 运行前端开发服务器
cd frontend
npm run dev

# 调试模式
npm run dev --debug

# 构建生产版本
npm run build

# 预览构建结果
npm run preview
```

---

## 🧪 测试环境

### 单元测试

```bash
# 运行所有测试
cd semantic-engine
pytest -v

# 运行特定测试
pytest tests/test_enhanced_topology.py -v

# 测试覆盖率
pytest --cov=. --cov-report=html

# 查看覆盖率报告
open htmlcov/index.html
```

### 集成测试

```bash
# 启动完整环境
./deploy/test-env.sh

# 运行集成测试脚本
python tests/integration/test_api_flow.py
```

---

## 📊 性能优化

### 后端优化

| 优化项 | 说明 | 工具 |
|--------|------|------|
| 异步处理 | asyncio | uvicorn |
| 数据库连接池 | SQLAlchemy | connection pool |
| 批量操作 | ClickHouse 批量插入 | batch size |
| 缓存 | Redis | 热点数据缓存 |
| 查询优化 | ClickHouse 索引、分区 | ORDER BY, PARTITION BY |

### 前端优化

| 优化项 | 说明 | 工具 |
|--------|------|------|
| 代码分割 | Code splitting | Vite |
| 懒加载 | 路由懒加载 | async components |
| 图片优化 | WebP/Vite | image optimization |
| 压缩 | Gzip/Brotli | Nginx |

---

## 📦 生产部署

### 镜像构建

```bash
# 构建语义引擎镜像
docker build -t logoscope/semantic-engine:v3.21.0 .

# 构建前端镜像
cd frontend
docker build -t logoscope/frontend:v3.21.0 .
```

### 推送到镜像仓库

```bash
# 标签镜像
docker tag logoscope/semantic-engine:v3.21.0 \
  logoscope/semantic-engine:latest

# 推送到 Docker Hub
docker push logoscope/semantic-engine:latest

# 推送到 GitHub
docker push ghcr.io/your-org/logoscope:latest
```

### Kubernetes 生产部署

参考 [部署架构](../architecture/deployment.md) 文档。

---

## 🔧 开发工具推荐

### VS Code 扩展

推荐安装：

- **Python**
  - Python (Microsoft)
  - Python Extension Pack (Jedi)
  - Pyright (类型检查)

- **Go**
  - Go (Google)
  - Go Test Coverage

- **前端**
  - ESLint
  - Prettier
  - Vue.js Extension
  - TypeScript Vue Plugin

### 代码质量

```bash
# Python 代码检查
pylint semantic-engine/

# 类型检查
mypy semantic-engine/

# 格式化
black semantic-engine/
isort semantic_engine/

# 前端类型检查
cd frontend
npx vue-tsc --noEmit
```

---

## 📚 开发最佳实践

### 代码规范

1. **遵循 PEP 8** (Python)
2. **使用类型注解** (Type Hints)
3. **编写文档字符串** (Docstrings)
4. **保持函数简短** (单一职责)
5. **使用有意义的变量名**

### Git 工作流

```bash
# 功能分支
git checkout -b feature/service-topology

# 定期提交
git commit -m "Add time correlation algorithm"

# 推送到远程
git push origin feature/service-topology

# 创建 Pull Request
gh pr create --base master --head feature/service-topology
```

### 安全实践

1. **不提交敏感信息**
   - API Keys
   - 密码
   - `.env` 文件

2. **使用环境变量**
   ```bash
   export GITHUB_TOKEN=xxx
   ```

3. **依赖扫描**
   ```bash
   npm audit
   pip-audit
   ```

---

**文档维护**: Semantic Engine Team
**最后更新**: 2026-02-11
