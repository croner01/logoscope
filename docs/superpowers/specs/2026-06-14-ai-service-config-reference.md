# ai-service 配置项参考

> 根据 `deploy/ai-service.yaml` ConfigMap + 代码 `os.getenv` 扫描自动整理。
> 扫描范围：`ai-service/` + `shared_src/`，排除了测试文件和 `__pycache__`。

## 运行时后端

### 运行时模式选择

```yaml
AI_RUNTIME_V4_OUTER_ENGINE: "temporal_required"    # 外层编排引擎
AI_RUNTIME_V4_INNER_ENGINE: "langgraph_required"    # 内层执行引擎
AI_RUNTIME_V4_AGENT_BACKEND: "langgraph"            # 运行时后端：langgraph | claude_sdk
AI_RUNTIME_V4_CLAUDE_SDK_ENABLED: "false"           # 安全开关：设为 true 才能启用 Claude SDK
```

**`AI_RUNTIME_V4_AGENT_BACKEND`** — 后端类型选择。
- `"langgraph"` → 离线模式（DeepSeek + LangGraph，当前运行）
- `"claude_sdk"` → 在线模式（Claude Agent SDK，需配合 `CLAUDE_SDK_ENABLED=true` + `ANTHROPIC_API_KEY`）

**`AI_RUNTIME_V4_CLAUDE_SDK_ENABLED`** — 安全门，防止误切到在线模式浪费 API token。代码默认 `"false"`，即使 `AGENT_BACKEND=claude_sdk` 也会拒绝启动。

**切换在线模式**：
```yaml
AI_RUNTIME_V4_CLAUDE_SDK_ENABLED: "true"
AI_RUNTIME_V4_AGENT_BACKEND: "claude_sdk"
# + Secret 中必须有 ANTHROPIC_API_KEY
```

### Temporal 编排（v4 运行时）

```yaml
AI_RUNTIME_V4_TEMPORAL_ADDRESS: "temporal-frontend:7233"
AI_RUNTIME_V4_TEMPORAL_NAMESPACE: "default"
AI_RUNTIME_V4_TEMPORAL_TASK_QUEUE: "ai-runtime-v4"
AI_RUNTIME_V4_TEMPORAL_WORKER_ENABLED: "true"
AI_RUNTIME_V4_TEMPORAL_CONNECT_TIMEOUT_SECONDS: "5"
AI_RUNTIME_V4_TEMPORAL_WORKFLOW_TIMEOUT_SECONDS: "3600"
AI_RUNTIME_V4_TEMPORAL_QUERY_ATTEMPTS: "15"
AI_RUNTIME_V4_TEMPORAL_QUERY_INTERVAL_MS: "200"
```

### 持久化表名

```yaml
AI_RUNTIME_V4_GRAPH_CHECKPOINT_TABLE: "logs.ai_runtime_v4_graph_checkpoints"
```

## 功能 Gate 开关

| Key | 代码默认 | ConfigMap | 含义 |
|-----|----------|-----------|------|
| `AI_FOLLOWUP_LONG_TERM_MEMORY_ENABLED` | `"true"` | `"true"` | 跨会话长期记忆。`false` = 每次分析独立会话 |
| `AI_FOLLOWUP_WEB_SEARCH_ENABLED` | — | `"true"` | AI 追问时联网搜索 |
| `AI_RUNTIME_EVIDENCE_GATE_ENABLED` | `True` | `"true"` | 结论必须有证据支撑 |
| `AI_RUNTIME_EVIDENCE_GATE_MODE` | `"progressive"` | `"progressive"` | `progressive` = 证据不足时降置信度；`strict` = 直接拒绝 |
| `AI_RUNTIME_SQL_PREFLIGHT_ENABLED` | `"true"` | `"true"` | `true` = SQL 预检防高危操作；`false` = 跳过 |
| `AI_RUNTIME_SHELL_EMERGENCY_ENABLED` | `"false"` | `"false"` | 允许 AI 通过 Shell 执行紧急修复（高危） |
| `AI_RUNTIME_K8S_POD_AUTORESOLVE_ENABLED` | `None`→`false` | `"false"` | 用户输入服务名时自动解析为具体 Pod |
| `AI_RUNTIME_V1_API_ENABLED` | — | `"false"` | v1 运行时 API（已废弃） |
| `AI_RUNTIME_UNIFIED_ENGINE_ENABLED` | `""`→`false` | `"true"` | 统一引擎 |

