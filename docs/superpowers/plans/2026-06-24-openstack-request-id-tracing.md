# OpenStack Request ID 跨组件日志链路追踪 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从 OpenStack 日志消息中提取 `request_id` / `global_request_id`，存储为 ClickHouse 独立列，实现跨组件全链路日志查询

**Architecture:** Semantic Engine normalizer 用正则从 `message` 字段提取的 req-id 写入 `_raw_attributes` 和顶层字段 → worker 写入 ClickHouse 新列 → query-service 新增过滤参数 → 前端自动检测 `req-` 搜索词触发 OR 查询

**Tech Stack:** Python re, ClickHouse ALTER TABLE, FastAPI Query, TypeScript

## Global Constraints

- 所有 Python 文件遵循三组 imports（stdlib → third-party → local）
- 新函数 Type Hints 完整
- 兼容新旧两种 OpenStack oslo.log 格式（1 个 req-id / 2 个 req-id）
- ClickHouse DDL 使用 `IF NOT EXISTS` / `IF NOT EXISTS` 保证幂等
- 解析仅对 `message` / `log` 字段做正则搜索，不做 `attributes_json`

---

### Task 1: Normalizer — `extract_openstack_request_ids()` 函数

**Files:**
- Create (functions in): `semantic-engine/normalize/normalizer.py`
- Test: `semantic-engine/tests/test_normalizer.py`

**Interfaces:**
- Produces: `extract_openstack_request_ids(log_data: Dict[str, Any]) -> Dict[str, str]` — 返回 `{"openstack_request_id": "...", "openstack_global_request_id": "..."}`

- [ ] **Step 1: 在 normalizer.py 顶部 imports 后新增正则常量**

```python
# normalizer.py — 在 _SPAN_ID_TEXT_PATTERNS 后（约 289 行）新增

_OPENSTACK_BRACKET_RE = re.compile(
    r'\[([^\]]*req-[0-9a-f-]+[^\]]*)\]'
)
_OPENSTACK_REQ_ID_RE = re.compile(
    r'(req-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
)
```

- [ ] **Step 2: 新增 `extract_openstack_request_ids()` 函数**

```python
def extract_openstack_request_ids(log_data: Dict[str, Any]) -> Dict[str, str]:
    """
    从 OpenStack 日志消息中提取 request_id 和 global_request_id。

    支持两种 oslo.log format：
      旧格式: [req-<UUID> <project_id> <user_id> ...] → request_id 有值, global 为空
      新格式: [req-<UUID> req-<UUID> <project_id> ...] → global=第一个, request_id=第二个
    0 个 req-id: 不是 OpenStack 请求上下文 → 返回空字典

    Args:
        log_data: 原始日志数据字典

    Returns:
        Dict[str, str]: 包含 openstack_request_id 和 openstack_global_request_id 的字典
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

- [ ] **Step 3: 写单元测试（添加在 test_normalizer.py 末尾）**

```python
# test_normalizer.py — 新增测试类

