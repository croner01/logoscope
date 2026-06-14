/**
 * Skill Manager — 技能管理页面
 *
 * 功能：
 * - 技能列表（来源分类/统一视图）
 * - GitHub 技能安装
 * - 自定义技能创建
 * - 技能详情查看（YAML 步骤）
 * - 可视化编辑器（编辑步骤）
 * - 技能删除/更新
 */
import React, { useState, useEffect, useCallback } from 'react';
import {
  BookOpen,
  Download,
  Plus,
  Trash2,
  RefreshCw,
  FileCode,
  CheckCircle,
  XCircle,
  Search,
  Edit3,
  Terminal,
} from 'lucide-react';
import axios from 'axios';

/* ─── Types ──────────────────────────────────────────────────────────────── */

interface SkillBrief {
  name: string;
  display_name: string;
  description: string;
  source_dir: 'builtin' | 'installed' | 'custom';
  risk_level: string;
  step_count: number;
}

interface SkillStep {
  id: string;
  title: string;
  tool: string;
  command: string;
  purpose: string;
  depends_on?: string[];
  timeout?: number;
  parse_hints?: Record<string, string[]>;
}

interface SkillDetail extends SkillBrief {
  file_path: string;
  trigger_patterns: string[];
  applicable_components: string[];
  install_meta: Record<string, string>;
  steps: SkillStep[];
}

const API_PREFIX = import.meta.env.VITE_API_URL || '';
const SKILLS_API = `${API_PREFIX}/api/v1/skills`;

/* ─── Helpers ────────────────────────────────────────────────────────────── */

const sourceLabel: Record<string, string> = {
  builtin: '内置',
  installed: '已安装',
  custom: '自定义',
};

const sourceColor: Record<string, string> = {
  builtin: 'bg-blue-100 text-blue-700 border-blue-200',
  installed: 'bg-purple-100 text-purple-700 border-purple-200',
  custom: 'bg-amber-100 text-amber-700 border-amber-200',
};

const riskBadge: Record<string, string> = {
  low: 'bg-green-100 text-green-700',
  medium: 'bg-yellow-100 text-yellow-700',
  high: 'bg-red-100 text-red-700',
};

/* ─── Component ──────────────────────────────────────────────────────────── */

