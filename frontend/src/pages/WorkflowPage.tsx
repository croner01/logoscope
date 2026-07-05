/**
 * Cloud Workflow Timeline — OpenStack 执行实例查看页
 *
 * 展示从 logs.logs 重建的 Workflow Execution 列表和 Timeline 详情。
 * 数据来源: semantic-engine WorkflowEngine → ClickHouse → GET /api/v1/workflows
 */
import React, { useEffect, useState, useCallback } from 'react';
import {
  Activity,
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  Clock,
  Database,
  ExternalLink,
  PlayCircle,
  RefreshCw,
  Search,
  XCircle,
  ChevronRight,
  Server,
} from 'lucide-react';
import LoadingState from '../components/common/LoadingState';

/* ─── Types ─────────────────────────────────────────────────────────────────── */

interface WorkflowSummary {
  execution_id: string;
  operation_type: string;
  resource_id: string;
  global_request_id: string;
  status: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  error_message: string;
  step_count: number;
}

interface WorkflowDetail extends WorkflowSummary {
  source_cluster: string;
  'steps.service_name': string[];
  'steps.action': string[];
  'steps.started_at': string[];
  'steps.duration_ms': number[];
  'steps.status': string[];
  'steps.level': string[];
}

interface WorkflowStep {
  service_name: string;
  action: string;
  started_at: string;
  duration_ms: number;
  status: string;
  level: string;
}

/* ─── Helpers ──────────────────────────────────────────────────────────────── */

const OPERATION_LABELS: Record<string, string> = {
  CreateVM: '创建虚拟机',
  DeleteVM: '删除虚拟机',
  CreateVolume: '创建云硬盘',
  DeleteVolume: '删除云硬盘',
  LiveMigrate: '在线迁移',
  AttachVolume: '挂载云硬盘',
  DetachVolume: '卸载云硬盘',
  RebuildServer: '重建实例',
  ResizeServer: '变更规格',
  CreateSnapshot: '创建快照',
  CreateBackup: '创建备份',
  ServerAction: '实例操作',
  VolumeAction: '云硬盘操作',
  CreateImage: '创建镜像',
};

const STATUS_CONFIG: Record<string, { bg: string; text: string; icon: React.ReactNode; label: string }> = {
  success: {
    bg: 'bg-emerald-500/20',
    text: 'text-emerald-400',
    icon: <CheckCircle2 size={14} />,
    label: '成功',
  },
  success_with_warnings: {
    bg: 'bg-amber-500/20',
    text: 'text-amber-400',
    icon: <AlertCircle size={14} />,
    label: '成功（有警告）',
  },
  failed: {
    bg: 'bg-red-500/20',
    text: 'text-red-400',
    icon: <XCircle size={14} />,
    label: '失败',
  },
};

const OPERATION_ICONS: Record<string, React.ReactNode> = {
  CreateVM: <PlayCircle size={16} />,
  DeleteVM: <XCircle size={16} />,
  CreateVolume: <Database size={16} />,
  LiveMigrate: <Activity size={16} />,
};

function getStatusConfig(status: string) {
  return STATUS_CONFIG[status] || STATUS_CONFIG.success;
}

function getOperationLabel(type: string) {
  return OPERATION_LABELS[type] || type || '未知操作';
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60000);
  const s = Math.round((ms % 60000) / 1000);
  return `${m}m ${s}s`;
}