class TestExtractOpenstackRequestIds:
    """测试 OpenStack request_id 提取函数"""

    def _make_log_data(self, message: str) -> Dict[str, Any]:
        return {"message": message}

    def test_old_format_single_req_id(self):
        """旧格式：1 个 req-id，没有 global_request_id"""
        msg = ('2026-06-22 16:10:47.797 12 INFO nova.api.openstack.wsgi '
               '[req-dcad8c91-32e7-4560-ba5d-8d1d51d0194c '
               'c5f2666761c24ec3a4ad4f14fe75f6cd '
               '4b3634c206414deb85e65c292b78951d - default default] '
               "Action: 'create'")
        result = extract_openstack_request_ids(self._make_log_data(msg))
        assert result["openstack_request_id"] == "req-dcad8c91-32e7-4560-ba5d-8d1d51d0194c"
        assert result["openstack_global_request_id"] == ""

    def test_new_format_two_req_ids(self):
        """新格式：2 个 req-id，第一个是 global_request_id"""
        msg = ('2026-06-22 16:10:53.251 13 INFO cinder.api.openstack.wsgi '
               '[req-dcad8c91-32e7-4560-ba5d-8d1d51d0194c '
               'req-db0b50d4-ddba-4053-ad86-3a9fb9d8e846 '
               'c5f2666761c24ec3a4ad4f14fe75f6cd '
               '4b3634c206414deb85e65c292b78951d - default default] '
               'GET http://cinder/v3/volumes')
        result = extract_openstack_request_ids(self._make_log_data(msg))
        assert result["openstack_request_id"] == "req-db0b50d4-ddba-4053-ad86-3a9fb9d8e846"
        assert result["openstack_global_request_id"] == "req-dcad8c91-32e7-4560-ba5d-8d1d51d0194c"

    def test_volume_id_bracket_not_req_id(self):
        """bracket 内是 volume-xxx 而不是 req- 开头 → 不提取"""
        msg = '2026-06-22 16:10:53.272 13 INFO ... [volume-0c4dc8e8-bdf4-48a8-b114-9f1d42615158] done'
        result = extract_openstack_request_ids(self._make_log_data(msg))
        assert result == {}

    def test_no_openstack_log(self):
        """非 OpenStack 日志 → 不提取"""
        msg = '2026-06-22 16:10:53 INFO query-service Starting service'
        result = extract_openstack_request_ids(self._make_log_data(msg))
        assert result == {}

    def test_extract_from_log_field(self):
        """使用 log 字段而不是 message 字段"""
        log_data = {"log": '[req-dcad8c91-32e7-4560-ba5d-8d1d51d0194c project_id user_id] test'}
        result = extract_openstack_request_ids(log_data)
        assert result["openstack_request_id"] == "req-dcad8c91-32e7-4560-ba5d-8d1d51d0194c"
        assert result["openstack_global_request_id"] == ""

    def test_empty_message(self):
        """空 message → 不提取"""
        assert extract_openstack_request_ids({}) == {}
        assert extract_openstack_request_ids({"message": ""}) == {}
```

- [ ] **Step 4: 运行测试验证全部通过**

```bash
cd /root/logoscope/semantic-engine
python -m pytest tests/test_normalizer.py::TestExtractOpenstackRequestIds -v 2>&1
```

Expected output: 6 passed (可能另有 skip/known issues 不影响)

- [ ] **Step 5: Commit**

```bash
cd /root/logoscope
git add semantic-engine/normalize/normalizer.py semantic-engine/tests/test_normalizer.py
git commit -m "feat(normalizer): 新增 extract_openstack_request_ids() 解析 OpenStack req-id

- 支持新旧两种 oslo.log format（1 个/2 个 req-id）
- 单元测试覆盖 6 种场景：新旧格式、volume-id、非 OpenStack、log 字段、空消息

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Normalizer — 集成到 `normalize_log()`

**Files:**
- Modify: `semantic-engine/normalize/normalizer.py` (around line 183-229)

**Interfaces:**
- Consumes: `extract_openstack_request_ids()` from Task 1
- Produces: OpenStack 字段注入到 `_raw_attributes` 和 `normalized` 顶层

- [ ] **Step 1: 在 normalize_log() 中的 extract_trace_info() 后调用**

找到 `normalizer.py` 中 `normalize_log()` 的 183-189 行：

```python
    trace_info = extract_trace_info(log_data)
    raw_attributes = log_data.get("_raw_attributes")
    if not isinstance(raw_attributes, dict):
        raw_attributes = log_data.get("attributes", {})
    if not isinstance(raw_attributes, dict):
        raw_attributes = {}
    raw_attributes = dict(raw_attributes)
```

在 `raw_attributes = dict(raw_attributes)` 后新增：

```python
    # OpenStack req-id 提取
    openstack_ids = extract_openstack_request_ids(log_data)
```

- [ ] **Step 2: 注入到 _raw_attributes 和 顶层字段**

在 raw_attributes 写入后（约第 189 行），找到 `normalized` 字典构建前的注入逻辑：

在 `existing_source = _candidate_text(raw_attributes.get("trace_id_source")).lower()` 之前（约 190 行），插入：

```python
    if openstack_ids:
        raw_attributes["openstack_request_id"] = openstack_ids.get("openstack_request_id", "")
        raw_attributes["openstack_global_request_id"] = openstack_ids.get("openstack_global_request_id", "")
```

