# Logoscope 文档索引

> 版本: v3.25.0  
> 更新: 2026-04-01

---

## 📚 文档结构

```
doc/
├── README.md                    # 文档导航 (本文件)
├── api/                         # API 文档
│   ├── reference.md                # API 参考手册
│   ├── topology.md                 # Topology Service 路径标准（TS-03）
│   └── API_UPDATE_SUMMARY.md      # API 更新总结
├── architecture/                 # 架构文档
│   ├── data-flow.md               # 数据流设计
│   ├── log-ingest-query-runtime-path.zh-CN.md # 单条日志接入到前端查询的当前运行时全链路
│   └── service-topology.md        # 服务拓扑设计
├── design/                      # 设计文档
│   ├── SYSTEM_DESIGN.md           # 系统设计文档
│   └── IMPLEMENTATION_GUIDE.md   # 设计实施指南
├── development/                 # 开发文档
│   └── setup.md                  # 开发环境搭建
├── operations/                  # 运维文档
│   ├── database-check-report.md  # 数据库检查报告
│   ├── log-not-found-troubleshooting-map.zh-CN.md # 单条日志查不到时的逐层排查地图
│   ├── clickhouse-release3-performance-2026-03-05.md # ClickHouse 性能优化发布记录（Release 3）
│   ├── redmine-db-profile-single-ha-2026-03-04.md # Redmine 发布记录（数据库 SINGLE/HA）
│   ├── trace-span-cst-delivery-plan-2026-03-04.md # 追踪页面时间与 Span 时长修复方案
│   ├── trace-correlation-release-record-2026-03-03.md # 链路追踪优化发布记录（trace关联修复）
│   ├── core-requirements-iteration-backlog-2026-02-27.md # 核心需求迭代任务清单
│   ├── int03-release-notes-2026-02-27.md # 迭代发布说明（INT-03）
│   └── observability-preagg-v2-ddl.sql # 预聚合 DDL
└── user-guide/                 # 用户指南
    └── quick-start.md            # 快速入门
```

---

## 📖 按角色导航

### 👨‍💻 开发者

#### 新手入门
1. [快速入门](user-guide/quick-start.md) - 5分钟快速了解系统
2. [开发环境搭建](development/setup.md) - 本地开发环境配置
3. [API 参考手册](api/reference.md) - 完整的 API 文档

#### 深入开发
1. [系统设计文档](design/SYSTEM_DESIGN.md) - 架构和模块设计
2. [数据流设计](architecture/data-flow.md) - 数据流转和处理
3. [单条日志接入到前端查询的运行时全链路](architecture/log-ingest-query-runtime-path.zh-CN.md) - 当前代码实现下的日志主链路
4. [服务拓扑设计](architecture/service-topology.md) - 拓扑生成算法

#### 代码贡献
1. 查看项目根目录的 `CLAUDE.md` (如有) - 编码规范和最佳实践
2. 运行单元测试: `./scripts/run-all-tests.sh`
3. 查看 [实施指南](design/IMPLEMENTATION_GUIDE.md) - 部署和配置

### 🚀 运维工程师

