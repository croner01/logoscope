/**
 * AI 分析页面
 * 参考 Datadog 设计风格
 * 支持从日志/拓扑页面跳转过来的数据
 * 集成相似案例推荐
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import LoadingState from '../components/common/LoadingState';
import EmptyState from '../components/common/EmptyState';
import SimilarCases, { type SimilarCase } from '../components/ai/SimilarCases';
import HistoryDiffView from '../components/ai/HistoryDiffView';
import { api } from '../utils/api';
import { copyTextToClipboard } from '../utils/clipboard';
import { useNavigation } from '../hooks/useNavigation';
import { BrainCircuit, Loader2, AlertCircle, Lightbulb, Bug, Zap, FileText, Network, BookOpen, Bookmark, History, MessageCircle, RotateCcw, Send, X, Link2, Copy, RefreshCw, Trash2 } from 'lucide-react';

interface AIAnalysisResult {
  overview?: {
    problem: string;
    severity: string;
    description: string;
    confidence: number;
  };
  rootCauses?: Array<{
    title: string;
    description: string;
  }>;
  solutions?: Array<{
    title: string;
    description: string;
    steps: string[];
  }>;
  similarCases?: Array<{
    title: string;
    description: string;
  }>;
}

interface AIAnalysisResponse extends AIAnalysisResult {
  analysis_method?: string;
  model?: string;
  cached?: boolean;
  latency_ms?: number;
  error?: string;
  session_id?: string;
}

interface LocationState {
  logData?: {
    id: string;
    timestamp: string;
    service_name: string;
    level: string;
    message: string;
    pod_name?: string;
    namespace?: string;
    trace_id?: string;
    attributes?: Record<string, any>;
  };
  traceId?: string;
  serviceName?: string;
  message?: string;
  mode?: 'log' | 'trace';
  autoAnalyze?: boolean;
  historySession?: {
    session_id: string;
    analysis_type: 'log' | 'trace' | string;
    title?: string;
    service_name: string;
    trace_id?: string;
    input_text: string;
    context?: Record<string, any>;
    result?: AIAnalysisResponse;
    summary?: string;
    summary_text?: string;
    analysis_method?: string;
    llm_model?: string;
    llm_provider?: string;
    source?: string;
    is_pinned?: boolean;
    is_archived?: boolean;
    context_pills?: Array<{ key: string; value: string }>;
    created_at: string;
    updated_at: string;
    messages: Array<{
      message_id?: string;
      role: 'user' | 'assistant';
      content: string;
      timestamp?: string;
      metadata?: Record<string, any>;
    }>;
  };
  historyCase?: {
    id: string;
    problem_type: string;
    severity: string;
    summary: string;
    log_content: string;
    service_name: string;
    root_causes: string[];
    solutions: Array<{ title?: string; description?: string; steps?: string[] }>;
    context: Record<string, any>;
    resolved?: boolean;
    resolution?: string;
    llm_model?: string;
    llm_metadata?: Record<string, any>;
    source?: string;
    case_status?: string;
    knowledge_version?: number;
    manual_remediation_steps?: string[];
    verification_result?: 'pass' | 'fail';
    verification_notes?: string;
    sync_status?: string;
    external_doc_id?: string;
    content_update_history?: CaseContentHistoryItem[];
    content_update_history_count?: number;
    analysis_result?: AIAnalysisResponse;
  };
}

interface ServiceErrorSnapshot {
  serviceName: string;
  generatedInput: string;
  summaryLines: string[];
  rawLogs: Array<{
    id: string;
    timestamp: string;
    level: string;
    message: string;
  }>;
  context: Record<string, any>;
}

interface AIHistoryItem {
  session_id: string;
  analysis_type: 'log' | 'trace' | string;
  title?: string;
  service_name: string;
  trace_id?: string;
  summary: string;
  summary_text?: string;
  analysis_method?: string;
  llm_model?: string;
  created_at: string;
  updated_at: string;
  is_pinned?: boolean;
  is_archived?: boolean;
  message_count?: number;
}

interface CaseContentHistoryItem {
  event_id?: string;
  event_type?: string;
  version?: number;
  updated_at?: string;
  editor?: string;
  changed_fields?: string[];
  changes?: Record<string, { before?: any; after?: any }>;
  requested_fields?: string[];
  unchanged_requested_fields?: string[];
  no_effective_change_reason?: string;
  effective_save_mode?: string;
  sync_status?: string;
  sync_error_code?: string;
  note?: string;
  source?: string;
}

interface FollowUpReference {
  id: string;
  type: string;
  title: string;
  snippet: string;
}

interface CaseStoredMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp?: string;
  message_id?: string;
  metadata?: Record<string, any>;
}

interface FollowUpMessage {
  message_id?: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp?: string;
  metadata?: {
    references?: FollowUpReference[];
    context_pills?: Array<{ key: string; value: string }>;
    token_budget?: number;
    token_estimate?: number;
    token_remaining?: number;
    token_warning?: boolean;
    history_compacted?: boolean;
    [key: string]: any;
  };
}

type RelatedLogAssistMode = 'auto' | 'ask' | 'off';
type CaseDetail = NonNullable<LocationState['historyCase']> & {
  resolved?: boolean;
  resolution?: string;
  tags?: string[];
  created_at?: string;
  updated_at?: string;
  resolved_at?: string;
  content_update_history?: CaseContentHistoryItem[];
  content_update_history_count?: number;
};

interface KBCandidate {
  id: string;
  summary: string;
  problem_type: string;
  service_name?: string;
  similarity_score: number;
  source_backend: 'local' | 'external';
  resolution?: string;
  verification_result?: 'pass' | 'fail';
}

const parseKBRuntimeError = (err: any): {
  code: string;
  message: string;
  effectiveRetrievalMode: 'local' | 'hybrid';
  effectiveSaveMode: 'local_only' | 'local_and_remote';
} => {
  const detail = (err?.response?.data?.detail && typeof err.response.data.detail === 'object')
    ? err.response.data.detail
    : {};
  const code = String(detail.code || '');
  const message = String(detail.message || err?.message || '知识库策略解析失败，已按本地模式处理');
  const effectiveRetrievalMode = detail.effective_retrieval_mode === 'hybrid' ? 'hybrid' : 'local';
  const effectiveSaveMode = detail.effective_save_mode === 'local_and_remote' ? 'local_and_remote' : 'local_only';
  return { code, message, effectiveRetrievalMode, effectiveSaveMode };
};

const parseFollowUpErrorMessage = (err: any): string => {
  const status = Number(err?.response?.status || err?.status || 0);
  const errorCode = String(err?.code || '').toUpperCase();
  const errorMessage = String(err?.message || '').toLowerCase();
  if (errorCode === 'ECONNABORTED' || errorMessage.includes('timeout')) {
    return '对话请求超时，已保留问题内容。可点击“重试”或关闭 LLM 后重试。';
  }
  const detail = err?.response?.data?.detail;
  if (status === 504) {
    return '对话请求超时（504），已保留问题内容。可点击“重试”或关闭 LLM 后重试。';
  }
  if (status === 503 || status === 502) {
    return `对话服务暂时不可用（${status}），请稍后重试。`;
  }
  if (typeof detail === 'string' && detail.trim()) {
    return detail.trim();
  }
  if (detail && typeof detail === 'object' && detail.message) {
    return String(detail.message);
  }
  return err?.message || '追问失败，请稍后重试';
};

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

const formatNoEffectiveChangeReason = (reason?: string): string => {
  const normalized = String(reason || '').trim();
  if (!normalized) {
    return '提交内容经过规范化后与当前版本一致';
  }
  if (normalized === 'submitted_values_equivalent_after_normalization') {
    return '提交内容经过规范化后与当前版本一致';
  }
  return normalized;
};

const formatHistoryNote = (note?: string): string => {
  const normalized = String(note || '').trim();
  if (!normalized) return '-';
  if (normalized === 'manual_content_update') return '手动更新内容';
  if (normalized === 'manual_content_update_no_effective_change') return '提交生效但无有效字段变更';
  return normalized;
};

const TOPOLOGY_AI_SOURCES = new Set(['topology-node', 'topology-edge']);

const AIAnalysis: React.FC = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const navigation = useNavigation();
  
  const [analysisType, setAnalysisType] = useState<'log' | 'trace'>('log');
  const [inputText, setInputText] = useState('');
  const [serviceName, setServiceName] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [result, setResult] = useState<AIAnalysisResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sourceLogData, setSourceLogData] = useState<LocationState['logData'] | null>(null);
  const [useLLM, setUseLLM] = useState(true);
  const [llmInfo, setLLMInfo] = useState<{ method?: string; model?: string; cached?: boolean; latency_ms?: number } | null>(null);
  const [similarCases, setSimilarCases] = useState<SimilarCase[]>([]);
  const [loadingSimilarCases, setLoadingSimilarCases] = useState(false);
  const [selectedSimilarCase, setSelectedSimilarCase] = useState<SimilarCase | null>(null);
  const [selectedSimilarCaseDetail, setSelectedSimilarCaseDetail] = useState<CaseDetail | null>(null);
  const [loadingSimilarCaseDetail, setLoadingSimilarCaseDetail] = useState(false);
  const [serviceErrorSnapshot, setServiceErrorSnapshot] = useState<ServiceErrorSnapshot | null>(null);
  const [serviceErrorSnapshotLoadedAt, setServiceErrorSnapshotLoadedAt] = useState('');
  const [loadingServiceErrorSnapshot, setLoadingServiceErrorSnapshot] = useState(false);
  const [serviceErrorSnapshotError, setServiceErrorSnapshotError] = useState<string | null>(null);
  const [historyItems, setHistoryItems] = useState<AIHistoryItem[]>([]);
  const [loadingHistoryItems, setLoadingHistoryItems] = useState(false);
  const [loadingMoreHistoryItems, setLoadingMoreHistoryItems] = useState(false);
  const [historyTotalAll, setHistoryTotalAll] = useState(0);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [analysisSessionId, setAnalysisSessionId] = useState('');
  const [conversationId, setConversationId] = useState('');
  const [followUpMessages, setFollowUpMessages] = useState<FollowUpMessage[]>([]);
  const [followUpQuestion, setFollowUpQuestion] = useState('');
  const [followUpLoading, setFollowUpLoading] = useState(false);
  const [followUpError, setFollowUpError] = useState<string | null>(null);
  const [followUpNotice, setFollowUpNotice] = useState<string>('');
  const [contextPills, setContextPills] = useState<Array<{ key: string; value: string }>>([]);
  const [tokenHint, setTokenHint] = useState<{
    warning?: boolean;
    historyCompacted?: boolean;
  }>({});
  const [actionDrafts, setActionDrafts] = useState<Record<string, { action_type: string; title: string; payload: Record<string, any> }>>({});
  const [actionLoadingKey, setActionLoadingKey] = useState<string>('');
  const [deletingMessageKey, setDeletingMessageKey] = useState<string>('');
  const [relatedLogAssistMode, setRelatedLogAssistMode] = useState<RelatedLogAssistMode>('auto');
  const [analysisAssistNotice, setAnalysisAssistNotice] = useState<string>('');
  const [historyHint, setHistoryHint] = useState<string>('');
  const [kbRemoteEnabled, setKbRemoteEnabled] = useState(false);
  const [kbRetrievalMode, setKbRetrievalMode] = useState<'local' | 'hybrid'>('local');
  const [kbSaveMode, setKbSaveMode] = useState<'local_only' | 'local_and_remote'>('local_only');
  const [kbRemoteAvailable, setKbRemoteAvailable] = useState(false);
  const [kbEffectiveRetrievalMode, setKbEffectiveRetrievalMode] = useState<'local' | 'hybrid'>('local');
  const [kbEffectiveSaveMode, setKbEffectiveSaveMode] = useState<'local_only' | 'local_and_remote'>('local_only');
  const [kbRuntimeNotice, setKbRuntimeNotice] = useState('');
  const [kbRuntimeLoading, setKbRuntimeLoading] = useState(false);
  const [kbSearchResults, setKbSearchResults] = useState<KBCandidate[]>([]);
  const [kbSearchSources, setKbSearchSources] = useState<{ local: number; external: number }>({ local: 0, external: 0 });
  const [kbSearchLoading, setKbSearchLoading] = useState(false);
  const [manualRemediationText, setManualRemediationText] = useState('');
  const [verificationResult, setVerificationResult] = useState<'pass' | 'fail'>('pass');
  const [verificationNotes, setVerificationNotes] = useState('');
  const [finalResolution, setFinalResolution] = useState('');
  const [manualCaseId, setManualCaseId] = useState('');
  const [kbDraftLoading, setKbDraftLoading] = useState(false);
  const [kbSubmitLoading, setKbSubmitLoading] = useState(false);
  const [kbActionNotice, setKbActionNotice] = useState('');
  const followUpListRef = useRef<HTMLDivElement | null>(null);
  const followUpNoticeTimerRef = useRef<number | null>(null);

  const normalizeFollowUpMessages = (raw: any): FollowUpMessage[] => {
    if (!Array.isArray(raw)) {
      return [];
    }
    return raw
      .map((item: any) => {
        const role = item?.role === 'assistant' ? 'assistant' : item?.role === 'user' ? 'user' : '';
        const content = String(item?.content || '').trim();
        if (!role || !content) {
          return null;
        }
        return {
          message_id: item?.message_id ? String(item.message_id) : undefined,
          role,
          content,
          timestamp: item?.timestamp ? String(item.timestamp) : undefined,
          metadata: (item?.metadata && typeof item.metadata === 'object') ? item.metadata : {},
        } as FollowUpMessage;
      })
      .filter(Boolean) as FollowUpMessage[];
  };

  const loadHistoryItems = useCallback(async (options?: { offset?: number; reset?: boolean }) => {
    const reset = options?.reset ?? true;
    const offset = Math.max(0, Number(options?.offset || 0));
    if (reset) {
      setLoadingHistoryItems(true);
    } else {
      setLoadingMoreHistoryItems(true);
    }
    try {
      const limit = 50;
      const response = await api.getAIHistory({
        limit,
        offset,
        include_archived: true,
        pinned_first: true,
      });
      const items = Array.isArray(response.sessions) ? response.sessions : [];
      if (reset) {
        setHistoryItems(items as AIHistoryItem[]);
      } else {
        setHistoryItems((prev) => {
          const existing = new Set(prev.map((item) => item.session_id));
          const appended = (items as AIHistoryItem[]).filter((item) => !existing.has(item.session_id));
          return [...prev, ...appended];
        });
      }
      const totalAll = Number(response.total_all ?? items.length);
      setHistoryTotalAll(totalAll);
      const hasMore = typeof response.has_more === 'boolean'
        ? response.has_more
        : offset + items.length < totalAll;
      setHistoryHasMore(hasMore);
      setHistoryHint(totalAll > limit ? `已加载 ${offset + items.length}/${totalAll} 条，可继续加载或在“查看全部”检索` : '');
    } catch (err) {
      console.warn('Failed to load AI history records:', err);
      if (reset) {
        setHistoryItems([]);
        setHistoryTotalAll(0);
        setHistoryHasMore(false);
      }
      setHistoryHint('历史记录加载失败，可点击“查看全部”或稍后重试。');
    } finally {
      if (reset) {
        setLoadingHistoryItems(false);
      } else {
        setLoadingMoreHistoryItems(false);
      }
    }
  }, []);

  useEffect(() => {
    loadHistoryItems({ reset: true, offset: 0 });
  }, [loadHistoryItems]);

  const handleLoadMoreHistoryItems = useCallback(() => {
    if (loadingHistoryItems || loadingMoreHistoryItems || !historyHasMore) {
      return;
    }
    loadHistoryItems({
      reset: false,
      offset: historyItems.length,
    });
  }, [historyHasMore, historyItems.length, loadHistoryItems, loadingHistoryItems, loadingMoreHistoryItems]);

  useEffect(() => {
    if (!followUpListRef.current) {
      return;
    }
    followUpListRef.current.scrollTop = followUpListRef.current.scrollHeight;
  }, [followUpMessages, followUpLoading]);

  useEffect(() => {
    return () => {
      if (followUpNoticeTimerRef.current) {
        window.clearTimeout(followUpNoticeTimerRef.current);
      }
    };
  }, []);

  const showFollowUpNotice = useCallback((message: string, timeoutMs = 1800) => {
    if (!message.trim()) {
      return;
    }
    setFollowUpNotice(message);
    if (followUpNoticeTimerRef.current) {
      window.clearTimeout(followUpNoticeTimerRef.current);
    }
    followUpNoticeTimerRef.current = window.setTimeout(() => {
      setFollowUpNotice('');
      followUpNoticeTimerRef.current = null;
    }, timeoutMs);
  }, []);

  useEffect(() => {
    const currentService = serviceName.trim().toLowerCase();
    if (!currentService) {
      return;
    }
    const snapshotService = String(serviceErrorSnapshot?.serviceName || '').trim().toLowerCase();
    if (serviceErrorSnapshot && snapshotService !== currentService) {
      setServiceErrorSnapshot(null);
      setServiceErrorSnapshotLoadedAt('');
      setServiceErrorSnapshotError(null);
    }
  }, [serviceName, serviceErrorSnapshot]);

  useEffect(() => {
    if (result?.overview?.description && !finalResolution) {
      setFinalResolution(String(result.overview.description));
    }
  }, [result?.overview?.description, finalResolution]);

  const resetFollowUpConversation = () => {
    setConversationId('');
    setFollowUpMessages([]);
    setFollowUpQuestion('');
    setFollowUpError(null);
    setFollowUpNotice('');
    setTokenHint({});
    setActionDrafts({});
  };

  const parseManualSteps = (raw: string): string[] => {
    return raw
      .split('\n')
      .map((line) => line.replace(/^[\-\*\d\.\)\s]+/, '').trim())
      .filter((line) => line.length >= 5);
  };

  const buildVerificationNotesFromDraft = (draft: Record<string, any>): string => {
    const summary = String(draft?.analysis_summary || draft?.summary || '').trim();
    const rootCauses = Array.isArray(draft?.root_causes) ? draft.root_causes : [];
    const solutions = Array.isArray(draft?.solutions) ? draft.solutions : [];
    const solutionTitles = solutions
      .map((item: any) => String(item?.title || '').trim())
      .filter((item: string) => item.length > 0)
      .slice(0, 3);

    const lines: string[] = [];
    if (summary) {
      lines.push(`会话总结：${summary}`);
    }
    if (rootCauses.length > 0) {
      lines.push(`根因要点：${rootCauses.slice(0, 5).map((item: any) => String(item)).join('；')}`);
    }
    if (solutionTitles.length > 0) {
      lines.push(`建议方案：${solutionTitles.join('；')}`);
    }
    lines.push('已根据会话内容生成草稿，请人工复核后提交。');

    const merged = lines.join('\n').trim();
    if (merged.length >= 20) {
      return merged;
    }
    return `${summary || '已生成会话草稿'}，请补充验证过程与结果。`;
  };

  const syncKBRuntimeOptions = useCallback(async (
    overrides?: {
      remoteEnabled?: boolean;
      retrievalMode?: 'local' | 'hybrid';
      saveMode?: 'local_only' | 'local_and_remote';
    },
  ) => {
    const remoteEnabled = overrides?.remoteEnabled ?? kbRemoteEnabled;
    const retrievalMode = overrides?.retrievalMode ?? kbRetrievalMode;
    const saveMode = overrides?.saveMode ?? kbSaveMode;
    setKbRuntimeLoading(true);
    try {
      const response = await api.resolveKBRuntimeOptions({
        remote_enabled: remoteEnabled,
        retrieval_mode: retrievalMode,
        save_mode: saveMode,
      });
      setKbEffectiveRetrievalMode(response.effective_retrieval_mode || 'local');
      setKbEffectiveSaveMode(response.effective_save_mode || 'local_only');
      setKbRemoteAvailable(Boolean(response.remote_available));
      if (!response.remote_available && kbSaveMode === 'local_and_remote') {
        setKbSaveMode('local_only');
      }
      setKbRuntimeNotice(String(response.message || ''));
      return response;
    } catch (err: any) {
      const parsed = parseKBRuntimeError(err);
      setKbEffectiveRetrievalMode('local');
      setKbEffectiveSaveMode('local_only');
      setKbRemoteAvailable(false);
      if (kbSaveMode === 'local_and_remote') {
        setKbSaveMode('local_only');
      }
      setKbRuntimeNotice(parsed.message);
      return {
        effective_retrieval_mode: parsed.effectiveRetrievalMode,
        effective_save_mode: parsed.effectiveSaveMode,
        remote_available: false,
        provider_name: '',
        message: parsed.message,
      };
    } finally {
      setKbRuntimeLoading(false);
    }
  }, [kbRemoteEnabled, kbRetrievalMode, kbSaveMode]);

  const handleKBSearch = useCallback(async (query: string, opts?: { problemType?: string; service?: string }) => {
    const content = String(query || '').trim();
    if (content.length < 3) {
      setKbSearchResults([]);
      setKbSearchSources({ local: 0, external: 0 });
      return;
    }
    setKbSearchLoading(true);
    try {
      const runtime = await syncKBRuntimeOptions();
      const mode = runtime?.effective_retrieval_mode || kbEffectiveRetrievalMode || 'local';
      const response = await api.searchKB({
        query: content,
        service_name: opts?.service || serviceName || undefined,
        problem_type: opts?.problemType || undefined,
        top_k: 5,
        retrieval_mode: mode,
      });
      setKbSearchResults(Array.isArray(response.cases) ? response.cases as KBCandidate[] : []);
      setKbSearchSources(response.sources || { local: 0, external: 0 });
      if (response.message) {
        setKbRuntimeNotice(String(response.message));
      }
    } catch (err: any) {
      setKbRuntimeNotice(err?.message || '联合知识检索失败，已回退本地流程');
      setKbSearchResults([]);
      setKbSearchSources({ local: 0, external: 0 });
    } finally {
      setKbSearchLoading(false);
    }
  }, [kbEffectiveRetrievalMode, serviceName, syncKBRuntimeOptions]);

  const handleBuildKBFromSession = async () => {
    if (kbDraftLoading || kbSubmitLoading) {
      return;
    }
    if (!analysisSessionId) {
      setKbActionNotice('缺少分析会话 ID，无法提取知识草稿');
      return;
    }
    setKbDraftLoading(true);
    try {
      const normalizedHistory = followUpMessages
        .slice(-240)
        .map((msg) => ({
          role: msg.role,
          content: String(msg.content || ''),
          timestamp: msg.timestamp,
          message_id: msg.message_id,
          metadata: (msg.metadata && typeof msg.metadata === 'object')
            ? msg.metadata
            : {},
        }))
        .filter((msg) => msg.content.trim());
      const runtime = await syncKBRuntimeOptions();
      const response = await api.buildKBFromAnalysisSession({
        analysis_session_id: analysisSessionId,
        include_followup: true,
        history: normalizedHistory,
        use_llm: useLLM,
        save_mode: runtime?.effective_save_mode || kbEffectiveSaveMode,
        remote_enabled: kbRemoteEnabled,
      });
      const draft = response.draft_case || {};
      const steps = Array.isArray(draft.manual_remediation_steps) ? draft.manual_remediation_steps : [];
      if (steps.length > 0) {
        setManualRemediationText(steps.map((item: string, idx: number) => `${idx + 1}. ${item}`).join('\n'));
      }
      const normalizedSummary = String(draft.analysis_summary || draft.summary || '').trim();
      if (normalizedSummary) {
        setFinalResolution(normalizedSummary);
      }
      setVerificationNotes(buildVerificationNotesFromDraft(draft));
      if (Array.isArray(response.missing_required_fields) && response.missing_required_fields.length > 0) {
        setKbActionNotice(`草稿已生成，但缺失字段: ${response.missing_required_fields.join(', ')}`);
      } else {
        const draftMethod = response.draft_method || (useLLM ? 'llm' : 'rule-based');
        const prefilledFields = [
          steps.length > 0 ? '人工修复步骤' : '',
          normalizedSummary ? '最终解决方案' : '',
          '验证说明',
        ].filter(Boolean).join('、');
        setKbActionNotice(`已从会话提取知识草稿（method=${draftMethod}），已预填：${prefilledFields}`);
      }
    } catch (err: any) {
      setKbActionNotice(err?.message || '提取知识草稿失败');
    } finally {
      setKbDraftLoading(false);
    }
  };

  useEffect(() => {
    syncKBRuntimeOptions().catch(() => {
      // no-op: runtime notice handled in helper
    });
  }, [kbRemoteEnabled, kbRetrievalMode, kbSaveMode, syncKBRuntimeOptions]);

  const handleSubmitManualRemediation = async () => {
    if (kbDraftLoading || kbSubmitLoading) {
      return;
    }
    if (!result) {
      setKbActionNotice('请先完成分析再提交知识库');
      return;
    }
    const steps = parseManualSteps(manualRemediationText);
    if (steps.length < 1) {
      setKbActionNotice('请至少填写 1 条人工修复步骤，且每条不少于 5 个字符');
      return;
    }
    if (verificationNotes.trim().length < 20) {
      setKbActionNotice('验证说明至少 20 字');
      return;
    }

    setKbSubmitLoading(true);
    try {
      const runtime = await syncKBRuntimeOptions();
      const effectiveSaveMode = runtime?.effective_save_mode || kbEffectiveSaveMode || 'local_only';
      let caseId = manualCaseId.trim();
      if (!caseId) {
        const saveResult = await api.saveCase({
          problem_type: result.overview?.problem || 'unknown',
          severity: result.overview?.severity || 'medium',
          summary: result.overview?.description || finalResolution || '',
          log_content: inputText,
          service_name: serviceName,
          root_causes: result.rootCauses?.map((r) => r.title) || [],
          solutions: result.solutions || [],
          context: {
            analysis_session_id: analysisSessionId || '',
            conversation_id: conversationId || '',
          },
          llm_provider: llmInfo?.method === 'llm' ? 'runtime' : '',
          llm_model: llmInfo?.model || '',
          llm_metadata: {
            analysis_method: llmInfo?.method,
            case_status: 'archived',
          },
          source: 'ai-analysis-page',
          save_mode: effectiveSaveMode,
          remote_enabled: kbRemoteEnabled,
        });
        caseId = String(saveResult.id || '').trim();
        if (!caseId) {
          throw new Error('创建知识库条目失败，未返回 case_id');
        }
        setManualCaseId(caseId);
      }

      const updateResult = await api.updateCaseManualRemediation(caseId, {
        manual_remediation_steps: steps,
        verification_result: verificationResult,
        verification_notes: verificationNotes.trim(),
        final_resolution: finalResolution.trim(),
        save_mode: effectiveSaveMode,
        remote_enabled: kbRemoteEnabled,
      });

      setKbActionNotice(
        `知识库提交成功：case=${caseId}，version=${updateResult.knowledge_version}，sync=${updateResult.sync_status}`
      );
      if (analysisType === 'log') {
        handleKBSearch(inputText, { problemType: result.overview?.problem, service: serviceName });
      }
    } catch (err: any) {
      setKbActionNotice(err?.message || '提交知识库失败');
    } finally {
      setKbSubmitLoading(false);
    }
  };

  const restoreFollowUpMessagesFromCase = (historyCase: NonNullable<LocationState['historyCase']>): FollowUpMessage[] => {
    return normalizeFollowUpMessages(historyCase?.llm_metadata?.follow_up_messages);
  };

  const restoreFollowUpMessagesFromSession = (messages: any): FollowUpMessage[] => {
    return normalizeFollowUpMessages(messages);
  };

  const hydrateFollowUpMessagesFromSession = async (sessionId: string) => {
    const targetSessionId = String(sessionId || '').trim();
    if (!targetSessionId) {
      return;
    }
    setFollowUpLoading(true);
    try {
      const detail = await api.getAIHistoryDetail(targetSessionId);
      const restoredMessages = restoreFollowUpMessagesFromSession(detail?.messages);
      if (restoredMessages.length > 0) {
        setFollowUpMessages(restoredMessages);
      }
      const restoredPills = Array.isArray(detail?.context_pills) ? detail.context_pills : [];
      if (restoredPills.length > 0) {
        setContextPills(restoredPills);
      }
      const maybeConversationId = String((detail?.context as any)?.conversation_id || '').trim();
      if (maybeConversationId) {
        setConversationId((prev) => prev || maybeConversationId);
      }
      const lastAssistant = [...restoredMessages].reverse().find((msg) => msg.role === 'assistant');
      const metadata = lastAssistant?.metadata || {};
      if (lastAssistant) {
        setTokenHint({
          warning: Boolean((metadata as any)?.token_warning),
          historyCompacted: Boolean((metadata as any)?.history_compacted),
        });
      }
    } catch (err) {
      console.warn(`Failed to hydrate follow-up messages from session ${targetSessionId}:`, err);
    } finally {
      setFollowUpLoading(false);
    }
  };

  const applyHistorySessionToAnalysis = (historySession: NonNullable<LocationState['historySession']>) => {
    const traceId = String(historySession.trace_id || historySession.context?.trace_id || '').trim();
    const normalizedType = String(historySession.analysis_type || '').toLowerCase();
    const useTraceMode = normalizedType === 'trace' || (!!traceId && normalizedType !== 'log');
    const recoveredResult = (historySession.result || {}) as AIAnalysisResponse;

    setAnalysisType(useTraceMode ? 'trace' : 'log');
    setSourceLogData(null);
    setServiceErrorSnapshot(null);
    setServiceErrorSnapshotError(null);
    setInputText(String(historySession.input_text || traceId || ''));
    setServiceName(String(historySession.service_name || ''));
    setResult(recoveredResult);
    setLLMInfo({
      method: recoveredResult.analysis_method || historySession.analysis_method || 'history',
      model: recoveredResult.model || historySession.llm_model,
      cached: recoveredResult.cached,
      latency_ms: recoveredResult.latency_ms,
    });
    setUseLLM(true);
    setAnalysisSessionId(String(historySession.session_id || ''));
    const normalizedHistoryMessages = restoreFollowUpMessagesFromSession(historySession.messages);
    setFollowUpMessages(normalizedHistoryMessages);
    setConversationId('');
    setFollowUpError(null);
    setSimilarCases([]);
    setAnalysisAssistNotice('');
    const recoveredPills = Array.isArray(historySession.context_pills) ? historySession.context_pills : [];
    if (recoveredPills.length > 0) {
      setContextPills(recoveredPills);
    } else {
      const summary = String(recoveredResult.overview?.description || historySession.summary || '').trim();
      const fallbackPills = [
        { key: 'analysis_type', value: useTraceMode ? 'trace' : 'log' },
        { key: 'service', value: String(historySession.service_name || '').trim() },
        { key: 'trace_id', value: traceId },
        { key: 'session_id', value: String(historySession.session_id || '') },
        { key: 'summary', value: summary.slice(0, 100) },
      ].filter((item) => item.value);
      setContextPills(fallbackPills);
    }
    const lastAssistant = [...normalizedHistoryMessages].reverse().find((msg) => msg.role === 'assistant');
    const metadata = lastAssistant?.metadata || {};
    setTokenHint({
      warning: Boolean(metadata.token_warning),
      historyCompacted: Boolean(metadata.history_compacted),
    });
    setActionDrafts({});
    setFinalResolution(String(recoveredResult.overview?.description || historySession.summary || ''));
    setManualRemediationText('');
    setVerificationResult('pass');
    setVerificationNotes('');
    setManualCaseId('');
    setKbActionNotice('');
    setError(null);
    setIsLoading(false);
  };

  const applyHistoryCaseToAnalysis = (historyCase: NonNullable<LocationState['historyCase']>) => {
    const traceIdFromContext = String(historyCase?.context?.trace_id || '').trim();
    const source = String(historyCase?.source || '').toLowerCase();
    const useTraceMode = source.includes('trace') || (!!traceIdFromContext && !historyCase.log_content);

    const recoveredResult: AIAnalysisResponse = historyCase.analysis_result || {
      overview: {
        problem: historyCase.problem_type || 'unknown',
        severity: historyCase.severity || 'unknown',
        description: historyCase.summary || '',
        confidence: Number(historyCase?.llm_metadata?.confidence || 0),
      },
      rootCauses: Array.isArray(historyCase.root_causes)
        ? historyCase.root_causes.map((item) => ({ title: String(item), description: '' }))
        : [],
      solutions: Array.isArray(historyCase.solutions)
        ? historyCase.solutions.map((item) => ({
            title: String(item?.title || item?.description || '建议项'),
            description: String(item?.description || ''),
            steps: Array.isArray(item?.steps) ? item.steps.map((s) => String(s)) : [],
          }))
        : [],
      similarCases: Array.isArray(historyCase?.llm_metadata?.similar_cases)
        ? historyCase.llm_metadata?.similar_cases
        : [],
      analysis_method: String(historyCase?.llm_metadata?.analysis_method || 'history'),
      model: historyCase.llm_model || String(historyCase?.llm_metadata?.model || ''),
      cached: Boolean(historyCase?.llm_metadata?.cached),
      latency_ms: Number(historyCase?.llm_metadata?.latency_ms || 0),
    };

    setAnalysisType(useTraceMode ? 'trace' : 'log');
    setSourceLogData(null);
    setServiceErrorSnapshot(null);
    setServiceErrorSnapshotLoadedAt('');
    setServiceErrorSnapshotError(null);
    setInputText(historyCase.log_content || traceIdFromContext || '');
    setServiceName(historyCase.service_name || '');
    setResult(recoveredResult);
    setLLMInfo({
      method: recoveredResult.analysis_method || String(historyCase?.llm_metadata?.analysis_method || 'history'),
      model: recoveredResult.model || historyCase.llm_model,
      cached: recoveredResult.cached,
      latency_ms: recoveredResult.latency_ms,
    });
    setUseLLM(true);
    const restoredAnalysisSessionId = String(
      historyCase?.context?.analysis_session_id
      || historyCase?.llm_metadata?.analysis_session_id
      || '',
    );
    const restoredConversationId = String(historyCase?.llm_metadata?.conversation_id || '');
    const restoredFollowUps = restoreFollowUpMessagesFromCase(historyCase);
    const restoredLastAssistant = [...restoredFollowUps].reverse().find((msg) => msg.role === 'assistant');
    const restoredMetadata = restoredLastAssistant?.metadata || {};

    setAnalysisSessionId(restoredAnalysisSessionId);
    setConversationId(restoredConversationId);
    setFollowUpMessages(restoredFollowUps);
    setFollowUpQuestion('');
    setFollowUpError(null);
    setSimilarCases([]);
    setSelectedSimilarCase(null);
    setSelectedSimilarCaseDetail(null);
    setAnalysisAssistNotice('');
    setContextPills([
      { key: 'analysis_type', value: useTraceMode ? 'trace' : 'log' },
      { key: 'service', value: String(historyCase.service_name || '').trim() },
      { key: 'trace_id', value: traceIdFromContext },
      { key: 'summary', value: String(historyCase.summary || '').slice(0, 100) },
    ].filter((item) => item.value));
    setTokenHint({
      warning: Boolean((restoredMetadata as any)?.token_warning),
      historyCompacted: Boolean((restoredMetadata as any)?.history_compacted),
    });
    const remediationSteps = Array.isArray(historyCase?.manual_remediation_steps)
      ? historyCase.manual_remediation_steps
      : Array.isArray(historyCase?.llm_metadata?.manual_remediation_steps)
        ? historyCase.llm_metadata.manual_remediation_steps
        : [];
    setManualRemediationText(remediationSteps.map((item, idx) => `${idx + 1}. ${String(item)}`).join('\n'));
    setVerificationResult(
      (historyCase?.verification_result === 'fail' || historyCase?.llm_metadata?.verification_result === 'fail')
        ? 'fail'
        : 'pass'
    );
    setVerificationNotes(
      String(historyCase?.verification_notes || historyCase?.llm_metadata?.verification_notes || '')
    );
    setFinalResolution(String(historyCase?.resolution || historyCase?.summary || ''));
    setManualCaseId(String(historyCase?.id || ''));
    setKbActionNotice('');
    setActionDrafts({});
    setError(null);
    setIsLoading(false);

    if (restoredAnalysisSessionId) {
      void hydrateFollowUpMessagesFromSession(restoredAnalysisSessionId);
    }
  };

  const handleOpenHistorySession = async (sessionId: string) => {
    try {
      setIsLoading(true);
      setError(null);
      const detail = await api.getAIHistoryDetail(sessionId);
      applyHistorySessionToAnalysis(detail as NonNullable<LocationState['historySession']>);
    } catch (err: any) {
      setError(err?.message || '加载历史记录详情失败');
    } finally {
      setIsLoading(false);
    }
  };

  const syncLLMInfo = (response: AIAnalysisResponse, fallbackMethod: 'llm' | 'rule-based' | 'none') => {
    setLLMInfo({
      method: response.analysis_method || fallbackMethod,
      model: response.model,
      cached: response.cached,
      latency_ms: response.latency_ms,
    });
  };

  const runLogAnalysis = async (params: {
    logContent: string;
    service?: string;
    context?: Record<string, any>;
    useLLM: boolean;
  }): Promise<AIAnalysisResponse> => {
    const { logContent, service, context, useLLM } = params;
    const response = await api.analyzeLogLLM({
      log_content: logContent,
      service_name: service || '',
      context: context || {},
      use_llm: useLLM,
    });
    syncLLMInfo(response, useLLM ? 'llm' : 'rule-based');
    setAnalysisSessionId(String(response.session_id || ''));
    return response;
  };

  const runTraceAnalysis = async (params: {
    traceId: string;
    service?: string;
    useLLM: boolean;
  }): Promise<AIAnalysisResponse> => {
    const { traceId, service, useLLM } = params;
    if (useLLM) {
      const response = await api.analyzeTraceLLM({
        trace_id: traceId,
        service_name: service || '',
      });
      syncLLMInfo(response, 'llm');
      setAnalysisSessionId(String(response.session_id || ''));
      return response;
    }

    const response = await api.analyzeTrace({
      trace_id: traceId,
      service_name: service || undefined,
    });
    syncLLMInfo(response, 'rule-based');
    setAnalysisSessionId(String(response.session_id || ''));
    return response;
  };

  const extractTraceId = (value: string): string => {
    const text = (value || '').trim();
    if (!text) return '';

    const inlineMatch = text.match(/trace\s*id\s*[:=]\s*([a-zA-Z0-9_-]+)/i);
    if (inlineMatch?.[1]) {
      return inlineMatch[1].trim();
    }

    if (text.startsWith('{')) {
      try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed.trace_id === 'string') {
          return parsed.trace_id.trim();
        }
      } catch {
        // ignore JSON parse errors and fallback to raw text detection
      }
    }

    if (/^[a-zA-Z0-9_-]{8,}$/.test(text)) {
      return text;
    }

    return '';
  };

  const isTopologySyntheticPayload = (logData?: LocationState['logData'] | null): boolean => {
    const source = String(logData?.attributes?.source || '').toLowerCase();
    return TOPOLOGY_AI_SOURCES.has(source);
  };

  const formatEventForAnalysis = (event: any): string => {
    const rawMessage = String(event?.message || '').replace(/\s+/g, ' ').trim();
    if (!rawMessage) {
      return '';
    }
    const timestamp = event?.timestamp ? new Date(event.timestamp).toISOString() : new Date().toISOString();
    const level = String(event?.level || 'INFO').toUpperCase();
    const service = String(event?.service_name || 'unknown');
    return `[${timestamp}] [${level}] [${service}] ${rawMessage}`;
  };

  const normalizeMessageFingerprint = (message: string): string => {
    return String(message || '')
      .replace(/\b\d+\b/g, '{n}')
      .replace(/\b[0-9a-f]{8,}\b/gi, '{hex}')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 120);
  };

  const buildServiceErrorSnapshot = async (
    service: string,
  ): Promise<ServiceErrorSnapshot> => {
    const targetService = String(service || '').trim();
    if (!targetService) {
      throw new Error('请先输入服务名称，再加载该服务报错信息');
    }

    const primaryResp = await api.getEvents({
      service_name: targetService,
      level: 'ERROR',
      limit: 40,
      exclude_health_check: true,
    });
    let events = primaryResp.events || [];
    let sampledFromLevel: 'ERROR' | 'WARN' | 'ALL' = 'ERROR';

    if (!events.length) {
      const warnResp = await api.getEvents({
        service_name: targetService,
        level: 'WARN',
        limit: 40,
        exclude_health_check: true,
      });
      events = warnResp.events || [];
      sampledFromLevel = 'WARN';
    }

    if (!events.length) {
      const allResp = await api.getEvents({
        service_name: targetService,
        limit: 30,
        exclude_health_check: true,
      });
      events = allResp.events || [];
      sampledFromLevel = 'ALL';
    }

    const selected = events.slice(0, 20);
    if (!selected.length) {
      return {
        serviceName: targetService,
        generatedInput: `[service-error-summary]\nservice=${targetService}\n未获取到日志，请确认服务名和时间窗口`,
        summaryLines: ['未获取到日志，请确认服务名和时间窗口'],
        rawLogs: [],
        context: {
          source: 'service-error-snapshot',
          sampled_level: sampledFromLevel,
          sampled_count: 0,
        },
      };
    }

    const levelCounter = selected.reduce<Record<string, number>>((acc, item: any) => {
      const level = String(item?.level || 'UNKNOWN').toUpperCase();
      acc[level] = (acc[level] || 0) + 1;
      return acc;
    }, {});

    const topPatterns = selected
      .reduce<Map<string, number>>((acc, item: any) => {
        const key = normalizeMessageFingerprint(String(item?.message || ''));
        if (!key) {
          return acc;
        }
        acc.set(key, (acc.get(key) || 0) + 1);
        return acc;
      }, new Map())
      .entries();
    const topPatternLines = Array.from(topPatterns)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3)
      .map(([pattern, count]) => `${count}x ${pattern}`);

    const summaryLines = [
      `采样级别: ${sampledFromLevel}`,
      `采样条数: ${selected.length}`,
      `级别分布: ${Object.entries(levelCounter).map(([k, v]) => `${k}:${v}`).join(', ')}`,
      ...topPatternLines.map((line) => `高频模式: ${line}`),
    ];

    const rawLogs = selected.map((item: any) => ({
      id: String(item.id || ''),
      timestamp: String(item.timestamp || ''),
      level: String(item.level || 'INFO').toUpperCase(),
      message: String(item.message || ''),
    }));
    const rawLines = rawLogs
      .slice(0, 12)
      .map((item) => formatEventForAnalysis({
        timestamp: item.timestamp,
        level: item.level,
        service_name: targetService,
        message: item.message,
      }))
      .filter(Boolean);

    const generatedInput = [
      '[service-error-summary]',
      `service=${targetService}`,
      ...summaryLines,
      '',
      '[raw-service-logs]',
      ...rawLines,
    ].join('\n');

    return {
      serviceName: targetService,
      generatedInput,
      summaryLines,
      rawLogs,
      context: {
        source: 'service-error-snapshot',
        sampled_level: sampledFromLevel,
        sampled_count: rawLogs.length,
        sampled_logs: rawLogs.slice(0, 12),
      },
    };
  };

  const extractTimestampFromInput = (text: string): string => {
    const value = String(text || '').trim();
    if (!value) {
      return '';
    }
    const isoMatch = value.match(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?/);
    if (isoMatch?.[0]) {
      return new Date(isoMatch[0]).toISOString();
    }
    const basicMatch = value.match(/\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}/);
    if (basicMatch?.[0]) {
      const normalized = basicMatch[0].replace(' ', 'T') + 'Z';
      return new Date(normalized).toISOString();
    }
    return '';
  };

  const buildRelatedLogAssistInput = async (params: {
    baseInput: string;
    service: string;
    traceId: string;
  }): Promise<{ logContent: string; context: Record<string, any>; notice: string }> => {
    const baseInput = String(params.baseInput || '').trim();
    const targetService = String(params.service || '').trim();
    const traceId = String(params.traceId || '').trim();
    const eventParams: Record<string, any> = {
      limit: 24,
      exclude_health_check: true,
    };
    if (traceId) {
      eventParams.trace_id = traceId;
    } else if (targetService) {
      eventParams.service_name = targetService;
      const ts = extractTimestampFromInput(baseInput);
      if (ts) {
        const center = new Date(ts).getTime();
        if (!Number.isNaN(center)) {
          eventParams.start_time = new Date(center - 5 * 60 * 1000).toISOString();
          eventParams.end_time = new Date(center + 5 * 60 * 1000).toISOString();
        }
      }
    } else {
      return {
        logContent: baseInput,
        context: {},
        notice: '未检测到 trace_id/服务名，未注入关联日志。',
      };
    }

    let events: any[] = [];
    try {
      const errorResp = await api.getEvents({ ...eventParams, level: 'ERROR' });
      events = errorResp.events || [];
      if (!events.length) {
        const warnResp = await api.getEvents({ ...eventParams, level: 'WARN' });
        events = warnResp.events || [];
      }
      if (!events.length) {
        const allResp = await api.getEvents(eventParams);
        events = allResp.events || [];
      }
    } catch (err) {
      console.warn('Failed to load related logs for AI assist:', err);
      return {
        logContent: baseInput,
        context: {},
        notice: '关联日志加载失败，已按原始输入继续分析。',
      };
    }

    const selected = events.slice(0, 12);
    if (!selected.length) {
      return {
        logContent: baseInput,
        context: {
          related_log_count: 0,
          related_log_mode: 'auto-assist',
          related_log_notice: '未检索到可用关联日志',
        },
        notice: '未检索到关联日志，已按原始输入继续分析。',
      };
    }

    const levelCounter = selected.reduce<Record<string, number>>((acc, item: any) => {
      const level = String(item?.level || 'UNKNOWN').toUpperCase();
      acc[level] = (acc[level] || 0) + 1;
      return acc;
    }, {});
    const topPatterns = selected
      .reduce<Map<string, number>>((acc, item: any) => {
        const key = normalizeMessageFingerprint(String(item?.message || ''));
        if (!key) return acc;
        acc.set(key, (acc.get(key) || 0) + 1);
        return acc;
      }, new Map())
      .entries();
    const patternSummary = Array.from(topPatterns)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3)
      .map(([pattern, count]) => `${count}x ${pattern}`);
    const rawLines = selected
      .map((event) => formatEventForAnalysis(event))
      .filter(Boolean)
      .slice(0, 8);
    const summaryLines = [
      `关联日志条数: ${selected.length}`,
      `级别分布: ${Object.entries(levelCounter).map(([k, v]) => `${k}:${v}`).join(', ')}`,
      ...patternSummary.map((line) => `高频模式: ${line}`),
    ];
    const assistedInput = [
      baseInput,
      '',
      '[related-log-assist-summary]',
      ...summaryLines,
      '',
      '[related-log-assist-raw]',
      ...rawLines,
    ]
      .filter(Boolean)
      .join('\n');
    const notice = traceId
      ? `已自动注入 trace_id=${traceId} 的关联日志（${selected.length} 条）`
      : `已自动注入服务 ${targetService || 'unknown'} 的相邻时间日志（${selected.length} 条）`;

    return {
      logContent: assistedInput,
      context: {
        related_log_mode: 'auto-assist',
        related_log_count: selected.length,
        related_log_level_distribution: levelCounter,
        related_log_patterns: patternSummary,
        related_log_trace_id: traceId || undefined,
        related_logs: selected.map((event: any) => ({
          id: event.id,
          timestamp: event.timestamp,
          level: event.level,
          service_name: event.service_name,
          message: event.message,
        })),
      },
      notice,
    };
  };

  const buildLogAnalysisInput = async (
    logData: NonNullable<LocationState['logData']>,
  ): Promise<{ logContent: string; context: Record<string, any> }> => {
    const baseMessage = String(logData.message || '').trim();
    const baseContext: Record<string, any> = { ...(logData.attributes || {}) };

    if (!isTopologySyntheticPayload(logData)) {
      return { logContent: baseMessage, context: baseContext };
    }

    const queryBase: any = {
      limit: 30,
      service_name: logData.service_name || undefined,
      trace_id: logData.trace_id || undefined,
      exclude_health_check: true,
    };
    const sourceService = String(baseContext.source_service || logData.service_name || '').trim();
    const targetService = String(baseContext.target_service || '').trim();
    if (baseContext.source_service) queryBase.source_service = baseContext.source_service;
    if (baseContext.target_service) queryBase.target_service = baseContext.target_service;
    if (baseContext.time_window) queryBase.time_window = baseContext.time_window;

    let events: any[] = [];
    try {
      if (sourceService && targetService) {
        const previewResp = await api.getTopologyEdgeLogPreview({
          source_service: sourceService,
          target_service: targetService,
          time_window: String(baseContext.time_window || '1 HOUR'),
          limit: 8,
          exclude_health_check: true,
        });
        events = previewResp.data || [];
      }
    } catch (fetchErr) {
      console.warn('Failed to load topology edge preview logs for AI analysis:', fetchErr);
    }

    try {
      if (!events.length) {
        const errorResp = await api.getEvents({ ...queryBase, level: 'ERROR' });
        events = errorResp.events || [];
      }
      if (!events.length) {
        const warnResp = await api.getEvents({ ...queryBase, level: 'WARN' });
        events = warnResp.events || [];
      }
      if (!events.length) {
        const recentResp = await api.getEvents({ ...queryBase, limit: 12 });
        events = (recentResp.events || []).slice(0, 6);
      }
    } catch (fetchErr) {
      console.warn('Failed to load supplemental logs for AI analysis:', fetchErr);
    }

    const selectedEvents = events.slice(0, 6);
    const supplementalLines = selectedEvents
      .map((item) => formatEventForAnalysis(item))
      .filter(Boolean);

    if (!supplementalLines.length) {
      const fallbackContent = [
        baseMessage ? `[topology-summary] ${baseMessage}` : '',
        '',
        '[related-error-logs]',
        '未检索到 ERROR/WARN 日志，请结合实时日志进一步排查。',
      ]
        .filter(Boolean)
        .join('\n');
      return {
        logContent: fallbackContent || baseMessage,
        context: {
          ...baseContext,
          supplemental_log_count: 0,
          supplemental_logs: [],
          supplemental_log_notice: '未检索到 ERROR/WARN 日志，当前分析基于拓扑摘要。',
        },
      };
    }

    const hasErrorLogs = selectedEvents.some((item) => {
      const level = String(item?.level || '').toUpperCase();
      return ['ERROR', 'FATAL', 'WARN', 'WARNING'].includes(level);
    });
    const sectionTitle = hasErrorLogs ? '[related-error-logs]' : '[related-service-logs]';
    const extraNotice = hasErrorLogs ? '' : '未检索到 ERROR/WARN 级别日志，以下为最近相关服务日志。';

    const mergedContent = [
      baseMessage ? `[topology-summary] ${baseMessage}` : '',
      '',
      sectionTitle,
      extraNotice,
      ...supplementalLines,
    ]
      .filter(Boolean)
      .join('\n');

    return {
      logContent: mergedContent,
      context: {
        ...baseContext,
        supplemental_log_count: supplementalLines.length,
        supplemental_log_notice: extraNotice || undefined,
        supplemental_logs: selectedEvents.map((item: any) => ({
          id: item.id,
          timestamp: item.timestamp,
          level: item.level,
          service_name: item.service_name,
          message: item.message,
        })),
      },
    };
  };

  // 处理从其他页面跳转过来的数据
  useEffect(() => {
    const state = location.state as LocationState | undefined;
    setServiceErrorSnapshot(null);
    setServiceErrorSnapshotError(null);
    if (state) {
      const shouldAutoAnalyze = state.autoAnalyze === true;
      if (state.historySession) {
        applyHistorySessionToAnalysis(state.historySession);
      } else if (state.historyCase) {
        applyHistoryCaseToAnalysis(state.historyCase);
      } else if (state.logData) {
        setSourceLogData(state.logData);
        setInputText(state.logData.message);
        setServiceName(state.logData.service_name);
        setAnalysisType('log');
        setError(null);
        setResult(null);
        setLLMInfo(null);
        setSimilarCases([]);
        resetFollowUpConversation();
        setAnalysisSessionId('');
        setContextPills([]);
        if (shouldAutoAnalyze) {
          // 自动开始分析
          handleAnalyzeWithData(state.logData);
        } else {
          setAnalysisAssistNotice('已载入日志内容，请点击“开始分析”。');
        }
      } else if (state.traceId) {
        setAnalysisType('trace');
        setInputText(state.traceId);
        if (state.serviceName) {
          setServiceName(state.serviceName);
        }
        setError(null);
        setResult(null);
        setLLMInfo(null);
        setSimilarCases([]);
        resetFollowUpConversation();
        setAnalysisSessionId('');
        setContextPills([]);
        if (state.mode === 'trace' && shouldAutoAnalyze) {
          void (async () => {
            setIsLoading(true);
            setError(null);
            setAnalysisAssistNotice('');
            setResult(null);
            setLLMInfo(null);
            setSimilarCases([]);
            resetFollowUpConversation();
            setAnalysisSessionId('');
            try {
              const response = await runTraceAnalysis({
                traceId: state.traceId as string,
                service: state.serviceName || undefined,
                useLLM,
              });
              setResult(response);
              buildDefaultContextPills({
                result: response,
                sessionId: String(response.session_id || ''),
                service: state.serviceName || '',
                traceId: state.traceId as string,
                input: state.traceId as string,
                type: 'trace',
              });
            } catch (err: any) {
              setError(err?.message || '分析失败，请稍后重试');
            } finally {
              setIsLoading(false);
            }
          })();
        } else {
          setAnalysisAssistNotice('已载入 Trace ID，请点击“开始分析”。');
        }
      } else if (state.message) {
        setInputText(state.message);
        if (state.serviceName) {
          setServiceName(state.serviceName);
        }
        setError(null);
        setResult(null);
        setLLMInfo(null);
        setSimilarCases([]);
        resetFollowUpConversation();
        setAnalysisSessionId('');
        setContextPills([]);
        setAnalysisAssistNotice('已载入待分析内容，请点击“开始分析”。');
      }
    }
  }, [location.state]);

  const handleAnalyzeWithData = async (logData: NonNullable<LocationState['logData']>) => {
    setIsLoading(true);
    setError(null);
    setAnalysisAssistNotice('');
    setResult(null);
    setLLMInfo(null);
    setSimilarCases([]);
    setSelectedSimilarCase(null);
    setSelectedSimilarCaseDetail(null);
    resetFollowUpConversation();
    setAnalysisSessionId('');

    try {
      const prepared = await buildLogAnalysisInput(logData);
      let finalLogContent = prepared.logContent || logData.message;
      const mergedContext: Record<string, any> = { ...(prepared.context || {}) };
      const canAssist = relatedLogAssistMode !== 'off' && !isTopologySyntheticPayload(logData);
      if (canAssist) {
        const traceId = String(logData.trace_id || prepared.context?.trace_id || '').trim();
        const service = String(logData.service_name || '').trim();
        let shouldAssist = Boolean((traceId || service) && relatedLogAssistMode === 'auto');
        if ((traceId || service) && relatedLogAssistMode === 'ask') {
          shouldAssist = window.confirm('检测到 trace_id/服务名称，是否自动补充关联日志辅助诊断？');
        }
        if (shouldAssist) {
          const assisted = await buildRelatedLogAssistInput({
            baseInput: finalLogContent,
            service,
            traceId,
          });
          finalLogContent = assisted.logContent || finalLogContent;
          Object.assign(mergedContext, assisted.context || {});
          if (assisted.notice) {
            setAnalysisAssistNotice(assisted.notice);
          }
        }
      }
      if (finalLogContent) {
        setInputText(finalLogContent);
      }

      const response = await runLogAnalysis({
        logContent: finalLogContent,
        service: logData.service_name,
        context: mergedContext,
        useLLM,
      });
      setResult(response);
      buildDefaultContextPills({
        result: response,
        sessionId: String(response.session_id || ''),
        service: logData.service_name,
        traceId: String(mergedContext?.trace_id || logData.trace_id || ''),
        input: finalLogContent,
        type: 'log',
      });
      
      // 分析完成后查找相似案例
      handleFindSimilarCases(
        finalLogContent,
        response.overview?.problem,
        logData.service_name,
        mergedContext,
      );
    } catch (err: any) {
      setError(err.message || '分析失败，请稍后重试');
    } finally {
      setIsLoading(false);
    }
  };

  const handleLoadServiceErrorSnapshot = async () => {
    setServiceErrorSnapshotError(null);
    setLoadingServiceErrorSnapshot(true);
    try {
      const snapshot = await buildServiceErrorSnapshot(serviceName);
      setServiceErrorSnapshot(snapshot);
      setServiceErrorSnapshotLoadedAt(new Date().toISOString());
    } catch (err: any) {
      setServiceErrorSnapshot(null);
      setServiceErrorSnapshotLoadedAt('');
      setServiceErrorSnapshotError(err?.message || '加载服务报错信息失败');
    } finally {
      setLoadingServiceErrorSnapshot(false);
    }
  };

  const handleAnalyzeServiceErrorSnapshot = async () => {
    if (!serviceErrorSnapshot) {
      setError('请先加载服务报错信息');
      return;
    }

    setAnalysisType('log');
    setInputText(serviceErrorSnapshot.generatedInput);
    setIsLoading(true);
    setError(null);
    setAnalysisAssistNotice('');
    setResult(null);
    setLLMInfo(null);
    setSimilarCases([]);
    setSelectedSimilarCase(null);
    setSelectedSimilarCaseDetail(null);
    resetFollowUpConversation();
    setAnalysisSessionId('');

    try {
      const response = await runLogAnalysis({
        logContent: serviceErrorSnapshot.generatedInput,
        service: serviceErrorSnapshot.serviceName,
        context: serviceErrorSnapshot.context,
        useLLM,
      });
      setResult(response);
      buildDefaultContextPills({
        result: response,
        sessionId: String(response.session_id || ''),
        service: serviceErrorSnapshot.serviceName,
        traceId: String(serviceErrorSnapshot.context?.trace_id || ''),
        input: serviceErrorSnapshot.generatedInput,
        type: 'log',
      });
      handleFindSimilarCases(
        serviceErrorSnapshot.generatedInput,
        response.overview?.problem,
        serviceErrorSnapshot.serviceName,
        serviceErrorSnapshot.context,
      );
    } catch (err: any) {
      setError(err.message || '分析失败，请稍后重试');
    } finally {
      setIsLoading(false);
    }
  };

  const handleAnalyze = async () => {
    if (!inputText.trim()) {
      setError('请输入要分析的内容');
      return;
    }

    setIsLoading(true);
    setError(null);
    setAnalysisAssistNotice('');
    setResult(null);
    setLLMInfo(null);
    setSimilarCases([]);
    setSelectedSimilarCase(null);
    setSelectedSimilarCaseDetail(null);
    resetFollowUpConversation();
    setAnalysisSessionId('');

    try {
      let response: AIAnalysisResponse;
      if (analysisType === 'log') {
        const snapshotContext = serviceErrorSnapshot && inputText === serviceErrorSnapshot.generatedInput
          ? serviceErrorSnapshot.context
          : undefined;
        let preparedInput = inputText;
        const mergedContext: Record<string, any> = { ...(snapshotContext || {}) };
        if (!snapshotContext && relatedLogAssistMode !== 'off') {
          const traceId = extractTraceId(inputText);
          const canAssist = Boolean(traceId || serviceName.trim());
          let shouldAssist = canAssist && relatedLogAssistMode === 'auto';
          if (canAssist && relatedLogAssistMode === 'ask') {
            shouldAssist = window.confirm('检测到 trace_id/服务名称，是否自动补充关联日志辅助诊断？');
          }
          if (shouldAssist) {
            const assisted = await buildRelatedLogAssistInput({
              baseInput: inputText,
              service: serviceName,
              traceId,
            });
            preparedInput = assisted.logContent || inputText;
            if (preparedInput && preparedInput !== inputText) {
              setInputText(preparedInput);
            }
            Object.assign(mergedContext, assisted.context || {});
            if (assisted.notice) {
              setAnalysisAssistNotice(assisted.notice);
            }
          } else if (canAssist && relatedLogAssistMode === 'ask') {
            setAnalysisAssistNotice('已跳过关联日志注入，仅按输入内容分析。');
          }
        }
        response = await runLogAnalysis({
          logContent: preparedInput,
          service: serviceName,
          context: mergedContext,
          useLLM,
        });
        
        // 分析完成后查找相似案例
        handleFindSimilarCases(preparedInput, response.overview?.problem, serviceName, mergedContext);
        handleKBSearch(preparedInput, {
          problemType: response.overview?.problem,
          service: serviceName,
        });
      } else {
        const traceId = extractTraceId(inputText);
        if (!traceId) {
          setError('请输入有效的 Trace ID，或粘贴包含 trace_id 的 JSON');
          return;
        }

        response = await runTraceAnalysis({
          traceId,
          service: serviceName || undefined,
          useLLM,
        });
        handleKBSearch(traceId, {
          problemType: response.overview?.problem,
          service: serviceName,
        });
      }
      setResult(response);
      buildDefaultContextPills({
        result: response,
        sessionId: String(response.session_id || ''),
        service: serviceName,
        traceId: analysisType === 'trace' ? extractTraceId(inputText) : '',
        input: inputText,
        type: analysisType,
      });
    } catch (err: any) {
      setError(err.message || '分析失败，请稍后重试');
    } finally {
      setIsLoading(false);
    }
  };

  const handleFindSimilarCases = async (
    logContent: string,
    problemType?: string,
    serviceNameOverride?: string,
    context?: Record<string, any>,
  ) => {
    setLoadingSimilarCases(true);
    setSelectedSimilarCase(null);
    setSelectedSimilarCaseDetail(null);
    try {
      const response = await api.findSimilarCases({
        log_content: logContent,
        service_name: serviceNameOverride ?? serviceName,
        problem_type: problemType,
        context,
        limit: 5,
      });
      setSimilarCases(response.cases);
    } catch (err) {
      console.error('Failed to find similar cases:', err);
      setSimilarCases([]);
    } finally {
      setLoadingSimilarCases(false);
    }
  };

  const handleSelectSimilarCase = async (caseItem: SimilarCase) => {
    setSelectedSimilarCase(caseItem);
    setSelectedSimilarCaseDetail(null);
    setLoadingSimilarCaseDetail(true);
    try {
      const detail = await api.getCaseDetail(caseItem.id);
      setSelectedSimilarCaseDetail(detail as CaseDetail);
    } catch (err: any) {
      console.error('Failed to load similar case detail:', err);
      setError(err?.message || '加载相似知识库详情失败');
    } finally {
      setLoadingSimilarCaseDetail(false);
    }
  };

  const handleCloseSimilarCaseDetail = () => {
    setSelectedSimilarCase(null);
    setSelectedSimilarCaseDetail(null);
    setLoadingSimilarCaseDetail(false);
  };

  const handleReplaySimilarCase = () => {
    if (!selectedSimilarCaseDetail) {
      return;
    }
    applyHistoryCaseToAnalysis(selectedSimilarCaseDetail as NonNullable<LocationState['historyCase']>);
    handleCloseSimilarCaseDetail();
  };

  const handleSaveCase = async () => {
    if (!result || !inputText) return;

    try {
      const saveContext: Record<string, any> = {};
      if (analysisType === 'trace') {
        const traceId = extractTraceId(inputText);
        if (traceId) {
          saveContext.trace_id = traceId;
        }
      }
      if (serviceErrorSnapshot && inputText === serviceErrorSnapshot.generatedInput) {
        Object.assign(saveContext, serviceErrorSnapshot.context);
      } else if (sourceLogData?.attributes) {
        Object.assign(saveContext, sourceLogData.attributes);
      }
      if (analysisSessionId) {
        saveContext.analysis_session_id = analysisSessionId;
      }
      if (conversationId) {
        saveContext.conversation_id = conversationId;
      }
      const caseFollowUpMessages: CaseStoredMessage[] = followUpMessages
        .slice(-200)
        .map((msg) => ({
          role: msg.role,
          content: String(msg.content || ''),
          timestamp: msg.timestamp,
          message_id: msg.message_id,
          metadata: (msg.metadata && typeof msg.metadata === 'object')
            ? {
                references: Array.isArray(msg.metadata.references) ? msg.metadata.references : [],
                context_pills: Array.isArray(msg.metadata.context_pills) ? msg.metadata.context_pills : [],
                history_compacted: Boolean(msg.metadata.history_compacted),
                token_warning: Boolean(msg.metadata.token_warning),
              }
            : {},
        }))
        .filter((msg) => msg.content.trim());

      await api.saveCase({
        problem_type: result.overview?.problem || 'unknown',
        severity: result.overview?.severity || 'medium',
        summary: result.overview?.description || '',
        log_content: inputText,
        service_name: serviceName,
        root_causes: result.rootCauses?.map(r => r.title) || [],
        solutions: result.solutions || [],
        context: saveContext,
        llm_provider: llmInfo?.method === 'llm' ? 'runtime' : '',
        llm_model: llmInfo?.model || '',
        llm_metadata: {
          analysis_method: llmInfo?.method,
          latency_ms: llmInfo?.latency_ms,
          cached: llmInfo?.cached,
          analysis_session_id: analysisSessionId || '',
          conversation_id: conversationId || '',
          follow_up_messages: caseFollowUpMessages,
          follow_up_message_count: caseFollowUpMessages.length,
          follow_up_last_saved_at: new Date().toISOString(),
        },
        source: 'ai-analysis-page',
        tags: [],
        save_mode: kbEffectiveSaveMode,
        remote_enabled: kbRemoteEnabled,
      });
      alert('内容已保存到知识库');
      loadHistoryItems();
      if (analysisType === 'log') {
        handleFindSimilarCases(inputText, result.overview?.problem, serviceName, saveContext);
      }
    } catch (err) {
      console.error('Failed to save case:', err);
      alert('保存失败，请重试');
    }
  };

  const handleSubmitFollowUp = async (options?: { overrideQuestion?: string; retryLastUser?: boolean }) => {
    const question = String(options?.overrideQuestion ?? followUpQuestion).trim();
    if (!question) {
      return;
    }
    if (!result) {
      setFollowUpError('请先完成一次分析，再进行追问');
      return;
    }

    const tailMessage = followUpMessages.length > 0 ? followUpMessages[followUpMessages.length - 1] : null;
    const canReuseTailMessage = Boolean(
      options?.retryLastUser
      && tailMessage
      && tailMessage.role === 'user'
      && String(tailMessage.content || '').trim() === question,
    );
    const pendingMessages: FollowUpMessage[] = canReuseTailMessage
      ? [...followUpMessages]
      : [
          ...followUpMessages,
          {
            role: 'user',
            content: question,
            timestamp: new Date().toISOString(),
          },
        ];

    if (!canReuseTailMessage) {
      setFollowUpMessages(pendingMessages);
    }
    if (!options?.overrideQuestion) {
      setFollowUpQuestion('');
    }
    setFollowUpLoading(true);
    setFollowUpError(null);

    try {
      const detectedTraceId = extractTraceId(inputText);
      const followUpContext: Record<string, any> = {
        session_id: analysisSessionId || undefined,
        analysis_type: analysisType,
        service_name: serviceName,
        input_text: inputText,
        trace_id: detectedTraceId,
        llm_info: llmInfo || {},
        result,
      };
      const canAssistFollowUp = Boolean(
        relatedLogAssistMode !== 'off'
        && (detectedTraceId || serviceName.trim()),
      );
      let followUpAssistNotice = '';
      let shouldAssistFollowUp = canAssistFollowUp && relatedLogAssistMode === 'auto';
      if (canAssistFollowUp && relatedLogAssistMode === 'ask') {
        shouldAssistFollowUp = window.confirm('追问时检测到 trace_id/服务名，是否补充关联日志作为上下文？');
      }
      if (shouldAssistFollowUp) {
        const assisted = await buildRelatedLogAssistInput({
          baseInput: question,
          service: serviceName,
          traceId: detectedTraceId,
        });
        const relatedLogs = Array.isArray(assisted.context?.related_logs)
          ? assisted.context.related_logs.slice(0, 8)
          : [];
        if (relatedLogs.length > 0) {
          followUpContext.followup_related_logs = relatedLogs;
          followUpContext.followup_related_log_count = Number(assisted.context?.related_log_count || relatedLogs.length);
          followUpContext.followup_related_log_patterns = Array.isArray(assisted.context?.related_log_patterns)
            ? assisted.context.related_log_patterns
            : [];
          followUpContext.followup_related_log_trace_id = String(assisted.context?.related_log_trace_id || '');
          followUpAssistNotice = `追问已补充关联日志上下文（${relatedLogs.length} 条）`;
        }
      }

      const response = await api.followUpAnalysis({
        question,
        analysis_session_id: analysisSessionId || undefined,
        conversation_id: conversationId || undefined,
        use_llm: useLLM,
        analysis_context: followUpContext,
        history: pendingMessages,
        reset: false,
      });
      setAnalysisSessionId(String(response.analysis_session_id || analysisSessionId || ''));
      setConversationId(response.conversation_id || conversationId);
      if (Array.isArray(response.context_pills) && response.context_pills.length > 0) {
        setContextPills(response.context_pills);
      } else {
        buildDefaultContextPills({
          result,
          sessionId: String(response.analysis_session_id || analysisSessionId || ''),
          service: serviceName,
          traceId: analysisType === 'trace' ? extractTraceId(inputText) : '',
          input: inputText,
          type: analysisType,
        });
      }
      setTokenHint({
        warning: Boolean(response.token_warning),
        historyCompacted: Boolean(response.history_compacted),
      });
      let methodNotice = '';
      if (response.analysis_method === 'rule-based') {
        if (useLLM && !response.llm_enabled) {
          methodNotice = '追问已切换为规则模式：当前 AI 服务未检测到可用 LLM 凭据。';
        } else if (useLLM && response.llm_timeout_fallback) {
          methodNotice = '追问触发 LLM 超时，已自动降级为规则模式，可重试。';
        } else if (!response.llm_requested) {
          methodNotice = '追问当前使用规则模式：已关闭 LLM 开关。';
        }
      }
      const mergedNotice = [followUpAssistNotice, methodNotice].filter(Boolean).join('；');
      setAnalysisAssistNotice(mergedNotice);
      setLLMInfo((prev) => ({
        method: String(response.analysis_method || (response.llm_enabled ? 'llm' : 'rule-based')),
        model: prev?.model,
        cached: prev?.cached,
        latency_ms: prev?.latency_ms,
      }));
      if (Array.isArray(response.history) && response.history.length > 0) {
        setFollowUpMessages(response.history as FollowUpMessage[]);
      } else {
        setFollowUpMessages([
          ...pendingMessages,
          {
            role: 'assistant',
            content: String(response.answer || ''),
            timestamp: new Date().toISOString(),
            metadata: {
              references: Array.isArray(response.references) ? response.references : [],
            },
          },
        ]);
      }
    } catch (err: any) {
      setFollowUpError(parseFollowUpErrorMessage(err));
    } finally {
      setFollowUpLoading(false);
    }
  };

  const handleCopyFollowUpMessage = async (msg: FollowUpMessage) => {
    const content = String(msg.content || '').trim();
    if (!content) {
      setFollowUpError('消息为空，无法复制');
      return;
    }
    const copied = await copyTextToClipboard(content);
    if (copied) {
      showFollowUpNotice(msg.role === 'user' ? '已复制你的提问' : '已复制 AI 回答');
      return;
    }
    setFollowUpError('复制失败，请检查浏览器剪贴板权限');
  };

  const handleRetryFollowUpMessage = (msg: FollowUpMessage) => {
    if (followUpLoading) {
      return;
    }
    if (msg.role !== 'user') {
      return;
    }
    const content = String(msg.content || '').trim();
    if (!content) {
      setFollowUpError('消息为空，无法重试');
      return;
    }
    void handleSubmitFollowUp({ overrideQuestion: content, retryLastUser: true });
  };

  const handleDeleteFollowUpMessage = async (msg: FollowUpMessage, index: number) => {
    if (followUpLoading) {
      return;
    }
    const confirmDelete = window.confirm('确认删除这条对话消息？删除后不会参与会话草稿总结。');
    if (!confirmDelete) {
      return;
    }

    const messageKey = msg.message_id || `${msg.role}-${index}-${msg.timestamp || ''}`;
    const removeLocally = () => {
      const nextMessages = followUpMessages.filter((_, msgIndex) => msgIndex !== index);
      setFollowUpMessages(nextMessages);
      const lastAssistant = [...nextMessages].reverse().find((item) => item.role === 'assistant');
      const metadata = lastAssistant?.metadata || {};
      setTokenHint({
        warning: Boolean((metadata as any)?.token_warning),
        historyCompacted: Boolean((metadata as any)?.history_compacted),
      });
      if (msg.message_id) {
        setActionDrafts((prev) => {
          if (!prev[msg.message_id as string]) {
            return prev;
          }
          const next = { ...prev };
          delete next[msg.message_id as string];
          return next;
        });
      }
    };

    if (!analysisSessionId || !msg.message_id) {
      removeLocally();
      showFollowUpNotice('已删除本地消息');
      return;
    }

    setDeletingMessageKey(messageKey);
    setFollowUpError(null);
    try {
      await api.deleteFollowUpMessage(analysisSessionId, msg.message_id);
      removeLocally();
      showFollowUpNotice(msg.role === 'user' ? '已删除该提问' : '已删除该回答');
    } catch (err: any) {
      setFollowUpError(err?.message || '删除消息失败，请稍后重试');
    } finally {
      setDeletingMessageKey('');
    }
  };

  const handleClear = () => {
    setInputText('');
    setServiceName('');
    setResult(null);
    setError(null);
    setSourceLogData(null);
    setLLMInfo(null);
    setUseLLM(true);
    setSimilarCases([]);
    setSelectedSimilarCase(null);
    setSelectedSimilarCaseDetail(null);
    setServiceErrorSnapshot(null);
    setServiceErrorSnapshotLoadedAt('');
    setServiceErrorSnapshotError(null);
    setAnalysisSessionId('');
    resetFollowUpConversation();
    setContextPills([]);
    setTokenHint({});
    setActionDrafts({});
    setAnalysisAssistNotice('');
    setKbSearchResults([]);
    setKbSearchSources({ local: 0, external: 0 });
    setManualRemediationText('');
    setVerificationResult('pass');
    setVerificationNotes('');
    setFinalResolution('');
    setManualCaseId('');
    setKbActionNotice('');
    navigate(location.pathname, { replace: true, state: null });
  };

  const currentTraceId = extractTraceId(inputText);
  const followUpSuggestions = useMemo(() => {
    if (!result?.overview) {
      return [];
    }
    return [
      `请按优先级给出排查顺序（服务=${serviceName || 'unknown'}）`,
      `针对“${result.overview.problem || 'unknown'}”补充更细化的修复步骤`,
      `如果只能做一项变更，最推荐做什么？请说明风险`,
    ];
  }, [result?.overview, serviceName]);

  const handleFollowUpKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      if (!followUpLoading && followUpQuestion.trim()) {
        void handleSubmitFollowUp();
      }
    }
  };

  const buildDefaultContextPills = (
    payload: {
      result?: AIAnalysisResult | null;
      sessionId?: string;
      service?: string;
      traceId?: string;
      input?: string;
      type?: 'log' | 'trace';
    } = {},
  ) => {
    const overview = payload.result?.overview;
    const summary = String(overview?.description || '').trim();
    const pills = [
      { key: 'analysis_type', value: payload.type || analysisType },
      { key: 'service', value: String(payload.service ?? serviceName).trim() },
      { key: 'trace_id', value: String(payload.traceId ?? (analysisType === 'trace' ? currentTraceId : '')).trim() },
      { key: 'session_id', value: String(payload.sessionId || analysisSessionId).trim() },
      { key: 'summary', value: summary.slice(0, 100) },
      { key: 'input_preview', value: String(payload.input ?? inputText).replace(/\s+/g, ' ').slice(0, 80).trim() },
    ].filter((item) => item.value);
    setContextPills(pills);
  };

  const handleCreateActionDraft = async (
    messageId: string | undefined,
    actionType: 'ticket' | 'runbook' | 'alert_suppression',
  ) => {
    if (!analysisSessionId || !messageId) {
      setFollowUpError('缺少会话或消息 ID，无法生成动作草案');
      return;
    }
    const actionKey = `${messageId}:${actionType}`;
    setActionLoadingKey(actionKey);
    try {
      const response = await api.createFollowUpAction(analysisSessionId, messageId, {
        action_type: actionType,
      });
      if (response?.action) {
        setActionDrafts((prev) => ({
          ...prev,
          [messageId]: response.action,
        }));
      }
    } catch (err: any) {
      setFollowUpError(err?.message || '动作草案生成失败');
    } finally {
      setActionLoadingKey('');
    }
  };

  const trimmedServiceName = serviceName.trim();
  const normalizedServiceName = trimmedServiceName.toLowerCase();
  const hasSnapshotForCurrentService = Boolean(
    serviceErrorSnapshot
      && String(serviceErrorSnapshot.serviceName || '').trim().toLowerCase() === normalizedServiceName,
  );
  const serviceSnapshotButtonText = loadingServiceErrorSnapshot
    ? '加载中...'
    : !trimmedServiceName
      ? '请先输入服务名'
      : hasSnapshotForCurrentService
        ? '重新采样报错'
        : '采样服务报错';

  return (
    <div className="flex flex-col h-full">
      {/* 页面标题 */}
      <div className="mb-4">
        <h1 className="text-2xl font-bold text-gray-900">AI 智能分析</h1>
        <p className="text-gray-500 mt-1">使用 AI 分析日志和追踪数据，定位根因并获取解决方案</p>
      </div>

      {/* 主内容区 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 flex-1">
        {/* 输入区 */}
        <div className="bg-white rounded-lg shadow-md overflow-hidden flex flex-col">
          <div className="p-4 border-b border-gray-200">
            <div className="flex items-center space-x-4">
              <button
                onClick={() => setAnalysisType('log')}
                className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                  analysisType === 'log'
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                }`}
              >
                日志分析
              </button>
              <button
                onClick={() => setAnalysisType('trace')}
                className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                  analysisType === 'trace'
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                }`}
              >
                追踪分析
              </button>
            </div>
          </div>

          <div className="p-4 flex-1 flex flex-col">
            {/* 服务名输入 */}
            <div className="mb-4">
              <label className="block text-sm font-medium text-gray-700 mb-1">
                服务名称（可选）
              </label>
              <input
                type="text"
                value={serviceName}
                onChange={(e) => setServiceName(e.target.value)}
                placeholder="输入服务名称"
                className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>

            {analysisType === 'log' && (
              <div className="mb-4 rounded-lg border border-gray-200 bg-gray-50 p-3">
                <div className="flex items-center justify-between gap-2 mb-2">
                  <div>
                    <div className="text-sm font-medium text-gray-700">服务报错样本</div>
                    <div className="text-xs text-gray-500">可查看未汇总明细 + 自动生成摘要，并直接提交分析</div>
                    {hasSnapshotForCurrentService && serviceErrorSnapshotLoadedAt && (
                      <div className="text-[11px] text-slate-500 mt-1">
                        当前样本服务: {trimmedServiceName} | 采样时间: {new Date(serviceErrorSnapshotLoadedAt).toLocaleString('zh-CN', { hour12: false })} | 样本条数: {serviceErrorSnapshot?.rawLogs?.length || 0}
                      </div>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={handleLoadServiceErrorSnapshot}
                    disabled={loadingServiceErrorSnapshot || !trimmedServiceName}
                    className="px-3 py-1.5 text-xs font-medium rounded bg-slate-200 text-slate-700 hover:bg-slate-300 disabled:opacity-50"
                  >
                    {serviceSnapshotButtonText}
                  </button>
                </div>

                {serviceErrorSnapshotError && (
                  <div className="text-xs text-red-600 mb-2">{serviceErrorSnapshotError}</div>
                )}

                {serviceErrorSnapshot && (
                  <div className="space-y-2">
                    <div className="rounded border border-blue-100 bg-blue-50 p-2">
                      <div className="text-xs font-medium text-blue-700 mb-1">摘要信息</div>
                      <ul className="text-xs text-blue-700 space-y-1">
                        {serviceErrorSnapshot.summaryLines.map((line, idx) => (
                          <li key={`summary-${idx}`}>{line}</li>
                        ))}
                      </ul>
                    </div>
                    <div className="rounded border border-gray-200 bg-white p-2">
                      <div className="text-xs font-medium text-gray-700 mb-1">未汇总原始日志（前 5 条）</div>
                      <div className="space-y-1 max-h-32 overflow-auto">
                        {serviceErrorSnapshot.rawLogs.slice(0, 5).map((item) => (
                          <div key={item.id || `${item.timestamp}-${item.message}`} className="text-[11px] text-gray-700 font-mono">
                            [{item.level}] {item.timestamp ? `${new Date(item.timestamp).toLocaleString('zh-CN', { hour12: false })} ` : ''}{item.message}
                          </div>
                        ))}
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={() => setInputText(serviceErrorSnapshot.generatedInput)}
                        className="px-3 py-1.5 text-xs rounded bg-indigo-50 text-indigo-700 hover:bg-indigo-100"
                      >
                        填充到输入框
                      </button>
                      <button
                        type="button"
                        onClick={handleAnalyzeServiceErrorSnapshot}
                        disabled={isLoading}
                        className="px-3 py-1.5 text-xs rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50"
                      >
                        直接提交分析
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* LLM 开关 */}
            <div className="mb-4 flex items-center justify-between p-3 bg-gray-50 rounded-lg">
              <div className="flex items-center gap-2">
                <BrainCircuit className="w-5 h-5 text-purple-600" />
                <span className="text-sm font-medium text-gray-700">使用 LLM 大模型分析</span>
              </div>
              <button
                onClick={() => setUseLLM(!useLLM)}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                  useLLM ? 'bg-purple-600' : 'bg-gray-300'
                }`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                    useLLM ? 'translate-x-6' : 'translate-x-1'
                  }`}
                />
              </button>
            </div>
            {useLLM && (
              <p className="text-xs text-gray-500 mb-4 -mt-2">
                使用 OpenAI / Claude / DeepSeek 进行深度分析，需要配置对应 provider 的 API Key 环境变量
              </p>
            )}
            {analysisType === 'log' && (
              <div className="mb-4 rounded-lg border border-emerald-100 bg-emerald-50 p-3">
                <div className="text-xs font-medium text-emerald-700 mb-1">关联日志辅助诊断</div>
                <div className="flex items-center gap-2">
                  <select
                    value={relatedLogAssistMode}
                    onChange={(e) => setRelatedLogAssistMode(e.target.value as RelatedLogAssistMode)}
                    className="px-2 py-1 text-xs border border-emerald-200 rounded bg-white text-emerald-700"
                  >
                    <option value="auto">自动引入关联日志</option>
                    <option value="ask">分析前询问是否引入</option>
                    <option value="off">关闭关联日志辅助</option>
                  </select>
                  <span className="text-[11px] text-emerald-700">
                    基于 trace_id 或相邻时间窗口自动补充相关日志并归纳摘要
                  </span>
                </div>
              </div>
            )}

            {/* 内容输入 */}
            <div className="flex-1">
              <label className="block text-sm font-medium text-gray-700 mb-1">
                {analysisType === 'log' ? '日志内容' : '追踪数据 / Trace ID'}
              </label>
              <textarea
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                placeholder={
                  analysisType === 'log'
                    ? '粘贴要分析的日志内容...\n例如：\n2024-01-15 10:30:45 ERROR [service-a] Connection timeout to database'
                    : '输入 Trace ID（推荐）或粘贴追踪 JSON...\n例如：\n1d7f9c2a7b8e4f6a\n或\n{"trace_id": "1d7f9c2a7b8e4f6a", "spans": [...]}'
                }
                className="w-full h-64 px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono text-sm resize-none"
              />
            </div>

            {/* 错误提示 */}
            {error && (
              <div className="mt-4 p-3 bg-red-50 border border-red-200 rounded-lg flex items-center text-red-700">
                <AlertCircle className="w-5 h-5 mr-2 shrink-0" />
                <span className="text-sm">{error}</span>
              </div>
            )}
            {analysisAssistNotice && (
              <div className="mt-3 p-2 rounded border border-emerald-200 bg-emerald-50 text-emerald-700 text-xs">
                {analysisAssistNotice}
              </div>
            )}

            {/* 分析按钮 */}
            <div className="mt-4 flex gap-2">
              <button
                onClick={handleAnalyze}
                disabled={isLoading || !inputText.trim()}
                className="flex-1 flex items-center justify-center px-4 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:bg-gray-400 disabled:cursor-not-allowed"
              >
                {isLoading ? (
                  <>
                    <Loader2 className="w-5 h-5 mr-2 animate-spin" />
                    分析中...
                  </>
                ) : (
                  <>
                    <BrainCircuit className="w-5 h-5 mr-2" />
                    开始分析
                  </>
                )}
              </button>
              {(sourceLogData || result) && (
                <button
                  onClick={handleClear}
                  className="px-4 py-3 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors"
                >
                  清除
                </button>
              )}
            </div>
            
            {/* 来源信息 */}
            {sourceLogData && (
              <div className="mt-3 p-3 bg-gray-50 rounded-lg border border-gray-200">
                <div className="text-xs text-gray-500 mb-1">来自日志</div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-800">{sourceLogData.service_name}</span>
                  <span className="text-xs text-gray-400">•</span>
                  <span className="text-xs text-gray-500">{sourceLogData.level}</span>
                  <span className="text-xs text-gray-400">•</span>
                  <span className="text-xs text-gray-500 truncate max-w-[200px]">{sourceLogData.pod_name}</span>
                </div>
              </div>
            )}

            <div className="mt-4 rounded-lg border border-gray-200 bg-slate-50 p-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
                  <History className="w-4 h-4 text-indigo-600" />
                  AI 历史记录
                </div>
                <button
                  type="button"
                  onClick={() => navigate('/ai-cases?tab=history')}
                  className="text-xs text-indigo-600 hover:text-indigo-700"
                >
                  查看全部
                </button>
              </div>
              <div className="mt-1 text-[11px] text-slate-500">
                已加载 {historyItems.length}/{historyTotalAll || historyItems.length}
              </div>
              <div className="mt-2 space-y-2 max-h-36 overflow-auto">
                {loadingHistoryItems ? (
                  <div className="text-xs text-gray-500">加载中...</div>
                ) : historyItems.length > 0 ? (
                  historyItems.map((item) => (
                    <button
                      type="button"
                      key={item.session_id}
                      onClick={() => handleOpenHistorySession(item.session_id)}
                      className="w-full text-left rounded border border-slate-200 bg-white px-2 py-1.5 hover:bg-slate-100"
                    >
                      <div className="text-xs font-medium text-gray-800 truncate">
                        {item.is_pinned ? '[置顶] ' : ''}{item.title || item.summary || item.session_id}
                      </div>
                      <div className="text-[11px] text-gray-500">
                        {item.service_name || 'unknown'} · {item.analysis_type || 'log'} · {new Date(item.created_at).toLocaleString('zh-CN', { hour12: false })}
                      </div>
                    </button>
                  ))
                ) : (
                  <div className="text-xs text-gray-500">暂无历史记录</div>
                )}
              </div>
              {!loadingHistoryItems && historyHasMore && (
                <div className="mt-2">
                  <button
                    type="button"
                    onClick={handleLoadMoreHistoryItems}
                    disabled={loadingMoreHistoryItems}
                    className="px-2 py-1 text-[11px] rounded bg-indigo-50 text-indigo-700 hover:bg-indigo-100 disabled:opacity-50"
                  >
                    {loadingMoreHistoryItems ? '加载中...' : '加载更多'}
                  </button>
                </div>
              )}
              {historyHint && (
                <div className="mt-2 text-[11px] text-amber-600">{historyHint}</div>
              )}
            </div>
          </div>
        </div>

        {/* 结果区 */}
        <div className="bg-white rounded-lg shadow-md overflow-hidden flex flex-col">
          <div className="p-4 border-b border-gray-200 flex items-center justify-between">
            <h3 className="font-semibold text-gray-900">分析结果</h3>
            {llmInfo && (
              <div className="flex items-center gap-2 text-xs text-gray-500">
                {llmInfo.method === 'llm' ? (
                  <>
                    <BrainCircuit className="w-4 h-4 text-purple-500" />
                    <span className="text-purple-600 font-medium">LLM</span>
                    {llmInfo.model && <span className="text-gray-400">({llmInfo.model})</span>}
                    {llmInfo.cached && <span className="text-green-500">缓存</span>}
                    {llmInfo.latency_ms && <span>{llmInfo.latency_ms}ms</span>}
                  </>
                ) : llmInfo.method === 'none' ? (
                  <>
                    <AlertCircle className="w-4 h-4 text-gray-500" />
                    <span className="text-gray-600 font-medium">LLM 未启用</span>
                  </>
                ) : (
                  <>
                    <Zap className="w-4 h-4 text-amber-500" />
                    <span className="text-amber-600 font-medium">规则引擎</span>
                  </>
                )}
              </div>
            )}
          </div>

          <div className="p-4 flex-1 overflow-auto">
            {isLoading ? (
              <LoadingState message={useLLM ? "LLM 正在深度分析..." : "AI 正在分析..."} />
            ) : result ? (
              <div className="space-y-6">
                {contextPills.length > 0 && (
                  <div className="rounded-lg border border-indigo-100 bg-indigo-50 p-3">
                    <div className="text-xs font-medium text-indigo-700 mb-2">上下文挂载</div>
                    <div className="flex flex-wrap gap-2">
                      {contextPills.map((pill) => (
                        <span
                          key={`${pill.key}:${pill.value}`}
                          className="inline-flex items-center rounded-full border border-indigo-200 bg-white px-2 py-1 text-[11px] text-indigo-700"
                        >
                          {pill.key}: {pill.value}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                {/* 摘要 */}
                {result.overview && (
                  <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                    <h4 className="font-medium text-blue-800 mb-2">分析摘要</h4>
                    <p className="text-sm text-blue-700 mb-2">{result.overview.description || result.overview.problem}</p>
                    <div className="flex items-center gap-2 text-xs text-blue-600">
                      <span>置信度: {Math.round((result.overview.confidence || 0) * 100)}%</span>
                      <span>•</span>
                      <span>级别: {result.overview.severity}</span>
                    </div>
                  </div>
                )}

                {/* 根因分析 */}
                {result.rootCauses && result.rootCauses.length > 0 && (
                  <div>
                    <div className="flex items-center mb-3">
                      <Bug className="w-5 h-5 text-red-500 mr-2" />
                      <h4 className="font-medium text-gray-900">可能根因</h4>
                    </div>
                    <ul className="space-y-2">
                      {result.rootCauses.map((cause, index) => (
                        <li key={index} className="text-sm">
                          <div className="font-medium text-gray-800 mb-1">{cause.title}</div>
                          <div className="text-gray-600">{cause.description}</div>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* 解决建议 */}
                {result.solutions && result.solutions.length > 0 && (
                  <div>
                    <div className="flex items-center mb-3">
                      <Lightbulb className="w-5 h-5 text-yellow-500 mr-2" />
                      <h4 className="font-medium text-gray-900">解决建议</h4>
                    </div>
                    <ul className="space-y-3">
                      {result.solutions.map((solution, index) => (
                        <li key={index} className="text-sm">
                          <div className="font-medium text-gray-800 mb-1">{solution.title}</div>
                          <div className="text-gray-600 mb-2">{solution.description}</div>
                          {solution.steps && solution.steps.length > 0 && (
                            <ol className="ml-4 space-y-1">
                              {solution.steps.map((step, stepIdx) => (
                                <li key={stepIdx} className="text-gray-700 text-xs">{step}</li>
                              ))}
                            </ol>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <div className="rounded-lg border border-indigo-200 bg-indigo-50 p-4 space-y-3">
                  <div className="flex items-center justify-between gap-2">
                    <h4 className="font-medium text-indigo-900">知识库联动与提交</h4>
                    {kbRuntimeLoading ? (
                      <span className="text-xs text-indigo-600 inline-flex items-center gap-1">
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        策略校验中
                      </span>
                    ) : (
                      <span className="text-xs text-indigo-600">
                        生效: 检索 {kbEffectiveRetrievalMode} / 保存 {kbEffectiveSaveMode}
                      </span>
                    )}
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                    <label className="inline-flex items-center gap-2 text-sm text-indigo-800">
                      <input
                        type="checkbox"
                        checked={kbRemoteEnabled}
                        onChange={(e) => {
                          const checked = e.target.checked;
                          setKbRemoteEnabled(checked);
                          setKbRetrievalMode(checked ? 'hybrid' : 'local');
                          if (!checked) {
                            setKbSaveMode('local_only');
                          }
                        }}
                      />
                      启用远端知识库
                    </label>
                    <label className="text-xs text-indigo-700">
                      检索策略
                      <select
                        value={kbRetrievalMode}
                        onChange={(e) => setKbRetrievalMode(e.target.value as 'local' | 'hybrid')}
                        className="mt-1 w-full rounded border border-indigo-200 bg-white px-2 py-1"
                        disabled={!kbRemoteEnabled}
                      >
                        <option value="local">仅本地</option>
                        <option value="hybrid">本地 + 远端</option>
                      </select>
                    </label>
                    <label className="text-xs text-indigo-700">
                      保存策略
                      <select
                        value={kbSaveMode}
                        onChange={(e) => setKbSaveMode(e.target.value as 'local_only' | 'local_and_remote')}
                        className="mt-1 w-full rounded border border-indigo-200 bg-white px-2 py-1"
                      >
                        <option value="local_only">仅存本地</option>
                        <option value="local_and_remote" disabled={!kbRemoteEnabled || !kbRemoteAvailable}>本地 + 远端</option>
                      </select>
                    </label>
                  </div>
                  {kbRuntimeNotice && (
                    <div className="text-xs text-amber-700 rounded border border-amber-200 bg-amber-50 px-2 py-1">
                      {kbRuntimeNotice}
                    </div>
                  )}
                  {kbRemoteEnabled && !kbRemoteAvailable && (
                    <div className="text-xs text-amber-700 rounded border border-amber-200 bg-amber-50 px-2 py-1">
                      远端知识库不可用，当前仅支持存入本地。
                    </div>
                  )}
                  <div>
                    <div className="text-sm font-medium text-indigo-900 mb-1">人工修复步骤（至少 1 条）</div>
                    <textarea
                      value={manualRemediationText}
                      onChange={(e) => setManualRemediationText(e.target.value)}
                      placeholder={'1. 调整 timeout\n2. 配置重试\n3. 灰度验证'}
                      className="w-full h-24 px-3 py-2 border border-indigo-200 rounded bg-white text-sm"
                    />
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    <label className="text-xs text-indigo-700">
                      验证结果
                      <select
                        value={verificationResult}
                        onChange={(e) => setVerificationResult(e.target.value as 'pass' | 'fail')}
                        className="mt-1 w-full rounded border border-indigo-200 bg-white px-2 py-1"
                      >
                        <option value="pass">pass</option>
                        <option value="fail">fail</option>
                      </select>
                    </label>
                    <label className="text-xs text-indigo-700">
                      关联知识库ID（可选，不填则自动新建）
                      <input
                        value={manualCaseId}
                        onChange={(e) => setManualCaseId(e.target.value)}
                        className="mt-1 w-full rounded border border-indigo-200 bg-white px-2 py-1"
                        placeholder="case-xxxx"
                      />
                    </label>
                  </div>
                  <div>
                    <label className="text-xs text-indigo-700 block mb-1">最终解决方案</label>
                    <textarea
                      value={finalResolution}
                      onChange={(e) => setFinalResolution(e.target.value)}
                      className="w-full h-16 px-3 py-2 border border-indigo-200 rounded bg-white text-sm"
                      placeholder="填写最终修复方案（可由分析摘要预填）"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-indigo-700 block mb-1">验证说明（至少20字）</label>
                    <textarea
                      value={verificationNotes}
                      onChange={(e) => setVerificationNotes(e.target.value)}
                      className="w-full h-16 px-3 py-2 border border-indigo-200 rounded bg-white text-sm"
                      placeholder="填写验证过程与结果，至少20字"
                    />
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={handleBuildKBFromSession}
                      disabled={kbDraftLoading || kbSubmitLoading || !analysisSessionId}
                      className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-white border border-indigo-300 text-indigo-700 text-xs hover:bg-indigo-100 disabled:opacity-50"
                    >
                      {kbDraftLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Link2 className="w-3.5 h-3.5" />}
                      从会话提取草稿
                    </button>
                    <button
                      type="button"
                      onClick={handleSubmitManualRemediation}
                      disabled={kbDraftLoading || kbSubmitLoading}
                      className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-indigo-600 text-white text-xs hover:bg-indigo-700 disabled:opacity-50"
                    >
                      {kbSubmitLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Bookmark className="w-3.5 h-3.5" />}
                      提交知识库
                    </button>
                  </div>
                  {kbActionNotice && (
                    <div className="text-xs text-indigo-800 rounded border border-indigo-200 bg-white px-2 py-1">
                      {kbActionNotice}
                    </div>
                  )}
                </div>

                {(kbSearchLoading || kbSearchResults.length > 0) && (
                  <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                    <div className="flex items-center justify-between mb-2">
                      <h4 className="text-sm font-medium text-slate-800">统一知识检索候选</h4>
                      <span className="text-xs text-slate-500">
                        local={kbSearchSources.local} / external={kbSearchSources.external}
                      </span>
                    </div>
                    {kbSearchLoading ? (
                      <div className="text-xs text-slate-500 inline-flex items-center gap-1">
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        检索中...
                      </div>
                    ) : (
                      <div className="space-y-2">
                        {kbSearchResults.map((item) => (
                          <div key={`${item.source_backend}:${item.id}`} className="rounded border border-slate-200 bg-white p-2">
                            <div className="text-sm text-slate-800 font-medium">{item.summary}</div>
                            <div className="text-[11px] text-slate-500 mt-1">
                              {item.source_backend} · score={Math.round((item.similarity_score || 0) * 100)}% · {item.problem_type || 'unknown'}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {/* 相似案例 - 使用新组件 */}
                {(similarCases.length > 0 || loadingSimilarCases) && (
                  <div className="pt-4 border-t border-gray-200">
                    <SimilarCases
                      cases={similarCases}
                      loading={loadingSimilarCases}
                      onSelectCase={handleSelectSimilarCase}
                    />
                  </div>
                )}
                
                {/* 快速操作 */}
                <div className="pt-4 border-t border-gray-200">
                  <h4 className="font-medium text-gray-900 mb-3">快速操作</h4>
                  <div className="flex flex-wrap gap-2">
                    {serviceName && (
                      <>
                        <button
                          onClick={() => navigation.goToLogs({ serviceName })}
                          className="flex items-center gap-2 px-3 py-2 bg-blue-50 hover:bg-blue-100 text-blue-700 rounded-lg transition-colors text-sm"
                        >
                          <FileText className="w-4 h-4" />
                          查看服务日志
                        </button>
                        <button
                          onClick={() => navigation.goToTopology({ serviceName })}
                          className="flex items-center gap-2 px-3 py-2 bg-green-50 hover:bg-green-100 text-green-700 rounded-lg transition-colors text-sm"
                        >
                          <Network className="w-4 h-4" />
                          查看服务拓扑
                        </button>
                      </>
                    )}
                    {analysisType === 'trace' && currentTraceId && (
                      <button
                        onClick={() => navigation.goToTraces({ traceId: currentTraceId, serviceName: serviceName || undefined })}
                        className="flex items-center gap-2 px-3 py-2 bg-emerald-50 hover:bg-emerald-100 text-emerald-700 rounded-lg transition-colors text-sm"
                      >
                        <Zap className="w-4 h-4" />
                        查看 Trace 详情
                      </button>
                    )}
                    <button
                      onClick={handleSaveCase}
                      disabled={!result}
                      className="flex items-center gap-2 px-3 py-2 bg-purple-50 hover:bg-purple-100 text-purple-700 rounded-lg transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <Bookmark className="w-4 h-4" />
                      保存到知识库
                    </button>
                    <button
                      onClick={() => handleFindSimilarCases(inputText, result?.overview?.problem)}
                      disabled={!inputText}
                      className="flex items-center gap-2 px-3 py-2 bg-amber-50 hover:bg-amber-100 text-amber-700 rounded-lg transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <BookOpen className="w-4 h-4" />
                      查找相似知识库
                    </button>
                    <button
                      onClick={() => navigate('/ai-cases?tab=history')}
                      className="flex items-center gap-2 px-3 py-2 bg-indigo-50 hover:bg-indigo-100 text-indigo-700 rounded-lg transition-colors text-sm"
                    >
                      <History className="w-4 h-4" />
                      查看 AI 历史
                    </button>
                  </div>
                </div>

                <div className="pt-4 border-t border-gray-200">
                  <div className="flex items-center justify-between mb-3">
                    <h4 className="font-medium text-gray-900 flex items-center gap-2">
                      <MessageCircle className="w-4 h-4 text-indigo-600" />
                      对话
                    </h4>
                    <button
                      type="button"
                      onClick={resetFollowUpConversation}
                      disabled={followUpLoading || followUpMessages.length === 0}
                      className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded bg-gray-100 text-gray-600 hover:bg-gray-200 disabled:opacity-50"
                    >
                      <RotateCcw className="w-3.5 h-3.5" />
                      清空会话
                    </button>
                  </div>

                  <div className="rounded-lg border border-gray-200 bg-slate-50 p-3">
                    <div className="mb-2 rounded border border-slate-200 bg-white p-2 text-[11px] text-slate-600 space-y-1">
                      <div>可解释性：AI 回答内会显示引用片段（分析结论 `[A*]`、原始日志 `[L*]`）。</div>
                      <div>操作闭环：每条 AI 回答支持一键转工单 / Runbook / 告警抑制建议。</div>
                      <div>安全与成本：追问内容自动脱敏，长会话会自动摘要压缩并在必要时提醒。</div>
                      <div>提示：先发送一次追问后，AI 回答卡片才会出现“引用片段”和“转工单/Runbook/告警抑制”按钮。</div>
                    </div>
                    <div ref={followUpListRef} className="space-y-2 max-h-52 overflow-auto pr-1">
                      {followUpMessages.length === 0 ? (
                        <div className="text-xs text-gray-500">
                          可基于当前分析结果连续追问，例如“优先排查哪条根因？”、“给出更细的修复步骤”。
                        </div>
                      ) : (
                        followUpMessages.map((msg, index) => (
                          <div
                            key={`${msg.message_id || `${msg.role}-${index}`}-${msg.timestamp || ''}`}
                            className={`rounded p-2 text-sm ${
                              msg.role === 'user'
                                ? 'bg-blue-50 border border-blue-100 text-blue-800'
                                : 'bg-white border border-gray-200 text-gray-700'
                            }`}
                          >
                            <div className="text-[11px] mb-1 opacity-70">
                              {msg.role === 'user' ? '你' : 'AI'} {msg.timestamp ? `· ${new Date(msg.timestamp).toLocaleString('zh-CN', { hour12: false })}` : ''}
                            </div>
                            <div className="whitespace-pre-wrap">{msg.content}</div>
                            <div className="mt-2 flex flex-wrap gap-2">
                              <button
                                type="button"
                                onClick={() => void handleCopyFollowUpMessage(msg)}
                                className="px-2 py-1 text-[11px] rounded bg-white text-slate-700 border border-slate-200 hover:bg-slate-100"
                              >
                                <span className="inline-flex items-center gap-1">
                                  <Copy className="w-3.5 h-3.5" />
                                  复制
                                </span>
                              </button>
                              {msg.role === 'user' && (
                                <button
                                  type="button"
                                  onClick={() => handleRetryFollowUpMessage(msg)}
                                  disabled={followUpLoading}
                                  className="px-2 py-1 text-[11px] rounded bg-blue-50 text-blue-700 border border-blue-200 hover:bg-blue-100 disabled:opacity-50"
                                >
                                  <span className="inline-flex items-center gap-1">
                                    <RefreshCw className="w-3.5 h-3.5" />
                                  重试
                                </span>
                              </button>
                              )}
                              <button
                                type="button"
                                onClick={() => void handleDeleteFollowUpMessage(msg, index)}
                                disabled={followUpLoading || deletingMessageKey === (msg.message_id || `${msg.role}-${index}-${msg.timestamp || ''}`)}
                                className="px-2 py-1 text-[11px] rounded bg-rose-50 text-rose-700 border border-rose-200 hover:bg-rose-100 disabled:opacity-50"
                              >
                                <span className="inline-flex items-center gap-1">
                                  {deletingMessageKey === (msg.message_id || `${msg.role}-${index}-${msg.timestamp || ''}`)
                                    ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                    : <Trash2 className="w-3.5 h-3.5" />}
                                  删除
                                </span>
                              </button>
                              {msg.role === 'assistant' && (
                                <>
                                  <button
                                    type="button"
                                    onClick={() => handleCreateActionDraft(msg.message_id, 'ticket')}
                                    disabled={!msg.message_id || !analysisSessionId || !!actionLoadingKey}
                                    className="px-2 py-1 text-[11px] rounded bg-indigo-50 text-indigo-700 border border-indigo-200 hover:bg-indigo-100 disabled:opacity-50"
                                  >
                                    {actionLoadingKey === `${msg.message_id}:ticket` ? '生成中...' : '转工单'}
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => handleCreateActionDraft(msg.message_id, 'runbook')}
                                    disabled={!msg.message_id || !analysisSessionId || !!actionLoadingKey}
                                    className="px-2 py-1 text-[11px] rounded bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100 disabled:opacity-50"
                                  >
                                    {actionLoadingKey === `${msg.message_id}:runbook` ? '生成中...' : '转 Runbook 步骤'}
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => handleCreateActionDraft(msg.message_id, 'alert_suppression')}
                                    disabled={!msg.message_id || !analysisSessionId || !!actionLoadingKey}
                                    className="px-2 py-1 text-[11px] rounded bg-amber-50 text-amber-700 border border-amber-200 hover:bg-amber-100 disabled:opacity-50"
                                  >
                                    {actionLoadingKey === `${msg.message_id}:alert_suppression` ? '生成中...' : '转告警抑制建议'}
                                  </button>
                                </>
                              )}
                            </div>
                            {msg.role === 'assistant' && Array.isArray(msg.metadata?.references) && msg.metadata?.references.length > 0 && (
                              <div className="mt-2 rounded border border-amber-200 bg-amber-50 p-2">
                                <div className="text-[11px] font-medium text-amber-700 mb-1">引用片段</div>
                                <div className="space-y-1">
                                  {(msg.metadata?.references || []).map((ref: FollowUpReference) => (
                                    <div key={`${msg.message_id || index}:${ref.id}`} className="text-[11px] text-amber-800">
                                      [{ref.id}] {ref.title}: {ref.snippet}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                            {msg.message_id && actionDrafts[msg.message_id] && (
                              <div className="mt-2 rounded border border-slate-200 bg-slate-50 p-2">
                                <div className="text-[11px] font-medium text-slate-700 mb-1">动作草案：{actionDrafts[msg.message_id].action_type}</div>
                                <div className="text-[11px] text-slate-600 mb-1">{actionDrafts[msg.message_id].title}</div>
                                <pre className="text-[11px] text-slate-700 whitespace-pre-wrap">{JSON.stringify(actionDrafts[msg.message_id].payload, null, 2)}</pre>
                              </div>
                            )}
                          </div>
                        ))
                      )}
                    </div>

                    {followUpError && (
                      <div className="mt-2 text-xs text-red-600">{followUpError}</div>
                    )}
                    {followUpNotice && (
                      <div className="mt-2 text-xs text-emerald-700">{followUpNotice}</div>
                    )}

                    {followUpSuggestions.length > 0 && (
                      <div className="mt-3 flex flex-wrap gap-2">
                        {followUpSuggestions.map((suggestion) => (
                          <button
                            key={suggestion}
                            type="button"
                            onClick={() => setFollowUpQuestion(suggestion)}
                            className="px-2 py-1 text-xs rounded bg-white border border-indigo-200 text-indigo-700 hover:bg-indigo-50"
                          >
                            {suggestion}
                          </button>
                        ))}
                      </div>
                    )}

                    <div className="mt-3 flex items-start gap-2">
                      <textarea
                        value={followUpQuestion}
                        onChange={(e) => setFollowUpQuestion(e.target.value)}
                        onKeyDown={handleFollowUpKeyDown}
                        placeholder="继续追问当前分析结果..."
                        className="flex-1 min-h-[72px] px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 resize-y"
                      />
                      <button
                        type="button"
                        onClick={() => {
                          void handleSubmitFollowUp();
                        }}
                        disabled={followUpLoading || !followUpQuestion.trim()}
                        className="inline-flex items-center gap-1 px-3 py-2 rounded bg-indigo-600 text-white text-sm hover:bg-indigo-700 disabled:opacity-50"
                      >
                        {followUpLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                        发送
                      </button>
                    </div>
                    <div className="mt-2 text-[11px] text-gray-400 flex flex-wrap gap-3">
                      <span>快捷键：Ctrl/Cmd + Enter 发送</span>
                      {analysisSessionId && <span>分析会话: {analysisSessionId}</span>}
                      {conversationId && <span>对话会话: {conversationId}</span>}
                      {followUpMessages.length > 0 && <span>消息数: {followUpMessages.length}</span>}
                      {tokenHint.warning && <span className="text-amber-600">对话较长，已启用上下文压缩策略</span>}
                      {tokenHint.historyCompacted && <span className="text-indigo-500">长会话已自动摘要压缩</span>}
                      {!tokenHint.warning && !tokenHint.historyCompacted && <span>追问后将按会话长度自动启用压缩策略</span>}
                      <span>敏感字段自动脱敏已开启</span>
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              <EmptyState
                icon={<BrainCircuit className="w-12 h-12 text-gray-400" />}
                title="等待分析"
                description='输入日志或追踪数据，点击"开始分析"获取 AI 智能分析结果'
              />
            )}
          </div>
        </div>
      </div>
      {selectedSimilarCase && (
        <div className="fixed inset-0 z-40 flex items-center justify-center p-4">
          <button
            type="button"
            aria-label="关闭相似案例详情"
            onClick={handleCloseSimilarCaseDetail}
            className="absolute inset-0 bg-black/40"
          />
          <div className="relative z-10 w-[96vw] max-w-[1480px] max-h-[90vh] overflow-auto rounded-lg bg-white shadow-xl border border-gray-200">
            <div className="sticky top-0 z-10 flex items-start justify-between gap-3 px-4 py-3 border-b border-gray-200 bg-white">
              <div>
                <div className="text-sm text-blue-700">
                  相似度 {Math.round((selectedSimilarCase.similarity_score || 0) * 100)}%
                </div>
                <h3 className="text-base font-semibold text-gray-900">{selectedSimilarCase.summary || '相似知识库详情'}</h3>
                <div className="text-xs text-gray-500 mt-1">
                  知识库ID: {selectedSimilarCase.id} | 服务: {selectedSimilarCase.service_name || '-'} | 类型: {selectedSimilarCase.problem_type || '-'}
                </div>
              </div>
              <button
                type="button"
                onClick={handleCloseSimilarCaseDetail}
                className="inline-flex items-center justify-center w-8 h-8 rounded hover:bg-gray-100 text-gray-600"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-4 lg:p-5 space-y-4">
              {loadingSimilarCaseDetail ? (
                <div className="py-8">
                  <LoadingState message="加载知识库详情中..." />
                </div>
              ) : selectedSimilarCaseDetail ? (
                <>
                  <div className="text-sm text-gray-700 whitespace-pre-wrap">{selectedSimilarCaseDetail.summary || '-'}</div>
                  <div className="text-xs text-gray-500 flex flex-wrap gap-3">
                    <span>级别: {selectedSimilarCaseDetail.severity || '-'}</span>
                    <span>状态: {selectedSimilarCaseDetail.resolved ? '已解决' : '待处理'}</span>
                    {selectedSimilarCaseDetail.created_at && <span>创建: {new Date(selectedSimilarCaseDetail.created_at).toLocaleString('zh-CN', { hour12: false })}</span>}
                    {selectedSimilarCaseDetail.resolved_at && <span>解决: {new Date(selectedSimilarCaseDetail.resolved_at).toLocaleString('zh-CN', { hour12: false })}</span>}
                  </div>
                  {selectedSimilarCaseDetail.tags && selectedSimilarCaseDetail.tags.length > 0 && (
                    <div className="flex flex-wrap gap-2">
                      {selectedSimilarCaseDetail.tags.map((tag, idx) => (
                        <span key={`${tag}-${idx}`} className="px-2 py-0.5 text-xs rounded bg-slate-100 text-slate-700 border border-slate-200">
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}
                  {Array.isArray(selectedSimilarCaseDetail.root_causes) && selectedSimilarCaseDetail.root_causes.length > 0 && (
                    <div>
                      <h4 className="text-sm font-medium text-gray-900 mb-1">根因</h4>
                      <ul className="list-disc ml-5 space-y-1 text-sm text-gray-700">
                        {selectedSimilarCaseDetail.root_causes.map((cause, idx) => (
                          <li key={`${cause}-${idx}`}>{cause}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {Array.isArray(selectedSimilarCaseDetail.solutions) && selectedSimilarCaseDetail.solutions.length > 0 && (
                    <div>
                      <h4 className="text-sm font-medium text-gray-900 mb-1">解决方案</h4>
                      <div className="space-y-2">
                        {selectedSimilarCaseDetail.solutions.map((solution, idx) => (
                          <div key={`${solution.title || solution.description || idx}-${idx}`} className="rounded border border-gray-200 bg-slate-50 p-2">
                            <div className="text-sm font-medium text-gray-800">{solution.title || `方案 ${idx + 1}`}</div>
                            {solution.description && <div className="text-sm text-gray-700 mt-1">{solution.description}</div>}
                            {Array.isArray(solution.steps) && solution.steps.length > 0 && (
                              <ol className="list-decimal ml-5 mt-1 space-y-1 text-sm text-gray-700">
                                {solution.steps.map((step, stepIdx) => (
                                  <li key={`${step}-${stepIdx}`}>{step}</li>
                                ))}
                              </ol>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  <div className="rounded border border-slate-200 bg-slate-50 p-3">
                    <div className="text-sm font-medium text-slate-800 mb-2">
                      内容变更历史（{Number(selectedSimilarCaseDetail.content_update_history_count || 0)}）
                    </div>
                    {Array.isArray(selectedSimilarCaseDetail.content_update_history) && selectedSimilarCaseDetail.content_update_history.length > 0 ? (
                      <div className="overflow-auto max-h-[46vh] border border-slate-200 rounded bg-white">
                        <table className="w-full min-w-[1180px] text-xs text-slate-700">
                          <thead className="bg-slate-100 text-slate-600">
                            <tr>
                              <th className="px-2 py-2 text-left min-w-[68px]">版本</th>
                              <th className="px-2 py-2 text-left min-w-[165px]">时间</th>
                              <th className="px-2 py-2 text-left min-w-[120px]">编辑人</th>
                              <th className="px-2 py-2 text-left min-w-[240px]">字段</th>
                              <th className="px-2 py-2 text-left min-w-[420px]">变更摘要</th>
                              <th className="px-2 py-2 text-left min-w-[140px]">同步</th>
                            </tr>
                          </thead>
                          <tbody>
                            {selectedSimilarCaseDetail.content_update_history.map((history, index) => (
                              <tr key={`${history.event_id || history.version || index}-${history.updated_at || index}`} className="border-t border-slate-100 align-top">
                                <td className="px-2 py-2 whitespace-nowrap">v{history.version || '-'}</td>
                                <td className="px-2 py-2 whitespace-nowrap">{toLocaleTime(history.updated_at)}</td>
                                <td className="px-2 py-2 whitespace-nowrap">{history.editor || '-'}</td>
                                <td className="px-2 py-2 min-w-[240px]">
                                  {Array.isArray(history.changed_fields) && history.changed_fields.length > 0
                                    ? (
                                      <div className="flex flex-wrap gap-1">
                                        {formatHistoryFields(history.changed_fields).map((field) => (
                                          <span key={`${history.event_id || history.version || index}-${field}`} className="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 whitespace-nowrap">
                                            {field}
                                          </span>
                                        ))}
                                      </div>
                                    ) : (
                                      <div>
                                        <div className="text-amber-700">无有效字段变更</div>
                                        {Array.isArray(history.requested_fields) && history.requested_fields.length > 0 && (
                                          <div className="mt-1 text-slate-500">
                                            提交字段：{formatHistoryFields(history.requested_fields).join('、')}
                                          </div>
                                        )}
                                      </div>
                                    )}
                                </td>
                                <td className="px-2 py-2 min-w-[320px]">
                                  {Object.keys((history.changes || {}) as Record<string, { before?: any; after?: any }>).length === 0 ? (
                                    <div className="text-slate-600 space-y-1">
                                      <div className="text-amber-700">本次提交未引入有效差异</div>
                                      <div>原因：{formatNoEffectiveChangeReason(history.no_effective_change_reason)}</div>
                                      {Array.isArray(history.unchanged_requested_fields) && history.unchanged_requested_fields.length > 0 && (
                                        <div>
                                          等效字段：{formatHistoryFields(history.unchanged_requested_fields).join('、')}
                                        </div>
                                      )}
                                    </div>
                                  ) : (
                                    <details className="group">
                                      <summary className="cursor-pointer text-indigo-700 hover:text-indigo-900">
                                        查看详情（{Object.keys((history.changes || {}) as Record<string, { before?: any; after?: any }>).length} 项）
                                      </summary>
                                      <div className="mt-2 max-h-56 overflow-auto space-y-2 pr-1">
                                        {Object.entries((history.changes || {}) as Record<string, { before?: any; after?: any }>).map(([field, diff]) => (
                                          <div key={`${history.event_id || history.version || index}-${field}`} className="rounded border border-slate-200 bg-slate-50 px-2 py-1">
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
                                  <div>{history.sync_status || '-'}</div>
                                  {history.sync_error_code && <div className="text-red-600 mt-1">{history.sync_error_code}</div>}
                                  <div className="mt-1 text-slate-500">{formatHistoryNote(history.note)}</div>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : (
                      <div className="text-xs text-slate-500">暂无内容变更历史</div>
                    )}
                  </div>
                  {selectedSimilarCaseDetail.resolution && (
                    <div className="rounded border border-green-200 bg-green-50 p-2 text-sm text-green-800">
                      解决结论: {selectedSimilarCaseDetail.resolution}
                    </div>
                  )}
                  <div>
                    <h4 className="text-sm font-medium text-gray-900 mb-1">原始日志片段</h4>
                    <pre className="rounded border border-gray-200 bg-gray-50 p-2 text-xs text-gray-700 whitespace-pre-wrap max-h-56 overflow-auto">
                      {String(selectedSimilarCaseDetail.log_content || '').slice(0, 4000) || '-'}
                    </pre>
                  </div>
                </>
              ) : (
                <div className="text-sm text-red-600">加载详情失败，请稍后重试。</div>
              )}
            </div>
            <div className="sticky bottom-0 z-10 px-4 py-3 border-t border-gray-200 bg-white flex flex-wrap justify-end gap-2">
              <button
                type="button"
                onClick={handleReplaySimilarCase}
                disabled={!selectedSimilarCaseDetail || loadingSimilarCaseDetail}
                className="px-3 py-1.5 rounded bg-indigo-600 text-white text-sm hover:bg-indigo-700 disabled:opacity-50"
              >
                在当前页回放该条目
              </button>
              <button
                type="button"
                onClick={() => navigate(`/ai-cases?tab=cases&case_id=${encodeURIComponent(selectedSimilarCase.id)}`)}
                className="px-3 py-1.5 rounded bg-slate-100 text-slate-700 text-sm hover:bg-slate-200"
              >
                去知识库管理页
              </button>
              <button
                type="button"
                onClick={handleCloseSimilarCaseDetail}
                className="px-3 py-1.5 rounded bg-white text-gray-700 border border-gray-300 text-sm hover:bg-gray-50"
              >
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default AIAnalysis;
