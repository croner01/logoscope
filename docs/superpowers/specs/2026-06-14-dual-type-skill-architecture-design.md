# 双类型技能架构设计

## 概述

Logoscope 技能管理系统目前只支持 YAML 格式的**诊断技能**（diagnostic），包含可执行的 `steps`。本设计新增 **参考技能**（reference）类型，支持安装 Superpowers 风格的 Markdown 技能目录，并实现前端查看 + AI 按需注入的双重能力。

## 动机

- 开源社区存在大量 Superpowers 风格的 AI 方法论技能（如 `obra/superpowers`）
- 这些技能提供系统化的调试、排错、分析方法论
- 用户希望一键安装即可在前端查阅，同时在 AI 分析时自动作为上下文注入

## 架构

### 适配器模式

```
SkillManager（调度器，无格式感知）
  ├── YamlAdapter      ← 现有 YAML 诊断技能（从 manager.py 提取）
  ├── MarkdownAdapter  ← 新增：SKILL.md 目录安装
  └── FutureAdapter    ← 预留：.py / .sh / 其他
```

### SkillAdapter 接口

```python
class SkillAdapter(ABC):
    skill_type: str                     # "diagnostic" | "reference"

    def detect(content: str, url: str) -> bool     # 判断是否由本适配器处理
    def read(file_path, source_dir) -> SkillSource  # 从文件系统读取
    def validate(data) -> Optional[str]             # 校验，返回错误或 None
    def install(content, parts, github_url, raw_url) -> SkillSource  # 安装
    def get_display_data(skill) -> Dict             # 返回前端展示数据
```

适配器通过 `SkillManager.register_adapter()` 注册，安装时按注册顺序遍历 `detect()` 派发。

## 数据模型

### SkillSource（后端）

```python
@dataclass
class SkillSource:
    name: str
    display_name: str
    description: str
    source_dir: str               # "builtin" | "installed" | "custom"
    file_path: str
    risk_level: str = "low"
    step_count: int = 0
    skill_type: str = "diagnostic"         # ← 新增
    trigger_patterns: List[str] = field(default_factory=list)
    applicable_components: List[str] = field(default_factory=list)
    install_meta: Dict[str, Any] = field(default_factory=dict)
    # reference 专属
    body: str = ""                           # SKILL.md 正文
    auxiliary_files: Dict[str, str] = field(default_factory=dict)
```

### API 响应

```python
class SkillBrief(BaseModel):
    skill_type: str = "diagnostic"    # ← 新增

class SkillDetail(SkillBrief):
    steps: List[Dict[str, Any]] = []       # diagnostic 用
    body: str = ""                         # ← 新增: reference 用
    auxiliary_files: Dict[str, str] = {}   # ← 新增
```

`steps` 和 `body` 互斥——前端根据 `skill_type` 决定渲染哪个。

## 文件系统布局

```
installed/
├── k8s_pod_diagnostics.yaml        ← YamlAdapter（单文件）
├── linux_system_diagnostics.yaml
└── systematic-debugging/           ← MarkdownAdapter（目录）
    ├── SKILL.md                    ← 主入口
    ├── root-cause-tracing.md       ← 辅助文档
    ├── defense-in-depth.md
    └── condition-based-waiting.md
```

## 安装流程

### 自动发现（用户输入仓库根 URL）

```
https://github.com/obra/superpowers
  ↓ parse_github_url
  owner=obra, repo=superpowers, path=""
  ↓
1. 检查 index.yaml → 404 Not Found
2. GitHub API: GET /repos/obra/superpowers/contents/skills
   → ["systematic-debugging/", "brainstorming/", ...]
3. 对每个子目录:
   下载 SKILL.md → detect() → MarkdownAdapter → 存入 installed/<name>/
```

### 适配器派发

```
下载内容 + URL
  ↓
for adapter in _adapters.values():
    if adapter.detect(content, URL):
        return adapter.install(...)
  ↓
raise ValueError("不支持的技能格式")
```

## MarkdownAdapter 设计

### 检测规则

- 文件后缀 `.md`，或
- URL 路径末尾为 `/SKILL.md`，或
- GitHub API 目录扫描返回的 `type: "dir"` 且目录存在 `SKILL.md`

### 安装行为

1. 下载 `SKILL.md`
2. 解析 YAML front matter 提取 `name` / `description` / `trigger_patterns`
3. 剥离 front matter，剩余 Markdown 存为 `body`
4. 通过 GitHub API 列出同目录文件，下载非 `SKILL.md` 的辅助文件（`.md` / `.py` / `.sh` / `.ts` 等）
5. 存入 `installed/<skill_name>/` 目录

### 读取行为

```python
def read(file_path, source_dir) -> SkillSource:
    # 如果是目录，定位 SKILL.md
    md_path = os.path.join(file_path, "SKILL.md") if os.path.isdir(file_path) else file_path
    content = read_file(md_path)
    front_matter = parse_front_matter(content)
    body = strip_front_matter(content)
    aux = collect_aux_files(os.path.dirname(md_path))
    return SkillSource(skill_type="reference", body=body, auxiliary_files=aux, ...)
```

