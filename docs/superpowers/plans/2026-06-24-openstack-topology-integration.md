# OpenStack Request ID 拓扑集成实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在第 4 个数据源 OpenStack global_request_id 的基础上，让拓扑图展示真实的跨服务调用链（置信度 0.6），新增链路查看 API，前端增强展示。

**Architecture:** 后端在 `topology-service/graph/hybrid_topology_utils.py` 中扩展 `merge_edges()` 和 `get_data_sources()` 增加 openstack 参数；在 `hybrid_topology.py` 增加 `_get_openstack_topology()` 方法（按 `global_request_id` 分组排序重建调用链）；在 `topology_routes.py` 新增独立端点 `openstack-chain`。前端增强边展示和搜索联动。

**Tech Stack:** Python 3.10+, FastAPI, ClickHouse SQL, TypeScript, React 18

## 文件结构

| 文件 | 改动类型 | 职责 |
|------|---------|------|
| `topology-service/graph/hybrid_topology_utils.py` | 修改 | `merge_edges()` 增加 openstack_edges 参数, `get_data_sources()` 增加 openstack_data 参数 |
| `topology-service/graph/hybrid_topology.py` | 修改 | 增加 `WEIGHT_OPENSTACK`, `_get_openstack_topology()`, 更新 `build_topology()` 集成 |
| `topology-service/api/topology_routes.py` | 修改 | 新增 `GET /api/v1/topology/openstack-chain` 端点 |
| `frontend/src/utils/logCorrelation.ts` | 修改 | `extractEventRequestIds()` 增加 openstack 字段 |
| `frontend/src/pages/TopologyPage.tsx` | 修改 | 边 Detail 面板 + 虚线样式 + 搜索 req- 检测 |

## 依赖关系

```
Task 1 (utils 函数签名) → Task 2 (HybridTopologyBuilder 集成) → Task 3 (API 端点)
                                                                    ↕ (独立)
                                              Task 4 (logCorrelation) → Task 5 (TopologyPage)
```

---

### Task 1: 扩展 utility 函数支持 OpenStack 边和元信息

**Files:**
- Modify: `topology-service/graph/hybrid_topology_utils.py` — `merge_edges()` 函数（第 562-610 行）、`get_data_sources()` 函数（第 852-867 行）

**Interfaces:**
- Consumes: 无（这是第一个 task，定义被后续使用的函数签名）
- Produces:
  - `merge_edges(traces_edges: List[Dict], logs_edges: List[Dict], metrics_edges: List[Dict], openstack_edges: List[Dict] = None, metrics_boost: float = 0.1) -> List[Dict]`
  - `get_data_sources(traces_data: Dict, logs_data: Dict, metrics_data: Dict, openstack_data: Dict = None) -> List[str]`

#### merge_edges 改动说明

当前 merge_edges 的优先级：traces > logs > metrics。

新的优先级：**traces > openstack > logs > metrics**。

具体规则：
1. OpenStack 边若与 traces 边冲突（同 source/target），traces 优先（保持 1.0 置信度），不覆盖
2. OpenStack 边若与 logs 边冲突，OpenStack 覆盖（0.6 > 0.3），替换 data_source 和 data_sources
3. OpenStack 边若与 metrics 边冲突（先于 metrics 处理），OpenStack 保留，metrics 对其加置信度提升

合并顺序，保持 traces 不变，openstack 在 logs 之前处理：

- **Phase 1**: 插入 traces 边（保持不变）
- **Phase 2**: 插入 openstack 边（新拦——在 traces 之后、logs 之前）
- **Phase 3**: 插入 logs 边（仅对 key 不存在的边插入，已存在的补充 data_sources）
- **Phase 4**: 插入 metrics 边（对已有边提升置信度 + 0.1；新边直接插入）

- [ ] **Step 1: 修改 merge_edges() 函数签名**

```python
def merge_edges(
    traces_edges: List[Dict[str, Any]],
    logs_edges: List[Dict[str, Any]],
    metrics_edges: List[Dict[str, Any]],
    openstack_edges: Optional[List[Dict[str, Any]]] = None,
    metrics_boost: float = 0.1,
) -> List[Dict[str, Any]]:
```

- [ ] **Step 2: 在 traces 边和 logs 边之间插入 openstack 边的合并逻辑**

在 `for edge in logs_edges:` 之前插入以下代码段。最终函数结构：