在 `normalized` 字典的构建中（约 225-230 行），在 `"flags": flags,` 后，`"_raw_attributes": raw_attributes` 前插入：

```python
        # OpenStack 请求 ID（独立字段供 _prepare_event_row 写入独立列）
        "openstack_request_id": openstack_ids.get("openstack_request_id", ""),
        "openstack_global_request_id": openstack_ids.get("openstack_global_request_id", ""),
```

最终 `normalized` 字典在 `"flags"` 和 `"_raw_attributes"` 之间多了这两个字段（~227-228 行）：

```python
        "openstack_request_id": openstack_ids.get("openstack_request_id", ""),
        "openstack_global_request_id": openstack_ids.get("openstack_global_request_id", ""),
```

- [ ] **Step 3: 运行 existing tests 确认不破坏既有逻辑**

```bash
cd /root/logoscope/semantic-engine
python -m pytest tests/test_normalizer.py -v 2>&1
```

Expected: 全部通过（既有测试 + 新增 6 个）

- [ ] **Step 4: Commit**

```bash
cd /root/logoscope
git add semantic-engine/normalize/normalizer.py
git commit -m "feat(normalizer): 集成 OpenStack req-id 提取到 normalize_log()

- 在 extract_trace_info() 后调用 extract_openstack_request_ids()
- 结果注入 _raw_attributes（→ attributes_json）和顶层字段（→ 独立列）
- 不影响既有日志处理路径

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: ClickHouse DDL 迁移

**Files:**
- Create: `deploy/migrations/002-add-openstack-request-ids.sql`
- Modify: `deploy/clickhouse-init-single.sql`
- Modify (possible read-only): `deploy/clickhouse-init-replicated.sql`

- [ ] **Step 1: 创建迁移 SQL**

`deploy/migrations/002-add-openstack-request-ids.sql`:

```sql
-- 002: Add openstack_request_id and openstack_global_request_id columns
-- to the logs.logs table for structured OpenStack request tracing.
--
-- These columns are populated by the Semantic Engine normalizer when
-- it detects OpenStack log format patterns in the message field.
--
-- Run: cat 002-add-openstack-request-ids.sql | clickhouse-client

ALTER TABLE logs.logs
ADD COLUMN IF NOT EXISTS openstack_request_id         String DEFAULT '';

ALTER TABLE logs.logs
ADD COLUMN IF NOT EXISTS openstack_global_request_id   String DEFAULT '';

-- Bloom filter skip index for fast exact-match lookups
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

- [ ] **Step 2: 修改 `deploy/clickhouse-init-single.sql` 的 `logs.logs` 建表语句**

在 `message String` (第 49 行) 后、`labels String` (第 50 行) 前增加 2 列：

```sql
    message String,
    openstack_request_id         String DEFAULT '',
    openstack_global_request_id   String DEFAULT '',
    labels String,
```

在 `INDEX idx_logs_message_ngram message TYPE ngrambf_v1(3, 65536, 3, 0) GRANULARITY 1` (第 67 行) 后、`PROJECTION proj_logs_trace_lookup` (第 68 行) 前增加 2 个索引：

```sql
    INDEX idx_logs_message_ngram message TYPE ngrambf_v1(3, 65536, 3, 0) GRANULARITY 1,
    INDEX idx_os_req_id openstack_request_id TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_os_greq_id openstack_global_request_id TYPE bloom_filter(0.01) GRANULARITY 4,
    PROJECTION proj_logs_trace_lookup
```

- [ ] **Step 3: Commit**

```bash
cd /root/logoscope
git add deploy/migrations/002-add-openstack-request-ids.sql deploy/clickhouse-init-single.sql
git commit -m "feat(db): 添加 openstack_request_id / openstack_global_request_id 列

- 迁移 SQL：ADD COLUMN IF NOT EXISTS + Bloom filter 跳数索引
- clickhouse-init-single.sql: 建表 DDL 增加新列定义

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Worker + Adapter 数据流

**Files:**
- Modify: `semantic-engine/msgqueue/worker.py` (around line 1039-1061)
- Modify: `shared_src/logoscope_storage/adapter.py` (around line 1190-1260, INSERT SQL + row tuple)

**Interfaces:**
- Consumes: `normalized["openstack_request_id"]`, `normalized["openstack_global_request_id"]` from Task 2
- Produces: ClickHouse row with 2 new columns inserted

- [ ] **Step 1: 修改 `worker.py` _prepare_event_row()**

在约 1039 行，`message_value` 处理后、`attributes_json = json.dumps(...)` 前，新增提取：

```python
            # OpenStack request IDs
            openstack_request_id = event.get("openstack_request_id", "") or ""
            openstack_global_request_id = event.get("openstack_global_request_id", "") or ""
