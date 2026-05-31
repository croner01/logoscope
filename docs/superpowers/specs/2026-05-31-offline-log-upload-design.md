# Offline Log Upload — 设计文档

## 概述

在 Logoscope 平台侧添加离线日志上传功能，允许用户通过前端页面上传本地日志文件（.log / .txt / .json），经过完整管道处理后在 LogsExplorer 中查询和分析。

**设计原则：**
- 上传的日志走 **完全相同的管道**（Redis → Semantic Engine → ClickHouse），与 Fluent Bit 实时采集无区别
- 不复用 Fluent Bit/OTel Collector，避免链路过长
- 不新建独立服务，最小化运维复杂度

## 架构

```
┌─────────────┐   multipart upload   ┌──────────────────┐
│  浏览器       │ ──────────────────▶ │  Ingest Service   │
│  LogsExplorer │                     │  /v1/logs/upload   │
└─────────────┘                       └────────┬─────────┘
                                               │ 流式解析、分批
                                               ▼
                                        ┌──────────────┐
                                        │  Redis Stream  │
                                        │  (logs.raw)    │
                                        └──────┬───────┘
                                               │
                                        ┌──────▼───────┐
                                        │Semantic Engine│
                                        │ (全链路处理)    │
                                        └──────┬───────┘
                                               │
                                        ┌──────▼───────┐
                                        │  ClickHouse   │
                                        │  (logs 表)    │
                                        └──────────────┘
```

## 1. 后端 — Ingest Service 新增 Upload 端点

### 路由说明

上传端点在 **Ingest Service** 上，与现有 OTLP 接收端点（`/v1/logs`）同级。前端通过 nginx 将 `/api/v1/logs/upload` 代理到 Ingest Service：

```
浏览器 → /api/v1/logs/upload → nginx → Ingest Service :8080/v1/logs/upload
```

需在 nginx 配置或 deploy 的 ingress 中新增一条路由规则。

### 端点

```
POST /api/v1/logs/upload
Content-Type: multipart/form-data

参数:
  file:          File (必填) — .log / .txt / .json
  service_name:  string (可选) — 用户手动指定，优先级最高
  namespace:     string (可选, 默认 "default")
```

### 处理流程

```
receive_file(file)
  │
  ├─ detect_format(file)
  │   ├─ .json / .ndjson → 按 JSON 数组或逐行 JSON 解析
  │   └─ .log / .txt     → 按文本行解析 + 自动提取时间戳/级别
  │
  ├─ parse_stream(file)  →  Generator[LogRecord]  流式读取
  │
  ├─ batch_write(records, batch_size=100)
  │   └─ 每批写入 Redis Stream (logs.raw)
  │
  └─ return 202 {"status":"accepted","total":N,"batches":M}
```

### 服务名解析策略

每条日志记录写入时的 `service_name` 按以下优先级确定：

| 优先级 | 来源 | 方法 |
|---|---|---|
| 1 (最高) | 用户显式填写 | 上传对话框的 `service_name` 字段，填了就用 |
| 2 | 日志行内容自动提取 | 从文件前 N 行用正则提取（见下方） |
| 3 | 文件名提取 | `nova-compute.log` → `nova-compute` |
| 兜底 | 默认值 | `offline-upload` |

```
def resolve_service_name(filename, first_lines, user_input):
    1. user_input 非空 → 直接返回
    2. 逐行匹配 OpenStack 模式: /\d+\s+(ERROR|CRITICAL)\s+([\w-]+)/ → 返回 group(2)
    3. filename.stem 排除常见通用名后 → 返回 filename.stem
    4. 返回 "offline-upload"
```

**文件级别的设计约束：** 一个上传文件对应一个服务。如果用户有多个服务的日志，分多次上传。

**JSON 文件** 支持两种格式：

```json
// 格式 A: JSON 数组
[
  {"timestamp": "2026-05-31 11:37:25", "level": "ERROR", "message": "..."},
  {"timestamp": "2026-05-31 11:37:26", "level": "INFO", "message": "..."}
]

// 格式 B: NDJSON（每行一个 JSON 对象）
{"timestamp": "2026-05-31 11:37:25", "level": "ERROR", "message": "..."}
{"timestamp": "2026-05-31 11:37:26", "level": "INFO", "message": "..."}
```

**文本文件** 按行解析，用优先级正则提取时间戳和级别：