#### 部署上线
1. [设计实施指南](design/IMPLEMENTATION_GUIDE.md) - 完整的部署流程
2. [部署检查清单](#部署检查清单) - 上线前验证
3. [数据库检查报告](operations/database-check-report.md) - 数据完整性验证

#### 日常运维
1. [系统设计文档](design/SYSTEM_DESIGN.md) - 了解系统架构
2. [单条日志查不到时的故障排查地图](operations/log-not-found-troubleshooting-map.zh-CN.md) - 按 topic / 表 / 接口 / 字段逐层排查
3. [故障排查指南](design/IMPLEMENTATION_GUIDE.md#故障排查) - 常见问题解决
4. [性能优化指南](design/IMPLEMENTATION_GUIDE.md#优化调整) - 性能调优

#### 监控告警
1. [实施指南 - 监控配置](design/IMPLEMENTATION_GUIDE.md#监控运维) - Prometheus/Grafana 配置
2. [API 系统管理端点](api/reference.md#系统管理-api) - 健康检查和指标
3. [告警配置](design/IMPLEMENTATION_GUIDE.md#告警配置) - AlertManager 规则

### 👨‍💼 产品经理/架构师

#### 系统概览
1. [系统设计文档](design/SYSTEM_DESIGN.md) - 完整的系统设计
2. [数据流设计](architecture/data-flow.md) - 数据流转架构
3. [单条日志接入到前端查询的运行时全链路](architecture/log-ingest-query-runtime-path.zh-CN.md) - 以代码为准的运行时路径
4. [服务拓扑设计](architecture/service-topology.md) - 拓扑发现和增强

#### API 了解
1. [API 参考手册](api/reference.md) - 完整的 API 规范
2. [API 更新总结](api/API_UPDATE_SUMMARY.md) - API 文档状态

#### 技术选型
1. [系统设计 - 技术栈](design/SYSTEM_DESIGN.md#技术选型) - 技术选型和理由
2. [架构设计](design/SYSTEM_DESIGN.md#架构设计) - 架构决策和权衡

### 👨‍🎓 学习者

#### 入门学习
1. [快速入门](user-guide/quick-start.md) - 系统功能介绍
2. [系统设计文档](design/SYSTEM_DESIGN.md#系统概述) - 理解系统概念
3. [术语表](design/SYSTEM_DESIGN.md#附录) - 学习专业术语

#### 进阶学习
1. [数据模型](design/SYSTEM_DESIGN.md#数据模型) - 理解数据存储
2. [接口设计](design/SYSTEM_DESIGN.md#接口设计) - API 设计理念
3. [性能设计](design/SYSTEM_DESIGN.md#性能设计) - 性能优化策略

---

## 🔍 按主题查找

### 部署相关
- [设计实施指南](design/IMPLEMENTATION_GUIDE.md) ⭐ **推荐**
- [开发环境搭建](development/setup.md)
- [数据库检查报告](operations/database-check-report.md)
- [数据库 SINGLE/HA Runbook](database-ha-runbook.md) ⭐ **推荐**
- [ClickHouse 性能优化发布记录（Release 3，2026-03-05）](operations/clickhouse-release3-performance-2026-03-05.md) ⭐ **推荐**
- [Redmine 发布记录（数据库 SINGLE/HA，2026-03-04）](operations/redmine-db-profile-single-ha-2026-03-04.md) ⭐ **推荐**
- [INT-03 发布说明](operations/int03-release-notes-2026-02-27.md) ⭐ **推荐**
- [链路追踪优化发布记录（2026-03-03）](operations/trace-correlation-release-record-2026-03-03.md) ⭐ **推荐**
- [追踪页面时间与 Span 时长修复方案（2026-03-04）](operations/trace-span-cst-delivery-plan-2026-03-04.md)

### API 相关
- [API 参考手册](api/reference.md) ⭐ **推荐**
- [Topology API 路径标准](api/topology.md) ⭐ **推荐**
- [API 更新总结](api/API_UPDATE_SUMMARY.md)

### 架构设计相关
- [系统设计文档](design/SYSTEM_DESIGN.md) ⭐ **推荐**
- [数据流设计](architecture/data-flow.md)
- [单条日志接入到前端查询的运行时全链路](architecture/log-ingest-query-runtime-path.zh-CN.md) ⭐ **推荐**
- [服务拓扑设计](architecture/service-topology.md)

### 性能优化相关
- [系统设计 - 性能设计](design/SYSTEM_DESIGN.md#性能设计)
- [实施指南 - 优化调整](design/IMPLEMENTATION_GUIDE.md#优化调整)
- [优化执行报告](../OPTIMIZATION_EXECUTION_REPORT.md)
- [核心需求迭代任务清单](operations/core-requirements-iteration-backlog-2026-02-27.md) ⭐ **推荐**

### 故障排查相关
- [单条日志查不到时的故障排查地图](operations/log-not-found-troubleshooting-map.zh-CN.md) ⭐ **推荐**
- [实施指南 - 故障排查](design/IMPLEMENTATION_GUIDE.md#故障排查) ⭐ **推荐**
- [系统优化分析](../SYSTEM_COMPREHENSIVE_OPTIMIZATION_ANALYSIS.md)
- [数据库检查报告](operations/database-check-report.md)

---

## 📝 文档更新记录

### 2026-04-01

#### 新增文档
- ✅ `docs/architecture/log-ingest-query-runtime-path.zh-CN.md` - 单条日志从接入到前端查询的当前运行时全链路
- ✅ `docs/operations/log-not-found-troubleshooting-map.zh-CN.md` - 单条日志查不到时的逐层排查地图

#### 说明
- ⚠️ `docs/architecture/data-flow.md` 包含历史 Redis/旧路径描述，阅读当前日志链路时应以新增文档和代码实现为准

### 2026-03-05

#### 新增文档
- ✅ `docs/operations/clickhouse-release3-performance-2026-03-05.md` - ClickHouse Release 3 性能优化发布记录（镜像发布、SQL 执行、滚动聚合验证证据）

### 2026-03-04

#### 新增文档
- ✅ `docs/operations/redmine-db-profile-single-ha-2026-03-04.md` - 数据库 `SINGLE/HA` 双 Profile 的 Redmine 发布记录与操作步骤
- ✅ `docs/database-ha-runbook.md` - 数据库双 Profile 运维手册（部署、校验、故障处理）
- ✅ `docs/operations/trace-span-cst-delivery-plan-2026-03-04.md` - 追踪页面时间与 Span 时长修复交付方案

### 2026-03-03

#### 新增文档
- ✅ `docs/operations/trace-correlation-release-record-2026-03-03.md` - 链路追踪优化发布记录（构建/push/部署/验证证据归档）

### 2026-02-27

#### 新增文档
- ✅ `docs/operations/core-requirements-iteration-backlog-2026-02-27.md` - 核心需求迭代任务清单（可直接入看板）
- ✅ `docs/api/topology.md` - Topology Service 路径标准清单（TS-03）
- ✅ `docs/operations/int03-release-notes-2026-02-27.md` - 迭代发布说明（INT-03）

### 2026-02-11

#### 新增文档
- ✅ `doc/design/SYSTEM_DESIGN.md` - 完整的系统设计文档
- ✅ `doc/design/IMPLEMENTATION_GUIDE.md` - 详细的设计实施指南
- ✅ `doc/api/API_UPDATE_SUMMARY.md` - API 文档更新总结
- ✅ `doc/INDEX.md` - 文档导航索引 (本文件)

#### 更新文档
- ✅ `doc/api/reference.md` - 验证完整性，无需更新
- ✅ `doc/README.md` - 同步更新

---

## 🔧 维护指南

### 添加新文档

1. 确定文档类型 (API/设计/架构/开发/运维/用户指南)
2. 在对应目录创建 Markdown 文件
3. 更新本索引文件 (`doc/INDEX.md`)
4. 遵循 [Markdown 编写规范](#markdown-编写规范)

### 更新现有文档

1. 修改文档内容
2. 更新文档头部的版本号和更新日期
3. 如果涉及 API 变更，更新 `API_UPDATE_SUMMARY.md`
4. 在文档底部添加更新记录

### Markdown 编写规范

#### 文档头部
```markdown
# 文档标题

> 版本: v3.21.0  
> 更新: 2026-02-11  
> 状态: 发布/草稿
```

#### 标题层级
```markdown
# 一级标题 (文档标题)
## 二级标题 (主要章节)
### 三级标题 (子章节)
#### 四级标题 (小节)
```

#### 代码块
````bash
# Bash 命令
```

```python
# Python 代码
```

```sql
-- SQL 查询
```

```yaml
# 配置文件
```

```javascript
// JavaScript 代码
```

#### 表格
```markdown
| 列1 | 列2 | 列3 |
|------|------|------|
| 内容1 | 内容2 | 内容3 |
```

#### 强调
```markdown
- **粗体**: 重要概念
- *斜体*: 术语
- `代码`: 内联代码
- ⭐ 星标: 重要/P0优先级
- ⚠️ 警告: 注意事项
- ✅ 勾选: 已完成/确认
- ❌ 叉号: 错误/未完成
```

---

## 📞 获取帮助

### 文档问题
如果发现文档问题：
1. 检查本文档索引是否指向正确的文件
2. 查看项目根目录的相关报告 (如 `AUTO_COMPLETION_REPORT.md`)
3. 提交 Issue 到项目仓库

### 技术支持
- **项目**: https://github.com/your-org/logoscope
- **文档**: https://docs.logoscope.io
- **问题追踪**: https://github.com/your-org/logoscope/issues

---

**文档版本**: v3.24.0  
**最后更新**: 2026-03-05  
**维护者**: Semantic Engine Team