```

在 row 构建时（约 1042 行），在 `message_value` 后、`event.get("context", {}).get("trace_id", "")` 前，插入 2 个新列：

```python
            row = [
                event.get("id", "") or "",
                ts_datetime,
                ts_datetime,
                event.get("entity", {}).get("name", "unknown") or "unknown",
                k8s_context.get("pod", "unknown") or "unknown",
                k8s_context.get("namespace", "islap") or "islap",
                host or "unknown",
                k8s_context.get("pod_id", "") or "",
                k8s_context.get("container_name", "") or "",
                k8s_context.get("container_id", "") or "",
                k8s_context.get("container_image", "") or "",
                event.get("event", {}).get("level", "info") or "info",
                severity_number,
                message_value,
                openstack_request_id,              # ← 新增
                openstack_global_request_id,       # ← 新增
                event.get("context", {}).get("trace_id", "") or "",
                event.get("context", {}).get("span_id", "") or "",
                flags,
                labels_json,
                attributes_json,
                host_ip,
                resources.get("cpu_limit", "") or "",
                resources.get("cpu_request", "") or "",
                resources.get("memory_limit", "") or "",
                resources.get("memory_request", "") or "",
                event.get("source_cluster", "") or "",
            ]
```

注意：索引 13 是 `message_value`，索引 14-15 是新增的，后续列索引 +2。

- [ ] **Step 2: 修改 `adapter.py` _init_clickhouse_tables() 增加启动时迁移**

在 `adapter.py` 的 `_init_clickhouse_tables()` 方法中（约 776-840 行），在 `CREATE TABLE IF NOT EXISTS` 之后，增加 DDL 迁移执行：

找到 `_init_clickhouse_tables` 方法末尾，约在 `logger.info("ClickHouse tables initialized")` 前，插入：

```python
        # 迁移：为存量 logs 表增加 OpenStack 列（幂等）
        self.ch_client.execute("""
            ALTER TABLE logs.logs
            ADD COLUMN IF NOT EXISTS openstack_request_id String DEFAULT ''
        """)
        self.ch_client.execute("""
            ALTER TABLE logs.logs
            ADD COLUMN IF NOT EXISTS openstack_global_request_id String DEFAULT ''
        """)
        # 跳数索引（幂等）
        self.ch_client.execute("""
            ALTER TABLE logs.logs
            ADD INDEX IF NOT EXISTS idx_openstack_request_id
            (openstack_request_id) TYPE bloom_filter(0.01) GRANULARITY 4
        """)
        self.ch_client.execute("""
            ALTER TABLE logs.logs
            ADD INDEX IF NOT EXISTS idx_openstack_global_request_id
            (openstack_global_request_id) TYPE bloom_filter(0.01) GRANULARITY 4
        """)
```

注意：由于 `self.ch_client` 是 `_ThreadLocalClickHouseClientProxy`，直接调用 `.execute()` 即可。

- [ ] **Step 3: 修改 `adapter.py` _save_event_native() 的 INSERT SQL**

找到约 1219-1223 行的 INSERT SQL 字符串，在 `message` 后增加 2 列：

```sql
INSERT INTO logs.logs (id, timestamp, observed_timestamp, service_name, pod_name, namespace, node_name, pod_id, container_name, container_id, container_image, level, severity_number, message, openstack_request_id, openstack_global_request_id, trace_id, span_id, flags, labels, attributes_json, host_ip, cpu_limit, cpu_request, memory_limit, memory_request, source_cluster) VALUES
```

注意：列数从 25 变为 27。

- [ ] **Step 4: Commit**

```bash
cd /root/logoscope
git add semantic-engine/msgqueue/worker.py shared_src/logoscope_storage/adapter.py
git commit -m "feat(pipeline): 将 OpenStack req-id 写入 ClickHouse 独立列