const SkillManager: React.FC = () => {
  const [skills, setSkills] = useState<SkillBrief[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [sourceFilter, setSourceFilter] = useState<string | null>(null);

  // Detail panel
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const [skillDetail, setSkillDetail] = useState<SkillDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Modals
  const [showInstall, setShowInstall] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [showEditor, setShowEditor] = useState(false);
  const [githubUrl, setGithubUrl] = useState('');
  const [newSkillName, setNewSkillName] = useState('');
  const [installError, setInstallError] = useState<string | null>(null);
  const [installSuccess, setInstallSuccess] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  // Editor state
  const [editingYaml, setEditingYaml] = useState('');
  const [editorSkillName, setEditorSkillName] = useState('');

  /* ── Fetch skills ────────────────────────────────────────────────────────── */

  const fetchSkills = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = sourceFilter ? { source: sourceFilter } : {};
      const res = await axios.get(SKILLS_API, { params });
      setSkills(res.data as SkillBrief[]);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load skills';
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [sourceFilter]);

  useEffect(() => {
    fetchSkills();
  }, [fetchSkills]);

  /* ── Fetch skill detail ─────────────────────────────────────────────────── */

  const fetchDetail = useCallback(async (name: string) => {
    setDetailLoading(true);
    setSkillDetail(null);
    try {
      const res = await axios.get(`${SKILLS_API}/${name}`);
      setSkillDetail(res.data as SkillDetail);
    } catch {
      setSkillDetail(null);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  /* ── Install skill ───────────────────────────────────────────────────────── */

  const handleInstall = async () => {
    if (!githubUrl.trim()) return;
    setActionLoading(true);
    setInstallError(null);
    setInstallSuccess(null);
    try {
      await axios.post(`${SKILLS_API}/install`, { url: githubUrl.trim() });
      setInstallSuccess('技能安装成功！');
      setGithubUrl('');
      fetchSkills();
      setTimeout(() => setInstallSuccess(null), 3000);
    } catch (err: unknown) {
      if (axios.isAxiosError(err) && err.response?.data?.detail) {
        setInstallError(String(err.response.data.detail));
      } else {
        setInstallError(err instanceof Error ? err.message : '安装失败');
      }
    } finally {
      setActionLoading(false);
    }
  };

  /* ── Create skill ────────────────────────────────────────────────────────── */

  const handleCreate = async () => {
    if (!newSkillName.trim()) return;
    setActionLoading(true);
    setInstallError(null);
    try {
      await axios.post(`${SKILLS_API}/create`, { name: newSkillName.trim() });
      setShowCreate(false);
      setNewSkillName('');
      fetchSkills();
      // Show the new skill detail
      setSelectedSkill(newSkillName.trim());
      fetchDetail(newSkillName.trim());
    } catch (err: unknown) {
      const msg = axios.isAxiosError(err) && err.response?.data?.detail
        ? String(err.response.data.detail)
        : err instanceof Error ? err.message : '创建失败';
      setInstallError(msg);
    } finally {
      setActionLoading(false);
    }
  };

  /* ── Delete skill ────────────────────────────────────────────────────────── */

  const handleDelete = async (name: string) => {
    if (!window.confirm(`确定要删除「${name}」吗？此操作不可恢复。`)) return;
    try {
      await axios.delete(`${SKILLS_API}/${name}`);
      setSelectedSkill(null);
      setSkillDetail(null);
      fetchSkills();
    } catch (err: unknown) {
      const msg = axios.isAxiosError(err) && err.response?.data?.detail
        ? String(err.response.data.detail)
        : '删除失败';
      alert(msg);
    }
  };

  /* ── Update skill ─────────────────────────────────────────────────────────── */

  const handleUpdate = async (name: string) => {
    try {
      await axios.post(`${SKILLS_API}/${name}/update`);
      fetchSkills();
      if (selectedSkill === name) fetchDetail(name);
    } catch (err: unknown) {
      const msg = axios.isAxiosError(err) && err.response?.data?.detail
        ? String(err.response.data.detail)
        : '更新失败';
      alert(msg);
    }
  };

  /* ── Save edited YAML ────────────────────────────────────────────────────── */

  const handleSaveYaml = async () => {
    if (!editorSkillName || !editingYaml.trim()) return;
    setActionLoading(true);
    try {
      await axios.put(`${SKILLS_API}/${editorSkillName}`, {
        name: editorSkillName,
        yaml_content: editingYaml,
      });
      setShowEditor(false);
      fetchSkills();
      if (selectedSkill === editorSkillName) fetchDetail(editorSkillName);
    } catch (err: unknown) {
      const msg = axios.isAxiosError(err) && err.response?.data?.detail
        ? String(err.response.data.detail)
        : '保存失败';
      alert(msg);
    } finally {
      setActionLoading(false);
    }
  };

  /* ── Open editor ─────────────────────────────────────────────────────────── */

  const openEditor = (detail: SkillDetail) => {
    // Build YAML content from the detail data
    const yamlLines: string[] = [
      `name: ${detail.name}`,
      `display_name: "${detail.display_name}"`,
      `description: >`,
      `  ${detail.description}`,
      `risk_level: ${detail.risk_level}`,
      `max_steps: ${detail.step_count}`,
      '',
      'steps:',
    ];
    detail.steps.forEach(step => {
      yamlLines.push(`  - id: ${step.id}`);
      yamlLines.push(`    title: "${step.title}"`);
      yamlLines.push(`    tool: ${step.tool}`);
      yamlLines.push(`    command: |`);
      step.command.split('\n').forEach(line => {
        yamlLines.push(`      ${line}`);
      });
      yamlLines.push(`    purpose: "${step.purpose}"`);
      if (step.timeout) yamlLines.push(`    timeout: ${step.timeout}`);
      if (step.depends_on?.length) {
        yamlLines.push(`    depends_on: [${step.depends_on.join(', ')}]`);
      }
      if (step.parse_hints && Object.keys(step.parse_hints).length) {
        yamlLines.push('    parse_hints:');
        Object.entries(step.parse_hints).forEach(([k, v]) => {
          yamlLines.push(`      ${k}: [${(Array.isArray(v) ? v : [v]).join(', ')}]`);
        });
      }
      yamlLines.push('');
    });
    setEditingYaml(yamlLines.join('\n'));
    setEditorSkillName(detail.name);
    setShowEditor(true);
  };

  /* ── Filtered & grouped skills ──────────────────────────────────────────── */

  const allSources: Array<{ key: string | null; label: string }> = [
    { key: null, label: '全部' },
    { key: 'builtin', label: '内置' },
    { key: 'installed', label: '已安装' },
    { key: 'custom', label: '自定义' },
  ];

  const displaySkills = skills.filter(s => {
    if (sourceFilter && s.source_dir !== sourceFilter) return false;
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      return (
        s.name.toLowerCase().includes(q) ||
        s.display_name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q)
      );
    }
    return true;
  });

  /* ── Render ──────────────────────────────────────────────────────────────── */

  return (
    <div className="flex h-full" style={{ background: 'var(--app-bg)' }}>
      {/* ── Left panel: skill list ────────────────────────────────────────── */}
      <div className="w-[420px] flex-shrink-0 border-r flex flex-col overflow-hidden"
        style={{ borderColor: 'var(--sidebar-border)' }}>

        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b"
          style={{ borderColor: 'var(--sidebar-border)' }}>
          <div className="flex items-center gap-2">
            <BookOpen size={18} className="text-teal-500" />
            <h2 className="text-sm font-semibold" style={{ color: 'var(--app-text)' }}>
              技能管理
            </h2>
            <span className="text-xs px-1.5 py-0.5 rounded bg-teal-100 text-teal-700 ml-1">
              {skills.length}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              onClick={() => setShowCreate(true)}
              className="flex items-center gap-1 px-2.5 py-1.5 rounded text-xs font-medium
                bg-teal-500 text-white hover:bg-teal-600 transition-colors"
              title="创建自定义技能"
            >
              <Plus size={14} />
              创建
            </button>
            <button
              onClick={() => setShowInstall(true)}
              className="flex items-center gap-1 px-2.5 py-1.5 rounded text-xs font-medium
                bg-purple-500 text-white hover:bg-purple-600 transition-colors"
              title="从 GitHub 安装技能"
            >
              <Download size={14} />
              安装
            </button>
          </div>
        </div>

        {/* Search */}
        <div className="px-4 py-2.5">
          <div className="relative">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
            <input
              type="text"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              placeholder="搜索技能..."
              className="w-full pl-8 pr-3 py-1.5 rounded text-xs border
                bg-white/50 focus:bg-white focus:outline-none focus:ring-1 focus:ring-teal-400"
              style={{ borderColor: 'var(--sidebar-border)' }}
            />
          </div>
        </div>

        {/* Source filter tabs */}
        <div className="flex gap-0.5 px-4 pb-2">
          {allSources.map(src => {
            const active = sourceFilter === src.key;
            const count = src.key === null
              ? skills.length
              : skills.filter(s => s.source_dir === src.key).length;
            return (
              <button
                key={src.label}
                onClick={() => setSourceFilter(src.key)}
                className={`px-2.5 py-1 text-xs rounded-md transition-colors ${
                  active
                    ? 'bg-teal-100 text-teal-700 font-medium'
                    : 'text-slate-500 hover:bg-slate-100'
                }`}
              >
                {src.label}
                <span className="ml-1 opacity-60">({count})</span>
              </button>
            );
          })}
        </div>

        {/* Skill list */}
        <div className="flex-1 overflow-y-auto px-3 pb-4">
          {loading && (
            <div className="flex items-center justify-center py-12 text-xs text-slate-400">
              <RefreshCw size={14} className="mr-2 animate-spin" />
              加载中...
            </div>
          )}
          {error && (
            <div className="mx-2 mt-3 px-3 py-2 rounded bg-red-50 border border-red-200 text-xs text-red-600">
              {error}
              <button onClick={fetchSkills} className="ml-2 underline">重试</button>
            </div>
          )}
          {!loading && !error && displaySkills.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16 text-xs text-slate-400">
              <BookOpen size={32} className="mb-2 opacity-30" />
              {searchQuery ? '没有匹配的技能' : sourceFilter === 'custom' ? '还没有自定义技能' : '暂无技能'}
            </div>
          )}
          {!loading && displaySkills.map(skill => {
            const isSelected = selectedSkill === skill.name;
            return (
              <div
                key={`${skill.source_dir}:${skill.name}`}
                onClick={() => {
                  setSelectedSkill(skill.name);
                  fetchDetail(skill.name);
                }}
                className={`mb-1.5 px-3 py-2.5 rounded-lg cursor-pointer border transition-all ${
                  isSelected
                    ? 'bg-teal-50 border-teal-200 shadow-sm'
                    : 'bg-white border-transparent hover:border-slate-200 hover:shadow-sm'
                }`}
                style={{ borderColor: isSelected ? undefined : 'var(--sidebar-border)' }}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="text-sm font-medium truncate"
                        style={{ color: 'var(--app-text)' }}>
                        {skill.display_name}
                      </span>
                      <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium ${sourceColor[skill.source_dir]}`}>
                        {sourceLabel[skill.source_dir] || skill.source_dir}
                      </span>
                    </div>
                    <p className="text-xs text-slate-400 mt-0.5 line-clamp-2">
                      {skill.description || '暂无描述'}
                    </p>
                  </div>
                  <div className="flex items-center gap-1.5 flex-shrink-0 mt-0.5">
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${riskBadge[skill.risk_level] || 'bg-slate-100 text-slate-600'}`}>
                      {skill.risk_level === 'low' ? '低风险' : skill.risk_level === 'high' ? '高风险' : '中风险'}
                    </span>
                    <span className="text-[10px] text-slate-400 bg-slate-100 px-1.5 py-0.5 rounded">
                      {skill.step_count} 步
                    </span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* ── Right panel: skill detail ─────────────────────────────────────── */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {!selectedSkill && (
          <div className="flex-1 flex flex-col items-center justify-center text-slate-300">
            <BookOpen size={48} className="mb-3 opacity-30" />
            <p className="text-sm">选择一个技能查看详情</p>
          </div>
        )}

        {selectedSkill && detailLoading && (
          <div className="flex-1 flex items-center justify-center text-xs text-slate-400">
            <RefreshCw size={14} className="mr-2 animate-spin" />
            加载中...
          </div>
        )}

        {selectedSkill && skillDetail && !detailLoading && (
          <div className="flex-1 overflow-y-auto p-6">
            {/* ── Detail header ─────────────────────────────────────────── */}
            <div className="flex items-start justify-between mb-5">
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <h1 className="text-lg font-semibold" style={{ color: 'var(--app-text)' }}>
                    {skillDetail.display_name}
                  </h1>
                  <span className={`text-xs px-2 py-0.5 rounded-full border font-medium ${sourceColor[skillDetail.source_dir]}`}>
                    {sourceLabel[skillDetail.source_dir] || skillDetail.source_dir}
                  </span>
                </div>
                <p className="text-xs text-slate-400 mt-1">
                  <code className="text-teal-600">{skillDetail.name}</code>
                  {' · '}共 {skillDetail.step_count} 步
                  {' · '}
                  <span className={riskBadge[skillDetail.risk_level]?.replace('text-', 'text-') || 'text-slate-500'}>
                    {skillDetail.risk_level === 'low' ? '低风险' : skillDetail.risk_level === 'high' ? '高风险' : '中风险'}
                  </span>
                </p>
                {skillDetail.description && (
                  <p className="text-sm text-slate-500 mt-2 max-w-xl">
                    {skillDetail.description}
                  </p>
                )}
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                {skillDetail.source_dir === 'installed' && (
                  <button
                    onClick={() => handleUpdate(skillDetail.name)}
                    className="flex items-center gap-1 px-2.5 py-1.5 rounded text-xs font-medium
                      border border-slate-200 hover:bg-slate-50 transition-colors"
                    title="从 GitHub 重新下载"
                  >
                    <RefreshCw size={13} />
                    更新
                  </button>
                )}
                {skillDetail.source_dir === 'custom' && (
                  <button
                    onClick={() => openEditor(skillDetail)}
                    className="flex items-center gap-1 px-2.5 py-1.5 rounded text-xs font-medium
                      border border-slate-200 hover:bg-slate-50 transition-colors"
                    title="编辑技能 YAML"
                  >
                    <Edit3 size={13} />
                    编辑
                  </button>
                )}
                {(skillDetail.source_dir === 'installed' || skillDetail.source_dir === 'custom') && (
                  <button
                    onClick={() => handleDelete(skillDetail.name)}
                    className="flex items-center gap-1 px-2.5 py-1.5 rounded text-xs font-medium
                      text-red-500 border border-red-200 hover:bg-red-50 transition-colors"
                    title="删除此技能"
                  >
                    <Trash2 size={13} />
                    删除
                  </button>
                )}
              </div>
            </div>

            {/* ── Metadata grid ─────────────────────────────────────────── */}
            <div className="grid grid-cols-3 gap-4 mb-6">
              <div className="px-3 py-2.5 rounded-lg bg-white border" style={{ borderColor: 'var(--sidebar-border)' }}>
                <p className="text-[10px] uppercase tracking-wider text-slate-400 mb-1">来源</p>
                <p className="text-sm font-medium">{sourceLabel[skillDetail.source_dir]}</p>
              </div>
              <div className="px-3 py-2.5 rounded-lg bg-white border" style={{ borderColor: 'var(--sidebar-border)' }}>
                <p className="text-[10px] uppercase tracking-wider text-slate-400 mb-1">步骤数</p>
                <p className="text-sm font-medium">{skillDetail.step_count}</p>
              </div>
              <div className="px-3 py-2.5 rounded-lg bg-white border" style={{ borderColor: 'var(--sidebar-border)' }}>
                <p className="text-[10px] uppercase tracking-wider text-slate-400 mb-1">风险等级</p>
                <p className="text-sm font-medium">
                  {skillDetail.risk_level === 'low' ? '✅ 低风险' : skillDetail.risk_level === 'high' ? '⚠️ 高风险' : '⚡ 中风险'}
                </p>
              </div>
            </div>

            {/* ── Trigger patterns & applicable components ─────────────── */}
            {(skillDetail.trigger_patterns.length > 0 || skillDetail.applicable_components.length > 0) && (
              <div className="mb-6 p-3 rounded-lg bg-white border" style={{ borderColor: 'var(--sidebar-border)' }}>
                {skillDetail.trigger_patterns.length > 0 && (
                  <div className="mb-2">
                    <p className="text-[10px] uppercase tracking-wider text-slate-400 mb-1.5">触发关键词</p>
                    <div className="flex flex-wrap gap-1">
                      {skillDetail.trigger_patterns.map(p => (
                        <span key={p} className="text-[11px] px-2 py-0.5 rounded bg-blue-50 text-blue-600 font-mono">
                          {p}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {skillDetail.applicable_components.length > 0 && (
                  <div>
                    <p className="text-[10px] uppercase tracking-wider text-slate-400 mb-1.5">适用组件</p>
                    <div className="flex flex-wrap gap-1">
                      {skillDetail.applicable_components.map(c => (
                        <span key={c} className="text-[11px] px-2 py-0.5 rounded bg-teal-50 text-teal-600">
                          {c}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* ── Install meta ──────────────────────────────────────────── */}
            {skillDetail.install_meta?.original_url && (
              <div className="mb-6 p-3 rounded-lg bg-purple-50 border border-purple-200">
                <p className="text-[10px] uppercase tracking-wider text-purple-500 mb-1">安装信息</p>
                <p className="text-xs text-purple-700 font-mono">{skillDetail.install_meta.original_url}</p>
              </div>
            )}

            {/* ── Steps ─────────────────────────────────────────────────── */}
            <h3 className="text-sm font-semibold mb-3 flex items-center gap-2"
              style={{ color: 'var(--app-text)' }}>
              <Terminal size={15} className="text-teal-500" />
              诊断步骤
            </h3>
            <div className="space-y-3">
              {skillDetail.steps.map((step, idx) => (
                <div key={step.id}
                  className="rounded-lg bg-white border overflow-hidden"
                  style={{ borderColor: 'var(--sidebar-border)' }}>
                  {/* Step header */}
                  <div className="px-4 py-2.5 bg-slate-50 border-b flex items-center gap-2"
                    style={{ borderColor: 'var(--sidebar-border)' }}>
                    <span className="flex items-center justify-center w-5 h-5 rounded-full bg-teal-500 text-white text-[10px] font-bold">
                      {idx + 1}
                    </span>
                    <span className="text-sm font-medium">{step.title}</span>
                    {step.depends_on && step.depends_on.length > 0 && (
                      <span className="text-[10px] text-slate-400 ml-auto">
                        依赖: {step.depends_on.join(', ')}
                      </span>
                    )}
                  </div>
                  {/* Step body */}
                  <div className="px-4 py-3">
                    <div className="mb-2">
                      <p className="text-[10px] uppercase tracking-wider text-slate-400 mb-1">目的</p>
                      <p className="text-xs text-slate-600">{step.purpose}</p>
                    </div>
                    <div>
                      <p className="text-[10px] uppercase tracking-wider text-slate-400 mb-1">命令</p>
                      <pre className="text-xs bg-slate-900 text-green-300 p-2.5 rounded overflow-x-auto font-mono leading-relaxed whitespace-pre-wrap">
                        {step.command}
                      </pre>
                    </div>
                    <div className="flex items-center gap-3 mt-2">
                      <span className="text-[10px] text-slate-400">
                        工具: <code className="text-teal-600">{step.tool}</code>
                      </span>
                      {step.timeout && (
                        <span className="text-[10px] text-slate-400">
                          超时: {step.timeout}s
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* ── Install Modal ────────────────────────────────────────────────── */}
      {showInstall && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
          onClick={() => { setShowInstall(false); setInstallError(null); setInstallSuccess(null); }}>
          <div className="w-[480px] bg-white rounded-xl shadow-xl p-6"
            onClick={e => e.stopPropagation()}>
            <h3 className="text-base font-semibold mb-1 flex items-center gap-2">
              <Download size={16} className="text-purple-500" />
              从 GitHub 安装技能
            </h3>
            <p className="text-xs text-slate-400 mb-4">
              输入 GitHub 技能 URL 来安装社区共享的诊断技能。
            </p>

            <label className="text-xs font-medium text-slate-600 mb-1 block">
              GitHub URL
            </label>
            <input
              type="text"
              value={githubUrl}
              onChange={e => setGithubUrl(e.target.value)}
              placeholder="github://owner/repo/path/to/skill.yaml"
              className="w-full px-3 py-2 rounded-lg border text-sm font-mono
                focus:outline-none focus:ring-1 focus:ring-purple-400 mb-4"
              style={{ borderColor: 'var(--sidebar-border)' }}
            />

            {/* Format hint */}
            <div className="mb-4 p-3 rounded-lg bg-slate-50 border text-xs text-slate-500">
              <p className="font-medium mb-1">支持的格式：</p>
              <ul className="space-y-1 ml-3 list-disc">
                <li><code className="text-purple-600">github://owner/repo/skills/my_skill.yaml</code></li>
                <li><code className="text-purple-600">github://owner/repo@v1.2.0/skills/my_skill.yaml</code></li>
                <li><code className="text-purple-600">github://logoscope/skills</code> — 同时安装 index.yaml 中列出的所有技能</li>
              </ul>
            </div>

            {installError && (
              <div className="mb-3 px-3 py-2 rounded bg-red-50 border border-red-200 text-xs text-red-600">
                <XCircle size={13} className="inline mr-1" />
                {installError}
              </div>
            )}
            {installSuccess && (
              <div className="mb-3 px-3 py-2 rounded bg-green-50 border border-green-200 text-xs text-green-600">
                <CheckCircle size={13} className="inline mr-1" />
                {installSuccess}
              </div>
            )}

            <div className="flex justify-end gap-2">
              <button
                onClick={() => { setShowInstall(false); setInstallError(null); setInstallSuccess(null); }}
                className="px-4 py-2 rounded-lg text-xs font-medium border hover:bg-slate-50 transition-colors"
              >
                取消
              </button>
              <button
                onClick={handleInstall}
                disabled={actionLoading || !githubUrl.trim()}
                className="px-4 py-2 rounded-lg text-xs font-medium bg-purple-500 text-white
                  hover:bg-purple-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors
                  flex items-center gap-1.5"
              >
                {actionLoading ? <RefreshCw size={13} className="animate-spin" /> : <Download size={13} />}
                {actionLoading ? '安装中...' : '安装'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Create Modal ─────────────────────────────────────────────────── */}
      {showCreate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
          onClick={() => { setShowCreate(false); setInstallError(null); }}>
          <div className="w-[400px] bg-white rounded-xl shadow-xl p-6"
            onClick={e => e.stopPropagation()}>
            <h3 className="text-base font-semibold mb-1 flex items-center gap-2">
              <Plus size={16} className="text-teal-500" />
              创建自定义技能
            </h3>
            <p className="text-xs text-slate-400 mb-4">
              输入技能名称，系统将生成一个包含步骤模板的 YAML 文件。
            </p>

            <label className="text-xs font-medium text-slate-600 mb-1 block">
              技能名称
            </label>
            <input
              type="text"
              value={newSkillName}
              onChange={e => setNewSkillName(e.target.value)}
              placeholder="如：my_nginx_check"
              className="w-full px-3 py-2 rounded-lg border text-sm
                focus:outline-none focus:ring-1 focus:ring-teal-400 mb-4"
              style={{ borderColor: 'var(--sidebar-border)' }}
            />
            <p className="text-xs text-slate-400 mb-4">
              名称必须以字母开头，仅包含字母、数字和下划线。
            </p>

            {installError && (
              <div className="mb-3 px-3 py-2 rounded bg-red-50 border border-red-200 text-xs text-red-600">
                {installError}
              </div>
            )}

            <div className="flex justify-end gap-2">
              <button
                onClick={() => { setShowCreate(false); setInstallError(null); }}
                className="px-4 py-2 rounded-lg text-xs font-medium border hover:bg-slate-50 transition-colors"
              >
                取消
              </button>
              <button
                onClick={handleCreate}
                disabled={actionLoading || !newSkillName.trim()}
                className="px-4 py-2 rounded-lg text-xs font-medium bg-teal-500 text-white
                  hover:bg-teal-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors
                  flex items-center gap-1.5"
              >
                {actionLoading ? <RefreshCw size={13} className="animate-spin" /> : <Plus size={13} />}
                {actionLoading ? '创建中...' : '创建'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Editor Modal ─────────────────────────────────────────────────── */}
      {showEditor && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
          onClick={() => { setShowEditor(false); }}>
          <div className="w-[700px] max-h-[80vh] bg-white rounded-xl shadow-xl p-6 flex flex-col"
            onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-base font-semibold flex items-center gap-2">
                <FileCode size={16} className="text-amber-500" />
                编辑技能：{editorSkillName}
              </h3>
              <div className="flex items-center gap-2">
                <span className="text-[10px] text-slate-400 bg-slate-100 px-2 py-0.5 rounded">
                  YAML
                </span>
              </div>
            </div>
            <p className="text-xs text-slate-400 mb-3">
              直接编辑 YAML 内容来修改技能步骤。修改后点击保存生效。
            </p>
            <textarea
              value={editingYaml}
              onChange={e => setEditingYaml(e.target.value)}
              className="flex-1 min-h-[300px] px-3 py-2.5 rounded-lg border text-xs font-mono
                leading-relaxed resize-none focus:outline-none focus:ring-1 focus:ring-amber-400
                bg-slate-900 text-green-200"
              style={{ borderColor: 'var(--sidebar-border)', tabSize: 2 }}
              spellCheck={false}
            />
            {installError && (
              <div className="mt-3 px-3 py-2 rounded bg-red-50 border border-red-200 text-xs text-red-600">
                {installError}
              </div>
            )}
            <div className="flex justify-end gap-2 mt-4">
              <button
                onClick={() => setShowEditor(false)}
                className="px-4 py-2 rounded-lg text-xs font-medium border hover:bg-slate-50 transition-colors"
              >
                取消
              </button>
              <button
                onClick={handleSaveYaml}
                disabled={actionLoading}
                className="px-4 py-2 rounded-lg text-xs font-medium bg-amber-500 text-white
                  hover:bg-amber-600 disabled:opacity-50 transition-colors
                  flex items-center gap-1.5"
              >
                {actionLoading ? <RefreshCw size={13} className="animate-spin" /> : <CheckCircle size={13} />}
                保存
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default SkillManager;
