# Logoscope 项目文档

> 版本: v3.21.0
> 更新时间: 2026-02-11

## 📚 文档导航

本文档提供 Logoscope 项目的完整技术文档和用户指南。

### 📖 核心文档

| 文档 | 描述 | 目标读者 |
|------|------|---------|
| [架构设计](./architecture/) | 理解系统整体架构 | 架构师、技术负责人 |
| [API 参考](./api/) | 查询 API 端点说明 | 开发者、集成方 |
| [开发指南](./development/) | 本地开发和测试 | 开发者、贡献者 |
| [运维手册](./operations/) | 部署和运维 | 运维工程师、SRE |
| [用户手册](./user-guide/) | 快速上手和配置 | 最终用户、分析师 |

### 🎯 快速链接

- **项目 README**: [../README.md](../README.md) - 项目总览和快速开始
- **变更日志**: [../CHANGELOG.md](../CHANGELOG.md) - 版本更新记录
- **会话上下文**: [../SESSION_CONTEXT.md](../SESSION_CONTEXT.md) - 开发历史和决策记录

---

## 📊 文档分类

### 架构文档 (architecture/)

系统设计和架构相关文档：

| 文档 | 说明 |
|------|------|
| [数据流架构](./architecture/data-flow.md) | Fluent Bit → OTel → Semantic Engine → ClickHouse/Neo4j |
| [服务拓扑架构](./architecture/service-topology.md) | 多模态数据融合、时间关联、手动调整 |
| [部署架构](./architecture/deployment.md) | Kubernetes 部署方案 |
| [存储架构](./architecture/storage.md) | ClickHouse + Neo4j + Redis Stream |
| [安全架构](./architecture/security.md) | 认证、授权、加密 |

### 设计文档 (design/)

| 文档 | 说明 | 目标读者 |
|------|------|----------|
| [系统设计文档](./design/SYSTEM_DESIGN.md) | 完整的系统设计（架构、模块、数据模型、接口、技术选型）| 架构师、开发者、产品经理 |
| [设计实施指南](./design/IMPLEMENTATION_GUIDE.md) | 部署准备、步骤、配置、验证、监控、故障排查 | 运维工程师、DevOps |

### API 文档 (api/)

API 接口文档和参考：

| 文档 | 说明 |
|------|------|
| [API 参考](./api/reference.md) | 完整 API 端点说明 |
| [拓扑 API](./api/topology.md) | 拓扑构建和查询 API |
| [追踪 API](./api/tracing.md) | 分布式追踪 API |
| [AI 分析 API](./api/ai-analysis.md) | 智能分析 API |
| [标签 API](./api/labels.md) | 标签发现和查询 |
| [告警 API](./api/alerts.md) | 告警规则和事件 |
| [WebSocket API](./api/websocket.md) | 实时通信协议 |

### 开发文档 (development/)

开发和测试相关：

| 文档 | 说明 |
|------|------|
| [环境搭建](./development/setup.md) | 开发环境配置 |
| [代码规范](./development/standards.md) | 编码规范和最佳实践 |
| [测试指南](./development/testing.md) | 单元测试、集成测试 |
| [贡献指南](./development/contributing.md) | PR 流程和代码审查 |
| [调试指南](./development/debugging.md) | 调试技巧和工具 |

### 运维文档 (operations/)

部署和运维相关：

| 文档 | 说明 |
|------|------|
| [监控告警](./operations/monitoring.md) | 监控指标和告警配置 |
| [故障排查](./operations/troubleshooting.md) | 常见问题和解决方案 |
| [性能优化](./operations/performance.md) | 性能调优和扩展性 |
| [备份恢复](./operations/backup.md) | 数据备份和灾难恢复 |
| [升级指南](./operations/upgrade.md) | 版本升级流程 |

### 用户手册 (user-guide/)

最终用户文档：

| 文档 | 说明 |
|------|------|
| [快速开始](./user-guide/quick-start.md) | 5 分钟快速上手 |
| [配置说明](./user-guide/configuration.md) | 详细配置选项 |
| [查询语法](./user-guide/query-syntax.md) | 搜索和过滤语法 |
| [可视化指南](./user-guide/visualization.md) | 图表和拓扑使用 |
| [常见问题](./user-guide/faq.md) | FAQ 和故障排除 |

---

## 🔍 文档使用指南

### 按角色查找

- **架构师**: 查看 [架构设计](./architecture/) 了解系统设计
- **后端开发**: 查看 [API 文档](./api/) 和 [开发指南](./development/)
- **前端开发**: 查看前端文档 (frontend/docs/)
- **运维工程师**: 查看 [运维手册](./operations/)
- **数据分析师**: 查看 [用户手册](./user-guide/) 和 [API 参考](./api/)

### 按场景查找

- **我要部署系统**: [快速开始](./user-guide/quick-start.md) → [环境搭建](./development/setup.md) → [部署架构](./architecture/deployment.md)
- **我要集成 API**: [API 参考](./api/reference.md)
- **遇到问题**: [故障排查](./operations/troubleshooting.md) → [常见问题](./user-guide/faq.md)
- **我要贡献代码**: [贡献指南](./development/contributing.md)

---

## 📝 文档维护规范

### 更新原则

1. **及时性**: 功能变更时同步更新文档
2. **准确性**: 代码实现和文档保持一致
3. **完整性**: 包含所有必要的端点和参数
4. **示例丰富**: 每个 API 提供完整示例
5. **版本标注**: 重要变更标注版本号

### 贡献流程

1. Fork 项目仓库
2. 创建文档分支 (`git checkout -b docs/your-topic`)
3. 修改或新增文档
4. 提交 PR (`git commit` 和 `git push`)
5. 等待 Review 和合并

---

## 📞 获取帮助

- **GitHub Issues**: [提交问题](https://github.com/your-org/logoscope/issues)
- **讨论区**: [GitHub Discussions](https://github.com/your-org/logoscope/discussions)
- **邮件联系**: logoscope@example.com

---

**文档版本**: v3.21.0
**维护者**: Logoscope Team
**最后更新**: 2026-02-11