## 前端展示

### 列表页

技能卡片新增 `skill_type` 徽标：

- `diagnostic` → 显示步骤数 + 📋 诊断
- `reference` → 显示 📖 参考

### 详情面板 — diagnostic（不变）

```
步骤 1: kubectl describe pod
步骤 2: kubectl logs --tail=100
```

### 详情面板 — reference（新增）

```
辅助文档: [root-cause-tracing.md] [defense-in-depth.md]

## Overview
Random fixes waste time...
## The Iron Law
NO FIXES WITHOUT...

[点击辅助文档文件名切换显示]
```

### 新增依赖

```bash
npm install react-markdown
```

### ReferenceSkillView 组件

```tsx
const ReferenceSkillView: React.FC<{
  body: string;
  auxFiles: Record<string, string>;
}> = ({ body, auxFiles }) => {
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const content = activeFile ? auxFiles[activeFile] : body;
  return (
    <div className="flex gap-4">
      {Object.keys(auxFiles).length > 0 && (
        <nav>{Object.keys(auxFiles).map(f => <button>{f}</button>)}</nav>
      )}
      <ReactMarkdown>{content}</ReactMarkdown>
    </div>
  );
};
```

## AI 集成

### 按需匹配

Superpowers 风格的 SKILL.md front matter 通常只有 `name` + `description`，没有 `trigger_patterns`。因此匹配策略采用三层回退：

1. `trigger_patterns` 正则匹配（diagnostic / 用户自定义 reference）
2. `applicable_components` 关键词匹配
3. `description` 关键词匹配（Superpowers 技能的主要匹配方式）

```python
def match_skill_to_query(skill, query: str) -> bool:
    q = query.lower()
    for pattern in skill.trigger_patterns:
        if re.search(pattern, q): return True
    for comp in skill.applicable_components:
        if comp.lower() in q: return True
    # 回退：description 关键词匹配（Superpowers 技能无 trigger 字段）
    for word in skill.description.lower().split():
        if word in q and len(word) > 3:
            return True
    return False
```

策略示例：

| 用户查询 | 匹配的 SKILL.md description |
|----------|----------------------------|
| "Pod 反复 CrashLoopBackOff" | systematic-debugging → "encountering **any bug**, test failure" |
| "DB 查询慢" | root-cause-tracing → "trace **bugs** backward..." |

### System prompt 注入

```
## 可用诊断工具
- k8s_pod_diagnostics（kubectl describe/logs/...）

## 参考方法论（当前问题适用）
### Systematic Debugging
Phase 1: Root Cause Investigation
...（按需注入 Markdown 正文）
```

### 集成点

```
ai-service/ai.py:
  - build_skills_context()        # ← 新增，同时构建 diagnostic + reference 上下文
  - 现有 tool 注册逻辑保留       # diagnostic 沿用
  - reference 注入到 system prompt 的独立段落
```

## 文件变更清单

### 后端（ai-service）

| 文件 | 变更 |
|------|------|
| `ai/skills/manager.py` | 提取适配器逻辑，新增 `register_adapter()`、自动发现 `_discover_skills()` |
| `ai/skills/adapters/__init__.py` | 新增包 |
| `ai/skills/adapters/base.py` | 新增 `SkillAdapter` 抽象基类 |
| `ai/skills/adapters/yaml_adapter.py` | 从 `manager.py` 提取现有 YAML 逻辑 |
| `ai/skills/adapters/markdown_adapter.py` | 新增 |
| `ai/skills/discovery.py` | 新增：GitHub API 目录扫描 |
| `api/skills_router.py` | `SkillBrief` / `SkillDetail` 加 `skill_type` 字段 |
| `api/ai.py` | 新增 `build_skills_context()`，注入参考技能 |

### 前端（frontend）

| 文件 | 变更 |
|------|------|
| `src/pages/SkillManager.tsx` | 加 `skill_type` 展示 + 渲染分支 |
| `src/pages/ReferenceSkillView.tsx` | 新增组件 |
| `package.json` | 加 `react-markdown` 依赖 |

## 测试策略

- **单元测试**：`MarkdownAdapter` 的 `detect()` / `read()` / `_parse_front_matter()` / `_strip_front_matter()`
- **单元测试**：`_discover_skills()` 的 GitHub API 响应解析
- **集成测试**：安装 `obra/superpowers` 仓库的一个技能目录，验证目录结构、辅助文件、前端渲染
- **AI 集成测试**：验证 `build_skills_context()` 对带 trigger 匹配的查询注入正确的方法论

## 向后兼容

- 所有现有 YAML 诊断技能完全不受影响
- `skill_type` 默认 `"diagnostic"`，旧客户端不传此字段仍然兼容
- 安装 API 的 URL 格式不变，只增加自动发现能力
