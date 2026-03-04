/**
 * AI 知识库管理页
 * 展示知识库条目列表、编辑、删除、修复闭环，以及 AI 历史记录
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Archive, BookOpen, CheckCircle2, Clock3, Eye, Loader2, Pencil, Pin, RefreshCw, Sparkles, Trash2, Wrench } from 'lucide-react';

import EmptyState from '../components/common/EmptyState';
import LoadingState from '../components/common/LoadingState';
import HistoryDiffView from '../components/ai/HistoryDiffView';
import { api } from '../utils/api';

type TabKey = 'cases' | 'history';
const HISTORY_PAGE_SIZE = 50;

interface CaseItem {
  id: string;
  problem_type: string;
  severity: string;
  summary: string;
  service_name: string;
  resolved: boolean;
  resolution?: string;
  tags?: string[];
  created_at: string;
  updated_at?: string;
  resolved_at?: string;
  source?: string;
  llm_provider?: string;
  llm_model?: string;
  case_status?: string;
  knowledge_version?: number;
  manual_remediation_steps?: string[];
  verification_result?: 'pass' | 'fail' | string;
  verification_notes?: string;
  sync_status?: string;
  external_doc_id?: string;
  sync_error?: string;
  last_editor?: string;
  content_update_history_count?: number;
  remediation_history?: Array<Record<string, any>>;
}

interface RemediationHistoryItem {
  version: number;
  updated_at: string;
  editor: string;
  manual_remediation_steps: string[];
  verification_result: 'pass' | 'fail' | string;
  verification_notes: string;
  final_resolution: string;
  sync_status: string;
  effective_save_mode: string;
}

interface KBEditableCase {
  id: string;
  problem_type: string;
  severity: string;
  summary: string;
  service_name: string;
  root_causes: string[];
  solutions: Array<{ title?: string; description?: string; steps?: string[] }>;
  analysis_summary: string;
  resolution: string;
  tags: string[];
  log_content: string;
  case_status?: string;
  knowledge_version?: number;
  sync_status?: string;
  external_doc_id?: string;
  content_update_history?: Array<Record<string, any>>;
}

interface KBContentUpdateHistoryItem {
  event_id: string;
  event_type: string;
  version: number;
  updated_at: string;
  editor: string;
  changed_fields: string[];
  changes: Record<string, { before?: any; after?: any }>;
  requested_fields: string[];
  unchanged_requested_fields: string[];
  no_effective_change_reason: string;
  effective_save_mode: string;
  sync_status: string;
  sync_error_code: string;
  note: string;
  source: string;
}

interface KBOutboxStatus {
  enabled: boolean;
  worker_running: boolean;
  queue_total: number;
  pending: number;
  failed: number;
  processing: number;
  failed_retry_attempts?: number;
  failed_by_code?: Record<string, number>;
  items: Array<{
    outbox_id: string;
    case_id: string;
    status: string;
    attempts: number;
    max_attempts: number;
    next_retry_at: number;
    last_error?: string;
    last_error_code?: string;
  }>;
}

interface AIHistorySessionItem {
  session_id: string;
  analysis_type: string;
  title?: string;
  service_name: string;
  trace_id?: string;
  summary: string;
  summary_text?: string;
  analysis_method?: string;
  llm_model?: string;
  llm_provider?: string;
  source?: string;
  status?: string;
  created_at: string;
  updated_at: string;
  is_pinned?: boolean;
  is_archived?: boolean;
  message_count?: number;
}

const toLocaleTime = (value?: string): string => {
  if (!value) return '-';
  const raw = String(value).trim().replace(/([+-]\d{2}:\d{2})Z$/i, '$1');
  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(raw);
  const normalized = hasTimezone ? raw : `${raw}Z`;
  const d = new Date(normalized);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
};

const HISTORY_FIELD_LABELS: Record<string, string> = {
  problem_type: '问题类型',
  severity: '严重级别',
  summary: '摘要',
  service_name: '服务名',
  root_causes: '根因',
  solutions: '解决建议',
  analysis_summary: '分析总结',
  resolution: '结论',
  tags: '标签',
};

const formatHistoryFields = (fields: string[]): string[] =>
  fields.map((field) => HISTORY_FIELD_LABELS[field] || field);

const formatNoEffectiveChangeReason = (reason: string): string => {
  const normalized = String(reason || '').trim();
  if (!normalized) {
    return '提交内容经过规范化后与当前版本一致';
  }
  if (normalized === 'submitted_values_equivalent_after_normalization') {
    return '提交内容经过规范化后与当前版本一致';
  }
  return normalized;
};

const formatHistoryNote = (note: string): string => {
  const normalized = String(note || '').trim();
  if (!normalized) return '-';
  if (normalized === 'manual_content_update') return '手动更新内容';
  if (normalized === 'manual_content_update_no_effective_change') return '提交生效但无有效字段变更';
  return normalized;
};

const severityClass = (severity: string): string => {
  const level = String(severity || '').toLowerCase();
  if (level === 'critical' || level === 'high') return 'bg-red-100 text-red-700';
  if (level === 'medium' || level === 'warn' || level === 'warning') return 'bg-amber-100 text-amber-700';
  return 'bg-slate-100 text-slate-700';
};

const syncStatusClass = (syncStatus?: string): string => {
  const value = String(syncStatus || '').toLowerCase();
  if (value === 'synced') return 'bg-green-100 text-green-700';
  if (value === 'failed') return 'bg-red-100 text-red-700';
  if (value === 'not_requested') return 'bg-slate-100 text-slate-700';
  return 'bg-amber-100 text-amber-700';
};

const normalizeRemediationHistory = (value: any): RemediationHistoryItem[] => {
  if (!Array.isArray(value)) {
    return [];
  }
  const mapped = value
    .filter((item) => item && typeof item === 'object')
    .map((item) => ({
      version: Number(item.version || 0) || 0,
      updated_at: String(item.updated_at || ''),
      editor: String(item.editor || 'manual'),
      manual_remediation_steps: Array.isArray(item.manual_remediation_steps)
        ? item.manual_remediation_steps.map((step: unknown) => String(step || '').trim()).filter(Boolean)
        : [],
      verification_result: String(item.verification_result || ''),
      verification_notes: String(item.verification_notes || ''),
      final_resolution: String(item.final_resolution || ''),
      sync_status: String(item.sync_status || ''),
      effective_save_mode: String(item.effective_save_mode || ''),
    }))
    .sort((a, b) => b.version - a.version);
  return mapped;
};

const normalizeKBContentUpdateHistory = (value: any): KBContentUpdateHistoryItem[] => {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item) => item && typeof item === 'object')
    .map((item) => ({
      event_id: String(item.event_id || ''),
      event_type: String(item.event_type || 'content_update'),
      version: Number(item.version || 0) || 0,
      updated_at: String(item.updated_at || ''),
      editor: String(item.editor || 'manual_content'),
      changed_fields: Array.isArray(item.changed_fields)
        ? item.changed_fields.map((field: unknown) => String(field || '').trim()).filter(Boolean)
        : [],
      changes: item.changes && typeof item.changes === 'object'
        ? item.changes as Record<string, { before?: any; after?: any }>
        : {},
      requested_fields: Array.isArray(item.requested_fields)
        ? item.requested_fields.map((field: unknown) => String(field || '').trim()).filter(Boolean)
        : [],
      unchanged_requested_fields: Array.isArray(item.unchanged_requested_fields)
        ? item.unchanged_requested_fields.map((field: unknown) => String(field || '').trim()).filter(Boolean)
        : [],
      no_effective_change_reason: String(item.no_effective_change_reason || ''),
      effective_save_mode: String(item.effective_save_mode || ''),
      sync_status: String(item.sync_status || ''),
      sync_error_code: String(item.sync_error_code || ''),
      note: String(item.note || ''),
      source: String(item.source || ''),
    }))
    .sort((a, b) => b.version - a.version);
};

const parseMultilineText = (value: string): string[] =>
  value
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);

const parseTagText = (value: string): string[] =>
  value
    .split(/[,\n，]/)
    .map((item) => item.trim())
    .filter(Boolean);

const formatSolutionsToEditorText = (solutions: Array<{ title?: string; description?: string; steps?: string[] }>): string => {
  if (!Array.isArray(solutions) || solutions.length === 0) {
    return '';
  }
  const blocks = solutions.map((solution, index) => {
    const title = String(solution?.title || '').trim();
    const description = String(solution?.description || '').trim();
    const steps = Array.isArray(solution?.steps)
      ? solution.steps.map((step) => String(step || '').trim()).filter(Boolean)
      : [];
    const lines: string[] = [];
    lines.push(`方案${index + 1}: ${title || '未命名方案'}`);
    if (description) {
      lines.push(`说明: ${description}`);
    }
    if (steps.length > 0) {
      lines.push('步骤:');
      steps.forEach((step, stepIndex) => lines.push(`${stepIndex + 1}. ${step}`));
    }
    return lines.join('\n');
  });
  return blocks.join('\n\n').trim();
};

const AICaseManagement: React.FC = () => {
  const location = useLocation();
  const navigate = useNavigate();

  const initialTab = useMemo<TabKey>(() => {
    const query = new URLSearchParams(location.search);
    return query.get('tab') === 'history' ? 'history' : 'cases';
  }, [location.search]);

  const [activeTab, setActiveTab] = useState<TabKey>(initialTab);
  const [items, setItems] = useState<CaseItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [actionLoadingId, setActionLoadingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [serviceFilter, setServiceFilter] = useState('');
  const [problemTypeFilter, setProblemTypeFilter] = useState('');
  const [showResolved, setShowResolved] = useState(true);
  const [historySessions, setHistorySessions] = useState<AIHistorySessionItem[]>([]);
  const [historyQuery, setHistoryQuery] = useState('');
  const [showArchivedHistory, setShowArchivedHistory] = useState(true);
  const [historyPinnedFirst, setHistoryPinnedFirst] = useState(true);
  const [historyTotalAll, setHistoryTotalAll] = useState(0);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const historyListRef = useRef<HTMLDivElement | null>(null);
  const [remediationEditorOpen, setRemediationEditorOpen] = useState(false);
  const [remediationTarget, setRemediationTarget] = useState<CaseItem | null>(null);
  const [remediationLoading, setRemediationLoading] = useState(false);
  const [remediationSubmitting, setRemediationSubmitting] = useState(false);
  const [remediationStepsText, setRemediationStepsText] = useState('');
  const [remediationVerificationResult, setRemediationVerificationResult] = useState<'pass' | 'fail'>('pass');
  const [remediationVerificationNotes, setRemediationVerificationNotes] = useState('');
  const [remediationFinalResolution, setRemediationFinalResolution] = useState('');
  const [remediationSaveMode, setRemediationSaveMode] = useState<'local_only' | 'local_and_remote'>('local_only');
  const [remediationRemoteEnabled, setRemediationRemoteEnabled] = useState(false);
  const [remediationHistory, setRemediationHistory] = useState<RemediationHistoryItem[]>([]);
  const [kbEditorOpen, setKbEditorOpen] = useState(false);
  const [kbEditorLoading, setKbEditorLoading] = useState(false);
  const [kbEditorSubmitting, setKbEditorSubmitting] = useState(false);
  const [kbEditorTarget, setKbEditorTarget] = useState<KBEditableCase | null>(null);
  const [kbProblemType, setKbProblemType] = useState('');
  const [kbSeverity, setKbSeverity] = useState('medium');
  const [kbSummary, setKbSummary] = useState('');
  const [kbServiceName, setKbServiceName] = useState('');
  const [kbRootCausesText, setKbRootCausesText] = useState('');
  const [kbSolutionsText, setKbSolutionsText] = useState('');
  const [kbOptimizingSolutions, setKbOptimizingSolutions] = useState(false);
  const [kbAnalysisSummary, setKbAnalysisSummary] = useState('');
  const [kbResolution, setKbResolution] = useState('');
  const [kbTagsText, setKbTagsText] = useState('');
  const [kbSaveMode, setKbSaveMode] = useState<'local_only' | 'local_and_remote'>('local_only');
  const [kbRemoteEnabled, setKbRemoteEnabled] = useState(false);
  const [kbUpdateHistory, setKbUpdateHistory] = useState<KBContentUpdateHistoryItem[]>([]);
  const [outboxStatus, setOutboxStatus] = useState<KBOutboxStatus | null>(null);
  const [outboxLoading, setOutboxLoading] = useState(false);
  const [outboxError, setOutboxError] = useState<string | null>(null);

  useEffect(() => {
    setActiveTab(initialTab);
  }, [initialTab]);

  const fetchCases = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const payload = await api.getCases({
        service_name: serviceFilter.trim() || undefined,
        problem_type: problemTypeFilter.trim() || undefined,
        limit: 200,
      });
      const nextItems = Array.isArray(payload.cases) ? payload.cases : [];
      nextItems.sort((a, b) => {
        const at = new Date(a.created_at || 0).getTime();
        const bt = new Date(b.created_at || 0).getTime();
        return bt - at;
      });
      setItems(nextItems as CaseItem[]);
    } catch (err: any) {
      setError(err?.message || '加载知识库列表失败');
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [problemTypeFilter, serviceFilter]);

  useEffect(() => {
    if (activeTab === 'history') {
      return;
    }
    fetchCases();
  }, [activeTab, fetchCases]);

  const fetchOutboxStatus = useCallback(async () => {
    setOutboxLoading(true);
    setOutboxError(null);
    try {
      const payload = await api.getKBOutboxStatus();
      setOutboxStatus(payload as KBOutboxStatus);
    } catch (err: any) {
      setOutboxStatus(null);
      setOutboxError(err?.message || '加载 Outbox 状态失败');
    } finally {
      setOutboxLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchOutboxStatus();
  }, [fetchOutboxStatus]);

  const fetchHistory = useCallback(async (options?: { offset?: number; reset?: boolean }) => {
    const reset = options?.reset ?? true;
    const offset = Math.max(0, Number(options?.offset || 0));
    if (reset) {
      setLoading(true);
      setError(null);
    } else {
      setHistoryLoadingMore(true);
    }
    try {
      const payload = await api.getAIHistory({
        limit: HISTORY_PAGE_SIZE,
        offset,
        service_name: serviceFilter.trim() || undefined,
        q: historyQuery.trim() || undefined,
        include_archived: showArchivedHistory,
        pinned_first: historyPinnedFirst,
      });
      const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
      if (reset) {
        setHistorySessions(sessions as AIHistorySessionItem[]);
      } else {
        setHistorySessions((prev) => {
          const existing = new Set(prev.map((item) => item.session_id));
          const appended = (sessions as AIHistorySessionItem[]).filter((item) => !existing.has(item.session_id));
          return [...prev, ...appended];
        });
      }
      const totalAll = Number(payload.total_all ?? sessions.length);
      setHistoryTotalAll(totalAll);
      if (typeof payload.has_more === 'boolean') {
        setHistoryHasMore(payload.has_more);
      } else {
        setHistoryHasMore(offset + sessions.length < totalAll);
      }
    } catch (err: any) {
      if (reset) {
        setError(err?.message || '加载 AI 历史失败');
        setHistorySessions([]);
        setHistoryTotalAll(0);
        setHistoryHasMore(false);
      } else {
        alert(err?.message || '加载更多 AI 历史失败');
      }
    } finally {
      if (reset) {
        setLoading(false);
      } else {
        setHistoryLoadingMore(false);
      }
    }
  }, [historyPinnedFirst, historyQuery, serviceFilter, showArchivedHistory]);

  useEffect(() => {
    if (activeTab !== 'history') {
      return;
    }
    fetchHistory({ reset: true, offset: 0 });
  }, [activeTab, fetchHistory]);

  const loadMoreHistory = useCallback(() => {
    if (activeTab !== 'history' || loading || historyLoadingMore || !historyHasMore) {
      return;
    }
    fetchHistory({
      reset: false,
      offset: historySessions.length,
    });
  }, [activeTab, fetchHistory, historyHasMore, historyLoadingMore, historySessions.length, loading]);

  useEffect(() => {
    if (activeTab !== 'history') {
      return;
    }
    const el = historyListRef.current;
    if (!el) {
      return;
    }
    const onScroll = () => {
      const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 120;
      if (nearBottom) {
        loadMoreHistory();
      }
    };
    el.addEventListener('scroll', onScroll);
    onScroll();
    return () => {
      el.removeEventListener('scroll', onScroll);
    };
  }, [activeTab, loadMoreHistory]);

  const visibleItems = useMemo(() => {
    if (showResolved) return items;
    return items.filter((item) => !item.resolved);
  }, [items, showResolved]);

  const handleDelete = async (caseId: string) => {
    if (!window.confirm(`确认删除知识库条目 ${caseId} 吗？`)) {
      return;
    }
    setActionLoadingId(caseId);
    try {
      await api.deleteCase(caseId);
      await fetchCases();
    } catch (err: any) {
      alert(err?.message || '删除失败');
    } finally {
      setActionLoadingId(null);
    }
  };

  const handleResolve = async (caseId: string) => {
    const resolution = window.prompt('请输入解决说明（可选）', '人工确认：已处理并恢复') || '';
    setActionLoadingId(caseId);
    try {
      await api.resolveCase(caseId, resolution);
      await fetchCases();
    } catch (err: any) {
      alert(err?.message || '标记已解决失败');
    } finally {
      setActionLoadingId(null);
    }
  };

  const handleOpenCaseInAI = async (caseId: string) => {
    setActionLoadingId(caseId);
    try {
      const detail = await api.getCaseDetail(caseId);
      navigate('/ai-analysis', {
        state: {
          historyCase: detail,
        },
      });
    } catch (err: any) {
      alert(err?.message || '加载知识条目详情失败');
    } finally {
      setActionLoadingId(null);
    }
  };

  const handleOpenRemediationEditor = async (caseId: string) => {
    setActionLoadingId(caseId);
    setRemediationLoading(true);
    try {
      const detail = await api.getCaseDetail(caseId);
      const metadata = (detail.llm_metadata && typeof detail.llm_metadata === 'object')
        ? detail.llm_metadata as Record<string, unknown>
        : {};
      const metadataHistory = Array.isArray(metadata.remediation_history) ? metadata.remediation_history : [];
      const history = normalizeRemediationHistory(
        detail.remediation_history
          || metadataHistory
      );
      setRemediationTarget({
        id: detail.id,
        problem_type: detail.problem_type,
        severity: detail.severity,
        summary: detail.summary,
        service_name: detail.service_name,
        resolved: detail.resolved,
        resolution: detail.resolution,
        tags: detail.tags,
        created_at: detail.created_at,
        updated_at: detail.updated_at,
        resolved_at: detail.resolved_at,
        source: detail.source,
        llm_provider: detail.llm_provider,
        llm_model: detail.llm_model,
        case_status: detail.case_status,
        knowledge_version: detail.knowledge_version,
        manual_remediation_steps: detail.manual_remediation_steps,
        verification_result: detail.verification_result,
        verification_notes: detail.verification_notes,
        sync_status: detail.sync_status,
        external_doc_id: detail.external_doc_id,
        sync_error: detail.sync_error,
        last_editor: detail.last_editor,
      });
      setRemediationStepsText((detail.manual_remediation_steps || []).join('\n'));
      setRemediationVerificationResult(detail.verification_result === 'fail' ? 'fail' : 'pass');
      setRemediationVerificationNotes(detail.verification_notes || '');
      setRemediationFinalResolution(detail.resolution || '');
      setRemediationSaveMode('local_only');
      setRemediationRemoteEnabled(false);
      setRemediationHistory(history);
      setRemediationEditorOpen(true);
    } catch (err: any) {
      alert(err?.message || '加载修复详情失败');
    } finally {
      setActionLoadingId(null);
      setRemediationLoading(false);
    }
  };

  const hydrateKBEditorFromDetail = useCallback((detail: any) => {
    const metadata = (detail?.llm_metadata && typeof detail.llm_metadata === 'object')
      ? detail.llm_metadata as Record<string, unknown>
      : {};
    const contentHistory = normalizeKBContentUpdateHistory(
      detail?.content_update_history
      || metadata?.content_update_history
    );
    const target: KBEditableCase = {
      id: detail.id,
      problem_type: detail.problem_type || 'unknown',
      severity: detail.severity || 'medium',
      summary: detail.summary || '',
      service_name: detail.service_name || '',
      root_causes: Array.isArray(detail.root_causes) ? detail.root_causes : [],
      solutions: Array.isArray(detail.solutions) ? detail.solutions : [],
      analysis_summary: detail.analysis_summary || detail.summary || '',
      resolution: detail.resolution || '',
      tags: Array.isArray(detail.tags) ? detail.tags : [],
      log_content: String(detail.log_content || ''),
      case_status: detail.case_status,
      knowledge_version: detail.knowledge_version,
      sync_status: detail.sync_status,
      external_doc_id: detail.external_doc_id,
      content_update_history: contentHistory as Array<Record<string, any>>,
    };

    setKbEditorTarget(target);
    setKbProblemType(target.problem_type);
    setKbSeverity(target.severity);
    setKbSummary(target.summary);
    setKbServiceName(target.service_name);
    setKbRootCausesText(target.root_causes.join('\n'));
    setKbSolutionsText(formatSolutionsToEditorText(target.solutions || []));
    setKbAnalysisSummary(target.analysis_summary);
    setKbResolution(target.resolution || '');
    setKbTagsText(target.tags.join(', '));
    setKbSaveMode('local_only');
    setKbRemoteEnabled(false);
    setKbUpdateHistory(contentHistory);
  }, []);

  const handleOpenKBContentEditor = async (caseId: string) => {
    setActionLoadingId(caseId);
    setKbEditorLoading(true);
    try {
      const detail = await api.getCaseDetail(caseId);
      hydrateKBEditorFromDetail(detail);
      setKbEditorOpen(true);
    } catch (err: any) {
      alert(err?.message || '加载知识库详情失败');
    } finally {
      setActionLoadingId(null);
      setKbEditorLoading(false);
    }
  };

  const closeRemediationEditor = () => {
    setRemediationEditorOpen(false);
    setRemediationTarget(null);
    setRemediationStepsText('');
    setRemediationVerificationResult('pass');
    setRemediationVerificationNotes('');
    setRemediationFinalResolution('');
    setRemediationSaveMode('local_only');
    setRemediationRemoteEnabled(false);
    setRemediationHistory([]);
  };

  const closeKBEditor = () => {
    setKbEditorOpen(false);
    setKbEditorTarget(null);
    setKbProblemType('');
    setKbSeverity('medium');
    setKbSummary('');
    setKbServiceName('');
    setKbRootCausesText('');
    setKbSolutionsText('');
    setKbOptimizingSolutions(false);
    setKbAnalysisSummary('');
    setKbResolution('');
    setKbTagsText('');
    setKbSaveMode('local_only');
    setKbRemoteEnabled(false);
    setKbUpdateHistory([]);
  };

  const handleOptimizeSolutionText = async () => {
    const content = kbSolutionsText.trim();
    if (!content) {
      alert('请先填写解决建议内容');
      return;
    }
    setKbOptimizingSolutions(true);
    try {
      const result = await api.optimizeKBSolutionContent({
        content,
        summary: kbSummary.trim(),
        service_name: kbServiceName.trim(),
        problem_type: kbProblemType.trim(),
        severity: kbSeverity.trim(),
        use_llm: true,
      });
      setKbSolutionsText(String(result.optimized_text || content));
      if (result.method === 'llm') {
        alert('内容优化已完成（LLM）');
      } else {
        const reason = String(result.llm_fallback_reason || '');
        alert(`内容优化已完成（规则模式${reason ? `，原因=${reason}` : ''}）`);
      }
    } catch (err: any) {
      alert(err?.message || '内容优化失败');
    } finally {
      setKbOptimizingSolutions(false);
    }
  };

  const handleSubmitKBContent = async () => {
    if (!kbEditorTarget?.id) {
      return;
    }
    if (!kbSummary.trim()) {
      alert('摘要不能为空');
      return;
    }
    if (!kbServiceName.trim()) {
      alert('服务名称不能为空');
      return;
    }

    setKbEditorSubmitting(true);
    try {
      const result = await api.updateCaseContent(kbEditorTarget.id, {
        problem_type: kbProblemType.trim(),
        severity: kbSeverity.trim(),
        summary: kbSummary.trim(),
        service_name: kbServiceName.trim(),
        root_causes: parseMultilineText(kbRootCausesText),
        solutions_text: kbSolutionsText,
        analysis_summary: kbAnalysisSummary.trim(),
        resolution: kbResolution.trim(),
        tags: parseTagText(kbTagsText),
        save_mode: kbSaveMode,
        remote_enabled: kbRemoteEnabled,
      });
      const message = String(
        result.friendly_message
        || result.message
        || `知识库内容已更新：version=${result.knowledge_version}，sync=${result.sync_status || 'unknown'}`
      );
      alert(message);
      const refreshedDetail = await api.getCaseDetail(kbEditorTarget.id);
      hydrateKBEditorFromDetail(refreshedDetail);
      await Promise.all([fetchCases(), fetchOutboxStatus()]);
    } catch (err: any) {
      alert(err?.message || '更新知识库内容失败');
    } finally {
      setKbEditorSubmitting(false);
    }
  };

  const handleSubmitRemediation = async () => {
    if (!remediationTarget?.id) {
      return;
    }
    const steps = remediationStepsText
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
    if (steps.length < 1) {
      alert('请至少填写 1 条人工修复步骤');
      return;
    }
    if (steps.some((step) => step.length < 5)) {
      alert('每条修复步骤不少于 5 个字符');
      return;
    }
    if (remediationVerificationNotes.trim().length < 20) {
      alert('验证说明不少于 20 个字符');
      return;
    }

    setRemediationSubmitting(true);
    try {
      const result = await api.updateCaseManualRemediation(remediationTarget.id, {
        manual_remediation_steps: steps,
        verification_result: remediationVerificationResult,
        verification_notes: remediationVerificationNotes.trim(),
        final_resolution: remediationFinalResolution.trim(),
        save_mode: remediationSaveMode,
        remote_enabled: remediationRemoteEnabled,
      });
      alert(
        `修复步骤已更新：version=${result.knowledge_version}，sync=${result.sync_status || 'unknown'}`
      );
      closeRemediationEditor();
      await Promise.all([fetchCases(), fetchOutboxStatus()]);
    } catch (err: any) {
      alert(err?.message || '提交人工修复失败');
    } finally {
      setRemediationSubmitting(false);
    }
  };

  const handleOpenHistorySessionInAI = async (sessionId: string) => {
    setActionLoadingId(sessionId);
    try {
      const detail = await api.getAIHistoryDetail(sessionId);
      navigate('/ai-analysis', {
        state: {
          historySession: detail,
        },
      });
    } catch (err: any) {
      alert(err?.message || '加载 AI 历史详情失败');
    } finally {
      setActionLoadingId(null);
    }
  };

  const handleRenameHistorySession = async (session: AIHistorySessionItem) => {
    const nextTitle = window.prompt('请输入新的会话标题', session.title || session.summary || '') || '';
    const title = nextTitle.trim();
    if (!title) {
      return;
    }
    setActionLoadingId(session.session_id);
    try {
      await api.updateAIHistorySession(session.session_id, { title });
      await fetchHistory({ reset: true, offset: 0 });
    } catch (err: any) {
      alert(err?.message || '重命名失败');
    } finally {
      setActionLoadingId(null);
    }
  };

  const handleTogglePinHistorySession = async (session: AIHistorySessionItem) => {
    setActionLoadingId(session.session_id);
    try {
      await api.updateAIHistorySession(session.session_id, { is_pinned: !session.is_pinned });
      await fetchHistory({ reset: true, offset: 0 });
    } catch (err: any) {
      alert(err?.message || '更新 Pin 状态失败');
    } finally {
      setActionLoadingId(null);
    }
  };

  const handleToggleArchiveHistorySession = async (session: AIHistorySessionItem) => {
    setActionLoadingId(session.session_id);
    try {
      await api.updateAIHistorySession(session.session_id, { is_archived: !session.is_archived });
      await fetchHistory({ reset: true, offset: 0 });
    } catch (err: any) {
      alert(err?.message || '更新归档状态失败');
    } finally {
      setActionLoadingId(null);
    }
  };

  const handleDeleteHistorySession = async (session: AIHistorySessionItem) => {
    if (!window.confirm(`确认删除 AI 历史会话 ${session.session_id} 吗？`)) {
      return;
    }
    setActionLoadingId(session.session_id);
    try {
      await api.deleteAIHistorySession(session.session_id);
      await fetchHistory({ reset: true, offset: 0 });
    } catch (err: any) {
      alert(err?.message || '删除 AI 历史会话失败');
    } finally {
      setActionLoadingId(null);
    }
  };

  return (
    <div className="flex flex-col h-full">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">AI 知识库管理</h1>
          <p className="text-gray-500 mt-1">管理 AI 分析沉淀知识库，支持内容编辑、修复闭环、版本沉淀、删除与历史回溯</p>
        </div>
        <button
          type="button"
          onClick={() => {
            if (activeTab === 'history') {
              fetchHistory({ reset: true, offset: 0 });
              fetchOutboxStatus();
              return;
            }
            fetchCases();
            fetchOutboxStatus();
          }}
          disabled={loading || historyLoadingMore}
          className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-slate-100 text-slate-700 hover:bg-slate-200 disabled:opacity-50"
        >
          <RefreshCw className={`w-4 h-4 ${(loading || historyLoadingMore) ? 'animate-spin' : ''}`} />
          刷新
        </button>
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setActiveTab('cases')}
          className={`px-3 py-1.5 rounded-lg text-sm ${
            activeTab === 'cases' ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
          }`}
        >
          知识库列表
        </button>
        <button
          type="button"
          onClick={() => setActiveTab('history')}
          className={`px-3 py-1.5 rounded-lg text-sm ${
            activeTab === 'history' ? 'bg-indigo-600 text-white' : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
          }`}
        >
          AI 历史记录
        </button>
      </div>

      <div className="mb-4 bg-white rounded-lg shadow-sm border border-gray-200 p-3">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <input
            value={serviceFilter}
            onChange={(e) => setServiceFilter(e.target.value)}
            placeholder="按服务过滤（service_name）"
            className="px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
          />
          <input
            value={activeTab === 'history' ? historyQuery : problemTypeFilter}
            onChange={(e) => (activeTab === 'history' ? setHistoryQuery(e.target.value) : setProblemTypeFilter(e.target.value))}
            placeholder={activeTab === 'history' ? '搜索历史问答（标题/内容）' : '按问题类型过滤（problem_type）'}
            className="px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
          />
          <label className="inline-flex items-center gap-2 text-sm text-gray-700">
            <input
              type="checkbox"
              checked={activeTab === 'history' ? showArchivedHistory : showResolved}
              onChange={(e) => {
                if (activeTab === 'history') {
                  setShowArchivedHistory(e.target.checked);
                } else {
                  setShowResolved(e.target.checked);
                }
              }}
              className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
            />
            {activeTab === 'history' ? '显示归档会话' : '显示已解决条目'}
          </label>
        </div>
        {activeTab === 'history' && (
          <div className="mt-2">
            <div className="flex flex-wrap items-center gap-4">
              <label className="inline-flex items-center gap-2 text-sm text-gray-700">
                <input
                  type="checkbox"
                  checked={historyPinnedFirst}
                  onChange={(e) => setHistoryPinnedFirst(e.target.checked)}
                  className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                />
                置顶会话优先
              </label>
              <div className="text-xs text-slate-500">
                已加载 {historySessions.length} / {historyTotalAll || historySessions.length}
              </div>
            </div>
          </div>
        )}
        <div className="mt-3">
          <button
            type="button"
            onClick={() => {
              if (activeTab === 'history') {
                fetchHistory({ reset: true, offset: 0 });
                fetchOutboxStatus();
                return;
              }
              fetchCases();
              fetchOutboxStatus();
            }}
            disabled={loading || historyLoadingMore}
            className="px-3 py-1.5 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
          >
            应用过滤
          </button>
        </div>
      </div>

      <div className="mb-4 bg-white rounded-lg shadow-sm border border-gray-200 p-3">
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-semibold text-slate-800">异步同步队列看板（Outbox）</h3>
          <button
            type="button"
            onClick={fetchOutboxStatus}
            disabled={outboxLoading}
            className="px-2 py-1 rounded bg-slate-100 text-slate-700 hover:bg-slate-200 disabled:opacity-50 text-xs"
          >
            {outboxLoading ? '刷新中...' : '刷新队列'}
          </button>
        </div>
        {outboxError && <div className="text-xs text-red-600 mt-2">{outboxError}</div>}
        {!outboxError && (
          <div className="mt-2 text-xs text-slate-600 grid grid-cols-2 md:grid-cols-5 gap-2">
            <div>队列总数: {outboxStatus?.queue_total ?? '-'}</div>
            <div>待重试: {outboxStatus?.pending ?? '-'}</div>
            <div>失败: {outboxStatus?.failed ?? '-'}</div>
            <div>处理中: {outboxStatus?.processing ?? '-'}</div>
            <div>Worker: {outboxStatus?.worker_running ? '运行中' : '未运行'}</div>
          </div>
        )}
        {outboxStatus?.failed_by_code && Object.keys(outboxStatus.failed_by_code).length > 0 && (
          <div className="mt-2 text-xs text-amber-700">
            失败码统计: {Object.entries(outboxStatus.failed_by_code).map(([k, v]) => `${k}:${v}`).join(' | ')}
          </div>
        )}
        {outboxStatus?.items && outboxStatus.items.length > 0 && (
          <div className="mt-2 max-h-28 overflow-auto border border-slate-200 rounded">
            <table className="min-w-full text-xs">
              <thead className="bg-slate-50 sticky top-0">
                <tr>
                  <th className="px-2 py-1 text-left">outbox_id</th>
                  <th className="px-2 py-1 text-left">case_id</th>
                  <th className="px-2 py-1 text-left">状态</th>
                  <th className="px-2 py-1 text-left">重试</th>
                  <th className="px-2 py-1 text-left">错误</th>
                </tr>
              </thead>
              <tbody>
                {outboxStatus.items.slice(0, 10).map((item) => (
                  <tr key={item.outbox_id} className="border-t border-slate-100">
                    <td className="px-2 py-1 font-mono">{item.outbox_id}</td>
                    <td className="px-2 py-1 font-mono">{item.case_id}</td>
                    <td className="px-2 py-1">{item.status}</td>
                    <td className="px-2 py-1">{item.attempts}/{item.max_attempts}</td>
                    <td className="px-2 py-1">{item.last_error_code || item.last_error || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="flex-1 bg-white rounded-lg shadow-md overflow-hidden border border-gray-200">
        {loading ? (
          <div className="p-6">
            <LoadingState message="加载知识库中..." />
          </div>
        ) : error ? (
          <div className="p-6 text-red-600 text-sm">{error}</div>
        ) : activeTab === 'cases' && visibleItems.length === 0 ? (
          <div className="p-6">
            <EmptyState
              icon={<BookOpen className="w-12 h-12 text-gray-400" />}
              title="暂无知识库条目"
              description="当前筛选条件下没有可展示的 AI 知识库内容"
            />
          </div>
        ) : activeTab === 'history' && historySessions.length === 0 ? (
          <div className="p-6">
            <EmptyState
              icon={<BookOpen className="w-12 h-12 text-gray-400" />}
              title="暂无 AI 历史"
              description="当前筛选条件下没有可展示的分析会话记录"
            />
          </div>
        ) : activeTab === 'cases' ? (
          <div className="overflow-auto h-full">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50 sticky top-0 z-10">
                <tr>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">ID</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">问题类型</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">级别</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">服务</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">摘要</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">状态</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">创建时间</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-600">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 bg-white">
                {visibleItems.map((item) => {
                  const isActionLoading = actionLoadingId === item.id;
                  return (
                    <tr key={item.id}>
                      <td className="px-3 py-2 font-mono text-xs text-gray-600">{item.id}</td>
                      <td className="px-3 py-2">{item.problem_type || '-'}</td>
                      <td className="px-3 py-2">
                        <span className={`px-2 py-0.5 rounded text-xs ${severityClass(item.severity)}`}>
                          {item.severity || 'unknown'}
                        </span>
                      </td>
                      <td className="px-3 py-2">{item.service_name || '-'}</td>
                      <td className="px-3 py-2 max-w-[420px]">
                        <div className="line-clamp-2 text-gray-700">{item.summary || '-'}</div>
                        <div className="text-xs text-gray-400 mt-1">
                          source={item.source || 'manual'} {item.llm_model ? `| model=${item.llm_model}` : ''}
                        </div>
                        <div className="text-xs text-gray-500 mt-1 flex flex-wrap gap-2">
                          <span>version: v{item.knowledge_version || 1}</span>
                          <span>editor: {item.last_editor || '-'}</span>
                          <span>content_updates: {item.content_update_history_count || 0}</span>
                          {item.sync_status && (
                            <span className={`px-1.5 py-0.5 rounded ${syncStatusClass(item.sync_status)}`}>
                              sync: {item.sync_status}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-3 py-2">
                        {item.case_status === 'archived' ? (
                          <span className="inline-flex items-center gap-1 text-slate-700 bg-slate-100 px-2 py-0.5 rounded text-xs">
                            <Archive className="w-3.5 h-3.5" />
                            已归档
                          </span>
                        ) : item.case_status === 'resolved' || item.resolved ? (
                          <span className="inline-flex items-center gap-1 text-green-700 bg-green-100 px-2 py-0.5 rounded text-xs">
                            <CheckCircle2 className="w-3.5 h-3.5" />
                            已解决
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-amber-700 bg-amber-100 px-2 py-0.5 rounded text-xs">
                            <Clock3 className="w-3.5 h-3.5" />
                            待处理
                          </span>
                        )}
                        <div className="text-xs text-gray-500 mt-1">
                          审核: {item.verification_result ? item.verification_result.toUpperCase() : '-'}
                        </div>
                      </td>
                      <td className="px-3 py-2 whitespace-nowrap">{toLocaleTime(item.created_at)}</td>
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-2">
                          {!item.resolved && (
                            <button
                              type="button"
                              onClick={() => handleResolve(item.id)}
                              disabled={isActionLoading}
                              className="px-2 py-1 rounded bg-green-50 text-green-700 hover:bg-green-100 disabled:opacity-50"
                            >
                              {isActionLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : '标记已解决'}
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => handleOpenRemediationEditor(item.id)}
                            disabled={isActionLoading}
                            className="inline-flex items-center gap-1 px-2 py-1 rounded bg-blue-50 text-blue-700 hover:bg-blue-100 disabled:opacity-50"
                          >
                            {isActionLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wrench className="w-3.5 h-3.5" />}
                            修复步骤
                          </button>
                          <button
                            type="button"
                            onClick={() => handleOpenKBContentEditor(item.id)}
                            disabled={isActionLoading}
                            className="inline-flex items-center gap-1 px-2 py-1 rounded bg-purple-50 text-purple-700 hover:bg-purple-100 disabled:opacity-50"
                          >
                            {isActionLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Pencil className="w-3.5 h-3.5" />}
                            查看/编辑知识库
                          </button>
                          <button
                            type="button"
                            onClick={() => handleDelete(item.id)}
                            disabled={isActionLoading}
                            className="inline-flex items-center gap-1 px-2 py-1 rounded bg-red-50 text-red-700 hover:bg-red-100 disabled:opacity-50"
                          >
                            {isActionLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                            删除
                          </button>
                          <button
                            type="button"
                            onClick={() => handleOpenCaseInAI(item.id)}
                            disabled={isActionLoading}
                            className="inline-flex items-center gap-1 px-2 py-1 rounded bg-indigo-50 text-indigo-700 hover:bg-indigo-100 disabled:opacity-50"
                          >
                            {isActionLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Eye className="w-3.5 h-3.5" />}
                            查看分析
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div ref={historyListRef} className="h-full overflow-auto p-4 space-y-3 bg-slate-50">
            {historySessions.map((item) => (
              <div key={item.session_id} className={`rounded-lg border p-4 ${item.is_pinned ? 'border-indigo-300 bg-indigo-50/30' : 'border-slate-200 bg-white'}`}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-sm font-medium text-gray-900">{item.title || item.summary || item.session_id}</div>
                  <div className="text-xs text-gray-500">{toLocaleTime(item.created_at)}</div>
                </div>
                <div className="mt-2 text-sm text-gray-700">{item.summary || '-'}</div>
                <div className="mt-2 text-xs text-gray-500 flex flex-wrap gap-2">
                  <span>Type: {item.analysis_type || 'log'}</span>
                  <span>Session: {item.session_id}</span>
                  <span>Status: {item.status || 'completed'}</span>
                  {item.is_pinned && (
                    <span className="inline-flex items-center gap-1 text-indigo-600">
                      <Pin className="w-3.5 h-3.5" />
                      已置顶
                    </span>
                  )}
                  {item.is_archived && (
                    <span className="inline-flex items-center gap-1 text-slate-600">
                      <Archive className="w-3.5 h-3.5" />
                      已归档
                    </span>
                  )}
                  <span>Source: {item.source || '-'}</span>
                  <span>Method: {item.analysis_method || '-'}</span>
                  {item.llm_provider && <span>Provider: {item.llm_provider}</span>}
                  {item.llm_model && <span>Model: {item.llm_model}</span>}
                  <span>消息数: {item.message_count ?? 0}</span>
                  {item.trace_id && <span>trace_id: {item.trace_id}</span>}
                </div>
                <div className="mt-2">
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => handleOpenHistorySessionInAI(item.session_id)}
                      disabled={actionLoadingId === item.session_id}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded bg-indigo-50 text-indigo-700 hover:bg-indigo-100 disabled:opacity-50 text-xs"
                    >
                      {actionLoadingId === item.session_id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Eye className="w-3.5 h-3.5" />}
                      在 AI 分析页回放
                    </button>
                    <button
                      type="button"
                      onClick={() => handleRenameHistorySession(item)}
                      disabled={actionLoadingId === item.session_id}
                      className="px-2 py-1 rounded bg-slate-100 text-slate-700 hover:bg-slate-200 disabled:opacity-50 text-xs"
                    >
                      重命名
                    </button>
                    <button
                      type="button"
                      onClick={() => handleTogglePinHistorySession(item)}
                      disabled={actionLoadingId === item.session_id}
                      className="px-2 py-1 rounded bg-purple-50 text-purple-700 hover:bg-purple-100 disabled:opacity-50 text-xs"
                    >
                      {item.is_pinned ? '取消置顶' : '置顶'}
                    </button>
                    <button
                      type="button"
                      onClick={() => handleToggleArchiveHistorySession(item)}
                      disabled={actionLoadingId === item.session_id}
                      className="px-2 py-1 rounded bg-amber-50 text-amber-700 hover:bg-amber-100 disabled:opacity-50 text-xs"
                    >
                      {item.is_archived ? '取消归档' : '归档'}
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDeleteHistorySession(item)}
                      disabled={actionLoadingId === item.session_id}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded bg-red-50 text-red-700 hover:bg-red-100 disabled:opacity-50 text-xs"
                    >
                      {actionLoadingId === item.session_id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                      删除
                    </button>
                  </div>
                </div>
              </div>
            ))}
            <div className="py-2 text-center">
              {historyLoadingMore && (
                <div className="text-xs text-slate-500 inline-flex items-center gap-1">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  加载更多历史记录...
                </div>
              )}
              {!historyLoadingMore && historyHasMore && (
                <button
                  type="button"
                  onClick={loadMoreHistory}
                  className="px-3 py-1.5 text-xs rounded bg-indigo-50 text-indigo-700 hover:bg-indigo-100"
                >
                  加载更多
                </button>
              )}
              {!historyLoadingMore && !historyHasMore && historySessions.length > 0 && (
                <div className="text-xs text-slate-400">
                  已加载全部历史记录（{historySessions.length}/{historyTotalAll || historySessions.length}）
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {kbEditorOpen && kbEditorTarget && (
        <div className="fixed inset-0 z-50 bg-black/35 p-4 flex items-center justify-center">
          <div className="w-full max-w-5xl max-h-[92vh] overflow-auto rounded-xl bg-white shadow-xl border border-slate-200">
            <div className="px-5 py-4 border-b border-slate-200 flex items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-slate-900">知识库内容查看与编辑</h2>
                <div className="text-xs text-slate-500 mt-1">
                  case={kbEditorTarget.id} | status={kbEditorTarget.case_status || '-'} | version=v{kbEditorTarget.knowledge_version || 1}
                </div>
              </div>
              <button
                type="button"
                onClick={closeKBEditor}
                disabled={kbEditorSubmitting}
                className="px-3 py-1.5 rounded bg-slate-100 text-slate-700 hover:bg-slate-200 disabled:opacity-50 text-sm"
              >
                关闭
              </button>
            </div>

            <div className="px-5 py-4 space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <label className="text-xs text-slate-700">
                  问题类型
                  <input
                    value={kbProblemType}
                    onChange={(e) => setKbProblemType(e.target.value)}
                    className="mt-1 w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                    placeholder="例如：timeout / network / database"
                  />
                </label>
                <label className="text-xs text-slate-700">
                  严重级别
                  <select
                    value={kbSeverity}
                    onChange={(e) => setKbSeverity(e.target.value)}
                    className="mt-1 w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                  >
                    <option value="critical">critical</option>
                    <option value="high">high</option>
                    <option value="medium">medium</option>
                    <option value="low">low</option>
                    <option value="unknown">unknown</option>
                  </select>
                </label>
                <label className="text-xs text-slate-700">
                  服务名称
                  <input
                    value={kbServiceName}
                    onChange={(e) => setKbServiceName(e.target.value)}
                    className="mt-1 w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                    placeholder="例如：query-service"
                  />
                </label>
              </div>

              <div>
                <label className="block text-xs text-slate-700 mb-1">
                  摘要<span className="text-red-600">*</span>
                </label>
                <textarea
                  value={kbSummary}
                  onChange={(e) => setKbSummary(e.target.value)}
                  rows={3}
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                />
              </div>

              <div>
                <label className="block text-xs text-slate-700 mb-1">分析总结</label>
                <textarea
                  value={kbAnalysisSummary}
                  onChange={(e) => setKbAnalysisSummary(e.target.value)}
                  rows={3}
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                />
              </div>

              <div>
                <label className="block text-xs text-slate-700 mb-1">根因（每行一条）</label>
                <textarea
                  value={kbRootCausesText}
                  onChange={(e) => setKbRootCausesText(e.target.value)}
                  rows={6}
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                />
              </div>

              <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                <div className="flex items-center justify-between gap-2 mb-2">
                  <div className="text-sm font-medium text-slate-800">解决建议（文本编辑）</div>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={handleOptimizeSolutionText}
                      disabled={kbOptimizingSolutions || kbEditorSubmitting}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100 text-xs disabled:opacity-50"
                    >
                      {kbOptimizingSolutions ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
                      内容优化
                    </button>
                  </div>
                </div>
                <div className="text-[11px] text-slate-500 mb-2">
                  建议输入草稿后点击“内容优化”，将自动规范为统一模板（目标/上下文/处理步骤/验证/回滚/风险）。
                </div>
                <textarea
                  value={kbSolutionsText}
                  onChange={(e) => setKbSolutionsText(e.target.value)}
                  rows={10}
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                  placeholder={'示例：\n方案1: 扩容连接池\n说明: 根据峰值调整 max connections\n步骤:\n1. 调整参数并灰度发布\n2. 观察连接池等待时长\n3. 对比错误率与延迟变化'}
                />
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-slate-700 mb-1">标签（逗号分隔）</label>
                  <input
                    value={kbTagsText}
                    onChange={(e) => setKbTagsText(e.target.value)}
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                    placeholder="例如：timeout, redis, p1"
                  />
                </div>
                <div>
                  <label className="block text-xs text-slate-700 mb-1">最终结论（可选）</label>
                  <textarea
                    value={kbResolution}
                    onChange={(e) => setKbResolution(e.target.value)}
                    rows={2}
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <label className="text-xs text-slate-700">
                  保存策略
                  <select
                    value={kbSaveMode}
                    onChange={(e) => setKbSaveMode(e.target.value === 'local_and_remote' ? 'local_and_remote' : 'local_only')}
                    disabled={!kbRemoteEnabled}
                    className="mt-1 w-full px-3 py-2 border border-slate-300 rounded-lg text-sm disabled:bg-slate-100 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
                  >
                    <option value="local_only">仅本地</option>
                    <option value="local_and_remote">本地 + 远端</option>
                  </select>
                </label>
                <label className="inline-flex items-center gap-2 text-sm text-slate-700 mt-6">
                  <input
                    type="checkbox"
                    checked={kbRemoteEnabled}
                    onChange={(e) => setKbRemoteEnabled(e.target.checked)}
                    className="rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                  />
                  同步到远端知识库
                </label>
              </div>

              <div>
                <label className="block text-xs text-slate-700 mb-1">原始日志片段（只读）</label>
                <pre className="max-h-48 overflow-auto rounded border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700 whitespace-pre-wrap">
                  {kbEditorTarget.log_content || '-'}
                </pre>
              </div>

              <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                <div className="text-sm font-medium text-slate-800 mb-2">
                  内容更新历史（最近 {kbUpdateHistory.length} 条）
                </div>
                {kbUpdateHistory.length === 0 ? (
                  <div className="text-xs text-slate-500">暂无内容更新历史</div>
                ) : (
                  <div className="overflow-auto border border-slate-200 rounded bg-white">
                    <table className="min-w-full text-xs text-slate-700">
                      <thead className="bg-slate-100 text-slate-600">
                        <tr>
                          <th className="px-2 py-2 text-left">版本</th>
                          <th className="px-2 py-2 text-left">时间</th>
                          <th className="px-2 py-2 text-left">编辑人</th>
                          <th className="px-2 py-2 text-left">变更字段</th>
                          <th className="px-2 py-2 text-left">变更摘要</th>
                          <th className="px-2 py-2 text-left">同步</th>
                          <th className="px-2 py-2 text-left">备注</th>
                        </tr>
                      </thead>
                      <tbody>
                        {kbUpdateHistory.map((item) => (
                          <tr key={`${item.event_id || item.version}-${item.updated_at}`} className="border-t border-slate-100 align-top">
                            <td className="px-2 py-2 whitespace-nowrap">v{item.version || '-'}</td>
                            <td className="px-2 py-2 whitespace-nowrap">{toLocaleTime(item.updated_at)}</td>
                            <td className="px-2 py-2 whitespace-nowrap">{item.editor || '-'}</td>
                            <td className="px-2 py-2">
                              {item.changed_fields.length > 0 ? (
                                <div className="flex flex-wrap gap-1">
                                  {formatHistoryFields(item.changed_fields).map((field) => (
                                    <span key={`${item.event_id || item.version}-${field}`} className="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700">
                                      {field}
                                    </span>
                                  ))}
                                </div>
                              ) : (
                                <div>
                                  <div className="text-amber-700">无有效字段变更</div>
                                  {item.requested_fields.length > 0 && (
                                    <div className="mt-1 text-slate-500">
                                      提交字段：{formatHistoryFields(item.requested_fields).join('、')}
                                    </div>
                                  )}
                                </div>
                              )}
                            </td>
                            <td className="px-2 py-2 min-w-[320px]">
                              {Object.keys(item.changes || {}).length === 0 ? (
                                <div className="text-slate-600 space-y-1">
                                  <div className="text-amber-700">本次提交未引入有效差异</div>
                                  <div>
                                    原因：{formatNoEffectiveChangeReason(item.no_effective_change_reason)}
                                  </div>
                                  {item.unchanged_requested_fields.length > 0 && (
                                    <div>
                                      等效字段：{formatHistoryFields(item.unchanged_requested_fields).join('、')}
                                    </div>
                                  )}
                                </div>
                              ) : (
                                <details className="group">
                                  <summary className="cursor-pointer text-indigo-700 hover:text-indigo-900">
                                    查看详情（{Object.keys(item.changes || {}).length} 项）
                                  </summary>
                                  <div className="mt-2 max-h-56 overflow-auto space-y-2 pr-1">
                                    {Object.entries(item.changes || {}).map(([field, diff]) => (
                                      <div key={`${item.event_id || item.version}-${field}`} className="rounded border border-slate-200 bg-slate-50 px-2 py-1">
                                        <div className="font-medium text-slate-800">{field}</div>
                                        <HistoryDiffView
                                          beforeValue={diff?.before}
                                          afterValue={diff?.after}
                                        />
                                      </div>
                                    ))}
                                  </div>
                                </details>
                              )}
                            </td>
                            <td className="px-2 py-2">
                              <span className={`px-1.5 py-0.5 rounded ${syncStatusClass(item.sync_status)}`}>
                                {item.sync_status || 'unknown'}
                              </span>
                              {item.sync_error_code && <div className="text-red-600 mt-1">{item.sync_error_code}</div>}
                            </td>
                            <td className="px-2 py-2 whitespace-pre-wrap">{formatHistoryNote(item.note)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>

            <div className="px-5 py-4 border-t border-slate-200 flex justify-end gap-2">
              <button
                type="button"
                onClick={closeKBEditor}
                disabled={kbEditorSubmitting}
                className="px-3 py-1.5 rounded bg-white border border-slate-300 text-slate-700 text-sm hover:bg-slate-50 disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={handleSubmitKBContent}
                disabled={kbEditorSubmitting || kbEditorLoading}
                className="inline-flex items-center gap-2 px-3 py-1.5 rounded bg-indigo-600 text-white text-sm hover:bg-indigo-700 disabled:opacity-50"
              >
                {(kbEditorSubmitting || kbEditorLoading) && <Loader2 className="w-4 h-4 animate-spin" />}
                保存知识库内容
              </button>
            </div>
          </div>
        </div>
      )}

      {remediationEditorOpen && remediationTarget && (
        <div className="fixed inset-0 z-50 bg-black/35 p-4 flex items-center justify-center">
          <div className="w-full max-w-4xl max-h-[92vh] overflow-auto rounded-xl bg-white shadow-xl border border-slate-200">
            <div className="px-5 py-4 border-b border-slate-200 flex items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-slate-900">人工修复步骤更新</h2>
                <div className="text-xs text-slate-500 mt-1">
                  case={remediationTarget.id} | version=v{remediationTarget.knowledge_version || 1}
                </div>
              </div>
              <button
                type="button"
                onClick={closeRemediationEditor}
                disabled={remediationSubmitting}
                className="px-3 py-1.5 rounded bg-slate-100 text-slate-700 hover:bg-slate-200 disabled:opacity-50 text-sm"
              >
                关闭
              </button>
            </div>

            <div className="px-5 py-4 space-y-4">
              <div className="text-sm text-slate-700">
                <span className="font-medium">摘要：</span>
                {remediationTarget.summary || '-'}
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs text-slate-600">
                <div>服务：{remediationTarget.service_name || '-'}</div>
                <div>问题类型：{remediationTarget.problem_type || '-'}</div>
                <div>
                  同步状态：
                  <span className={`ml-1 px-1.5 py-0.5 rounded ${syncStatusClass(remediationTarget.sync_status)}`}>
                    {remediationTarget.sync_status || '-'}
                  </span>
                </div>
                <div>外部文档 ID：{remediationTarget.external_doc_id || '-'}</div>
              </div>

              <div>
                <label className="block text-sm font-medium text-slate-800 mb-1">
                  修复步骤（每行一条，至少 1 条）<span className="text-red-600">*</span>
                </label>
                <textarea
                  value={remediationStepsText}
                  onChange={(e) => setRemediationStepsText(e.target.value)}
                  rows={5}
                  placeholder="例如：\n1. 调整 payment-service timeout 为 5s\n2. 开启指数退避重试并灰度 20%"
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                />
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-slate-800 mb-1">
                    验证结果<span className="text-red-600">*</span>
                  </label>
                  <select
                    value={remediationVerificationResult}
                    onChange={(e) => setRemediationVerificationResult(e.target.value === 'fail' ? 'fail' : 'pass')}
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                  >
                    <option value="pass">pass（验证通过）</option>
                    <option value="fail">fail（验证未通过）</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-800 mb-1">
                    保存策略
                  </label>
                  <select
                    value={remediationSaveMode}
                    onChange={(e) => setRemediationSaveMode(e.target.value === 'local_and_remote' ? 'local_and_remote' : 'local_only')}
                    disabled={!remediationRemoteEnabled}
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm disabled:bg-slate-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                  >
                    <option value="local_only">仅本地</option>
                    <option value="local_and_remote">本地 + 远端</option>
                  </select>
                </div>
              </div>

              <label className="inline-flex items-center gap-2 text-sm text-slate-700">
                <input
                  type="checkbox"
                  checked={remediationRemoteEnabled}
                  onChange={(e) => setRemediationRemoteEnabled(e.target.checked)}
                  className="rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                />
                同步到远端知识库（未接入时后端会自动回退本地）
              </label>

              <div>
                <label className="block text-sm font-medium text-slate-800 mb-1">
                  验证说明（不少于 20 字）<span className="text-red-600">*</span>
                </label>
                <textarea
                  value={remediationVerificationNotes}
                  onChange={(e) => setRemediationVerificationNotes(e.target.value)}
                  rows={4}
                  placeholder="说明如何验证、观测指标是否恢复、是否有副作用"
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                />
                <div className="text-xs text-slate-500 mt-1">当前长度：{remediationVerificationNotes.trim().length}</div>
              </div>

              <div>
                <label className="block text-sm font-medium text-slate-800 mb-1">
                  最终解决方案（可选）
                </label>
                <textarea
                  value={remediationFinalResolution}
                  onChange={(e) => setRemediationFinalResolution(e.target.value)}
                  rows={3}
                  placeholder="可补充最终方案摘要，便于后续检索命中"
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                />
              </div>

              <div className="border border-slate-200 rounded-lg p-3 bg-slate-50">
                <div className="text-sm font-medium text-slate-800 mb-2">
                  修复版本历史（最近 {remediationHistory.length} 条）
                </div>
                {remediationLoading ? (
                  <div className="text-xs text-slate-500 inline-flex items-center gap-1">
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    加载修复历史...
                  </div>
                ) : remediationHistory.length === 0 ? (
                  <div className="text-xs text-slate-500">暂无历史修复记录</div>
                ) : (
                  <div className="space-y-2">
                    {remediationHistory.map((item) => (
                      <div key={`${item.version}-${item.updated_at}`} className="rounded border border-slate-200 bg-white p-2 text-xs text-slate-700">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">v{item.version || '-'}</span>
                          <span>{toLocaleTime(item.updated_at)}</span>
                          <span>editor={item.editor || '-'}</span>
                          <span className={`px-1.5 py-0.5 rounded ${syncStatusClass(item.sync_status)}`}>
                            {item.sync_status || 'unknown'}
                          </span>
                          <span>{item.effective_save_mode || '-'}</span>
                        </div>
                        {item.manual_remediation_steps.length > 0 && (
                          <div className="mt-1">
                            步骤：{item.manual_remediation_steps.join('；')}
                          </div>
                        )}
                        <div className="mt-1">验证：{item.verification_result || '-'}</div>
                        {item.verification_notes && <div className="mt-1">说明：{item.verification_notes}</div>}
                        {item.final_resolution && <div className="mt-1">结论：{item.final_resolution}</div>}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>

            <div className="px-5 py-4 border-t border-slate-200 flex justify-end gap-2">
              <button
                type="button"
                onClick={closeRemediationEditor}
                disabled={remediationSubmitting}
                className="px-3 py-2 rounded-lg bg-slate-100 text-slate-700 hover:bg-slate-200 disabled:opacity-50 text-sm"
              >
                取消
              </button>
              <button
                type="button"
                onClick={handleSubmitRemediation}
                disabled={remediationSubmitting}
                className="inline-flex items-center gap-1 px-3 py-2 rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 text-sm"
              >
                {remediationSubmitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                提交修复步骤
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default AICaseManagement;