### Gate 使用场景速查

| 场景 | 需要改动的配置 |
|------|--------------|
| 切换到 Claude SDK 在线模式 | `AGENT_BACKEND=claude_sdk` + `CLAUDE_SDK_ENABLED=true` |
| 临时关闭 SQL 预检排查问题 | `SQL_PREFLIGHT_ENABLED=false` |
| 做安全审计需要最严格分析 | `EVIDENCE_GATE_MODE=strict` |
| 快速修复集群紧急故障 | `SHELL_EMERGENCY_ENABLED=true`（事毕关掉） |
| 调试时不想被跨会话记忆干扰 | `LONG_TERM_MEMORY_ENABLED=false` |
| 服务名到 Pod 解析不稳定 | `K8S_POD_AUTORESOLVE_ENABLED=false` |

## LLM 模型

```yaml
LLM_PROVIDER: "deepseek"                  # 模型提供商
LLM_API_BASE: ""                          # API 地址（空=使用提供商标配地址）
LLM_MODEL: "deepseek-v4-flash"            # 模型名
LLM_MAX_TOKENS: "8192"                    # 最大 token 数
LOCAL_MODEL_API_BASE: ""                  # 本地模型 API 地址
LOCAL_MODEL_PATH: ""                      # 本地模型路径（本地部署时用）
```

**`LLM_PROVIDER`** 可选值（代码读取的 provider 列表）：`deepseek`、`openai`、`claude`、`local`。
API Key 通过 Secret 注入（见下文 Secret 章节）。

## 服务地址

```yaml
AI_AGENT_QUERY_API_BASE: "http://query-service:8002"
AI_AGENT_QUERY_API_TIMEOUT_SECONDS: "6"
EXEC_SERVICE_BASE_URL: "http://exec-service:8095"
QUERY_SERVICE_BASE_URL: "http://query-service:8002"
```

## 数据库

```yaml
CLICKHOUSE_HOST: "clickhouse"
CLICKHOUSE_PORT: "9000"
CLICKHOUSE_DATABASE: "logs"
CLICKHOUSE_USER: "default"
CLICKHOUSE_PASSWORD: ""
NEO4J_HOST: "neo4j"
NEO4J_PORT: "7687"
NEO4J_USER: "neo4j"
NEO4J_PASSWORD: "password"
NEO4J_DATABASE: "neo4j"
```

## 遥测

```yaml
OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED: "true"
OTEL_SERVICE_NAME: "ai-service"
OTEL_RESOURCE_ATTRIBUTES: "service.name=ai-service,deployment.environment=production"
OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel-collector.islap.svc.cluster.local:4318"
```

## 应用基础

```yaml
APP_NAME: "ai-service"
APP_VERSION: "1.0.0"
HOST: "0.0.0.0"
PORT: "8090"
DEBUG: "false"
LOG_LEVEL: "info"
TZ: "UTC"
AI_RUNTIME_SESSION_COMMAND_LIMIT: "10"          # 单会话最大命令执行次数
```

## 远程目标

```yaml
AI_RUNTIME_V4_REMOTE_TARGETS_JSON: '[{"target_kind":"k8s_cluster",...}]'
```

定义 AI 可以操作的远程目标（K8s 集群、主机节点）。当前注册了两个目标：
- **OpenStack K8s Cluster** — K8s 集群操作（读日志、重启负载、Helm）
- **OpenStack Node-3** — 主机节点操作（读系统状态、SSH 操作）

## Secret（非 ConfigMap）

```yaml
# 通过 Secret 注入，不会明文出现在 ConfigMap
name: LLM_API_KEY            # 通用 LLM API Key（fallback）
name: OPENAI_API_KEY         # OpenAI
name: ANTHROPIC_API_KEY      # Claude / Anthropic SDK
name: DEEPSEEK_API_KEY       # DeepSeek
name: LOCAL_MODEL_API_KEY    # 本地模型 API Key
```

来源：`deploy/ai-service.yaml` Secret 引用，从 `semantic-engine-llm` Secret 读取。

## 未在 ConfigMap 中声明的代码配置

以下环境变量代码读取但 ConfigMap 未显式声明（使用代码默认值即可，无需配置）。仅在需要覆盖默认行为时通过 `kubectl set env` 临时设置。

### AI Agent

