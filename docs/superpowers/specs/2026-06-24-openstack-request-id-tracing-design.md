# OpenStack Request ID 跨组件日志链路追踪 — 设计文档

## 概述

在 Logoscope 中实现对 OpenStack 日志的 `request_id` / `global_request_id` 结构化提取，实现跨组件（Nova、Cinder、Glance、Neutron 等）的全链路日志关联查询，并利用提取的链路信息增强拓扑图。

## 背景

OpenStack 是分布式系统，一个 API 请求（如创建虚拟机）会穿越多个服务组件。每个组件在日志中通过 `[req-<UUID> ...]` 格式记录请求 ID：

- **旧格式**（Nova/Glance/Neutron 等较老版本 oslo.log）：`[req-<UUID> <project_id> <user_id> ...]`——只有一个 req-id，即本地 `request_id`
- **新格式**（Cinder 等较新版本）：`[req-<UUID> req-<UUID> <project_id> <user_id> ...]`——第一个是 `global_request_id`（调用方传递的 ID），第二个是本服务 `request_id`

当前 Logoscope 仅通过 `message LIKE '%req-xxx%'` 全文搜索，无法结构化查询，也无法自动串联跨服务链路。

## 目标

1. 在日志入库时从 `message` 中提取 `openstack_request_id`、`openstack_global_request_id`
2. 存储为 ClickHouse 独立列，支持索引优化查询
3. 查询接口支持按 request_id 快速检索全链路
4. 前端自动识别 `req-` 搜索词，触发跨组件追踪
5. （可选）将提取的链路信息注入拓扑构建

## 设计方案

### 1. 解析算法（normalizer.py）

```python
_OPENSTACK_BRACKET_RE = re.compile(r'\[([^\]]*req-[0-9a-f-]+[^\]]*)\]')
_OPENSTACK_REQ_ID_RE = re.compile(
    r'(req-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
)

def extract_openstack_request_ids(log_data: Dict[str, Any]) -> Dict[str, str]:
    """
    从 OpenStack 日志消息中提取 request_id 和 global_request_id。
    
    规则：
    - 从 message/log 字段中定位 [...] 括号
    - 提取括号内所有 req-<UUID> 格式的 token
    - 0 个：不是 OpenStack 请求上下文，跳过
    - 1 个：旧格式 → 该值设为 openstack_request_id
    - 2+ 个：新格式 → 第一个为 openstack_global_request_id，第二个为 openstack_request_id
    """
    message = _candidate_text(log_data.get("message")) or _candidate_text(log_data.get("log"))
    if not message:
        return {}
    
    bracket_match = _OPENSTACK_BRACKET_RE.search(message)
    if not bracket_match:
        return {}
    
    req_ids = _OPENSTACK_REQ_ID_RE.findall(bracket_match.group(1))
    
    if len(req_ids) >= 2:
        return {
            "openstack_request_id": req_ids[1],
            "openstack_global_request_id": req_ids[0],
        }
    elif len(req_ids) == 1:
        return {
            "openstack_request_id": req_ids[0],
            "openstack_global_request_id": "",
        }
    return {}
```

在 `normalize_log()` 中的 `extract_trace_info()` 后调用，结果写入 `_raw_attributes` 和 `normalized` 顶层字段。

### 2. ClickHouse 表结构变更

在 `logs.logs` 表增加 2 列：

```sql
ADD COLUMN IF NOT EXISTS openstack_request_id         String DEFAULT '';
ADD COLUMN IF NOT EXISTS openstack_global_request_id   String DEFAULT '';
```

索引策略：使用 Bloom filter 跳数索引（不修改排序键）：

```sql
ALTER TABLE logs.logs
ADD INDEX IF NOT EXISTS idx_openstack_request_id
    (openstack_request_id)
TYPE bloom_filter(0.01)
GRANULARITY 4;

ALTER TABLE logs.logs
ADD INDEX IF NOT EXISTS idx_openstack_global_request_id
    (openstack_global_request_id)
TYPE bloom_filter(0.01)
GRANULARITY 4;
```