- worker._prepare_event_row(): 从事件读取新字段并加入 row tuple
- adapter._init_clickhouse_tables(): 启动时自动执行迁移 DDL
- adapter._save_event_native(): INSERT SQL 增加 2 列

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Query Service — 新增过滤参数

**Files:**
- Modify: `query-service/api/query_routes.py`
- Modify: `query-service/api/query_logs_service.py`

**Interfaces:**
- Consumes: `logs.logs.openstack_request_id`, `logs.logs.openstack_global_request_id` columns
- Produces: New API query params that filter using the dedicated columns

- [ ] **Step 1: query_routes.py 增加新参数**

在 `query_routes.py` 中 `query_logs()` 函数的 Query 参数列表（约 1077-1094 行），在 `request_id` 参数后增加：

```python
    openstack_request_id: Optional[str] = Query(None, description="OpenStack request_id 精确过滤（独立列）"),
    openstack_global_request_id: Optional[str] = Query(None, description="OpenStack global_request_id 精确过滤（独立列）"),
    openstack_trace_mode: Optional[str] = Query("or", description="OpenStack req-id 查询模式: and|or — and 需要两个都匹配, or 任一匹配"),
```

在参数标准化段（约 1125-1141 行）增加：

```python
    normalized_openstack_request_id = _normalize_optional_str(openstack_request_id)
    normalized_openstack_global_request_id = _normalize_optional_str(openstack_global_request_id)
    normalized_openstack_trace_mode = _normalize_optional_str(openstack_trace_mode) or "or"
    if normalized_openstack_trace_mode not in ("and", "or"):
        normalized_openstack_trace_mode = "or"
```

在 `await _run_blocking(logs_query_utils.query_logs, ...)` 调用中增加 3 个参数（约 1142 行往后）：

```python
            openstack_request_id=normalized_openstack_request_id,
            openstack_global_request_id=normalized_openstack_global_request_id,
            openstack_trace_mode=normalized_openstack_trace_mode,
```

- [ ] **Step 2: query_logs_service.py — 新增 OpenStack 过滤逻辑**

在 `query_logs()` 函数签名（~740 行）增加 3 个参数：

```python
    openstack_request_id: Optional[str] = None,
    openstack_global_request_id: Optional[str] = None,
    openstack_trace_mode: str = "or",
```

在函数体参数标准化后（约 815 行 `requested_request_ids = ...` 后），新增：

```python
    normalized_openstack_request_id = normalize_optional_str_fn(openstack_request_id)
    normalized_openstack_global_request_id = normalize_optional_str_fn(openstack_global_request_id)
    effective_openstack_trace_mode = str(openstack_trace_mode or "or").strip().lower()
    if effective_openstack_trace_mode not in ("and", "or"):
        effective_openstack_trace_mode = "or"
```

在 `_append_text_search_filter(where_conditions, params, effective_search)` 前（约 985 行），新增过滤条件追加：

```python
    # OpenStack request_id 过滤（使用独立列，避免 attributes_json 全表扫描）
    if normalized_openstack_request_id or normalized_openstack_global_request_id:
        openstack_conditions = []
        if normalized_openstack_request_id:
            openstack_conditions.append(
                "openstack_request_id = {openstack_request_id:String}"
            )
            params["openstack_request_id"] = normalized_openstack_request_id
        if normalized_openstack_global_request_id:
            openstack_conditions.append(
                "openstack_global_request_id = {openstack_global_request_id:String}"
            )
            params["openstack_global_request_id"] = normalized_openstack_global_request_id
        if openstack_conditions:
            joiner = " OR " if effective_openstack_trace_mode == "or" else " AND "
            where_conditions.append(f"({joiner.join(openstack_conditions)})")
```

- [ ] **Step 3: 更新 `_LOGS_LIGHT_FIELDS` 将新列加入 SELECT**

找到 `_LOGS_LIGHT_FIELDS`（约 585-606 行），在 `message` 行后增加：

```python
_LOGS_LIGHT_FIELDS = """
        id,
        timestamp,
        toUnixTimestamp64Nano(timestamp) AS _cursor_ts_ns,
        service_name,
        level,
        message,
        openstack_request_id,
        openstack_global_request_id,
        pod_name,
        ...
"""
```