| Key | 代码默认 | 说明 |
|-----|----------|------|
| `AI_AGENT_INPUT_DEFAULT_TZ` | — | Agent 输入时区 |
| `AI_AGENT_LOG_FETCH_LIMIT` | — | 日志抓取上限 |
| `AI_AGENT_LOG_INJECT_LIMIT` | — | 日志注入上限 |
| `AI_AGENT_LOG_RAW_LIMIT` | — | 原始日志上限 |
| `AI_AGENT_TIME_WINDOW_MINUTES` | — | 分析时间窗口（分钟） |
| `AI_AGENT_TRACE_FETCH_LIMIT` | — | Trace 抓取上限 |
| `AI_AGENT_WEB_SEARCH_ENABLED` | — | Agent 联网搜索 |
| `AI_AGENT_WEB_SEARCH_ENDPOINT` | — | Agent 搜索端点 |
| `AI_AGENT_WEB_SEARCH_TIMEOUT_SECONDS` | — | Agent 搜索超时 |
| `AI_AGENT_RUN_CH_TABLE` | `{database}.ai_agent_runs` | Agent Run 表 |
| `AI_AGENT_RUN_EVENT_CH_TABLE` | `{database}.ai_agent_run_events` | Agent Run 事件表 |
| `AI_AGENT_RUN_LATEST_VIEW` | `{database}.v_ai_agent_runs_latest` | Agent Run 实时视图 |
| `AI_AGENT_RUN_READ_SOURCE_CACHE_TTL_SECONDS` | `30` | 缓存 TTL（秒） |

### AI Follow-up（追问系统）

| Key | 代码默认 | 说明 |
|-----|----------|------|
| `AI_FOLLOWUP_ENGINE` | — | 追问引擎（langchain / 其他） |
| `AI_FOLLOWUP_TOKEN_BUDGET` | `12000` | Token 预算 |
| `AI_FOLLOWUP_TOKEN_WARN_THRESHOLD` | — | Token 预警阈值 |
| `AI_FOLLOWUP_COMMAND_MAX_CHARS` | `320` | 命令最大字符数 |
| `AI_FOLLOWUP_COMMAND_TIMEOUT_SECONDS` | — | 命令超时 |
| `AI_FOLLOWUP_COMMAND_MAX_OUTPUT_CHARS` | — | 命令输出上限 |
| `AI_FOLLOWUP_LLM_TIMEOUT_SECONDS` | — | LLM 调用超时 |
| `AI_FOLLOWUP_LLM_FIRST_TOKEN_TIMEOUT_SECONDS` | — | LLM 首个 token 超时 |
| `AI_FOLLOWUP_REQUEST_DEADLINE_SECONDS` | — | 请求总截止时间 |
| `AI_FOLLOWUP_REACT_MAX_ITERATIONS` | — | ReAct 最大迭代 |
| `AI_FOLLOWUP_REACT_RETRY_PER_COMMAND` | — | 每个命令重试次数 |
| `AI_FOLLOWUP_REACT_MEMORY_TIMEOUT_SECONDS` | — | ReAct 记忆超时 |
| `AI_FOLLOWUP_REACT_MEMORY_MAX_APPEND` | — | ReAct 记忆追加上限 |
| `AI_FOLLOWUP_REFLECTION_MAX_ITERATIONS` | — | 反思最大轮次 |
| `AI_FOLLOWUP_SESSION_CACHE_MAX` | — | 会话缓存上限 |
| `AI_FOLLOWUP_SESSION_CACHE_TTL_SECONDS` | — | 会话缓存 TTL |
| `AI_FOLLOWUP_SESSION_PREPARE_TIMEOUT_SECONDS` | — | 会话准备超时 |
| `AI_FOLLOWUP_ACTION_MAX_ITEMS` | — | 动作草案上限 |
| `AI_FOLLOWUP_AUTO_EXEC_COMMAND_TIMEOUT_SECONDS` | — | 自动执行命令超时 |
| `AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS` | — | 只读自动执行上限 |
| `AI_FOLLOWUP_LANGCHAIN_STREAM_RAW_TOKENS` | — | 流式原生 token |
| `AI_FOLLOWUP_PERSIST_TIMEOUT_SECONDS` | — | 持久化超时 |
| `AI_FOLLOWUP_HISTORY_LOAD_TIMEOUT_SECONDS` | — | 历史加载超时 |
| `AI_FOLLOWUP_COMPACT_TRIGGER` | `12` | 触发 compact 的消息数阈值 |
| `AI_FOLLOWUP_COMPACT_KEEP_RECENT` | `8` | compact 保留的最近消息数 |
| `AI_FOLLOWUP_LONG_TERM_MEMORY_SESSION_LIMIT` | — | 长期记忆会话上限 |
| `AI_FOLLOWUP_LONG_TERM_MEMORY_MAX_SNIPPETS` | — | 长期记忆摘要上限 |
| `AI_FOLLOWUP_LONG_TERM_MEMORY_MESSAGE_LIMIT` | — | 长期记忆消息上限 |
| `AI_FOLLOWUP_LONG_TERM_MEMORY_TIMEOUT_SECONDS` | — | 长期记忆超时 |
| `AI_FOLLOWUP_WEB_SEARCH_ENDPOINT` | — | 联网搜索端点 |
| `AI_FOLLOWUP_WEB_SEARCH_TIMEOUT_SECONDS` | — | 联网搜索超时 |