```python
PATTERNS = [
    r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))',  # RFC3339
    r'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+\d+\s+(ERROR|CRITICAL|WARN|INFO|DEBUG)',  # OpenStack
    r'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+(?:\[)?(ERROR|CRITICAL|WARN|INFO|DEBUG)',  # 常见
    r'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)',  # 纯时间戳
]
```

解析失败的字段留空，由下游 Semantic Engine 的 Normalizer 兜底处理。完全无法解析的行以 `level=INFO, message=原文` 写入。

### 与 OTLP 管道的兼容性

上传日志与 OTLP 日志写入 **同一 Redis Stream（logs.raw）**。Semantic Engine 的 queue_reader 消费时通过 `type` 字段区分：

- `type=otlp` → OTLP 管道原有的 Protobuf/JSON 消息，走现有解析逻辑
- `type=upload` → 上传消息，直接取出 `records` 数组交给 Normalizer

Normalizer 收到记录后，通过 `source=upload` 标记走虚拟服务分支，其余处理不变。

| 场景 | 处理方式 |
|---|---|
| 空文件 | 400 `{"error":"empty file"}` |
| 文件 > 500MB | 400 `{"error":"file too large, max 500MB"}` |
| 格式无法解析 | 按行 best-effort，解析失败的行保留原文 |
| Redis 写入失败 | 批量重试 3 次，最终失败返回 503 |
| 上传中断 | 客户端重传（服务端不做去重） |

## 2. 语义引擎适配 — VirtualServiceSource

Normalizer 中新增一个 `source=upload` 的处理分支，补充 K8s 缺失的容器字段：

```python
# normalizer.py
if source == "upload":
    record["pod_name"] = f"upload-{upload_id[:12]}"
    record["labels"] = record.get("labels", {})
    record["labels"]["source"] = "upload"
    # service_name / namespace 由上传时指定或自动识别
```

随后走正常流水线：Classifier → Correlator → Storage Adapter。无需修改 Semantic Engine 核心逻辑。

## 3. 中间消息格式 — Redis Stream

上传日志写入 Redis Stream 的消息结构与 OTLP 管道兼容：

```json
{
    "type": "upload",
    "upload_id": "upl_<server_generated_uuid_short>",
    "records": [
        {
            "timestamp": "2026-05-31T03:37:25.174Z",
            "level": "ERROR",
            "message": "2026-05-31 11:37:25.174 19 ERROR nova-conductor ...",
            "service_name": "nova-conductor",
            "source": "upload",
            "attributes": {
                "upload_id": "upl_20260531_abc123",
                "original_line": "2026-05-31 11:37:25.174 19 ERROR nova-conductor ..."
            }
        }
    ]
}
```

## 4. 前端 — LogsExplorer 上传入口

### 位置

在 LogsExplorer 顶部操作栏，刷新按钮右侧、导出按钮左侧：

```
[筛选] [刷新] [上传 ▲] [导出 ▼]   □ 实时模式   结果: 1234
```

### 上传对话框

点击上传或拖拽文件到指定区域，支持 `.log` / `.txt` / `.json`。可选字段：服务名、命名空间。

### 进度反馈

上传过程中显示：
- 进度条（百分比）
- 已解析/已发送条数
- 预计剩余时间

上传完成后自动触发日志列表刷新。上传中可点击取消按钮中断上传（通过 `AbortController` abort 请求）。

### API 集成

在 `api.ts` 中新增 `uploadLogs` 方法，使用 `multipart/form-data` 上传，支持 `onUploadProgress` 回调：

```typescript
// 返回类型
interface UploadResult {
  status: string;      // "accepted"
  upload_id: string;   // 服务端生成的 upload_id
  total: number;       // 解析出的日志总条数
  batches: number;     // 分批写入的批次数
}

// 上传方法
async uploadLogs(file: File, options?: {
  serviceName?: string;
  namespace?: string;
  onProgress?: (percent: number) => void;
  signal?: AbortSignal;  // 用于取消上传
}): Promise<UploadResult>;
```

## 5. 非功能性约束

| 维度 | 要求 |
|---|---|
| 最大文件 | 500MB |
| 并发上传 | 单次一个文件，上传中可取消 |
| 处理延迟 | 上传完成后 5-30 秒内日志可查（取决于 Redis 队列积压） |
| 浏览器兼容 | 使用标准 FormData / fetch uploadProgress |
| 安全性 | 仅限已认证用户；文件内容不落盘，流式处理后丢弃 |