- [ ] **Step 4: Commit**

```bash
cd /root/logoscope
git add query-service/api/query_routes.py query-service/api/query_logs_service.py
git commit -m "feat(query): 新增 OpenStack req-id 查询过滤参数

- GET /api/v1/logs 新增 openstack_request_id / openstack_global_request_id
- query_logs_service 新增独立列过滤（不走 attributes_json JSONExtract）
- 支持 and/or 组合模式
- _LOGS_LIGHT_FIELDS 包含新列

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: 前端 — `req-` 搜索自动触发全链路追踪

**Files:**
- Modify: `frontend/src/utils/api.ts` (LogsQueryParams 接口 + getLogs 调用)
- Modify: `frontend/src/pages/LogsExplorer.tsx` (搜索逻辑)

- [ ] **Step 1: api.ts — LogsQueryParams 新增字段**

在 `frontend/src/utils/api.ts` 约 728 行 (`search?: string` 后)，增加 3 个字段：

```typescript
  search?: string;
  openstack_request_id?: string;
  openstack_global_request_id?: string;
  openstack_trace_mode?: 'and' | 'or' | string;
  source_service?: string;
```

- [ ] **Step 2: LogsExplorer.tsx — 搜索逻辑增加 req-id 检测**

在 `frontend/src/pages/LogsExplorer.tsx` 中，找到 `apiParams` 构建的 useMemo 块（约 965-994 行），修改第 979 行的搜索条件：

原代码（约 979 行）：
```typescript
    if (debouncedSearchQuery) params.search = debouncedSearchQuery;
```

改为：
```typescript
    // 检测是否为 OpenStack req-id 格式 → 走独立列精准查询
    if (debouncedSearchQuery && /^req-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(debouncedSearchQuery)) {
      params.openstack_request_id = debouncedSearchQuery;
      params.openstack_global_request_id = debouncedSearchQuery;
      params.openstack_trace_mode = 'or';
    } else if (debouncedSearchQuery) {
      params.search = debouncedSearchQuery;
    }
```

同时在 `useMemo` 依赖数组（约 995 行之后）确认 `debouncedSearchQuery` 已在依赖中——它应该已经在。

- [ ] **Step 3: 确认 typecheck 通过**

```bash
cd /root/logoscope/frontend
npm run typecheck 2>&1
```

- [ ] **Step 4: Commit**

```bash
cd /root/logoscope
git add frontend/src/utils/api.ts frontend/src/pages/LogsExplorer.tsx
git commit -m "feat(frontend): req- 搜索词自动触发 OpenStack 全链路追踪

- LogsQueryParams 新增 openstack_request_id/openstack_global_request_id
- 搜索框输入 req-<UUID> 时自动走独立列 OR 搜索
- 普通搜索词走原有 search 逻辑

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### 部署验证步骤

完成所有代码改动后，在测试环境验证：

```bash
# 1. 执行 ClickHouse 迁移
kubectl exec -i -n islap deploy/clickhouse -- clickhouse-client \
  < deploy/migrations/002-add-openstack-request-ids.sql

# 2. 重启 Semantic Engine worker 和 query-service
kubectl rollout restart -n islap deployment semantic-engine
kubectl rollout restart -n islap deployment query-service

# 3. 验证新列存在
kubectl exec -i -n islap deploy/clickhouse -- clickhouse-client \
  --query "DESCRIBE logs.logs" | grep openstack

# 4. 验证新日志写入新列
kubectl exec -i -n islap deploy/clickhouse -- clickhouse-client \
  --query "
    SELECT count() FROM logs.logs
    WHERE openstack_request_id != ''
    AND timestamp >= now() - INTERVAL 10 MINUTE
  "

# 5. 验证 API 查询
curl "http://topology-service/api/v1/logs?openstack_request_id=req-dcad8c91-32e7-4560-ba5d-8d1d51d0194c&limit=5"

# 6. 验证前端自动检测
# 在 LogsExplorer 搜索框中输入: req-dcad8c91-32e7-4560-ba5d-8d1d51d0194c
# 应自动触发跨组件全链路日志查询
```