### AI Runtime

| Key | 代码默认 | 说明 |
|-----|----------|------|
| `AI_RUNTIME_APPROVAL_TIMEOUT_SECONDS` | `900` | 审批超时（秒） |
| `AI_RUNTIME_DIAGNOSIS_CONTRACT_REASK_MAX_ROUNDS` | — | 诊断合同重试轮次 |
| `AI_RUNTIME_SQL_LLM_REPAIR_TIMEOUT_SECONDS` | `8` | SQL LLM 修复超时 |
| `AI_RUNTIME_CLICKHOUSE_EXEC_TARGET` | `deploy/clickhouse` | ClickHouse 执行目标 |
| `AI_RUNTIME_CLICKHOUSE_EXEC_NAMESPACE` | — | ClickHouse 执行命名空间 |
| `AI_RUNTIME_CLICKHOUSE_POD_SELECTOR_DEFAULT` | — | Pod 选择器默认值 |
| `AI_RUNTIME_FOLLOWUP_DEADLINE_SECONDS` | — | 追问截止时间 |
| `AI_RUNTIME_FOLLOWUP_MIN_DEADLINE_SECONDS` | — | 最小截止时间 |
| `AI_RUNTIME_INPUT_CONTEXT_HYDRATE_TIMEOUT_SECONDS` | — | 上下文注入超时 |
| `AI_RUNTIME_INPUT_CONTEXT_HYDRATE_RETRY_MAX` | — | 上下文注入重试上限 |
| `AI_RUNTIME_MIN_EVIDENCE_COVERAGE` | — | 最小证据覆盖率 |
| `AI_RUNTIME_MIN_FINAL_CONFIDENCE` | — | 最低最终置信度 |
| `AI_RUNTIME_V2_CREATE_RUN_MAX_ATTEMPTS` | — | v2 Run 创建重试次数 |
| `AI_RUNTIME_V2_CREATE_RUN_RETRY_AFTER_SECONDS` | — | v2 Run 重试间隔 |
| `AI_RUNTIME_V2_CREATE_RUN_RETRY_DELAYS_MS` | — | v2 Run 重试延迟（毫秒） |
| `AI_RUNTIME_V4_TEMPORAL_QUERY_RPC_TIMEOUT_SECONDS` | — | Temporal RPC 超时 |
| `AI_RUNTIME_V4_TARGET_TABLE` | — | Target 表名 |
| `AI_RUNTIME_V4_TARGET_LATEST_VIEW` | — | Target 实时视图 |
| `AI_RUNTIME_V4_TARGET_CHANGE_TABLE` | — | Target 变更表 |
| `AI_RUNTIME_V4_TARGET_AUTO_SEED_ENABLED` | — | 自动种子化 |
| `AI_RUNTIME_V4_TARGET_AUTO_SEED_REASON` | — | 种子化原因 |
| `AI_RUNTIME_V4_TARGET_AUTO_SEED_UPDATED_BY` | — | 种子化更新者 |
| `AI_RUNTIME_V4_TARGET_DEFAULT_CLICKHOUSE_DATABASE` | — | 默认 ClickHouse 数据库 |
| `AI_RUNTIME_V4_TARGET_DEFAULT_CLUSTER_ID` | — | 默认集群 ID |
| `AI_RUNTIME_V4_TARGET_DEFAULT_NAMESPACE` | — | 默认命名空间 |
| `AI_RUNTIME_V4_TARGET_DEFAULT_RISK_TIER` | — | 默认风险等级 |