function formatTime(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatDate(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString('zh-CN', {
    month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

/* ─── Step Timeline ────────────────────────────────────────────────────────── */

const StepTimeline: React.FC<{ steps: WorkflowStep[] }> = ({ steps }) => {
  const totalDuration = steps.length > 1
    ? new Date(steps[steps.length - 1].started_at).getTime() - new Date(steps[0].started_at).getTime()
    : steps[0]?.duration_ms || 0;

  return (
    <div className="space-y-0">
      {steps.map((step, i) => {
        const stepDuration = step.duration_ms;
        const widthPct = totalDuration > 0 ? (stepDuration / totalDuration) * 100 : 0;

        return (
          <div key={i} className="group relative flex gap-4">
            {/* 左侧时间轴 */}
            <div className="flex flex-col items-center pt-1">
              <div className={`z-10 flex h-7 w-7 items-center justify-center rounded-full border-2 ${
                step.status === 'failed'
                  ? 'border-red-500 bg-red-500/20'
                  : step.status === 'warning'
                    ? 'border-amber-500 bg-amber-500/20'
                    : 'border-slate-600 bg-slate-800'
              }`}>
                <div className={`h-2 w-2 rounded-full ${
                  step.status === 'failed' ? 'bg-red-400' :
                  step.status === 'warning' ? 'bg-amber-400' :
                  'bg-slate-400'
                }`} />
              </div>
              {i < steps.length - 1 && (
                <div className="mt-0 h-full w-px bg-slate-700/60 group-hover:bg-slate-600/80 transition-colors" />
              )}
            </div>

            {/* 步骤内容 */}
            <div className={`flex-1 pb-6 ${i === steps.length - 1 ? 'pb-1' : ''}`}>
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-slate-200">{step.service_name}</span>
                {step.action && step.action !== step.service_name && (
                  <span className="rounded bg-slate-800/80 px-1.5 py-0.5 text-[11px] text-slate-400 font-mono">
                    {step.action}
                  </span>
                )}
                <span className="text-[11px] text-slate-500">{formatTime(step.started_at)}</span>
              </div>

              {/* 耗时进度条 */}
              <div className="mt-1.5 flex items-center gap-2">
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-800">
                  <div
                    className={`h-full rounded-full transition-all duration-300 ${
                      step.status === 'failed' ? 'bg-red-500/60' :
                      step.status === 'warning' ? 'bg-amber-500/60' :
                      'bg-cyan-500/40'
                    }`}
                    style={{ width: `${Math.max(0.5, Math.min(100, widthPct))}%` }}
                  />
                </div>
                <span className="w-14 text-right text-[11px] text-slate-500 font-mono tabular-nums">
                  {formatDuration(stepDuration)}
                </span>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
};

/* ─── Detail View ───────────────────────────────────────────────────────────── */

const WorkflowDetailView: React.FC<{
  detail: WorkflowDetail | null;
  loading: boolean;
  onBack: () => void;
}> = ({ detail, loading, onBack }) => {
  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <LoadingState message="加载执行详情..." />
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="flex h-64 items-center justify-center text-slate-500">
        未找到执行详情
      </div>
    );
  }

  const steps: WorkflowStep[] = Array.isArray(detail['steps.service_name'])
    ? detail['steps.service_name'].map((svc, i) => ({
        service_name: svc,
        action: detail['steps.action']?.[i] || '',
        started_at: detail['steps.started_at']?.[i] || '',
        duration_ms: detail['steps.duration_ms']?.[i] || 0,
        status: detail['steps.status']?.[i] || 'success',
        level: detail['steps.level']?.[i] || 'INFO',
      }))
    : [];

  const statusConfig = getStatusConfig(detail.status);

  return (
    <div className="space-y-5">
      {/* 返回按钮 */}
      <button onClick={onBack} className="flex items-center gap-1 text-sm text-slate-400 hover:text-slate-200 transition-colors">
        <ArrowLeft size={14} />
        返回列表
      </button>

      {/* 执行概要 */}
      <div className="rounded-xl border border-slate-700/60 bg-slate-800/50 p-5">
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-semibold text-slate-100">
                {getOperationLabel(detail.operation_type)}
              </h2>
              <span className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${statusConfig.bg} ${statusConfig.text}`}>
                {statusConfig.icon}
                {statusConfig.label}
              </span>
            </div>
            {detail.error_message && (
              <p className="mt-2 max-w-2xl truncate text-sm text-red-400">{detail.error_message}</p>
            )}
          </div>
          <div className="text-right text-xs text-slate-500">
            <div className="flex items-center gap-1">
              <Clock size={12} />
              {formatDuration(detail.duration_ms)}
            </div>
            <div className="mt-1">
              {detail.step_count} 个步骤
            </div>
          </div>
        </div>

        {/* 元数据行 */}
        <div className="mt-4 flex flex-wrap gap-4 border-t border-slate-700/40 pt-3 text-xs text-slate-500">
          {detail.resource_id && (
            <div className="flex items-center gap-1">
              <Server size={12} />
              <span className="font-mono">{detail.resource_id}</span>
            </div>
          )}
          {detail.source_cluster && (
            <div className="flex items-center gap-1">
              <Activity size={12} />
              {detail.source_cluster}
            </div>
          )}
          <div className="flex items-center gap-1">
            <ExternalLink size={12} />
            <span className="font-mono">{detail.execution_id.slice(0, 12)}</span>
          </div>
        </div>
      </div>

      {/* Timeline */}
      <div className="rounded-xl border border-slate-700/60 bg-slate-800/50 p-5">
        <h3 className="mb-4 text-sm font-medium text-slate-300">执行时间线</h3>
        <StepTimeline steps={steps} />
      </div>
    </div>
  );
};

/* ─── List View ─────────────────────────────────────────────────────────────── */

const WorkflowListView: React.FC<{
  workflows: WorkflowSummary[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onSelect: (executionId: string) => void;
  filter: string;
  onFilterChange: (v: string) => void;
}> = ({ workflows, loading, error, onRefresh, onSelect, filter, onFilterChange }) => {
  const opTypes = Array.from(new Set(workflows.map((w) => w.operation_type))).sort();

  const filtered = filter
    ? workflows.filter((w) => w.operation_type === filter)
    : workflows;

  return (
    <div className="space-y-4">
      {/* 工具栏 */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <select
            value={filter}
            onChange={(e) => onFilterChange(e.target.value)}
            className="w-full rounded-lg border border-slate-700 bg-slate-800/80 py-2 pl-9 pr-3 text-sm text-slate-300 outline-none focus:border-cyan-600/50 focus:ring-1 focus:ring-cyan-600/30"
          >
            <option value="">所有操作类型</option>
            {opTypes.map((t) => (
              <option key={t} value={t}>{getOperationLabel(t)}</option>
            ))}
          </select>
        </div>
        <button
          onClick={onRefresh}
          disabled={loading}
          className="flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-2 text-sm text-slate-300 hover:bg-slate-700/80 disabled:opacity-50 transition-colors"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          刷新
        </button>
      </div>

      {/* 错误状态 */}
      {error && (
        <div className="rounded-lg border border-red-800/40 bg-red-900/20 px-4 py-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* 空状态 */}
      {!loading && !error && filtered.length === 0 && (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-slate-700/50 py-16 text-slate-500">
          <Activity size={36} className="mb-3 opacity-40" />
          <p className="text-sm">暂无 Workflow 执行记录</p>
          <p className="mt-1 text-xs text-slate-600">当前时间窗口内没有发现 OpenStack 操作日志</p>
          <button onClick={onRefresh} className="mt-4 rounded-lg bg-slate-800 px-4 py-2 text-sm text-slate-300 hover:bg-slate-700 transition-colors">
            重新扫描
          </button>
        </div>
      )}

      {/* 加载状态 */}
      {loading && workflows.length === 0 && (
        <div className="flex h-48 items-center justify-center">
          <LoadingState message="加载 Workflow 列表..." />
        </div>
      )}

      {/* Workflow 列表 */}
      {filtered.length > 0 && (
        <div className="space-y-2">
          {filtered.map((wf) => {
            const sConfig = getStatusConfig(wf.status);
            return (
              <div
                key={wf.execution_id + wf.started_at}
                onClick={() => onSelect(wf.execution_id)}
                className="group cursor-pointer rounded-xl border border-slate-700/50 bg-slate-800/40 p-4 hover:border-slate-600/60 hover:bg-slate-800/70 transition-all"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <div className={`flex h-8 w-8 items-center justify-center rounded-lg ${
                      wf.status === 'failed' ? 'bg-red-500/15' : 'bg-cyan-500/10'
                    }`}>
                      {OPERATION_ICONS[wf.operation_type] || <Activity size={16} className={
                        wf.status === 'failed' ? 'text-red-400' : 'text-cyan-400'
                      } />}
                    </div>
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-slate-200">
                          {getOperationLabel(wf.operation_type)}
                        </span>
                        <span className={`flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] ${sConfig.bg} ${sConfig.text}`}>
                          {sConfig.icon}
                          {sConfig.label}
                        </span>
                      </div>
                      <div className="mt-0.5 flex items-center gap-3 text-[11px] text-slate-500">
                        <span>{formatDate(wf.started_at)}</span>
                        <span>{formatDuration(wf.duration_ms)}</span>
                        <span>{wf.step_count} 步</span>
                        {wf.resource_id && <span className="font-mono">{wf.resource_id.slice(0, 12)}</span>}
                      </div>
                    </div>
                  </div>
                  <ChevronRight size={16} className="text-slate-600 opacity-0 group-hover:opacity-100 transition-opacity" />
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* 信息：总数 */}
      {!loading && filtered.length > 0 && (
        <p className="text-center text-xs text-slate-600">
          共 {filtered.length} 条执行记录
          {filter ? `（${workflows.length} 条全部）` : ''}
        </p>
      )}
    </div>
  );
};

/* ─── Main Page ─────────────────────────────────────────────────────────────── */

const WorkflowPage: React.FC = () => {
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');

  // Detail state
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const fetchWorkflows = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch('/api/v1/workflows?limit=100');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setWorkflows(data.workflows || []);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchDetail = useCallback(async (executionId: string) => {
    setDetailLoading(true);
    setDetail(null);
    try {
      const resp = await fetch(`/api/v1/workflows/${encodeURIComponent(executionId)}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setDetail(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载详情失败');
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchWorkflows();
  }, [fetchWorkflows]);

  const handleSelect = useCallback((executionId: string) => {
    setSelectedId(executionId);
    fetchDetail(executionId);
  }, [fetchDetail]);

  const handleBack = useCallback(() => {
    setSelectedId(null);
    setDetail(null);
    setError(null);
  }, []);

  return (
    <div className="mx-auto max-w-5xl px-6 py-6">
      {/* 页面标题 */}
      <div className="mb-6">
        <h1 className="text-xl font-bold text-slate-100">Cloud Workflow Timeline</h1>
        <p className="mt-1 text-sm text-slate-500">OpenStack 操作执行历史记录</p>
      </div>

      {selectedId ? (
        <WorkflowDetailView
          detail={detail}
          loading={detailLoading}
          onBack={handleBack}
        />
      ) : (
        <WorkflowListView
          workflows={workflows}
          loading={loading}
          error={error}
          onRefresh={fetchWorkflows}
          onSelect={handleSelect}
          filter={filter}
          onFilterChange={setFilter}
        />
      )}
    </div>
  );
};

export default WorkflowPage;