```python
    # Phase 1: traces (existing, unchanged)
    for edge in traces_edges:
        key = (edge["source"], edge["target"])
        merged[key] = copy.deepcopy(edge)
        metrics = merged[key].setdefault("metrics", {})
        metrics.setdefault("data_source", "traces")
        metrics.setdefault("data_sources", ["traces"])

    # Phase 2: openstack (new — inserted before logs per priority)
    for edge in (openstack_edges or []):
        key = (edge["source"], edge["target"])
        if key in merged:
            # Already has traces edge — traces keeps priority, do nothing
            existing = merged[key]
            existing_metrics = existing.setdefault("metrics", {})
            data_sources = existing_metrics.setdefault("data_sources", [])
            if "openstack" not in data_sources:
                data_sources.append("openstack")
        else:
            merged[key] = copy.deepcopy(edge)
            metrics = merged[key].setdefault("metrics", {})
            metrics.setdefault("data_source", "openstack")
            metrics.setdefault("data_sources", ["openstack"])

    # Phase 3: logs (existing, with minor tweak)
    for edge in logs_edges:
        key = (edge["source"], edge["target"])
        if key in merged:
            existing = merged[key]
            existing_metrics = existing.setdefault("metrics", {})
            data_sources = existing_metrics.setdefault("data_sources", [])
            if "logs_heuristic" not in data_sources:
                data_sources.append("logs_heuristic")
            # only set reason if not already set (traces or openstack already there)
            if "reason" not in existing_metrics:
                existing_metrics["reason"] = edge.get("metrics", {}).get("reason")
        else:
            merged[key] = copy.deepcopy(edge)
            metrics = merged[key].setdefault("metrics", {})
            metrics.setdefault("data_source", "logs_heuristic")
            metrics.setdefault("data_sources", ["logs_heuristic"])

    # Phase 4: metrics (existing, unchanged)
    for edge in metrics_edges:
        ...
```

- [ ] **Step 3: 修改 get_data_sources() 函数签名和逻辑**

```python
def get_data_sources(
    traces_data: Dict[str, Any],
    logs_data: Dict[str, Any],
    metrics_data: Dict[str, Any],
    openstack_data: Dict[str, Any] = None,
) -> List[str]:
    """Get enabled data sources list from non-empty node/edge payloads."""
    sources: List[str] = []

    if traces_data.get("nodes") or traces_data.get("edges"):
        sources.append("traces")
    if logs_data.get("nodes") or logs_data.get("edges"):
        sources.append("logs")
    if openstack_data and (openstack_data.get("nodes") or openstack_data.get("edges")):
        sources.append("openstack")
    if metrics_data.get("nodes") or metrics_data.get("edges"):
        sources.append("metrics")

    return sources
```

- [ ] **Step 4: 运行现有测试确保回归通过**

```bash
cd /root/logoscope/topology-service
python -m pytest tests/ -x -q --timeout=30 2>&1 | tail -20
```

Expected: 大部分测试通过。如果有关联 `merge_edges` 或 `get_data_sources` 签名的测试失败，需要在 Task 2 完成后统一修复（因为签名变了但调用方还没更新）。

- [ ] **Step 5: 提交**