### ClickHouse 后台写入

| Key | 代码默认 | 说明 |
|-----|----------|------|
| `CH_BATCH_SIZE` | — | 批量写入条数 |
| `CH_BATCH_LOG_EVERY` | — | 写入日志频率 |
| `CH_FLUSH_INTERVAL` | — | 刷新间隔 |
| `CH_FLUSH_SPLIT_MIN_ROWS` | — | 拆分最小行数 |
| `CH_FLUSH_FAILURE_BACKOFF_SECONDS` | — | 失败退避（秒） |
| `CH_FLUSH_FAILURE_MAX_BACKOFF_SECONDS` | — | 最大退避（秒） |
| `CH_MAX_INSERT_ROWS_PER_QUERY` | — | 单次插入最大行数 |
| `CH_STATS_TIME_WINDOW` | — | 统计时间窗口 |
| `CH_TOPOLOGY_TIME_WINDOW` | — | 拓扑时间窗口 |
| `TOPOLOGY_BUILDER_TIME_WINDOW` | — | 拓扑构建时间窗口 |

### History & Cases

| Key | 代码默认 | 说明 |
|-----|----------|------|
| `AI_HISTORY_SESSION_CH_TABLE` | — | 历史会话表 |
| `AI_HISTORY_SESSION_LATEST_VIEW` | — | 历史会话视图 |
| `AI_HISTORY_MESSAGE_CH_TABLE` | — | 历史消息表 |
| `AI_HISTORY_READ_SOURCE_CACHE_TTL_SECONDS` | — | 历史缓存 TTL |
| `AI_HISTORY_MESSAGE_METADATA_LIST_MAX_ITEMS` | — | 元数据列表上限 |
| `AI_HISTORY_MESSAGE_METADATA_MAX_CHARS` | — | 元数据字符上限 |
| `AI_HISTORY_MESSAGE_METADATA_TEXT_MAX_CHARS` | — | 元数据文本上限 |
| `AI_CASE_STORE_PATH` | — | 案例存储路径 |
| `AI_CASE_STORE_PERSIST` | — | 案例持久化 |
| `AI_CASE_STORE_CH_TABLE` | — | 案例 ClickHouse 表 |
| `AI_CASE_STORE_LATEST_VIEW` | — | 案例实时视图 |
| `AI_CASE_CHANGE_HISTORY_CH_TABLE` | — | 案例变更历史表 |
| `AI_CASE_READ_SOURCE_CACHE_TTL_SECONDS` | — | 案例缓存 TTL |

### Knowledge Base（知识库）

| Key | 代码默认 | 说明 |
|-----|----------|------|
| `AI_PROJECT_KNOWLEDGE_ROOT` | — | 项目知识根目录 |
| `KB_DEPLOYMENT_FILE_PATH` | — | 知识库部署文件路径 |
| `KB_PROVIDER_STATUS_CACHE_SECONDS` | — | Provider 状态缓存 |
| `KB_RAGFLOW_BASE_URL` | — | RagFlow API 地址 |
| `KB_RAGFLOW_API_KEY` | — | RagFlow API Key |
| `KB_RAGFLOW_DATASET_ID` | — | RagFlow 数据集 ID |
| `KB_RAGFLOW_CHUNKS_PATH` | — | RagFlow 分块路径 |
| `KB_RAGFLOW_CHUNK_METHOD` | — | RagFlow 分块方法 |
| `KB_RAGFLOW_CHUNK_TOKEN_NUM` | — | RagFlow 分块 token 数 |
| `KB_RAGFLOW_DOCUMENT_NAME_PREFIX` | — | 文档名前缀 |
| `KB_RAGFLOW_UPLOAD_FIELD_NAME` | — | 上传字段名 |
| `KB_REMOTE_PROVIDER` | — | 远程知识库 Provider |
| `KB_REMOTE_BASE_URL` | — | 远程知识库地址 |
| `KB_REMOTE_API_KEY` | — | 远程知识库 API Key |
| `KB_REMOTE_DATASET_ID` | — | 远程数据集 ID |
| `KB_REMOTE_SEARCH_PATH` | — | 远程搜索路径 |
| `KB_REMOTE_UPSERT_PATH` | — | 远程 upsert 路径 |
| `KB_REMOTE_HEALTH_PATH` | — | 远程健康检查路径 |
| `KB_REMOTE_TIMEOUT_SECONDS` | — | 远程超时 |
| `KB_REMOTE_OUTBOX_ENABLED` | — | 远程发件箱 |
| `KB_REMOTE_OUTBOX_PATH` | — | 发件箱路径 |
| `KB_REMOTE_OUTBOX_POLL_SECONDS` | — | 发件箱轮询间隔 |
| `KB_REMOTE_OUTBOX_MAX_ATTEMPTS` | — | 发件箱最大尝试 |
| `KB_REMOTE_OUTBOX_BACKOFF_BASE_SECONDS` | — | 发件箱退避基准 |
| `KB_REMOTE_OUTBOX_BACKOFF_MAX_SECONDS` | — | 发件箱最大退避 |
| `AI_KB_DRAFT_HISTORY_MAX_ITEMS` | — | 草稿历史上限 |
| `AI_KB_DRAFT_LLM_MAX_MESSAGES` | — | 草稿 LLM 最大消息 |
| `AI_KB_DRAFT_LLM_MAX_MESSAGE_CHARS` | — | 草稿 LLM 最大字符 |
| `AI_KB_DRAFT_LLM_TIMEOUT_SECONDS` | — | 草稿 LLM 超时 |
| `AI_KB_SOLUTION_OPTIMIZE_TIMEOUT_SECONDS` | — | 解决方案优化超时 |

