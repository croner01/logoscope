# Topology Service API 路径标准（TS-03）

> 版本: v1.0  
> 更新: 2026-02-27  
> 状态: 发布

---

## 1. 说明

本文档是 Topology Service 的权威路径清单，用于对齐前后端、运维脚本和文档引用。

约定：
- `REST` 接口默认基于 `http://topology-service:8080`
- `WebSocket` 接口默认基于 `ws://topology-service:8080`
- Query Service 的实时日志 WS 仍为 `ws://query-service:8080/ws/logs`，不在本文范围

---

## 2. 核心拓扑接口（/api/v1/topology）

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/v1/topology/hybrid` | GET | 混合拓扑（推荐主入口） |
| `/api/v1/topology/enhanced` | GET | 增强拓扑 |
| `/api/v1/topology/stats` | GET | 拓扑统计 |
| `/api/v1/topology/health` | GET | 拓扑路由健康状态 |

说明：
- `/api/v1/topology/hybrid`、`/enhanced`、`/stats` 在多个 router 中存在实现扩展，路径保持一致，返回契约以前端可消费结构为准。

---

## 3. 监控拓扑接口（/api/v1/monitor）

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/v1/monitor/topology` | GET | 监控视图拓扑数据 |
| `/api/v1/monitor/topology/legend` | GET | 监控视图图例 |
| `/api/v1/monitor/topology/search` | GET | 监控视图搜索 |
| `/api/v1/monitor/topology/views` | GET | 预置视图 |
| `/api/v1/monitor/topology/aggregated` | GET | 聚合监控拓扑 |

---

## 4. 拓扑快照接口（/api/v1/topology）

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/v1/topology/snapshots` | POST | 创建快照 |
| `/api/v1/topology/snapshots` | GET | 查询快照列表 |
| `/api/v1/topology/snapshots/{snapshot_id}` | GET | 查询快照详情 |
| `/api/v1/topology/snapshots/compare` | GET | 对比快照 |
| `/api/v1/topology/snapshots/cleanup` | DELETE | 清理旧快照 |

---

## 5. 手动调整接口（/api/v1/topology）

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/v1/topology/nodes/manual` | POST | 手动添加节点 |
| `/api/v1/topology/nodes/manual/{node_id}` | DELETE | 删除节点 |
| `/api/v1/topology/edges/manual` | POST | 手动添加边 |
| `/api/v1/topology/edges/manual` | DELETE | 删除边（query: `source`, `target`） |
| `/api/v1/topology/edges/manual/batch` | POST | 批量添加边 |
| `/api/v1/topology/edges/suppress` | POST | 禁用边 |
| `/api/v1/topology/edges/unsuppress` | POST | 取消禁用边 |
| `/api/v1/topology/config/manual` | GET | 查询手动配置 |
| `/api/v1/topology/config/manual` | DELETE | 清空手动配置 |
| `/api/v1/topology/highlight/comparison` | GET | 高亮对比信息 |

---

## 6. WebSocket 路径标准

| 路径 | 说明 | 交互模型 |
|---|---|---|
| `/ws/topology` | 主实时拓扑通道（推荐） | 双向消息（`ping/get/subscribe`） |
| `/api/v1/topology/subscribe` | 订阅式拓扑推送通道 | 订阅后被动接收 `topology_update/heartbeat` |
| `/ws/status` | WS 连接状态 | HTTP GET |

建议：
- 前端交互式拓扑页面优先使用 `/ws/topology`
- 只需被动订阅更新的客户端可使用 `/api/v1/topology/subscribe`

---

## 7. 常见误用路径（已纠正）

以下路径在历史文档中曾出现，但不是当前标准：
- `WS /api/v1/topology/ws`（应使用 `/ws/topology` 或 `/api/v1/topology/subscribe`）
- `GET /api/v1/topology/monitor`（应使用 `/api/v1/monitor/topology`）
- `WS ws://query-service.../ws/topology`（拓扑 WS 应连接 topology-service）

