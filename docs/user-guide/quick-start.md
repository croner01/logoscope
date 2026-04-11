# Logoscope 快速开始指南

> 版本: v3.21.0
> 更新: 2026-02-11

## 🎯 5 分钟快速上手

本指南帮助你在 **5 分钟内**启动并使用 Logoscope 平台。

---

## 📋 前置条件

### 勀查清单

- [ ] Kubernetes 集群已启动（`kubectl get pods`）
- [ ] ClickHouse 已运行（`docker ps | grep clickhouse`）
- [ ] Neo4j 已运行（`docker ps | grep neo4j`）
- [ ] Redis 已运行（`docker ps | grep redis`）
- [ ] 前端服务已启动（可选）

### 快速启动

如果已有 Kubernetes 环境：

```bash
# 1. 部署数据服务
kubectl apply -f deploy/databases/
kubectl apply -f deploy/semantic-engine.yaml
kubectl apply -f deploy/otel-collector/

# 2. 启动前端（可选）
cd frontend && npm run dev

# 3. 验证部署
kubectl get pods -n logoscope
kubectl get svc -n logoscope
```

---

## 🚀 快速验证

### 1. 访问前端

```bash
# 开发环境
export ENDPOINT=http://localhost:3000

# 生产环境（根据实际配置）
export ENDPOINT=http://your-logoscope-url.com

# 打开浏览器
open $ENDPOINT
```

### 2. 检查后端健康

```bash
# 检查健康状态
curl http://localhost:8080/health

# 预期响应
{
  "status": "healthy",
  "version": "3.21.0",
  "components": {
    "api": "healthy",
    "worker": "healthy",
    "storage": {
      "clickhouse": "healthy",
      "neo4j": "healthy"
    }
  }
}
```

### 3. 查看日志采集状态

```bash
# 查看 Fluent Bit Pod
kubectl get pods -n logoscope -l name=fluent-bit

# 预期输出
NAME                          READY   STATUS    RESTARTS   AGE
fluent-bit-ds-xxxxx           1/1     Running   0          5d
```

### 4. 测试服务拓扑

```bash
# 获取增强拓扑
curl -X GET "http://localhost:8080/api/v1/topology/enhanced?time_window=1%20HOUR&enable_time_correlation=true"

# 预期响应
{
  "nodes": [
    {
      "id": "frontend",
      "label": "Frontend",
      "metrics": {
        "log_count": 15234,
        "data_source": ["traces", "logs"],
        "confidence": 0.85
      }
    }
  ],
  "edges": [
    {
      "source": "frontend",
      "target": "backend",
      "label": "calls",
      "metrics": {
        "call_count": 1523,
        "confidence": 0.9,
        "data_sources": ["traces"]
      }
    }
  ]
}
```

---

## 📊 查看数据

### 1. 查询日志

```bash
# 查询最近 100 条日志
curl -X GET "http://localhost:8080/api/v1/logs?limit=100&sort_by=timestamp:desc"

# 预期输出
{
  "status": "success",
  "data": {
    "logs": [
      {
        "service_name": "semantic-engine",
        "level": "info",
        "message": "Processing OTLP logs...",
        "timestamp": "2026-02-11T10:30:45Z"
      }
    ],
    "total": 1523,
    "limit": 100
  }
}
```

### 2. 查询追踪

```bash
# 查询追踪列表
curl -X GET "http://localhost:8080/api/v1/traces?limit=50&time_window=1%20HOUR"

# 预期输出
{
  "status": "success",
  "data": {
    "traces": [
      {
        "trace_id": "4bf8c9e2...",
        "spans": [
          {
            "service_name": "frontend",
            "operation_name": "GET /api/data",
            "duration_ms": 125,
            "status": "ok"
          }
        ]
      }
    ],
    "total": 234
  }
}
```

### 3. 查看服务拓扑

```bash
# 获取拓扑统计
curl -X GET "http://localhost:8080/api/v1/topology/stats?time_window=1%20HOUR"

# 预期输出
{
  "status": "success",
  "data": {
    "total_nodes": 8,
    "total_edges": 12,
    "avg_confidence": 0.75,
    "data_sources": ["traces", "logs", "metrics"]
  }
}
```

---

## 🎨 核心功能

### 服务拓扑（项目亮点）

Logoscope 的服务拓扑是**核心创新点**，特性：

✅ **不依赖 Trace ID**：使用时间关联算法
✅ **多模态数据融合**：traces + logs + metrics
✅ **五级置信度**：精确到启发式
✅ **完全可调整**：手动修正误差
✅ **历史快照**：版本对比和回滚