### LLM Service

| Key | 代码默认 | 说明 |
|-----|----------|------|
| `LLM_CACHE_ENABLED` | `"true"` | LLM 响应缓存 |
| `LLM_CACHE_MAX_ENTRIES` | — | 缓存上限 |
| `LLM_TEMPERATURE` | — | 生成温度 |
| `LLM_DEPLOYMENT_FILE_PATH` | — | 部署文件路径 |
| `DEEPSEEK_API_BASE` | — | DeepSeek API 地址 |
| `DEEPSEEK_MODEL` | — | DeepSeek 模型（读前端的） |
| `OPENAI_API_BASE` | — | OpenAI API 地址 |
| `CLAUDE_SDK_API_KEY` | — | Claude SDK API Key（替代 ANTHROPIC_API_KEY） |
| `CLAUDE_SDK_MODEL` | — | Claude SDK 模型名 |
| `CLAUDE_SDK_MAX_TOKENS` | — | Claude SDK 最大 token |
| `CLAUDE_SDK_MAX_TURNS` | — | Claude SDK 最大轮次 |
| `LOCAL_MODEL_BASE_URL` | — | 本地模型 URL（如 Ollama） |
| `LOCAL_MODEL_NAME` | — | 本地模型名 |

### 其他

| Key | 代码默认 | 说明 |
|-----|----------|------|
| `DLQ_ENABLED` | — | 死信队列 |
| `DLQ_MAX_RETRIES` | — | 死信队列最大重试 |
| `REDIS_BATCH_SIZE` | — | Redis 批量大小 |
| `REDIS_BLOCK_MS` | — | Redis 阻塞等待（毫秒） |
| `K8S_CLUSTER_ID` | — | K8s 集群标识 |
| `NAMESPACE` | — | 命名空间（K8s） |
| `LOGOSCOPE_SHARED_LIB` | — | 共享库路径 |
| `LOGOSCOPE_SKILLS_CUSTOM` | — | 自定义技能路径 |
| `LOGOSCOPE_SKILLS_INSTALLED` | — | 已安装技能路径 |
| `LOG_FORMAT` | — | 日志输出格式 |
| `OTEL_ENABLED` | — | OpenTelemetry 总开关 |

## 如何热更新配置

```bash
# 1. 修改 ConfigMap
kubectl apply -f deploy/ai-service.yaml

# 2. 重启 Pod 加载新值
kubectl rollout restart deploy/ai-service -n islap

# 3. 检查状态
kubectl rollout status deploy/ai-service -n islap
```

**临时覆盖**（不需改文件，适合调试）：
```bash
kubectl set env deploy/ai-service -n islap \
  AI_RUNTIME_EVIDENCE_GATE_MODE=strict \
  AI_RUNTIME_SQL_PREFLIGHT_ENABLED=false
```

**查看当前运行时配置**：
```bash
kubectl exec deploy/ai-service -n islap -- env | grep AI_RUNTIME | sort
```