### 3. Semantic Engine 数据流

```
normalize_log()
  → extract_openstack_request_ids(message)
  → raw_attributes["openstack_request_id"] = ...
  → raw_attributes["openstack_global_request_id"] = ...
  → normalized["openstack_request_id"] = ...
  → normalized["openstack_global_request_id"] = ...

_prepare_event_row()
  → openstack_request_id = event.get("openstack_request_id", "")
  → openstack_global_request_id = event.get("openstack_global_request_id", "")
  → row 中加入 message 和 labels 之间

_save_event_native()
  → INSERT INTO logs.logs (..., message, openstack_request_id,
     openstack_global_request_id, labels, attributes_json, ...) VALUES
```

### 4. 查询接口

query-service 日志搜索接口新增参数：

```
GET /api/v1/logs?
  openstack_request_id=req-xxx
  &openstack_global_request_id=req-xxx
  &search=&time_start=&time_end=
```

SQL 生成逻辑：

```python
conditions = []
if params.openstack_request_id:
    conditions.append("openstack_request_id = {rid}")
if params.openstack_global_request_id:
    conditions.append("openstack_global_request_id = {gid}")
```

### 5. 前端增强

LogsExplorer 搜索框的自动检测：

```typescript
// 当用户输入以 req- 开头的搜索词时
if (searchText.startsWith('req-') && /^req-[0-9a-f-]{36}$/.test(searchText)) {
    // 同时搜索 request_id 和 global_request_id
    params.set('openstack_request_id', searchText);
    params.set('openstack_global_request_id', searchText);
    // OR 搜索 = 搜到任何匹配 req-id 的日志
}
```

### 6. 拓扑关联（可选，后续）

利用已提取的 `openstack_global_request_id`，在 `HybridTopologyBuilder` 中新增一个数据源：

- 查询指定时间窗口内 `openstack_global_request_id != ''` 的日志
- 按 global_request_id 分组，按时间排序得到服务调用链
- 生成拓扑 edge：source 为调用方，target 为被调用方

## 迁移计划

1. 代码变更在所有 Python 服务中生效
2. DDL 迁移通过 `ADD COLUMN IF NOT EXISTS` 执行（零停机）
3. 新列只对新写入的数据生效——存量数据不会自动回填
4. 回填存量数据：可通过执行一次 ClickHouse SQL 处理历史日志（性能由用户自行评估）：
   ```sql
   ALTER TABLE logs.logs UPDATE
     openstack_request_id = extractAllGroups(message, 'req-([0-9a-f-]{36})')[1][1],
     openstack_global_request_id = ...
   WHERE timestamp >= '2026-06-01'
   ```

## 改动文件清单

| 文件 | 改动类型 |
|------|---------|
| `semantic-engine/normalize/normalizer.py` | 新增 `extract_openstack_request_ids()` + 集成到 `normalize_log()` |
| `semantic-engine/msgqueue/worker.py` | `_prepare_event_row()` 读取并写入新字段 |
| `shared_src/logoscope_storage/adapter.py` | INSERT SQL 增加 2 列 |
| `shared_src/logoscope_storage/adapter.py` | `_init_clickhouse_tables()` 执行迁移 DDL |
| `deploy/migrations/002-add-openstack-request-ids.sql` | 新增迁移 SQL |
| `deploy/clickhouse-init-single.sql` | CREATE TABLE 增加新列 |
| `query-service/query_service/api/logs.py` | 新增查询过滤参数 |
| `frontend/src/pages/LogsExplorer.tsx` | `req-` 搜索词自动检测 |
| `semantic-engine/tests/test_normalizer.py` | 新增测试用例 |

## 设计评审记录

- 2026-06-24: 方案 3（独立列）确认
- 2026-06-24: 排序键选择不加 `openstack_global_request_id`，使用 Bloom filter 跳数索引