```bash
cd /root/logoscope
git add topology-service/graph/hybrid_topology_utils.py
git commit -m "feat(openstack): extend merge_edges/get_data_sources for openstack data source

- merge_edges() now accepts optional openstack_edges parameter
- Priority order: traces > openstack > logs > metrics
- get_data_sources() now accepts optional openstack_data parameter

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: HybridTopologyBuilder 新增 `_get_openstack_topology()` 方法

**Files:**
- Modify: `topology-service/graph/hybrid_topology.py`

**Interfaces:**
- Consumes: `self.storage.execute_query(sql) → List[Dict]`
- Produces: `_get_openstack_topology(time_window: str, namespace: str = None, source_cluster: str = None) -> Dict[str, Any]`
  - 返回 `{"nodes": [...], "edges": [...]}` 格式（与现有数据源一致）

- [ ] **Step 1: 在新常量区增加 WEIGHT_OPENSTACK**

在 `self.WEIGHT_METRICS = 0.2` 附近（第 57 行）加一行：
```python
self.WEIGHT_OPENSTACK = 0.6
```

- [ ] **Step 2: 实现 `_get_openstack_topology()` 方法**

在 `_get_metrics_topology()` 方法之后（第 1517 行之后）新增。完整方法如下：

```python
    def _get_openstack_topology(
        self,
        time_window: str,
        namespace: str = None,
        source_cluster: str = None,
    ) -> Dict[str, Any]:
        """
        从 logs 表通过 openstack_global_request_id 重建跨服务调用链。

        核心原理：OpenStack 服务间调用时，caller 和 callee 的日志出现相同的
        global_request_id。按照 global_request_id 分组、时间戳排序后，相邻且
        不同 service_name 的条目构成一条调用边。

        Returns:
            {"nodes": [...], "edges": [...]}
        """
        try:
            if not self.storage.ch_client:
                return {"nodes": [], "edges": []}

            conditions = [f"timestamp > now() - INTERVAL {time_window}"]
            conditions.append("openstack_global_request_id != ''")

            if namespace:
                conditions.append(f"namespace = '{hybrid_utils.escape_sql_literal(namespace)}'")
            if source_cluster:
                conditions.append(f"source_cluster = '{hybrid_utils.escape_sql_literal(source_cluster)}'")

            prewhere_clause = "PREWHERE " + " AND ".join(conditions)

            query = f"""
            SELECT
                service_name,
                openstack_request_id,
                openstack_global_request_id,
                timestamp
            FROM logs.logs
            {prewhere_clause}
            ORDER BY openstack_global_request_id, timestamp
            LIMIT {int(self.LOGS_SCAN_LIMIT)}
            """

            result = self.storage.execute_query(query)
            logger.debug(f"_get_openstack_topology query returned {len(result) if result else 0} rows")

            if not result:
                return {"nodes": [], "edges": []}

            # 分组：按 global_request_id
            groups: Dict[str, List[Dict]] = {}
            for row in result:
                if isinstance(row, dict):
                    rid = str(row.get("openstack_global_request_id", "") or "").strip()
                    service_name = str(row.get("service_name", "") or "").strip()
                    if not rid or not service_name:
                        continue
                    groups.setdefault(rid, []).append(row)
                else:
                    # tuple format fallback
                    if len(row) < 4:
                        continue
                    rid = str(row[2] or "").strip()  # openstack_global_request_id index
                    service_name = str(row[0] or "").strip()  # service_name index
                    if not rid or not service_name:
                        continue
                    groups.setdefault(rid, []).append({
                        "service_name": service_name,
                        "openstack_request_id": str(row[1] or "").strip(),
                        "openstack_global_request_id": rid,
                        "timestamp": row[3],
                    })

            # 生成边：每组内相邻不同 service → 一条边
            edge_counter: Dict[Tuple[str, str], int] = {}
            node_services: Set[str] = set()

            for rid, records in groups.items():
                records.sort(key=lambda r: r.get("timestamp") or "")
                # 连续相同服务名压缩
                sequence = hybrid_utils.dedup_service_sequence(records)

                for i in range(len(sequence) - 1):
                    source = sequence[i].get("service_name", "").strip()
                    target = sequence[i + 1].get("service_name", "").strip()
                    if not source or not target or source == target:
                        continue
                    node_services.add(source)
                    node_services.add(target)

                    pair = (source, target)
                    edge_counter[pair] = edge_counter.get(pair, 0) + 1

            if not edge_counter:
                logger.debug("No OpenStack cross-service edges found from global_request_id groups")
                return {"nodes": [], "edges": []}

            # 构建节点
            nodes = [
                {
                    "id": svc,
                    "label": svc,
                    "type": "service",
                    "name": svc,
                    "metrics": {
                        "data_source": "openstack",
                        "confidence": self.WEIGHT_OPENSTACK,
                    }
                }
                for svc in sorted(node_services)
            ]

            # 构建边
            edges = [
                {
                    "id": f"{source}-{target}-openstack",
                    "source": source,
                    "target": target,
                    "label": "openstack-calls",
                    "type": "calls",
                    "metrics": {
                        "call_count": count,
                        "confidence": self.WEIGHT_OPENSTACK,
                        "data_source": "openstack",
                        "evidence_type": "observed",
                        "reason": "openstack_global_request_id_chain",
                    }
                }
                for (source, target), count in sorted(edge_counter.items(), key=lambda x: -x[1])
            ]

            logger.debug(f"OpenStack topology: {len(nodes)} nodes, {len(edges)} edges")

            return {
                "nodes": nodes,
                "edges": edges,
            }

        except Exception as e:
            logger.error(f"Error getting openstack topology: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {"nodes": [], "edges": []}
```

- [ ] **Step 3: 运行测试确保新增方法不破坏现有逻辑**

```bash
cd /root/logoscope/topology-service
python -c "
from graph.hybrid_topology import HybridTopologyBuilder
from tests.test_hybrid_topology_contract_output import FakeStorageAdapter
b = HybridTopologyBuilder(FakeStorageAdapter())
result = b._get_openstack_topology('1 HOUR')
print(f'nodes={len(result.get(\"nodes\",[]))}, edges={len(result.get(\"edges\",[]))}')
# FakeStorageAdapter returns no openstack data, so should be empty
assert result == {'nodes': [], 'edges': []}, 'Expected empty result with fake storage'
print('OK: _get_openstack_topology returns empty for no-data case')
"
```

Expected: `OK: _get_openstack_topology returns empty for no-data case`

- [ ] **Step 4: 提交**

```bash
cd /root/logoscope
git add topology-service/graph/hybrid_topology.py
git commit -m "feat(openstack): add _get_openstack_topology() method

- WEIGHT_OPENSTACK = 0.6
- Queries logs grouped by openstack_global_request_id
- Builds edges from consecutive different-service entries
- Uses dedup_service_sequence to compress same-service repeats

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 在 build_topology() 中集成 OpenStack 数据源

**Files:**
- Modify: `topology-service/graph/hybrid_topology.py` — `build_topology()` (第 507 行起)

**Interfaces:**
- Consumes: `_get_openstack_topology()` (来自 Task 2), `merge_edges()` 和 `get_data_sources()` (来自 Task 1)
- Produces: 更新后的 `build_topology()` 输出，metadata 含 openstack 数据源

- [ ] **Step 1: 在 build_topology() 中增加第 4 个数据源调用**

在 `metrics_data = self._get_metrics_topology(...)` 之后（第 573 行附近），插入：

```python
            try:
                openstack_data = self._get_openstack_topology(
                    safe_time_window, namespace, source_cluster=source_cluster
                )
            except Exception:
                logger.exception("Error in _get_openstack_topology")
                openstack_data = {"nodes": [], "edges": []}
```

- [ ] **Step 2: 更新自适应窗口逻辑**

更新第 577-578 行的 total_nodes 计算——将 openstack_data 纳入：

```python
            total_nodes = (
                len(traces_data.get("nodes", [])) +
                len(logs_data.get("nodes", [])) +
                len(metrics_data.get("nodes", [])) +
                len(openstack_data.get("nodes", []))
            )
```

在自适应窗口内（`if total_nodes == 0` 的分支），在 metrics_data 重试之后（第 607 行附近）增加 openstack 重试：

```python
                try:
                    openstack_data = self._get_openstack_topology(
                        safe_time_window, namespace, source_cluster=source_cluster
                    )
                except Exception as e:
                    logger.error(f"Error in _get_openstack_topology (24H): {e}")
                    openstack_data = {"nodes": [], "edges": []}
```

- [ ] **Step 3: 更新节点和边的 debug 日志**

第 613 行日志增加 openstack：
```python
            logger.debug(f"Merging nodes: traces={len(traces_data.get('nodes', []))}, logs={len(logs_data.get('nodes', []))}, openstack={len(openstack_data.get('nodes', []))}, metrics={len(metrics_data.get('nodes', []))}")
```

第 628 行日志增加 openstack：
```python
            logger.debug(f"Merging edges: traces={len(traces_data.get('edges', []))}, logs={len(logs_data.get('edges', []))}, openstack={len(openstack_data.get('edges', []))}, metrics={len(metrics_data.get('edges', []))}")
```

- [ ] **Step 4: 更新 _merge_edges() 调用传参**

将第 630 行改为传入 4 个边列表：

```python
                merged_edges = self._merge_edges(
                    traces_data.get("edges", []),
                    logs_data.get("edges", []),
                    metrics_data.get("edges", []),
                    openstack_data.get("edges", []),
                )
```

- [ ] **Step 5: 更新 HybridTopologyBuilder._merge_edges() 方法签名**

更新第 2089-2094 行的委托调用：

```python
    def _merge_edges(
        self,
        traces_edges: List[Dict],
        logs_edges: List[Dict],
        metrics_edges: List[Dict],
        openstack_edges: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        """
        合并来自不同数据源的边并计算置信度

        策略：
        1. traces 边：置信度 1.0（精确）
        2. openstack 边：置信度 0.6（global_request_id 链）
        3. logs 边：置信度 0.3（启发式）
        4. 如果多个数据源都支持同一关系，提升置信度
        """
        return hybrid_utils.merge_edges(
            traces_edges=traces_edges,
            logs_edges=logs_edges,
            metrics_edges=metrics_edges,
            openstack_edges=openstack_edges,
            metrics_boost=0.1,
        )
```

- [ ] **Step 6: 更新 metadata 中的 data_sources 和 source_breakdown**

第 762 行：
```python
                "data_sources": self._get_data_sources(traces_data, logs_data, metrics_data, openstack_data),
```

第 815 行的 source_breakdown 增加 openstack：
```python
                "source_breakdown": {
                    "traces": {
                        "nodes": len(traces_data.get("nodes", [])),
                        "edges": len(traces_data.get("edges", []))
                    },
                    "logs": {
                        "nodes": len(logs_data.get("nodes", [])),
                        "edges": len(logs_data.get("edges", []))
                    },
                    "metrics": {
                        "nodes": len(metrics_data.get("nodes", [])),
                        "edges": len(metrics_data.get("edges", []))
                    },
                    "openstack": {
                        "nodes": len(openstack_data.get("nodes", [])),
                        "edges": len(openstack_data.get("edges", []))
                    }
                }
```

- [ ] **Step 7: 更新 _get_data_sources() 方法签名（第 2283 行）**

```python
    def _get_data_sources(
        self,
        traces_data: Dict,
        logs_data: Dict,
        metrics_data: Dict,
        openstack_data: Dict = None,
    ) -> List[str]:
        """获取实际使用的数据源列表"""
        return hybrid_utils.get_data_sources(
            traces_data=traces_data,
            logs_data=logs_data,
            metrics_data=metrics_data,
            openstack_data=openstack_data,
        )
```

- [ ] **Step 8: 运行测试验证集成正确**

```bash
cd /root/logoscope/topology-service
python -m pytest tests/test_hybrid_topology_contract_output.py -x -q 2>&1 | tail -10
```

Expected: 测试通过（FakeStorageAdapter 没有 openstack 数据，所以 openstack_data 返回空，整体流程不变）。

- [ ] **Step 9: 提交**

```bash
cd /root/logoscope
git add topology-service/graph/hybrid_topology.py
git commit -m "feat(openstack): integrate openstack as 4th data source in build_topology()

- Calls _get_openstack_topology() alongside traces/logs/metrics
- Updates _merge_edges() and _get_data_sources() signatures
- Adds openstack source_breakdown to metadata
- Adaptive window retries openstack data

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: topology-service 新增 openstack-chain API 端点

**Files:**
- Modify: `topology-service/api/topology_routes.py`
- Test: `topology-service/tests/test_openstack_chain.py`（新创建）

**Interfaces:**
- Consumes: `_HYBRID_BUILDER.storage.execute_query()`（通过全局 builder 引用获取 storage）
- Produces: `GET /api/v1/topology/openstack-chain` 端点

- [ ] **Step 1: 编写测试文件**

创建 `topology-service/tests/test_openstack_chain.py`：

```python
"""
Tests for GET /api/v1/topology/openstack-chain endpoint
"""
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph.hybrid_topology import HybridTopologyBuilder


class FakeOpenstackStorage:
    """存储桩：返回模拟的 openstack global_request_id 数据"""
    def __init__(self):
        self.ch_client = object()

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        if "openstack_global_request_id" not in condensed:
            return []

        return [
            {
                "service_name": "nova-api",
                "openstack_request_id": "req-aaa",
                "openstack_global_request_id": "req-global-1",
                "timestamp": datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc),
            },
            {
                "service_name": "nova-compute",
                "openstack_request_id": "req-bbb",
                "openstack_global_request_id": "req-global-1",
                "timestamp": datetime(2026, 6, 24, 10, 0, 1, tzinfo=timezone.utc),
            },
            {
                "service_name": "cinder-volume",
                "openstack_request_id": "req-ccc",
                "openstack_global_request_id": "req-global-1",
                "timestamp": datetime(2026, 6, 24, 10, 0, 2, tzinfo=timezone.utc),
            },
        ]