```bash
# 获取增强拓扑（包含时间关联）
curl -X GET "http://localhost:8080/api/v1/topology/enhanced?enable_time_correlation=true"

# 对比业界最佳实践
curl -X GET "http://localhost:8080/api/v1/topology/highlight/comparison"
```

### 日志搜索

```bash
# 全文搜索
curl -G "http://localhost:8080/api/v1/logs?search=Failed+to+connect&limit=50"

# 按服务过滤
curl -G "http://localhost:8080/api/v1/logs?service_name=semantic-engine&level=error&limit=20"

# 按时间范围
curl -G "http://localhost:8080/api/v1/logs?start_time=2026-02-11T00:00:00Z&end_time=2026-02-11T23:59:59Z"
```

### AI 分析

```bash
# 智能日志分析
curl -X POST "http://localhost:8080/api/v1/ai/analyze-log" \
  -H "Content-Type: application/json" \
  -d '{
    "log_entries": [
      {
        "service_name": "payment-service",
        "message": "Connection timeout after 30s",
        "timestamp": "2026-02-11T10:30:45Z"
      }
    ],
    "analysis_type": "root_cause"
  }'
```

---

## 🔧 手动调整拓扑

```bash
# 添加已知的服务
curl -X POST "http://localhost:8080/api/v1/topology/nodes/manual" \
  -H "Content-Type: application/json" \
  -d '{"node_id": "redis-cache", "node_type": "cache"}'

# 添加调用关系
curl -X POST "http://localhost:8080/api/v1/topology/edges/manual" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "frontend",
    "target": "backend",
    "confidence": 0.95,
    "reason": "Based on code review"
  }'

# 删除错误的边
curl -X POST "http://localhost:8080/api/v1/topology/edges/suppress?source=wrong-service&target=another-service"

# 查询手动配置
curl -X GET "http://localhost:8080/api/v1/topology/config/manual"
```

---

## 📈 监控指标

### 关键指标

| 指标 | 说明 | 查询方式 |
|------|------|---------|
| **数据采集量** | Fluent Bit 输出 | `kubectl logs` |
| **处理吞吐** | OTel Gateway 处理速度 | `Gateway` Pod metrics |
| **API 响应时间** | Semantic Engine 延迟 | `/health` 端点 |
| **存储空间** | ClickHouse 磁盘使用 | `df -h` on ClickHouse |
| **拓扑节点数** | 服务数量 | `/api/v1/topology/stats` |
| **拓扑边数** | 调用关系 | `/api/v1/topology/stats` |
| **平均置信度** | 拓扑准确性 | `/api/v1/topology/stats` |

---

## 🎓 学习资源

### 完整文档

- **系统架构**: [数据流架构](./architecture/data-flow.md)
- **服务拓扑**: [拓扑架构详解](./architecture/service-topology.md)
- **API 参考**: [完整 API 手册](./api/reference.md)
- **开发环境**: [本地开发搭建](./development/setup.md)

### 视频教程

- **5 分钟部署**: [YouTube](https://youtube.com/watch?v=xxxxx)
- **服务拓扑演示**: [视频教程](https://youtube.com/watch?v=yyyyy)

---

## 🔍 故障排除

### 常见问题

#### Q: 无法访问 API？

**A**: 检查服务状态
```bash
curl http://localhost:8080/health
kubectl get pods -n logoscope
```

#### Q: 没有日志数据？

**A**: 检查数据流
```bash
# 检查 Fluent Bit
kubectl logs -n logoscope -l name=fluent-bit --tail=100

# 检查 OTel Collector
kubectl logs -n logoscope -l name=otel-collector --tail=100

# 检查 Semantic Engine
kubectl logs -n logoscope -l name=semantic-engine --tail=100
```

#### Q: 服务拓扑不显示？

**A**: 检查数据源
```bash
# 查看拓扑统计
curl http://localhost:8080/api/v1/topology/stats

# 查看数据源明细
# 确认 traces、logs、metrics 是否有数据
```

#### Q: 如何手动调整拓扑？

**A**: 参考 [拓扑架构文档](./architecture/service-topology.md)

---

## 📞 下一步

完成快速验证后，建议：

1. **深入学习服务拓扑**：这是项目的核心亮点
2. **配置告警规则**：设置关键指标的监控
3. **集成现有系统**：将 Logoscope 接入现有监控体系
4. **性能优化**：根据实际负载调整配置

---

**需要帮助？**

- 📧 [开发文档](./development/setup.md)
- 📖 [API 参考](./api/reference.md)
- 🔍 [故障排除](./operations/troubleshooting.md)
- 💬 [联系支持](mailto:support@logoscope.example.com)

---

**文档维护**: Semantic Engine Team
**最后更新**: 2026-02-11