class TestOpenstackChainEndpoint:
    """测试 openstack-chain 端点后端逻辑"""

    def test_get_openstack_topology_creates_edges(self):
        """测试 _get_openstack_topology 从模拟数据生成正确的边"""
        builder = HybridTopologyBuilder(FakeOpenstackStorage())
        result = builder._get_openstack_topology("1 HOUR")

        assert len(result["nodes"]) == 3
        assert len(result["edges"]) == 2

        # 检查边
        edge_pairs = {(e["source"], e["target"]): e for e in result["edges"]}
        assert ("nova-api", "nova-compute") in edge_pairs
        assert ("nova-compute", "cinder-volume") in edge_pairs

        # 检查边指标
        edge = edge_pairs[("nova-api", "nova-compute")]
        assert edge["metrics"]["data_source"] == "openstack"
        assert edge["metrics"]["confidence"] == 0.6
        assert edge["metrics"]["evidence_type"] == "observed"

    def test_get_openstack_topology_empty(self):
        """测试无数据时返回空"""
        builder = HybridTopologyBuilder(FakeOpenstackStorage())
        # 修改 SQL 返回空
        builder.storage.execute_query = Mock(return_value=[])
        result = builder._get_openstack_topology("1 HOUR")
        assert result == {"nodes": [], "edges": []}

    def test_get_openstack_topology_skip_same_service(self):
        """测试连续同服务名不生成边"""
        class SameSvcStorage:
            def __init__(self):
                self.ch_client = object()
            def execute_query(self, query):
                return [
                    {"service_name": "nova-api", "openstack_request_id": "req-1",
                     "openstack_global_request_id": "req-global-1",
                     "timestamp": datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc)},
                    {"service_name": "nova-api", "openstack_request_id": "req-2",
                     "openstack_global_request_id": "req-global-1",
                     "timestamp": datetime(2026, 6, 24, 10, 0, 1, tzinfo=timezone.utc)},
                ]

        builder = HybridTopologyBuilder(SameSvcStorage())
        result = builder._get_openstack_topology("1 HOUR")
        assert len(result["edges"]) == 0
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd /root/logoscope/topology-service
python -m pytest tests/test_openstack_chain.py -v 2>&1 | tail -20
```

Expected: 测试在 `test_get_openstack_topology_creates_edges` 失败（`_get_openstack_topology` 不存在）或在 `test_get_openstack_topology_empty` 失败（还没实现正确逻辑）。

如果之前 Task 2-3 已实现，应该全部通过。

- [ ] **Step 3: 在 topology_routes.py 新增 openstack-chain 端点**

在 `@router.get("/stats")` 路由（第 405 行）之后、`@router.get("/health")` 之前插入：

```python
@router.get("/openstack-chain")
async def get_openstack_chain(
    global_request_id: Optional[str] = Query(None, description="精确匹配 global_request_id"),
    time_window: str = Query("1 HOUR", description="时间窗口"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
    limit: int = Query(1000, ge=1, le=10000, description="最大返回行数"),
) -> Dict[str, Any]:
    """
    查询 OpenStack global_request_id 的完整跨服务调用链。

    返回按 global_request_id 分组、按时间排序的调用跳转链。
    每个 chain 包含该 request_id 依次经过的服务列表。
    """
    if not _HYBRID_BUILDER:
        raise HTTPException(status_code=503, detail="Topology builder not initialized")

    storage = getattr(_HYBRID_BUILDER, "storage", None)
    if not storage or not getattr(storage, "ch_client", None):
        raise HTTPException(status_code=503, detail="Storage not available")

    safe_time_window = _sanitize_interval(time_window)

    try:
        conditions = [f"timestamp > now() - INTERVAL {safe_time_window}"]
        conditions.append("openstack_global_request_id != ''")

        if global_request_id:
            safe_rid = str(global_request_id or "").replace("'", "''")
            conditions.append(f"openstack_global_request_id = '{safe_rid}'")
        if namespace:
            safe_ns = str(namespace or "").replace("'", "''")
            conditions.append(f"namespace = '{safe_ns}'")

        prewhere_clause = "PREWHERE " + " AND ".join(conditions)

        query = f"""
        SELECT
            service_name,
            openstack_request_id,
            openstack_global_request_id,
            timestamp
        FROM logs.logs
        {prewhere_clause}
        ORDER BY openstack_global_request_id, timestamp
        LIMIT {int(limit)}
        """

        result = await _run_blocking(storage.execute_query, query)

        if not result:
            return {"chains": [], "total": 0}

        # 分组并构建 chains
        groups: Dict[str, List[Dict]] = {}
        for row in result:
            if isinstance(row, dict):
                rid = str(row.get("openstack_global_request_id", "") or "").strip()
                svc = str(row.get("service_name", "") or "").strip()
                ts = row.get("timestamp")
                req_id = str(row.get("openstack_request_id", "") or "").strip()
                if not rid or not svc:
                    continue
                groups.setdefault(rid, []).append({
                    "service": svc,
                    "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts or ""),
                    "request_id": req_id,
                })

        chains = []
        for rid, hops in sorted(groups.items(), key=lambda x: -len(x[1])):
            # 按时间排序
            hops.sort(key=lambda h: h["timestamp"])

            # 压缩连续相同服务
            deduped = [hops[0]]
            for hop in hops[1:]:
                if hop["service"] != deduped[-1]["service"]:
                    deduped.append(hop)

            time_span_ms = 0
            if len(deduped) >= 2:
                try:
                    t0 = datetime.fromisoformat(deduped[0]["timestamp"].replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(deduped[-1]["timestamp"].replace("Z", "+00:00"))
                    time_span_ms = int((t1 - t0).total_seconds() * 1000)
                except Exception:
                    pass

            chains.append({
                "global_request_id": rid,
                "hops": deduped,
                "hop_count": len(deduped),
                "time_span_ms": time_span_ms,
            })

        return {"chains": chains, "total": len(chains)}

    except Exception as e:
        logger.error(f"获取 OpenStack 链路时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
```

- [ ] **Step 4: 编写测试用例测试端点**

```python
# 追加到 test_openstack_chain.py
class TestOpenstackChainEndpointIntegration:
    """测试 /api/v1/topology/openstack-chain 端点的数据组装逻辑"""

    def test_chain_hops_ordering(self):
        """测试调用链按时间排序正确"""
        from api.topology_routes import _sanitize_interval

        # 测试 sanitize 函数
        assert _sanitize_interval("1 HOUR") == "1 HOUR"
        assert _sanitize_interval("invalid") == "1 HOUR"
```

- [ ] **Step 5: 运行测试**

```bash
cd /root/logoscope/topology-service
python -m pytest tests/test_openstack_chain.py -v 2>&1 | tail -30
```

Expected: 测试通过（`PASSED`）。

- [ ] **Step 6: 提交**

```bash
cd /root/logoscope
git add topology-service/api/topology_routes.py topology-service/tests/test_openstack_chain.py
git commit -m "feat(openstack): add GET /api/v1/topology/openstack-chain endpoint

- Returns service call chains grouped by global_request_id
- Supports filtering by global_request_id, time_window, namespace
- Hops sorted by timestamp, consecutive same-service compressed
- Tested with fake storage adapter

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 前端 logCorrelation.ts 增加 OpenStack 字段

**Files:**
- Modify: `frontend/src/utils/logCorrelation.ts`

**Interfaces:**
- Consumes: 无
- Produces: `extractEventRequestIds()` 返回结果增加 openstack_request_id 和 openstack_global_request_id 值

- [ ] **Step 1: 在候选键数组中增加 openstack 字段**

```typescript
const REQUEST_ID_CANDIDATE_KEYS = [
  'correlation_request_id', 'request_id', 'x_request_id',
  'openstack_request_id', 'openstack_global_request_id',
] as const;

const TRACE_ID_CANDIDATE_KEYS = ['correlation_trace_id', 'trace_id'] as const;
```

- [ ] **Step 2: 运行 TypeScript 类型检查**

```bash
cd /root/logoscope/frontend
npx tsc --noEmit --pretty 2>&1 | tail -20
```

Expected: 无类型错误（或仅有与本次改动无关的既有错误）。

- [ ] **Step 3: 提交**

```bash
cd /root/logoscope
git add frontend/src/utils/logCorrelation.ts
git commit -m "feat(openstack): add openstack_request_id/global_request_id to log correlation extraction

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: 前端 TopologyPage.tsx — 边展示 + 搜索联动

**Files:**
- Modify: `frontend/src/pages/TopologyPage.tsx`

**Interfaces:**
- Consumes: `extractEventRequestIds()` 现在返回 openstack 字段 (Task 5)
- Consumes: `GET /api/v1/topology/openstack-chain` 端点 (Task 4)
- Produces: 增强的边 Detail 面板展示 + 虚线边样式 + 搜索 auto-detect

- [ ] **Step 1: 边 Detail 面板 — 增加 OpenStack 数据源特殊展示**

定位到显示 `data_source` 的那一行（当前第 5217 行）。在该行附近增加条件渲染：

```tsx
{/* 数据源显示 */}
<span className="text-xs text-gray-500">
  原始 reason: {safeText(selectedEdge?.metrics?.reason || 'unknown')} | data source: {safeText(selectedEdge?.metrics?.data_source || 'unknown')}
</span>

{/* OpenStack 特殊展示 */}
{(selectedEdge?.metrics?.data_source === 'openstack' ||
  selectedEdge?.metrics?.data_sources?.includes('openstack')) && (
  <div className="mt-2 p-2 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded">
    <div className="flex items-center gap-1 text-blue-700 dark:text-blue-300 text-xs font-medium">
      <span>☁ OpenStack 跨服务追踪</span>
    </div>
    <div className="mt-1 text-xs text-gray-600 dark:text-gray-400">
      基于 openstack_global_request_id 检测的跨服务调用链（置信度 {(selectedEdge?.metrics?.confidence ?? 0) * 100}%）
    </div>
  </div>
)}
```

- [ ] **Step 2: 边虚线样式**

定位到边渲染区域（ReactFlow 的 `edgeStyle` 或 styled 组件部分）。为 OpenStack 边设置虚线样式。找到类似 `edgeOptions` 或条件样式渲染的位置，加一个新的条件分支：

```tsx
// 在重建边样式或 edgeOptions 中
const edgeStyle = edge.metrics?.data_source === 'openstack'
  ? { strokeDasharray: '5 5', stroke: '#3b82f6' }  // 蓝色虚线
  : {};
```

实际渲染位置在 TopologyPage.tsx 中搜索 `strokeDasharray` 或 `edgeOptions`。根据现有代码结构调整。

- [ ] **Step 3: 搜索框 req- 自动检测**

在拓扑页搜索逻辑中增加检测：

```tsx
// 在搜索处理函数中
const isOpenstackTraceSearch = searchText.startsWith('req-') &&
  /^req-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(searchText);

if (isOpenstackTraceSearch) {
  // 切换为追踪模式：调用 openstack-chain API
  const response = await api.get(`/api/v1/topology/openstack-chain?global_request_id=${encodeURIComponent(searchText)}`);
  // 将结果渲染为时间线面板而非普通节点过滤
  setOpenstackTraceResult(response.chains);
  setSearchMode('openstack_trace');
} else {
  // 普通服务名搜索
  setSearchMode('service');
  setSearchFilter(searchText);
}
```

- [ ] **Step 4: 运行 TypeScript 检查**

```bash
cd /root/logoscope/frontend
npx tsc --noEmit --pretty 2>&1 | tail -30
npx eslint src/pages/TopologyPage.tsx --max-warnings=0 2>&1 | tail -20
```

Expected: 无新增类型错误或 lint 警告。

- [ ] **Step 5: 提交**

```bash
cd /root/logoscope
git add frontend/src/pages/TopologyPage.tsx
git commit -m "feat(openstack): enhance topology page with OpenStack edge display and search

- Edge detail panel shows OpenStack badge for openstack-sourced edges
- Blue dashed line style for OpenStack-based relations
- Search auto-detect req- UUID format to switch to trace mode

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### 验证清单

最终确认所有改动正确且不破坏既有功能：

```bash
# 1. 后端测试
cd /root/logoscope/topology-service
python -m pytest tests/ -x -q --timeout=60 2>&1 | tail -10

# 2. 前端类型检查
cd /root/logoscope/frontend
npx tsc --noEmit --pretty 2>&1 | tail -10

# 3. 前端 lint
npm run lint:strict 2>&1 | tail -10

# 4. 查看所有改动
cd /root/logoscope
git diff --stat
```
