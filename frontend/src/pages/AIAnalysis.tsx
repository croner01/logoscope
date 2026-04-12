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
import RuntimeActivityPanel from '../features/ai-runtime/components/RuntimeActivityPanel';
import useAgentRuntimeCommandFlow from '../features/ai-runtime/hooks/useAgentRuntimeCommandFlow';
import useRuntimeCommandSessions from '../features/ai-runtime/hooks/useRuntimeCommandSessions';
import {
  buildRuntimeAnalysisFollowUpMessage,
  buildRuntimeFollowUpMessage,
  buildRuntimeThoughtTimeline,
} from '../features/ai-runtime/utils/runtimeMessages';
import { api } from '../utils/api';
import { buildRuntimeFollowUpContext } from '../utils/runtimeFollowUpContext';
import type { Event } from '../utils/api';
import { normalizeAgentRunEventEnvelope, type AgentRunEventEnvelope } from '../utils/aiAgentRuntime';
import {
  agentRunReducer,
  createInitialAgentRunState,
  selectPendingApprovals,
  type AgentRunState,
} from '../utils/aiAgentRuntimeReducer';
import { reconcileAIRunState } from '../utils/aiRuntimeSync';
import { copyTextToClipboard } from '../utils/clipboard';
import { formatTime } from '../utils/formatters';
import {
  normalizeExecutableCommand,
  normalizeFollowUpCommandMatchKey,
} from '../utils/followUpCommandMatch';
import type { AgentRuntimeCommandSession } from '../features/ai-runtime/types/command';
import type { RuntimeApprovalEntry } from '../features/ai-runtime/types/view';
import { useNavigation } from '../hooks/useNavigation';
import { BrainCircuit, Loader2, AlertCircle, Lightbulb, Bug, Zap, FileText, Network, BookOpen, Bookmark, History, MessageCircle, RotateCcw, Send, X, Link2, Copy, RefreshCw, Trash2 } from 'lucide-react';

type UnknownObject = Record<string, unknown>;
type EventTextLike = {
  id?: string;
  timestamp?: string;
  level?: string;
  service_name?: string;
  message?: string;
};
const asObject = (value: unknown): UnknownObject =>
  value && typeof value === 'object' ? (value as UnknownObject) : {};
const asOptionalObject = (value: unknown): UnknownObject | undefined =>
  value && typeof value === 'object' ? (value as UnknownObject) : undefined;
const getErrorMessage = (error: unknown, fallback: string): string => {
  const response = asObject(asObject(error).response);
  const responseData = asObject(response.data);
  const detail = responseData.detail;
  if (typeof detail === 'string' && detail.trim()) {
    return detail.trim();
  }
  const responseMessage = responseData.message;
  if (typeof responseMessage === 'string' && responseMessage.trim()) {
    return responseMessage.trim();
  }
  const message = asObject(error).message;
  if (typeof message === 'string' && message.trim()) {
    return message.trim();
  }
  return fallback;
};

const parseCommandSpecRecovery = (value: unknown): {
  fixHint?: string;
  fixDetail?: string;
  suggestedCommand?: string;
  suggestedCommandSpec?: UnknownObject;
} => {
  const payload = asObject(value);
  const fixHint = String(payload.fix_hint || payload.fixHint || '').trim() || undefined;
  const fixDetail = String(payload.fix_detail || payload.fixDetail || '').trim() || undefined;
  const suggestedCommand = String(payload.suggested_command || payload.suggestedCommand || '').trim() || undefined;
  const suggestedCommandSpec = (
    asOptionalObject(payload.suggested_command_spec)
    || asOptionalObject(payload.suggestedCommandSpec)
  );
  return {
    fixHint,
    fixDetail,
    suggestedCommand,
    suggestedCommandSpec,
  };
};

interface AIAnalysisResult {
  overview?: {
    problem: string;
    severity: string;
    description: string;
    confidence: number;
  };
  dataFlow?: {
    summary?: string;
    path?: Array<{
      step?: number;
      component?: string;
      operation?: string;
      status?: string;
      evidence?: string;
      from?: string;
      to?: string;
      latency_ms?: number;
    }>;
    evidence?: string[];
    confidence?: number;
  };
  rootCauses?: Array<{
    title: string;
    description: string;
  }>;
  handlingIdeas?: Array<{
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

type ContextPillPayload = {
  result?: AIAnalysisResult | null;
  sessionId?: string;
  service?: string;
  traceId?: string;
  input?: string;
  type?: 'log' | 'trace';
};

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
    attributes?: Record<string, unknown>;
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
    context?: Record<string, unknown>;
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
      metadata?: Record<string, unknown>;
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
    context: Record<string, unknown>;
    resolved?: boolean;
    resolution?: string;
    llm_model?: string;
    llm_metadata?: Record<string, unknown>;
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
  context: Record<string, unknown>;
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
  changes?: Record<string, { before?: unknown; after?: unknown }>;
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

interface FollowUpSubgoal {
  id: string;
  title: string;
  status: 'pending' | 'in_progress' | 'completed' | 'needs_data' | string;
  reason?: string;
  evidence?: string[];
  next_action?: string;
}

interface FollowUpReflection {
  iterations?: number;
  completed_count?: number;
  total_count?: number;
  final_confidence?: number;
  gaps?: string[];
  next_actions?: string[];
  rounds?: Array<{
    iteration?: number;
    summary?: string;
    unresolved_subgoals?: string[];
    gaps?: string[];
    actions?: string[];
    confidence?: number;
  }>;
}

interface FollowUpActionPlan {
  id?: string;
  source?: string;
  priority?: number;
  title?: string;
  purpose?: string;
  question?: string;
  action_type?: 'query' | 'write' | 'manual' | string;
  command?: string;
  command_type?: 'query' | 'repair' | 'unknown' | string;
  risk_level?: 'low' | 'high' | string;
  executable?: boolean;
  requires_confirmation?: boolean;
  requires_write_permission?: boolean;
  requires_elevation?: boolean;
  reason?: string;
  command_spec?: Record<string, unknown>;
}

interface FollowUpStructuredContent {
  conclusion?: string;
  request_flow?: string[];
  root_causes?: Array<{
    title?: string;
    confidence?: string;
    evidence_ids?: string[];
  }>;
  actions?: Array<{
    priority?: number;
    title?: string;
    action?: string;
    command?: string;
    expected_outcome?: string;
    reason?: string;
  }>;
  verification?: string[];
  rollback?: string[];
  missing_evidence?: string[];
  summary?: string;
}

type FollowUpThoughtPhase = 'plan' | 'thought' | 'action' | 'observation' | 'replan' | 'system';
type FollowUpThoughtStatus = 'info' | 'success' | 'warning' | 'error';

interface FollowUpThoughtItem {
  id?: string;
  phase?: FollowUpThoughtPhase | string;
  status?: FollowUpThoughtStatus | string;
  title?: string;
  detail?: string;
  timestamp?: string;
  iteration?: number;
}

interface FollowUpApprovalCandidate {
  id: string;
  message_id?: string;
  action_id?: string;
  command: string;
  command_type?: string;
  risk_level?: string;
  requires_elevation?: boolean;
  requires_confirmation?: boolean;
  confirmation_ticket?: string;
  message?: string;
  title?: string;
  runtime_run_id?: string;
  runtime_approval_id?: string;
}

interface CaseStoredMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp?: string;
  message_id?: string;
  metadata?: Record<string, unknown>;
}

interface ExtractedFollowUpCommand {
  command: string;
  commandType: 'query' | 'repair' | 'unknown';
  riskLevel: 'low' | 'high';
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
    subgoals?: FollowUpSubgoal[];
    reflection?: FollowUpReflection;
    actions?: FollowUpActionPlan[];
    action_observations?: Array<Record<string, unknown>>;
    react_loop?: Record<string, unknown>;
    react_iterations?: Array<Record<string, unknown>>;
    approval_required?: Array<Record<string, unknown>>;
    stream_loading?: boolean;
    stream_stage?: string;
    stream_timeline?: FollowUpThoughtItem[];
    thoughts?: FollowUpThoughtItem[];
    [key: string]: unknown;
  };
}

interface FollowUpAnalysisRuntimeSession {
  runId: string;
  messageId: string;
  state: AgentRunState;
  sourceMessageId?: string;
  title: string;
  question?: string;
}

interface CrossPullResult {
  requestId: string;
  traceId: string;
  sourceService: string;
  targetService: string;
  anchorIso: string;
  startTime: string;
  endTime: string;
  selectedEvents: Event[];
  failedReasons: string[];
}

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

const parseKBRuntimeError = (err: unknown): {
  code: string;
  message: string;
  effectiveRetrievalMode: 'local' | 'hybrid' | 'remote_only';
  effectiveSaveMode: 'local_only' | 'local_and_remote';
} => {
  const errObj = asObject(err);
  const response = asObject(errObj.response);
  const responseData = asObject(response.data);
  const detail = asObject(responseData.detail);
  const code = String(detail.code || '');
  const message = String(detail.message || errObj.message || '知识库策略解析失败，已按本地模式处理');
  const effectiveRetrievalMode = detail.effective_retrieval_mode === 'remote_only' ? 'remote_only' : detail.effective_retrieval_mode === 'hybrid' ? 'hybrid' : 'local';
  const effectiveSaveMode = detail.effective_save_mode === 'local_and_remote' ? 'local_and_remote' : 'local_only';
  return { code, message, effectiveRetrievalMode, effectiveSaveMode };
};

const parseFollowUpErrorMessage = (err: unknown): string => {
  const errObj = asObject(err);
  const response = asObject(errObj.response);
  const responseData = asObject(response.data);
  const status = Number(response.status || errObj.status || 0);
  const errorCode = String(errObj.code || '').toUpperCase();
  const errorMessage = String(errObj.message || '').toLowerCase();
  if (errorCode === 'ECONNABORTED' || errorMessage.includes('timeout')) {
    return '对话请求超时，已保留问题内容。可点击“重试”或关闭 LLM 后重试。';
  }
  const detail = responseData.detail;
  if (status === 504) {
    return '对话请求超时（504），已保留问题内容。可点击“重试”或关闭 LLM 后重试。';
  }
  if (status === 503 || status === 502) {
    return `对话服务暂时不可用（${status}），请稍后重试。`;
  }
  if (typeof detail === 'string' && detail.trim()) {
    return detail.trim();
  }
  if (detail && typeof detail === 'object' && asObject(detail).message) {
    return String(asObject(detail).message);
  }
  return String(errObj.message || '追问失败，请稍后重试');
};

const parseAnalyzeErrorMessage = (
  err: unknown,
  options?: { useLLM?: boolean },
): string => {
  const errObj = asObject(err);
  const response = asObject(errObj.response);
  const responseData = asObject(response.data);
  const status = Number(response.status || errObj.status || 0);
  const errorCode = String(errObj.code || '').toUpperCase();
  const errorMessage = String(errObj.message || '').toLowerCase();
  const usingLLM = Boolean(options?.useLLM);

  if (errorCode === 'ECONNABORTED' || errorMessage.includes('timeout')) {
    return `分析请求超时。请重试${usingLLM ? '，或临时关闭 LLM 后重试。' : '。'}`;
  }
  if (status === 504) {
    return `分析请求超时（504）。请稍后重试${usingLLM ? '，或关闭 LLM 后重试。' : '。'}`;
  }
  if (status === 503 || status === 502) {
    return `分析服务暂时不可用（${status}），请稍后重试。`;
  }
  const detail = responseData.detail;
  if (typeof detail === 'string' && detail.trim()) {
    return detail.trim();
  }
  if (detail && typeof detail === 'object' && asObject(detail).message) {
    return String(asObject(detail).message);
  }
  return String(errObj.message || '分析失败，请稍后重试');
};

const parseCrossPullTaskError = (label: string, err: unknown): string => {
  const errObj = asObject(err);
  const response = asObject(errObj.response);
  const responseData = asObject(response.data);
  const detail = responseData.detail;
  const status = Number(response.status || errObj.status || 0);
  let detailText = '';
  if (typeof detail === 'string' && detail.trim()) {
    detailText = detail.trim();
  } else if (detail && typeof detail === 'object' && asObject(detail).message) {
    detailText = String(asObject(detail).message).trim();
  } else {
    detailText = String(errObj.message || responseData.message || 'unknown_error').trim();
  }
  const safeLabel = String(label || 'query').trim();
  if (status > 0) {
    return `${safeLabel}(HTTP ${status}): ${detailText}`;
  }
  return `${safeLabel}: ${detailText}`;
};

const FOLLOWUP_SUBGOAL_STATUS_LABELS: Record<string, string> = {
  pending: '待执行',
  in_progress: '进行中',
  completed: '已完成',
  needs_data: '待补证据',
};

const formatFollowUpSubgoalStatus = (status: string): string => {
  const normalized = String(status || '').trim().toLowerCase();
  return FOLLOWUP_SUBGOAL_STATUS_LABELS[normalized] || normalized || '待执行';
};

const getFollowUpSubgoalStatusTagClass = (status: string): string => {
  const normalized = String(status || '').trim().toLowerCase();
  if (normalized === 'completed') {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700';
  }
  if (normalized === 'in_progress') {
    return 'border-blue-200 bg-blue-50 text-blue-700';
  }
  if (normalized === 'needs_data') {
    return 'border-amber-200 bg-amber-50 text-amber-700';
  }
  return 'border-slate-200 bg-slate-100 text-slate-600';
};

const FOLLOWUP_ACTION_STATUS_LABELS: Record<string, string> = {
  precheck: '策略预检',
  running: '执行中',
  executed: '已执行',
  failed: '执行失败',
  cancelled: '已取消',
  skipped: '策略跳过',
  permission_required: '需人工执行',
  confirmation_required: '待确认',
  elevation_required: '待提权',
  unknown: '未知状态',
};

const normalizeFollowUpActionStatus = (status: unknown): string =>
  String(status || '').trim().toLowerCase() || 'unknown';

const formatFollowUpActionStatus = (status: unknown): string => {
  const normalized = normalizeFollowUpActionStatus(status);
  return FOLLOWUP_ACTION_STATUS_LABELS[normalized] || normalized || '未知状态';
};

const getFollowUpActionStatusTagClass = (status: unknown): string => {
  const normalized = normalizeFollowUpActionStatus(status);
  if (normalized === 'running') {
    return 'border-blue-200 bg-blue-50 text-blue-700';
  }
  if (normalized === 'executed') {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700';
  }
  if (normalized === 'failed') {
    return 'border-rose-200 bg-rose-50 text-rose-700';
  }
  if (normalized === 'cancelled') {
    return 'border-slate-300 bg-slate-100 text-slate-700';
  }
  if (normalized === 'confirmation_required' || normalized === 'elevation_required') {
    return 'border-amber-300 bg-amber-50 text-amber-800';
  }
  if (normalized === 'permission_required' || normalized === 'skipped' || normalized === 'unknown') {
    return 'border-orange-200 bg-orange-50 text-orange-700';
  }
  return 'border-slate-200 bg-slate-100 text-slate-700';
};

const formatReflectionConfidence = (confidence: unknown): string => {
  const value = Number(confidence);
  if (!Number.isFinite(value)) {
    return '-';
  }
  const normalized = Math.min(1, Math.max(0, value));
  return `${Math.round(normalized * 100)}%`;
};

const toLocaleTime = (value?: string): string => {
  if (!value) return '-';
  const formatted = formatTime(String(value));
  return formatted === '--' ? value : formatted;
};

const FOLLOWUP_THOUGHT_EVENT_MAX = 36;
const FOLLOWUP_THOUGHT_RENDER_MAX = 12;

const normalizeFollowUpThoughtPhase = (phase: unknown): FollowUpThoughtPhase => {
  const normalized = String(phase || '').trim().toLowerCase();
  if (normalized === 'plan' || normalized === 'planning') return 'plan';
  if (normalized === 'thought') return 'thought';
  if (normalized === 'action') return 'action';
  if (normalized === 'observation') return 'observation';
  if (normalized === 'replan') return 'replan';
  return 'system';
};

const normalizeFollowUpThoughtStatus = (status: unknown): FollowUpThoughtStatus => {
  const normalized = String(status || '').trim().toLowerCase();
  if (normalized === 'success') return 'success';
  if (normalized === 'warning' || normalized === 'warn') return 'warning';
  if (normalized === 'error' || normalized === 'failed') return 'error';
  return 'info';
};

const truncateFollowUpThoughtText = (raw: unknown, maxLen = 180): string => {
  const text = String(raw || '').trim();
  if (!text) {
    return '';
  }
  if (text.length <= maxLen) {
    return text;
  }
  return `${text.slice(0, Math.max(20, maxLen - 1)).trim()}…`;
};

const filterPlanningThoughtTimeline = (
  timeline: FollowUpThoughtItem[],
  suppressPlanning: boolean,
): FollowUpThoughtItem[] => {
  if (!suppressPlanning) {
    return timeline;
  }
  return timeline.filter((item) => normalizeFollowUpThoughtPhase(item.phase) !== 'plan');
};

const normalizeFollowUpThoughtTimeline = (raw: unknown): FollowUpThoughtItem[] => {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw
    .map((item, index) => {
      if (!item || typeof item !== 'object') {
        return null;
      }
      const payload = item as UnknownObject;
      const title = String(payload.title || '').trim();
      if (!title) {
        return null;
      }
      const detail = truncateFollowUpThoughtText(payload.detail);
      const iterationRaw = Number(payload.iteration);
      return {
        id: String(payload.id || `timeline-${index}`),
        phase: normalizeFollowUpThoughtPhase(payload.phase),
        status: normalizeFollowUpThoughtStatus(payload.status),
        title,
        detail: detail || undefined,
        timestamp: String(payload.timestamp || '').trim() || undefined,
        iteration: Number.isFinite(iterationRaw) ? Math.max(1, Math.floor(iterationRaw)) : undefined,
      } as FollowUpThoughtItem;
    })
    .filter(Boolean) as FollowUpThoughtItem[];
};

const getFollowUpThoughtTagClass = (status: unknown): string => {
  const normalized = normalizeFollowUpThoughtStatus(status);
  if (normalized === 'success') {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700';
  }
  if (normalized === 'warning') {
    return 'border-amber-200 bg-amber-50 text-amber-700';
  }
  if (normalized === 'error') {
    return 'border-rose-200 bg-rose-50 text-rose-700';
  }
  return 'border-slate-200 bg-slate-100 text-slate-700';
};

const appendFollowUpThoughtTimeline = (
  metadata: Record<string, unknown>,
  thought: FollowUpThoughtItem | null,
  options?: {
    suppressPlanning?: boolean;
  },
): FollowUpThoughtItem[] => {
  const suppressPlanning = Boolean(options?.suppressPlanning);
  if (!thought || !String(thought.title || '').trim()) {
    return filterPlanningThoughtTimeline(
      normalizeFollowUpThoughtTimeline(metadata.stream_timeline),
      suppressPlanning,
    );
  }
  const timeline = filterPlanningThoughtTimeline(
    normalizeFollowUpThoughtTimeline(metadata.stream_timeline),
    suppressPlanning,
  );
  const safePhase = normalizeFollowUpThoughtPhase(thought.phase);
  if (suppressPlanning && safePhase === 'plan') {
    return timeline;
  }
  const safeStatus = normalizeFollowUpThoughtStatus(thought.status);
  const title = String(thought.title || '').trim();
  const detail = truncateFollowUpThoughtText(thought.detail);
  const iterationRaw = Number(thought.iteration);
  const normalizedThought: FollowUpThoughtItem = {
    id: String(thought.id || `${safePhase}-${Date.now()}-${Math.floor(Math.random() * 1000)}`),
    phase: safePhase,
    status: safeStatus,
    title,
    detail: detail || undefined,
    timestamp: String(thought.timestamp || new Date().toISOString()),
    iteration: Number.isFinite(iterationRaw) ? Math.max(1, Math.floor(iterationRaw)) : undefined,
  };
  const dedupeKey = `${safePhase}|${normalizedThought.iteration || 0}|${title}|${detail}`;
  const existingKeys = new Set(
    timeline.map((item) => (
      `${normalizeFollowUpThoughtPhase(item.phase)}|${Number(item.iteration || 0)}|${String(item.title || '').trim()}|${String(item.detail || '').trim()}`
    )),
  );
  if (existingKeys.has(dedupeKey)) {
    return timeline;
  }
  return [...timeline, normalizedThought].slice(-FOLLOWUP_THOUGHT_EVENT_MAX);
};

const buildFollowUpThoughtFromStreamEvent = (
  eventNameRaw: string,
  data: Record<string, unknown>,
): FollowUpThoughtItem | null => {
  const eventName = String(eventNameRaw || '').trim().toLowerCase();
  const timestamp = new Date().toISOString();
  if (eventName === 'thought') {
    const title = String(data.title || data.summary || data.thought || '').trim();
    if (!title) {
      return null;
    }
    return {
      phase: normalizeFollowUpThoughtPhase(data.phase || 'thought'),
      status: normalizeFollowUpThoughtStatus(data.status),
      title,
      detail: truncateFollowUpThoughtText(data.detail || data.content || ''),
      timestamp,
      iteration: Number.isFinite(Number(data.iteration)) ? Number(data.iteration) : undefined,
    };
  }
  if (eventName === 'plan') {
    const stage = String(data.stage || '').trim().toLowerCase();
    if (!stage) {
      return null;
    }
    if (stage === 'react_memory_load') {
      return { phase: 'plan', status: 'info', title: '加载历史闭环记忆', timestamp };
    }
    if (stage === 'planning_ready') {
      const subgoals = Array.isArray(data.subgoals) ? data.subgoals.length : 0;
      const gaps = Array.isArray(asObject(data.reflection).gaps) ? (asObject(data.reflection).gaps as unknown[]).length : 0;
      return {
        phase: 'plan',
        status: 'info',
        title: `完成问题拆解（子目标 ${subgoals}）`,
        detail: gaps > 0 ? `待补证据点 ${gaps}` : '已生成首轮执行计划',
        timestamp,
      };
    }
    if (stage === 'llm_start') {
      const llmRequested = Boolean(data.llm_requested);
      const llmEnabled = Boolean(data.llm_enabled);
      const tokenWarning = Boolean(data.token_warning);
      const modeLabel = llmRequested && llmEnabled ? 'LLM' : '规则模式';
      return {
        phase: 'thought',
        status: tokenWarning ? 'warning' : 'info',
        title: `开始生成回答（${modeLabel}）`,
        detail: tokenWarning ? '上下文较长，已进入压缩预算区间' : '',
        timestamp,
      };
    }
    if (stage === 'react_execute') {
      const iteration = Number(data.iteration);
      const candidateActions = Number(data.candidate_actions);
      return {
        phase: 'action',
        status: 'info',
        title: `执行第 ${Number.isFinite(iteration) ? Math.max(1, Math.floor(iteration)) : 1} 轮查询动作`,
        detail: Number.isFinite(candidateActions) ? `候选动作 ${Math.max(0, Math.floor(candidateActions))}` : '',
        timestamp,
        iteration: Number.isFinite(iteration) ? Math.max(1, Math.floor(iteration)) : undefined,
      };
    }
    return {
      phase: 'plan',
      status: 'info',
      title: `流程阶段: ${stage}`,
      timestamp,
    };
  }
  if (eventName === 'action') {
    const actions = Array.isArray(data.actions) ? data.actions : [];
    const actionTitles = actions
      .slice(0, 3)
      .map((item) => String(asObject(item).title || asObject(item).command || '').trim())
      .filter(Boolean);
    return {
      phase: 'action',
      status: 'info',
      title: `生成执行计划（${actions.length}）`,
      detail: actionTitles.join('；'),
      timestamp,
    };
  }
  if (eventName === 'observation') {
    const statusRaw = String(data.status || '').trim().toLowerCase();
    const exitCode = Number(data.exit_code);
    let status: FollowUpThoughtStatus = 'info';
    if (statusRaw === 'executed' && Number.isFinite(exitCode) && exitCode === 0) {
      status = 'success';
    } else if (statusRaw === 'failed') {
      status = 'error';
    } else if (
      statusRaw === 'skipped'
      || statusRaw === 'precheck'
      || statusRaw === 'confirmation_required'
      || statusRaw === 'elevation_required'
      || statusRaw === 'permission_required'
      || (statusRaw === 'executed' && Number.isFinite(exitCode) && exitCode !== 0)
    ) {
      status = 'warning';
    }
    const command = truncateFollowUpThoughtText(data.command, 80);
    const message = truncateFollowUpThoughtText(data.message || data.detail || '', 140);
    const title = command ? `命令观察: ${command}` : `命令观察: ${statusRaw || 'unknown'}`;
    return {
      phase: 'observation',
      status,
      title,
      detail: message || undefined,
      timestamp,
      iteration: Number.isFinite(Number(data.iteration)) ? Number(data.iteration) : undefined,
    };
  }
  if (eventName === 'approval_required') {
    const command = truncateFollowUpThoughtText(data.command, 80);
    const message = truncateFollowUpThoughtText(
      data.message || '检测到写操作命令，需要提权审批后执行',
      160,
    );
    return {
      phase: 'observation',
      status: 'warning',
      title: command ? `待审批命令: ${command}` : '检测到需提权审批的命令',
      detail: message || undefined,
      timestamp,
      iteration: Number.isFinite(Number(data.iteration)) ? Number(data.iteration) : undefined,
    };
  }
  if (eventName === 'replan') {
    const reactLoop = asObject(data.react_loop);
    const replan = asObject(reactLoop.replan);
    const needed = Boolean(replan.needed);
    const nextActions = Array.isArray(replan.next_actions) ? replan.next_actions : [];
    return {
      phase: 'replan',
      status: needed ? 'warning' : 'success',
      title: needed ? '需要重规划下一轮动作' : '闭环验证通过',
      detail: nextActions
        .slice(0, 2)
        .map((item) => String(item || '').trim())
        .filter(Boolean)
        .join('；'),
      timestamp,
      iteration: Number.isFinite(Number(data.iteration)) ? Number(data.iteration) : undefined,
    };
  }
  return null;
};

const buildFollowUpThoughtTimelineFromMetadata = (
  metadata: unknown,
  options?: {
    suppressPlanning?: boolean;
  },
): FollowUpThoughtItem[] => {
  const suppressPlanning = Boolean(options?.suppressPlanning);
  const safeMetadata = asObject(metadata);
  const streamed = normalizeFollowUpThoughtTimeline(safeMetadata.stream_timeline);
  if (streamed.length > 0) {
    return filterPlanningThoughtTimeline(
      streamed.slice(-FOLLOWUP_THOUGHT_RENDER_MAX),
      suppressPlanning,
    );
  }
  const explicitThoughts = normalizeFollowUpThoughtTimeline(safeMetadata.thoughts);
  if (explicitThoughts.length > 0) {
    return filterPlanningThoughtTimeline(
      explicitThoughts.slice(-FOLLOWUP_THOUGHT_RENDER_MAX),
      suppressPlanning,
    );
  }

  const derived: FollowUpThoughtItem[] = [];
  const subgoals = Array.isArray(safeMetadata.subgoals) ? safeMetadata.subgoals : [];
  const actions = Array.isArray(safeMetadata.actions) ? safeMetadata.actions : [];
  const observations = Array.isArray(safeMetadata.action_observations) ? safeMetadata.action_observations : [];
  const reactLoop = asObject(safeMetadata.react_loop);
  const reactReplan = asObject(reactLoop.replan);

  if (!suppressPlanning && subgoals.length > 0) {
    derived.push({
      phase: 'plan',
      status: 'info',
      title: `问题拆解完成（子目标 ${subgoals.length}）`,
      detail: truncateFollowUpThoughtText(asObject(safeMetadata.reflection).summary || ''),
    });
  }
  if (actions.length > 0) {
    const actionTitle = actions
      .slice(0, 3)
      .map((item) => String(asObject(item).title || asObject(item).command || '').trim())
      .filter(Boolean)
      .join('；');
    derived.push({
      phase: 'action',
      status: 'info',
      title: `执行计划共 ${actions.length} 项`,
      detail: actionTitle || undefined,
    });
  }
  observations.slice(-4).forEach((item, index) => {
    const obs = asObject(item);
    const statusRaw = String(obs.status || '').trim().toLowerCase();
    const exitCode = Number(obs.exit_code);
    let status: FollowUpThoughtStatus = 'info';
    if (statusRaw === 'executed' && Number.isFinite(exitCode) && exitCode === 0) {
      status = 'success';
    } else if (statusRaw === 'failed') {
      status = 'error';
    } else if (
      statusRaw === 'skipped'
      || statusRaw === 'precheck'
      || statusRaw === 'confirmation_required'
      || statusRaw === 'elevation_required'
      || statusRaw === 'permission_required'
      || (statusRaw === 'executed' && Number.isFinite(exitCode) && exitCode !== 0)
    ) {
      status = 'warning';
    }
    const command = truncateFollowUpThoughtText(obs.command, 80);
    const message = truncateFollowUpThoughtText(obs.message || obs.detail || '', 120);
    derived.push({
      id: `derived-observation-${index}`,
      phase: 'observation',
      status,
      title: command ? `命令观察: ${command}` : `命令观察: ${statusRaw || 'unknown'}`,
      detail: message || undefined,
      iteration: Number.isFinite(Number(obs.iteration)) ? Number(obs.iteration) : undefined,
    });
  });
  if (Object.keys(reactLoop).length > 0) {
    const needed = Boolean(reactReplan.needed);
    const nextActions = Array.isArray(reactReplan.next_actions) ? reactReplan.next_actions : [];
    derived.push({
      phase: 'replan',
      status: needed ? 'warning' : 'success',
      title: needed ? '闭环建议继续重试/重规划' : '闭环评估已收敛',
      detail: nextActions
        .slice(0, 2)
        .map((item) => String(item || '').trim())
        .filter(Boolean)
        .join('；'),
    });
  }
  return filterPlanningThoughtTimeline(
    normalizeFollowUpThoughtTimeline(derived).slice(-FOLLOWUP_THOUGHT_RENDER_MAX),
    suppressPlanning,
  );
};

const extractFollowUpStructuredJson = (content: string): UnknownObject | null => {
  const normalized = String(content || '').trim();
  if (!normalized) {
    return null;
  }
  const candidates = [
    normalized,
    normalized.replace(/^json\s*/i, '').trim(),
  ].filter(Boolean);

  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as UnknownObject;
      }
    } catch (_err) {
      // ignore and continue with loose extraction
    }
    const startIndex = candidate.search(/[[{]/);
    if (startIndex < 0) {
      continue;
    }
    const sliced = candidate.slice(startIndex);
    try {
      const parsed = JSON.parse(sliced);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as UnknownObject;
      }
    } catch (_err) {
      // ignore invalid partial json
    }
  }
  return null;
};

const parseFollowUpStructuredContent = (content: string): FollowUpStructuredContent | null => {
  const payload = extractFollowUpStructuredJson(content);
  if (!payload) {
    return null;
  }
  const hasStructuredShape = [
    'conclusion',
    'request_flow',
    'root_causes',
    'actions',
    'verification',
    'rollback',
    'missing_evidence',
    'summary',
  ].some((key) => key in payload);
  if (!hasStructuredShape) {
    return null;
  }
  return {
    conclusion: String(payload.conclusion || '').trim() || undefined,
    request_flow: Array.isArray(payload.request_flow)
      ? payload.request_flow.map((item) => String(item || '').trim()).filter(Boolean)
      : [],
    root_causes: Array.isArray(payload.root_causes)
      ? payload.root_causes
        .filter((item) => item && typeof item === 'object')
        .map((item) => {
          const cause = item as UnknownObject;
          return {
            title: String(cause.title || '').trim() || undefined,
            confidence: String(cause.confidence || '').trim() || undefined,
            evidence_ids: Array.isArray(cause.evidence_ids)
              ? cause.evidence_ids.map((id) => String(id || '').trim()).filter(Boolean)
              : [],
          };
        })
        .filter((item) => item.title)
      : [],
    actions: Array.isArray(payload.actions)
      ? payload.actions
        .filter((item) => item && typeof item === 'object')
        .map((item) => {
          const action = item as UnknownObject;
          return {
            priority: Number(action.priority),
            title: String(action.title || '').trim() || undefined,
            action: String(action.action || '').trim() || undefined,
            command: String(action.command || '').trim() || undefined,
            expected_outcome: String(action.expected_outcome || '').trim() || undefined,
            reason: String(action.reason || '').trim() || undefined,
          };
        })
      : [],
    verification: Array.isArray(payload.verification)
      ? payload.verification.map((item) => String(item || '').trim()).filter(Boolean)
      : [],
    rollback: Array.isArray(payload.rollback)
      ? payload.rollback.map((item) => String(item || '').trim()).filter(Boolean)
      : [],
    missing_evidence: Array.isArray(payload.missing_evidence)
      ? payload.missing_evidence.map((item) => String(item || '').trim()).filter(Boolean)
      : [],
    summary: String(payload.summary || '').trim() || undefined,
  };
};

const looksLikeStructuredStreamContent = (content: string): boolean => {
  const normalized = String(content || '').trim().toLowerCase();
  if (!normalized) {
    return false;
  }
  return (
    normalized.startsWith('json{')
    || normalized.startsWith('{')
    || normalized.startsWith('```json')
    || normalized.startsWith('{"conclusion"')
  );
};

const renderFollowUpInlineRichText = (text: string, keyPrefix: string): React.ReactNode[] => {
  const parts = String(text || '').split(/(`[^`\n]+`)/g);
  return parts.map((part, index) => {
    if (part.startsWith('`') && part.endsWith('`') && part.length >= 2) {
      return (
        <code key={`${keyPrefix}-code-${index}`} className="rounded bg-slate-100 px-1 py-0.5 text-[12px] text-slate-700">
          {part.slice(1, -1)}
        </code>
      );
    }
    return <React.Fragment key={`${keyPrefix}-text-${index}`}>{part}</React.Fragment>;
  });
};

const renderFollowUpRichContent = (
  content: string,
  keyPrefix: string,
  options?: { streamLoading?: boolean },
): React.ReactNode => {
  const normalized = String(content || '').replace(/\r\n/g, '\n');
  if (!normalized.trim()) {
    return (
      <span className="text-slate-400">
        {options?.streamLoading ? '正在生成回答...' : '...'}
      </span>
    );
  }

  const structuredContent = parseFollowUpStructuredContent(normalized);
  if (structuredContent) {
    const structuredActions = [...(structuredContent.actions || [])].sort((left, right) => {
      const leftPriority = Number.isFinite(Number(left.priority)) ? Number(left.priority) : 999;
      const rightPriority = Number.isFinite(Number(right.priority)) ? Number(right.priority) : 999;
      return leftPriority - rightPriority;
    });
    return (
      <div className="space-y-3">
        {structuredContent.conclusion && (
          <div className="rounded border border-slate-200 bg-slate-50 p-3">
            <div className="text-[11px] font-medium uppercase tracking-wide text-slate-500">结论</div>
            <div className="mt-1 whitespace-pre-wrap text-sm leading-relaxed text-slate-800">
              {structuredContent.conclusion}
            </div>
          </div>
        )}
        {Array.isArray(structuredContent.request_flow) && structuredContent.request_flow.length > 0 && (
          <div>
            <div className="mb-1 text-[11px] font-medium text-slate-500">请求流程</div>
            <ol className="list-decimal space-y-1 pl-5 text-sm text-slate-700">
              {structuredContent.request_flow.map((item, index) => (
                <li key={`${keyPrefix}-structured-flow-${index}`} className="leading-relaxed">
                  {item}
                </li>
              ))}
            </ol>
          </div>
        )}
        {Array.isArray(structuredContent.root_causes) && structuredContent.root_causes.length > 0 && (
          <div>
            <div className="mb-1 text-[11px] font-medium text-slate-500">根因分析</div>
            <div className="space-y-2">
              {structuredContent.root_causes.map((item, index) => (
                <div
                  key={`${keyPrefix}-structured-cause-${index}`}
                  className="rounded border border-amber-100 bg-amber-50/70 p-2"
                >
                  <div className="text-sm font-medium text-amber-900">{item.title}</div>
                  <div className="mt-1 flex flex-wrap gap-2 text-[11px] text-amber-700">
                    {item.confidence && <span>置信度: {item.confidence}</span>}
                    {Array.isArray(item.evidence_ids) && item.evidence_ids.length > 0 && (
                      <span>证据: {item.evidence_ids.join(', ')}</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
        {structuredActions.length > 0 && (
          <div>
            <div className="mb-1 text-[11px] font-medium text-slate-500">建议动作</div>
            <div className="space-y-2">
              {structuredActions.map((item, index) => {
                const title = String(item.title || item.action || item.command || `动作 ${index + 1}`).trim();
                return (
                  <div
                    key={`${keyPrefix}-structured-action-${index}`}
                    className="rounded border border-emerald-100 bg-emerald-50/60 p-2"
                  >
                    <div className="text-sm font-medium text-emerald-900">
                      P{Number.isFinite(Number(item.priority)) ? Math.max(1, Math.floor(Number(item.priority))) : index + 1}
                      {' '}
                      {title}
                    </div>
                    {item.command && (
                      <div className="mt-1 rounded border border-emerald-200 bg-white px-2 py-1 font-mono text-[12px] text-emerald-900 break-all">
                        {item.command}
                      </div>
                    )}
                    {item.expected_outcome && (
                      <div className="mt-1 text-[12px] text-emerald-800">预期: {item.expected_outcome}</div>
                    )}
                    {item.reason && (
                      <div className="mt-1 text-[12px] text-emerald-700">原因: {item.reason}</div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}
        {Array.isArray(structuredContent.verification) && structuredContent.verification.length > 0 && (
          <div>
            <div className="mb-1 text-[11px] font-medium text-slate-500">验证</div>
            <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700">
              {structuredContent.verification.map((item, index) => (
                <li key={`${keyPrefix}-structured-verification-${index}`}>{item}</li>
              ))}
            </ul>
          </div>
        )}
        {Array.isArray(structuredContent.rollback) && structuredContent.rollback.length > 0 && (
          <div>
            <div className="mb-1 text-[11px] font-medium text-slate-500">回滚</div>
            <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700">
              {structuredContent.rollback.map((item, index) => (
                <li key={`${keyPrefix}-structured-rollback-${index}`}>{item}</li>
              ))}
            </ul>
          </div>
        )}
        {Array.isArray(structuredContent.missing_evidence) && structuredContent.missing_evidence.length > 0 && (
          <div className="rounded border border-rose-100 bg-rose-50/70 p-2">
            <div className="mb-1 text-[11px] font-medium text-rose-700">仍缺失证据</div>
            <ul className="list-disc space-y-1 pl-5 text-sm text-rose-800">
              {structuredContent.missing_evidence.map((item, index) => (
                <li key={`${keyPrefix}-structured-missing-${index}`}>{item}</li>
              ))}
            </ul>
          </div>
        )}
        {structuredContent.summary && (
          <div className="text-sm text-slate-600 whitespace-pre-wrap">{structuredContent.summary}</div>
        )}
      </div>
    );
  }

  if (options?.streamLoading && looksLikeStructuredStreamContent(normalized)) {
    return <span className="text-slate-400">正在整理结构化回答...</span>;
  }

  const lines = normalized.split('\n');
  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  let blockIndex = 0;

  const pushParagraph = (paragraphLines: string[]) => {
    const paragraph = paragraphLines.join('\n').trim();
    if (!paragraph) {
      return;
    }
    nodes.push(
      <p key={`${keyPrefix}-p-${blockIndex}`} className="whitespace-pre-wrap leading-relaxed text-sm text-slate-700">
        {renderFollowUpInlineRichText(paragraph, `${keyPrefix}-p-${blockIndex}`)}
      </p>,
    );
    blockIndex += 1;
  };

  while (cursor < lines.length) {
    const currentLine = lines[cursor] || '';
    const trimmed = currentLine.trim();

    if (!trimmed) {
      cursor += 1;
      continue;
    }

    if (trimmed.startsWith('```')) {
      const language = trimmed.replace(/^```/, '').trim();
      cursor += 1;
      const codeLines: string[] = [];
      while (cursor < lines.length && !String(lines[cursor] || '').trim().startsWith('```')) {
        codeLines.push(lines[cursor]);
        cursor += 1;
      }
      if (cursor < lines.length && String(lines[cursor] || '').trim().startsWith('```')) {
        cursor += 1;
      }
      nodes.push(
        <div key={`${keyPrefix}-code-${blockIndex}`} className="rounded border border-slate-200 bg-slate-950/95 p-2 text-[12px] text-slate-100">
          {language && <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-400">{language}</div>}
          <pre className="whitespace-pre-wrap break-words">
            <code>{codeLines.join('\n')}</code>
          </pre>
        </div>,
      );
      blockIndex += 1;
      continue;
    }

    const headingMatch = trimmed.match(/^(#{1,3})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      const headingText = headingMatch[2];
      const className = level === 1
        ? 'text-base font-semibold text-slate-900'
        : level === 2
          ? 'text-sm font-semibold text-slate-800'
          : 'text-sm font-medium text-slate-700';
      nodes.push(
        <div key={`${keyPrefix}-h-${blockIndex}`} className={className}>
          {renderFollowUpInlineRichText(headingText, `${keyPrefix}-h-${blockIndex}`)}
        </div>,
      );
      blockIndex += 1;
      cursor += 1;
      continue;
    }

    if (/^\s*[-*]\s+/.test(currentLine)) {
      const items: string[] = [];
      while (cursor < lines.length && /^\s*[-*]\s+/.test(lines[cursor] || '')) {
        items.push(String(lines[cursor] || '').replace(/^\s*[-*]\s+/, '').trim());
        cursor += 1;
      }
      nodes.push(
        <ul key={`${keyPrefix}-ul-${blockIndex}`} className="list-disc space-y-1 pl-5 text-sm text-slate-700">
          {items.map((item, idx) => (
            <li key={`${keyPrefix}-ul-${blockIndex}-${idx}`}>
              {renderFollowUpInlineRichText(item, `${keyPrefix}-ul-${blockIndex}-${idx}`)}
            </li>
          ))}
        </ul>,
      );
      blockIndex += 1;
      continue;
    }

    if (/^\s*\d+\.\s+/.test(currentLine)) {
      const items: string[] = [];
      while (cursor < lines.length && /^\s*\d+\.\s+/.test(lines[cursor] || '')) {
        items.push(String(lines[cursor] || '').replace(/^\s*\d+\.\s+/, '').trim());
        cursor += 1;
      }
      nodes.push(
        <ol key={`${keyPrefix}-ol-${blockIndex}`} className="list-decimal space-y-1 pl-5 text-sm text-slate-700">
          {items.map((item, idx) => (
            <li key={`${keyPrefix}-ol-${blockIndex}-${idx}`}>
              {renderFollowUpInlineRichText(item, `${keyPrefix}-ol-${blockIndex}-${idx}`)}
            </li>
          ))}
        </ol>,
      );
      blockIndex += 1;
      continue;
    }

    if (/^\s*>\s*/.test(currentLine)) {
      const quoteLines: string[] = [];
      while (cursor < lines.length && /^\s*>\s*/.test(lines[cursor] || '')) {
        quoteLines.push(String(lines[cursor] || '').replace(/^\s*>\s*/, '').trim());
        cursor += 1;
      }
      nodes.push(
        <blockquote
          key={`${keyPrefix}-q-${blockIndex}`}
          className="border-l-2 border-slate-300 bg-slate-100/70 px-3 py-1 text-sm text-slate-700"
        >
          {renderFollowUpInlineRichText(quoteLines.join('\n'), `${keyPrefix}-q-${blockIndex}`)}
        </blockquote>,
      );
      blockIndex += 1;
      continue;
    }

    const paragraphLines: string[] = [];
    while (cursor < lines.length) {
      const line = lines[cursor] || '';
      const lineTrimmed = line.trim();
      if (!lineTrimmed) {
        break;
      }
      if (
        lineTrimmed.startsWith('```')
        || /^(#{1,3})\s+/.test(lineTrimmed)
        || /^\s*[-*]\s+/.test(line)
        || /^\s*\d+\.\s+/.test(line)
        || /^\s*>\s*/.test(line)
      ) {
        break;
      }
      paragraphLines.push(line);
      cursor += 1;
    }
    pushParagraph(paragraphLines);
  }

  if (!nodes.length) {
    return <div className="whitespace-pre-wrap text-sm text-slate-700">{content}</div>;
  }
  return <div className="space-y-2">{nodes}</div>;
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

const FOLLOWUP_COMMAND_FENCE_REGEX = /```(?:bash|sh|shell|zsh)?\s*([\s\S]*?)```/gi;
const FOLLOWUP_COMMAND_INLINE_REGEX = /`([^`\n]+)`/g;
const FOLLOWUP_COMMAND_MAX_CANDIDATES = 8;
const FOLLOWUP_COMMAND_EXEC_PREFIX_REGEX = /^(?:\/exec|\/run|执行命令)\s*[:：]?\s*(.+)$/i;
const FOLLOWUP_COMMAND_ALLOWED_HEADS = new Set([
  'kubectl',
  'curl',
  'clickhouse-client',
  'clickhouse',
  'rg',
  'grep',
  'cat',
  'tail',
  'head',
  'awk',
  'jq',
  'ls',
  'echo',
  'pwd',
  'sed',
  'helm',
  'systemctl',
  'service',
]);

const KUBECTL_FLAGS_WITH_VALUE = new Set([
  '-n',
  '--namespace',
  '-c',
  '--container',
  '-o',
  '--output',
  '-l',
  '--selector',
  '--field-selector',
  '--context',
  '--kubeconfig',
  '--cluster',
  '--user',
  '--token',
  '--as',
  '--as-group',
  '--server',
  '--request-timeout',
  '-f',
  '--filename',
  '-k',
  '--kustomize',
]);
const KUBECTL_BOOLEAN_FLAGS = new Set([
  '-A',
  '--all-namespaces',
  '--watch',
  '--watch-only',
  '--ignore-not-found',
  '--no-headers',
  '--show-labels',
  '--recursive',
]);

const consumeKubectlFlag = (tokens: string[], index: number): number => {
  const token = String(tokens[index] || '').trim().toLowerCase();
  if (!token.startsWith('-')) {
    return index;
  }
  if (token.startsWith('--')) {
    if (token.includes('=')) {
      return index + 1;
    }
    if (KUBECTL_BOOLEAN_FLAGS.has(token)) {
      return index + 1;
    }
    return Math.min(tokens.length, index + 2);
  }
  if (KUBECTL_BOOLEAN_FLAGS.has(token)) {
    return index + 1;
  }
  if (KUBECTL_FLAGS_WITH_VALUE.has(token)) {
    return Math.min(tokens.length, index + 2);
  }
  if (token.length > 2 && KUBECTL_FLAGS_WITH_VALUE.has(token.slice(0, 2))) {
    return index + 1;
  }
  return index + 1;
};

const resolveKubectlVerbs = (tokens: string[]): { verb: string; subVerb: string } => {
  let cursor = 1;
  let verb = '';
  while (cursor < tokens.length) {
    const token = String(tokens[cursor] || '').trim().toLowerCase();
    if (!token) {
      cursor += 1;
      continue;
    }
    if (token.startsWith('-')) {
      const nextCursor = consumeKubectlFlag(tokens, cursor);
      cursor = nextCursor > cursor ? nextCursor : cursor + 1;
      continue;
    }
    verb = token;
    cursor += 1;
    break;
  }

  let subVerb = '';
  while (cursor < tokens.length) {
    const token = String(tokens[cursor] || '').trim().toLowerCase();
    if (!token) {
      cursor += 1;
      continue;
    }
    if (token.startsWith('-')) {
      const nextCursor = consumeKubectlFlag(tokens, cursor);
      cursor = nextCursor > cursor ? nextCursor : cursor + 1;
      continue;
    }
    subVerb = token;
    break;
  }
  return { verb, subVerb };
};

const classifyFollowUpCommand = (command: string): ExtractedFollowUpCommand => {
  const normalized = String(command || '').trim();
  const tokens = normalized.split(/\s+/).filter(Boolean);
  const [headRaw = ''] = tokens;
  const head = headRaw.toLowerCase();

  if (head === 'kubectl') {
    const { verb, subVerb } = resolveKubectlVerbs(tokens);
    if (verb === 'rollout') {
      if (['status', 'history'].includes(subVerb)) {
        return { command: normalized, commandType: 'query', riskLevel: 'low' };
      }
      if (['restart', 'undo', 'pause', 'resume'].includes(subVerb)) {
        return { command: normalized, commandType: 'repair', riskLevel: 'high' };
      }
      return { command: normalized, commandType: 'unknown', riskLevel: 'high' };
    }
    if (['get', 'describe', 'logs', 'top', 'events', 'wait', 'version', 'cluster-info', 'explain', 'api-resources', 'api-versions'].includes(verb)) {
      return { command: normalized, commandType: 'query', riskLevel: 'low' };
    }
    if (['apply', 'delete', 'patch', 'edit', 'replace', 'scale', 'set', 'annotate', 'label', 'create', 'expose', 'autoscale', 'cordon', 'uncordon', 'drain', 'taint'].includes(verb)) {
      return { command: normalized, commandType: 'repair', riskLevel: 'high' };
    }
    return { command: normalized, commandType: 'unknown', riskLevel: 'high' };
  }

  if (head === 'curl') {
    let method = 'GET';
    const methodXMatch = normalized.match(/(?:^|\s)-X\s+([A-Za-z]+)/i);
    const methodXInlineMatch = normalized.match(/(?:^|\s)-X([A-Za-z]+)/i);
    const methodRequestMatch = normalized.match(/(?:^|\s)--request\s+([A-Za-z]+)/i);
    const methodRequestInlineMatch = normalized.match(/(?:^|\s)--request=([A-Za-z]+)/i);
    if (methodXMatch?.[1]) {
      method = String(methodXMatch[1]).toUpperCase();
    } else if (methodXInlineMatch?.[1]) {
      method = String(methodXInlineMatch[1]).toUpperCase();
    } else if (methodRequestMatch?.[1]) {
      method = String(methodRequestMatch[1]).toUpperCase();
    } else if (methodRequestInlineMatch?.[1]) {
      method = String(methodRequestInlineMatch[1]).toUpperCase();
    }
    const hasGetQueryFlag = /(?:^|\s)-G(?:\s|$)|(?:^|\s)--get(?:\s|$)/.test(normalized);
    const hasBodyPayload = /(?:^|\s)(?:-d(?:\S+)?|--data(?:\s|=|$)|--data-raw(?:\s|=|$)|--data-binary(?:\s|=|$)|--data-urlencode(?:\s|=|$)|--form(?:\s|=|$)|--json(?:\s|=|$)|-F(?:\S+)?|-T(?:\S+)?|--upload-file(?:\s|=|$))/i.test(normalized);
    if (hasBodyPayload && (method === 'GET' || method === 'HEAD') && !hasGetQueryFlag) {
      method = 'POST';
    }
    if ((method === 'GET' || method === 'HEAD') && (!hasBodyPayload || hasGetQueryFlag)) {
      return { command: normalized, commandType: 'query', riskLevel: 'low' };
    }
    return { command: normalized, commandType: 'repair', riskLevel: 'high' };
  }

  if (head === 'clickhouse-client' || head === 'clickhouse') {
    let queryText = '';
    for (let index = 1; index < tokens.length; index += 1) {
      const token = String(tokens[index] || '');
      const loweredToken = token.toLowerCase();
      if ((loweredToken === '--query' || loweredToken === '-q') && index + 1 < tokens.length) {
        queryText = tokens.slice(index + 1).join(' ');
        break;
      }
      if (loweredToken.startsWith('--query=')) {
        queryText = token.slice(token.indexOf('=') + 1);
        break;
      }
      if (loweredToken.startsWith('-q=') && token.length > 3) {
        queryText = token.slice(3);
        break;
      }
      if (loweredToken.startsWith('-q') && token.length > 2 && loweredToken !== '-query') {
        queryText = token.slice(2);
        break;
      }
    }
    if (!queryText) {
      const queryMatch = normalized.match(/(?:^|\s)(?:--query|-q)\s+(.+)/i);
      queryText = String(queryMatch?.[1] || '').trim();
    }
    const compactQuery = queryText
      .replace(/['"`]/g, '')
      .replace(/\s+/g, '')
      .toLowerCase();
    const mutatingPrefix = ['insert', 'alter', 'create', 'drop', 'truncate', 'optimize', 'system', 'delete', 'update', 'rename', 'grant', 'revoke'];
    const readonlyPrefix = ['select', 'show', 'describe', 'desc', 'explain', 'with'];
    if (!queryText) {
      return { command: normalized, commandType: 'unknown', riskLevel: 'high' };
    }
    if (mutatingPrefix.some((keyword) => compactQuery.startsWith(keyword))) {
      return { command: normalized, commandType: 'repair', riskLevel: 'high' };
    }
    if (readonlyPrefix.some((keyword) => compactQuery.startsWith(keyword))) {
      return { command: normalized, commandType: 'query', riskLevel: 'low' };
    }
    return { command: normalized, commandType: 'unknown', riskLevel: 'high' };
  }

  if (['rg', 'grep', 'cat', 'tail', 'head', 'awk', 'jq', 'ls', 'echo', 'pwd'].includes(head)) {
    return { command: normalized, commandType: 'query', riskLevel: 'low' };
  }

  if (head === 'sed') {
    const tokens = normalized.split(/\s+/);
    const isInplace = tokens.slice(1).some((token) => {
      const lowered = String(token || '').toLowerCase();
      if (!lowered) {
        return false;
      }
      if (lowered === '-i' || lowered.startsWith('-i')) {
        return true;
      }
      if (lowered === '--in-place' || lowered.startsWith('--in-place=')) {
        return true;
      }
      return lowered.startsWith('-') && !lowered.startsWith('--') && lowered.slice(1).includes('i');
    });
    return isInplace
      ? { command: normalized, commandType: 'repair', riskLevel: 'high' }
      : { command: normalized, commandType: 'query', riskLevel: 'low' };
  }

  if (['helm', 'systemctl', 'service'].includes(head)) {
    return { command: normalized, commandType: 'repair', riskLevel: 'high' };
  }

  return { command: normalized, commandType: 'unknown', riskLevel: 'high' };
};

const extractExecutableCommandsFromText = (content: string): ExtractedFollowUpCommand[] => {
  const text = String(content || '');
  if (!text.trim()) {
    return [];
  }

  const commands: ExtractedFollowUpCommand[] = [];
  const seen = new Set<string>();
  const append = (line: string) => {
    const normalized = normalizeExecutableCommand(line);
    if (!normalized || normalized.startsWith('#')) {
      return;
    }
    if (!/^[A-Za-z0-9_.-]+(?:\s+.+)?$/.test(normalized)) {
      return;
    }
    const head = String(normalized.split(/\s+/, 1)[0] || '').toLowerCase();
    if (!FOLLOWUP_COMMAND_ALLOWED_HEADS.has(head)) {
      return;
    }
    const matchKey = normalizeFollowUpCommandMatchKey(normalized);
    if (!matchKey || seen.has(matchKey)) {
      return;
    }
    seen.add(matchKey);
    commands.push(classifyFollowUpCommand(normalized));
  };

  for (const match of text.matchAll(FOLLOWUP_COMMAND_FENCE_REGEX)) {
    const block = String(match[1] || '');
    block.split('\n').forEach((line) => append(line));
    if (commands.length >= FOLLOWUP_COMMAND_MAX_CANDIDATES) {
      return commands.slice(0, FOLLOWUP_COMMAND_MAX_CANDIDATES);
    }
  }

  for (const match of text.matchAll(FOLLOWUP_COMMAND_INLINE_REGEX)) {
    append(String(match[1] || ''));
    if (commands.length >= FOLLOWUP_COMMAND_MAX_CANDIDATES) {
      return commands.slice(0, FOLLOWUP_COMMAND_MAX_CANDIDATES);
    }
  }

  text.split('\n').forEach((line) => {
    const stripped = line.trim();
    if (stripped.startsWith('$') || /^[A-Za-z0-9_.-]+(?:\s+.+)?$/.test(normalizeExecutableCommand(stripped))) {
      append(line);
    }
  });

  return commands.slice(0, FOLLOWUP_COMMAND_MAX_CANDIDATES);
};

const TOPOLOGY_AI_SOURCES = new Set(['topology-node', 'topology-edge']);
const TRACEBACK_HINT_REGEX = /(traceback|exception|caused by:|stack trace|^\s*at\s+\S+\()/im;
const resolveCrossPullWindowMinutes = (): number => {
  const raw = Number((import.meta as LooseAny)?.env?.VITE_CROSS_COMPONENT_PULL_WINDOW_MINUTES ?? 5);
  if (!Number.isFinite(raw)) {
    return 5;
  }
  return Math.max(1, Math.min(30, Math.floor(raw)));
};
const CROSS_COMPONENT_PULL_WINDOW_MINUTES = resolveCrossPullWindowMinutes();
const CROSS_COMPONENT_PULL_LIMIT = 48;
const CROSS_COMPONENT_PULL_MAX_CHARS = 48000;
const FOLLOWUP_CONTEXT_RELATED_LOG_LIMIT = 24;
const FOLLOWUP_AUTO_EXEC_QUERY_MAX_ACTIONS = 3;
const FOLLOWUP_AUTO_EXEC_QUERY_ENABLED = String(
  (import.meta as LooseAny)?.env?.VITE_AI_FOLLOWUP_AUTO_EXEC_QUERY_ENABLED ?? 'true',
).trim().toLowerCase() !== 'false';
const FOLLOWUP_STREAM_ENABLED = String(
  (import.meta as LooseAny)?.env?.VITE_AI_FOLLOWUP_STREAM_ENABLED ?? 'true',
).trim().toLowerCase() !== 'false';
const FOLLOWUP_AGENT_RUNTIME_COMMANDS_ENABLED = String(
  (import.meta as LooseAny)?.env?.VITE_AI_AGENT_RUNTIME_COMMANDS_ENABLED ?? 'true',
).trim().toLowerCase() !== 'false';
const FOLLOWUP_ANALYSIS_RUNTIME_ENABLED = String(
  (import.meta as LooseAny)?.env?.VITE_AI_FOLLOWUP_RUNTIME_ENABLED ?? 'true',
).trim().toLowerCase() !== 'false';
const FOLLOWUP_SHOW_THOUGHT_ENABLED = String(
  (import.meta as LooseAny)?.env?.VITE_AI_FOLLOWUP_SHOW_THOUGHT_ENABLED ?? 'true',
).trim().toLowerCase() !== 'false';

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
  const [followUpCrossLogLoading, setFollowUpCrossLogLoading] = useState(false);
  const [followUpError, setFollowUpError] = useState<string | null>(null);
  const [followUpNotice, setFollowUpNotice] = useState<string>('');
  const [followUpAutoScrollEnabled, setFollowUpAutoScrollEnabled] = useState(true);
  const [followUpHasUnseenUpdate, setFollowUpHasUnseenUpdate] = useState(false);
  const [approvalDialog, setApprovalDialog] = useState<FollowUpApprovalCandidate | null>(null);
  const [approvalDialogSubmitting, setApprovalDialogSubmitting] = useState(false);
  const [contextPills, setContextPills] = useState<Array<{ key: string; value: string }>>([]);
  const [tokenHint, setTokenHint] = useState<{
    warning?: boolean;
    historyCompacted?: boolean;
  }>({});
  const [actionDrafts, setActionDrafts] = useState<Record<string, { action_type: string; title: string; payload: Record<string, unknown> }>>({});
  const [actionLoadingKey, setActionLoadingKey] = useState<string>('');
  const [deletingMessageKey, setDeletingMessageKey] = useState<string>('');
  const [analysisAssistNotice, setAnalysisAssistNotice] = useState<string>('');
  const [crossLogLoading, setCrossLogLoading] = useState(false);
  const [historyHint, setHistoryHint] = useState<string>('');
  const [kbRemoteEnabled, setKbRemoteEnabled] = useState(false);
  const [kbRetrievalMode, setKbRetrievalMode] = useState<'local' | 'hybrid' | 'remote_only'>('local');
  const [kbSaveMode, setKbSaveMode] = useState<'local_only' | 'local_and_remote'>('local_only');
  const [kbRemoteAvailable, setKbRemoteAvailable] = useState(false);
  const [kbEffectiveRetrievalMode, setKbEffectiveRetrievalMode] = useState<'local' | 'hybrid' | 'remote_only'>('local');
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
  const autoExecutedQueryCommandKeysRef = useRef<Set<string>>(new Set());
  const followUpRuntimeSessionsRef = useRef<Record<string, FollowUpAnalysisRuntimeSession>>({});
  const followUpRuntimeControllersRef = useRef<Record<string, AbortController>>({});
  const agentRuntimeCommandSessionsRef = useRef<Record<string, AgentRuntimeCommandSession>>({});
  const agentRuntimeCommandControllersRef = useRef<Record<string, AbortController>>({});
  const resetRuntimeSessionsRef = useRef<() => void>(() => {});
  const useLLMRef = useRef(useLLM);
  const applyHistorySessionToAnalysisRef = useRef<((historySession: NonNullable<LocationState['historySession']>) => void) | null>(null);
  const applyHistoryCaseToAnalysisRef = useRef<((historyCase: NonNullable<LocationState['historyCase']>) => void) | null>(null);
  const handleAnalyzeWithDataRef = useRef<((logData: NonNullable<LocationState['logData']>) => Promise<void>) | null>(null);
  const runTraceAnalysisRef = useRef<((params: { traceId: string; service?: string; useLLM: boolean }) => Promise<AIAnalysisResponse>) | null>(null);
  const buildDefaultContextPillsRef = useRef<((payload?: ContextPillPayload) => void) | null>(null);

  useLLMRef.current = useLLM;

  const normalizeFollowUpActions = (raw: unknown): FollowUpActionPlan[] => {
    if (!Array.isArray(raw)) {
      return [];
    }
    const normalized = raw
      .map((item, index) => {
        if (!item || typeof item !== 'object') {
          return null;
        }
        const payload = item as UnknownObject;
        const title = String(payload.title || '').trim();
        const command = String(payload.command || '').trim();
        const purpose = String(payload.purpose || '').trim();
        if (!title && !command && !purpose) {
          return null;
        }
        const priorityRaw = Number(payload.priority);
        const priority = Number.isFinite(priorityRaw)
          ? Math.max(1, Math.floor(priorityRaw))
          : index + 1;
        const rawCommandType = payload.command_type ? String(payload.command_type).trim().toLowerCase() : '';
        const classified = command ? classifyFollowUpCommand(command) : null;
        const commandType = rawCommandType && rawCommandType !== 'unknown'
          ? rawCommandType
          : (classified?.commandType || (rawCommandType || undefined));
        const riskLevel = payload.risk_level
          ? String(payload.risk_level)
          : (classified?.riskLevel || undefined);
        const requiresWritePermission = payload.requires_write_permission !== undefined
          ? Boolean(payload.requires_write_permission)
          : commandType === 'repair';
        const commandSpec = (
          payload.command_spec && typeof payload.command_spec === 'object' && !Array.isArray(payload.command_spec)
            ? payload.command_spec as Record<string, unknown>
            : undefined
        );
        const executable = payload.executable !== undefined
          ? Boolean(payload.executable)
          : Boolean(command && commandType === 'query');
        return {
          id: payload.id ? String(payload.id) : undefined,
          source: payload.source ? String(payload.source) : undefined,
          priority,
          title: title || undefined,
          purpose: purpose || undefined,
          question: payload.question ? String(payload.question) : undefined,
          action_type: payload.action_type ? String(payload.action_type) : undefined,
          command: command || undefined,
          command_type: commandType,
          risk_level: riskLevel,
          executable,
          requires_confirmation: Boolean(payload.requires_confirmation),
          requires_write_permission: requiresWritePermission,
          requires_elevation: payload.requires_elevation !== undefined
            ? Boolean(payload.requires_elevation)
            : requiresWritePermission,
          reason: payload.reason ? String(payload.reason) : undefined,
          command_spec: commandSpec,
        } as FollowUpActionPlan;
      })
      .filter(Boolean) as FollowUpActionPlan[];
    return normalized.slice(0, 8);
  };

  const normalizeFollowUpMessages = (raw: unknown): FollowUpMessage[] => {
    if (!Array.isArray(raw)) {
      return [];
    }
    return raw
      .map((item: UnknownObject) => {
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
          metadata: (item?.metadata && typeof item.metadata === 'object')
            ? (() => {
                const metadata = item.metadata as UnknownObject;
                return {
                  ...(metadata as Record<string, unknown>),
                  actions: normalizeFollowUpActions(metadata.actions),
                  stream_timeline: normalizeFollowUpThoughtTimeline(
                    metadata.stream_timeline ?? metadata.thoughts,
                  ),
                  thoughts: normalizeFollowUpThoughtTimeline(metadata.thoughts),
                } as FollowUpMessage['metadata'];
              })()
            : {},
        } as FollowUpMessage;
      })
      .filter(Boolean) as FollowUpMessage[];
  };

  const suppressPlanningInFollowUpHistory = (messages: FollowUpMessage[]): FollowUpMessage[] => {
    let assistantSeen = 0;
    return messages.map((message) => {
      if (message.role !== 'assistant') {
        return message;
      }
      assistantSeen += 1;
      if (assistantSeen <= 1) {
        return message;
      }
      const metadata = (message.metadata && typeof message.metadata === 'object')
        ? message.metadata as UnknownObject
        : null;
      if (!metadata) {
        return message;
      }
      const streamTimeline = filterPlanningThoughtTimeline(
        normalizeFollowUpThoughtTimeline(metadata.stream_timeline),
        true,
      );
      const thoughts = filterPlanningThoughtTimeline(
        normalizeFollowUpThoughtTimeline(metadata.thoughts),
        true,
      );
      return {
        ...message,
        metadata: {
          ...(metadata as Record<string, unknown>),
          stream_timeline: streamTimeline,
          thoughts,
        },
      };
    });
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

  const isFollowUpListNearBottom = useCallback((element: HTMLDivElement | null): boolean => {
    if (!element) {
      return true;
    }
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
    return distance <= 48;
  }, []);

  const scrollFollowUpListToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const element = followUpListRef.current;
    if (!element) {
      return;
    }
    element.scrollTo({ top: element.scrollHeight, behavior });
  }, []);

  const handleFollowUpListScroll = useCallback(() => {
    const element = followUpListRef.current;
    if (!element) {
      return;
    }
    const nearBottom = isFollowUpListNearBottom(element);
    setFollowUpAutoScrollEnabled(nearBottom);
    if (nearBottom) {
      setFollowUpHasUnseenUpdate(false);
    }
  }, [isFollowUpListNearBottom]);

  const handleJumpToFollowUpBottom = useCallback(() => {
    setFollowUpAutoScrollEnabled(true);
    setFollowUpHasUnseenUpdate(false);
    scrollFollowUpListToBottom('smooth');
  }, [scrollFollowUpListToBottom]);

  useEffect(() => {
    const element = followUpListRef.current;
    if (!element) {
      return;
    }
    if (followUpAutoScrollEnabled || isFollowUpListNearBottom(element)) {
      scrollFollowUpListToBottom();
      setFollowUpHasUnseenUpdate(false);
      return;
    }
    if (followUpMessages.length > 0) {
      setFollowUpHasUnseenUpdate(true);
    }
  }, [
    followUpMessages,
    followUpLoading,
    followUpAutoScrollEnabled,
    isFollowUpListNearBottom,
    scrollFollowUpListToBottom,
  ]);

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

  const resetFollowUpConversation = useCallback(() => {
    resetRuntimeSessionsRef.current();
    setConversationId('');
    setFollowUpMessages([]);
    setFollowUpQuestion('');
    setFollowUpError(null);
    setFollowUpNotice('');
    setFollowUpAutoScrollEnabled(true);
    setFollowUpHasUnseenUpdate(false);
    setApprovalDialog(null);
    setTokenHint({});
    setActionDrafts({});
    autoExecutedQueryCommandKeysRef.current.clear();
  }, []);

  const parseManualSteps = (raw: string): string[] => {
    return raw
      .split('\n')
      .map((line) => line.replace(/^[-*\d.)\s]+/, '').trim())
      .filter((line) => line.length >= 5);
  };

  const buildVerificationNotesFromDraft = (draft: Record<string, unknown>): string => {
    const summary = String(draft?.analysis_summary || draft?.summary || '').trim();
    const rootCauses = Array.isArray(draft?.root_causes) ? draft.root_causes : [];
    const solutions = Array.isArray(draft?.solutions) ? draft.solutions : [];
    const solutionTitles = solutions
      .map((item: UnknownObject) => String(item?.title || '').trim())
      .filter((item: string) => item.length > 0)
      .slice(0, 3);

    const lines: string[] = [];
    if (summary) {
      lines.push(`会话总结：${summary}`);
    }
    if (rootCauses.length > 0) {
      lines.push(`根因要点：${rootCauses.slice(0, 5).map((item: UnknownObject) => String(item)).join('；')}`);
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
      retrievalMode?: 'local' | 'hybrid' | 'remote_only';
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
    } catch (err: unknown) {
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
    } catch (err: unknown) {
      setKbRuntimeNotice(getErrorMessage(err, '联合知识检索失败，已回退本地流程'));
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
    } catch (err: unknown) {
      setKbActionNotice(getErrorMessage(err, '提取知识草稿失败'));
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
    } catch (err: unknown) {
      setKbActionNotice(getErrorMessage(err, '提交知识库失败'));
    } finally {
      setKbSubmitLoading(false);
    }
  };

  const restoreFollowUpMessagesFromCase = (historyCase: NonNullable<LocationState['historyCase']>): FollowUpMessage[] => {
    return normalizeFollowUpMessages(historyCase?.llm_metadata?.follow_up_messages);
  };

  const restoreFollowUpMessagesFromSession = (messages: unknown): FollowUpMessage[] => {
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
        setFollowUpMessages(suppressPlanningInFollowUpHistory(restoredMessages));
      }
      const restoredPills = Array.isArray(detail?.context_pills) ? detail.context_pills : [];
      if (restoredPills.length > 0) {
        setContextPills(restoredPills);
      }
      const maybeConversationId = String((detail?.context as UnknownObject)?.conversation_id || '').trim();
      if (maybeConversationId) {
        setConversationId((prev) => prev || maybeConversationId);
      }
      const lastAssistant = [...restoredMessages].reverse().find((msg) => msg.role === 'assistant');
      const metadata = lastAssistant?.metadata || {};
      if (lastAssistant) {
        setTokenHint({
          warning: Boolean((metadata as UnknownObject)?.token_warning),
          historyCompacted: Boolean((metadata as UnknownObject)?.history_compacted),
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
    setFollowUpMessages(suppressPlanningInFollowUpHistory(normalizedHistoryMessages));
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
  applyHistorySessionToAnalysisRef.current = applyHistorySessionToAnalysis;

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
    setFollowUpMessages(suppressPlanningInFollowUpHistory(restoredFollowUps));
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
      warning: Boolean((restoredMetadata as UnknownObject)?.token_warning),
      historyCompacted: Boolean((restoredMetadata as UnknownObject)?.history_compacted),
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
  applyHistoryCaseToAnalysisRef.current = applyHistoryCaseToAnalysis;

  const handleOpenHistorySession = async (sessionId: string) => {
    try {
      setIsLoading(true);
      setError(null);
      const detail = await api.getAIHistoryDetail(sessionId);
      applyHistorySessionToAnalysis(detail as NonNullable<LocationState['historySession']>);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '加载历史记录详情失败'));
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

  const buildClientTimezoneContext = (): Record<string, unknown> => {
    const timezoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    return {
      input_timezone: timezoneName || undefined,
      client_timezone_offset_minutes: -new Date().getTimezoneOffset(),
    };
  };

  const runLogAnalysis = async (params: {
    logContent: string;
    service?: string;
    context?: Record<string, unknown>;
    useLLM: boolean;
  }): Promise<AIAnalysisResponse> => {
    const { logContent, service, context, useLLM } = params;
    const response = await api.analyzeLogLLM({
      log_content: logContent,
      service_name: service || '',
      context: {
        ...buildClientTimezoneContext(),
        ...(context || {}),
      },
      use_llm: useLLM,
      enable_agent: true,
      enable_web_search: false,
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
  runTraceAnalysisRef.current = runTraceAnalysis;

  const extractTraceIdFromRecord = (record?: Record<string, unknown> | null): string => {
    if (!record || typeof record !== 'object') {
      return '';
    }
    const keys = [
      'trace_id',
      'trace.id',
      'traceId',
      'trace-id',
      'otel.trace_id',
    ];

    for (const key of keys) {
      const direct = record[key];
      if (direct !== undefined && direct !== null) {
        const normalized = String(direct).trim();
        if (normalized) {
          return normalized;
        }
      }
      if (key.includes('.')) {
        const nested = resolveDotPathValue(record, key);
        if (nested !== undefined && nested !== null) {
          const normalized = String(nested).trim();
          if (normalized) {
            return normalized;
          }
        }
      }
    }
    return '';
  };

  const extractTraceId = (value: string): string => {
    const text = (value || '').trim();
    if (!text) return '';

    if (text.startsWith('{')) {
      try {
        const parsed = JSON.parse(text);
        const parsedTraceId = extractTraceIdFromRecord(parsed);
        if (parsedTraceId) {
          return parsedTraceId;
        }
      } catch {
        // ignore JSON parse errors and fallback to raw text detection
      }
    }

    const inlineMatch = text.match(/(?:trace[_-]?id|trace\.id)\s*[:=]\s*([a-zA-Z0-9_-]{8,})/i);
    if (inlineMatch?.[1]) {
      return inlineMatch[1].trim();
    }

    if (/^[a-zA-Z0-9_-]{8,}$/.test(text)) {
      return text;
    }

    return '';
  };

  const resolveDotPathValue = (input: Record<string, unknown>, path: string): unknown => {
    const segments = String(path || '').split('.').filter(Boolean);
    let current: unknown = input;
    for (const segment of segments) {
      if (!current || typeof current !== 'object') {
        return undefined;
      }
      current = asObject(current)[segment];
    }
    return current;
  };

  const extractRequestIdFromRecord = (record?: Record<string, unknown> | null): string => {
    if (!record || typeof record !== 'object') {
      return '';
    }
    const keys = [
      'request_id',
      'request.id',
      'requestId',
      'req_id',
      'x-request-id',
      'x_request_id',
      'http.request_id',
      'trace.request_id',
    ];

    for (const key of keys) {
      const direct = record[key];
      if (direct !== undefined && direct !== null) {
        const normalized = String(direct).trim();
        if (normalized) {
          return normalized;
        }
      }
      if (key.includes('.')) {
        const nested = resolveDotPathValue(record, key);
        if (nested !== undefined && nested !== null) {
          const normalized = String(nested).trim();
          if (normalized) {
            return normalized;
          }
        }
      }
    }
    return '';
  };

  const extractRequestId = (value: string): string => {
    const text = String(value || '').trim();
    if (!text) {
      return '';
    }

    if (text.startsWith('{')) {
      try {
        const parsed = JSON.parse(text);
        const parsedRequestId = extractRequestIdFromRecord(parsed);
        if (parsedRequestId) {
          return parsedRequestId;
        }
      } catch {
        // ignore JSON parse errors and continue with text regex extraction
      }
    }

    const explicitMatch = text.match(
      /(?:request[_-]?id|req[_-]?id|x-request-id)\s*[:=]\s*([a-zA-Z0-9._:-]{6,})/i,
    );
    if (explicitMatch?.[1]) {
      return explicitMatch[1].trim();
    }

    const reqPrefixMatch = text.match(/\b(req-[a-zA-Z0-9._:-]{3,})\b/i);
    if (reqPrefixMatch?.[1]) {
      return reqPrefixMatch[1].trim();
    }

    return '';
  };

  const extractTimestampFromInput = (value: string): string => {
    const text = String(value || '').trim();
    if (!text) {
      return '';
    }
    const timestampMatch = text.match(
      /\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?/,
    );
    if (!timestampMatch?.[0]) {
      return '';
    }
    const candidate = timestampMatch[0].replace(' ', 'T');
    const parsed = new Date(candidate);
    if (Number.isNaN(parsed.getTime())) {
      return '';
    }
    return parsed.toISOString();
  };

  const resolveAnchorTimestampForCrossPull = (): string => {
    const sourceAttrs = asObject(sourceLogData?.attributes);
    const candidates = [
      String(sourceLogData?.timestamp || '').trim(),
      String(sourceAttrs.timestamp || '').trim(),
      extractTimestampFromInput(inputText),
    ];
    for (const candidate of candidates) {
      if (!candidate) {
        continue;
      }
      const parsed = new Date(candidate);
      if (!Number.isNaN(parsed.getTime())) {
        return parsed.toISOString();
      }
    }
    return new Date().toISOString();
  };

  const buildCrossPullTimeWindow = (anchorIso: string): { start_time: string; end_time: string } => {
    const center = new Date(anchorIso).getTime();
    if (Number.isNaN(center)) {
      const now = Date.now();
      return {
        start_time: new Date(now - CROSS_COMPONENT_PULL_WINDOW_MINUTES * 60 * 1000).toISOString(),
        end_time: new Date(now + CROSS_COMPONENT_PULL_WINDOW_MINUTES * 60 * 1000).toISOString(),
      };
    }
    return {
      start_time: new Date(center - CROSS_COMPONENT_PULL_WINDOW_MINUTES * 60 * 1000).toISOString(),
      end_time: new Date(center + CROSS_COMPONENT_PULL_WINDOW_MINUTES * 60 * 1000).toISOString(),
    };
  };

  const compactTracebackForDraft = (rawMessage: string): string => {
    const text = String(rawMessage || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
    if (!text) {
      return '';
    }

    if (!TRACEBACK_HINT_REGEX.test(text)) {
      return text.replace(/\s+/g, ' ').trim().slice(0, 420);
    }

    const lines = text.split('\n').map((line) => line.trimEnd()).filter((line) => line.trim().length > 0);
    if (!lines.length) {
      return '';
    }
    const head = lines.slice(0, 14);
    const tail = lines.slice(-24);
    const combined = lines.length > 40
      ? [...head, '...<truncated traceback>...', ...tail]
      : lines;
    const result = combined.join('\n').trim();
    if (result.length <= 2200) {
      return result;
    }
    return `${result.slice(0, 2150).trimEnd()}\n...<truncated>...`;
  };

  const dedupeCrossPullEvents = (events: Event[]): Event[] => {
    const deduped: Event[] = [];
    const seen = new Set<string>();
    for (const event of events) {
      const key = String(event.id || '').trim()
        || `${String(event.timestamp || '').trim()}|${String(event.service_name || '').trim()}|${String(event.level || '').trim()}|${String(event.message || '').trim()}`;
      if (!key || seen.has(key)) {
        continue;
      }
      seen.add(key);
      deduped.push(event);
    }
    return deduped;
  };

  const prioritizeCrossPullEvents = (events: Event[]): Event[] => {
    const sorted = [...events].sort((a, b) => {
      const left = new Date(String(a.timestamp || '')).getTime();
      const right = new Date(String(b.timestamp || '')).getTime();
      return left - right;
    });
    const tracebackErrors = sorted.filter((event) => {
      const level = String(event.level || '').toUpperCase();
      return ['ERROR', 'FATAL'].includes(level) && TRACEBACK_HINT_REGEX.test(String(event.message || ''));
    });
    const otherErrors = sorted.filter((event) => {
      const level = String(event.level || '').toUpperCase();
      return ['ERROR', 'FATAL', 'WARN', 'WARNING'].includes(level) && !TRACEBACK_HINT_REGEX.test(String(event.message || ''));
    });
    const others = sorted.filter((event) => {
      const level = String(event.level || '').toUpperCase();
      return !['ERROR', 'FATAL', 'WARN', 'WARNING'].includes(level);
    });
    return [...tracebackErrors, ...otherErrors, ...others].slice(0, CROSS_COMPONENT_PULL_LIMIT);
  };

  const formatCrossPullEvent = (event: Event): string => {
    const timestampRaw = String(event.timestamp || '').trim();
    const parsed = timestampRaw ? new Date(timestampRaw) : new Date();
    const timestamp = Number.isNaN(parsed.getTime()) ? timestampRaw : parsed.toISOString();
    const level = String(event.level || 'INFO').toUpperCase();
    const service = String(event.service_name || 'unknown');
    const message = compactTracebackForDraft(String(event.message || ''));
    if (!message) {
      return '';
    }
    if (message.includes('\n')) {
      return `[${timestamp}] [${level}] [${service}]\n${message}`;
    }
    return `[${timestamp}] [${level}] [${service}] ${message}`;
  };

  const buildCrossPullDraftText = (
    baseText: string,
    payload: CrossPullResult,
  ): string => {
    const {
      requestId,
      traceId,
      sourceService,
      targetService,
      anchorIso,
      startTime,
      endTime,
      selectedEvents,
    } = payload;
    const logLines = selectedEvents.map((event) => formatCrossPullEvent(event)).filter(Boolean);
    const timezoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    const draftSections = [
      String(baseText || '').trim(),
      '',
      '[cross-component-related-logs]',
      `timezone=${timezoneName}`,
      `anchor_utc=${anchorIso}`,
      `window_utc=${startTime} ~ ${endTime}`,
      requestId ? `request_id=${requestId}` : '',
      traceId ? `trace_id=${traceId}` : '',
      sourceService ? `source_service=${sourceService}` : '',
      targetService ? `target_service=${targetService}` : '',
      `selected_count=${logLines.length}`,
      '',
      ...logLines,
    ].filter((line) => Boolean(String(line).trim()));
    let draft = draftSections.join('\n').trim();
    if (draft.length > CROSS_COMPONENT_PULL_MAX_CHARS) {
      draft = `${draft.slice(0, CROSS_COMPONENT_PULL_MAX_CHARS - 22)}\n...<ui-truncated>...`;
    }
    return draft;
  };

  const normalizeEventForFollowUpContext = (event: Event): Record<string, unknown> => {
    const attrs = asObject(event.attributes);
    return {
      id: String(event.id || ''),
      timestamp: String(event.timestamp || ''),
      service_name: String(event.service_name || ''),
      level: String(event.level || ''),
      message: compactTracebackForDraft(String(event.message || '')),
      trace_id: String(event.trace_id || ''),
      request_id: extractRequestIdFromRecord(attrs) || extractRequestId(String(event.message || '')),
      pod_name: String(event.pod_name || ''),
      namespace: String(event.namespace || ''),
    };
  };

  const fetchCrossComponentLogs = async (baseText: string): Promise<CrossPullResult> => {
    const sourceAttrs = asObject(sourceLogData?.attributes);
    const requestId = extractRequestId(baseText)
      || extractRequestId(inputText)
      || extractRequestIdFromRecord(sourceAttrs)
      || extractRequestIdFromRecord(asObject(sourceAttrs.log_meta));
    const traceId = extractTraceId(baseText)
      || extractTraceId(inputText)
      || extractTraceIdFromRecord(sourceAttrs)
      || String(sourceLogData?.trace_id || '').trim();
    const sourceService = String(
      sourceAttrs.source_service
      || sourceAttrs.sourceService
      || sourceLogData?.service_name
      || serviceName
      || '',
    ).trim();
    const targetService = String(sourceAttrs.target_service || sourceAttrs.targetService || '').trim();

    const anchorIso = resolveAnchorTimestampForCrossPull();
    const { start_time, end_time } = buildCrossPullTimeWindow(anchorIso);
    const queryTasks: Array<{ label: string; task: Promise<{ events: Event[] }> }> = [];

    if (requestId) {
      queryTasks.push({
        label: `request_id=${requestId}`,
        task: api.getEvents({
          request_id: requestId,
          start_time,
          end_time,
          limit: 240,
          exclude_health_check: true,
        }),
      });
    }
    if (traceId) {
      queryTasks.push({
        label: `trace_id=${traceId}`,
        task: api.getEvents({
          trace_id: traceId,
          start_time,
          end_time,
          limit: 240,
          exclude_health_check: true,
        }),
      });
    }
    if (sourceService && targetService) {
      queryTasks.push({
        label: `${sourceService}->${targetService}`,
        task: api.getEvents({
          source_service: sourceService,
          target_service: targetService,
          start_time,
          end_time,
          limit: 180,
          exclude_health_check: true,
        }),
      });
    }
    if (!queryTasks.length) {
      const fallbackService = String(serviceName || sourceService || '').trim();
      if (fallbackService) {
        queryTasks.push({
          label: `service_name=${fallbackService}`,
          task: api.getEvents({
            service_name: fallbackService,
            level: 'ERROR',
            start_time,
            end_time,
            limit: 120,
            exclude_health_check: true,
          }),
        });
      } else {
        queryTasks.push({
          label: 'window_error_scan',
          task: api.getEvents({
            level: 'ERROR',
            start_time,
            end_time,
            limit: 120,
            exclude_health_check: true,
          }),
        });
      }
    }

    const settled = await Promise.allSettled(queryTasks.map((item) => item.task));
    const fetched: Event[] = [];
    const failedReasons: string[] = [];
    let succeededTaskCount = 0;
    settled.forEach((item, index) => {
      if (item.status === 'fulfilled' && Array.isArray(item.value?.events)) {
        succeededTaskCount += 1;
        fetched.push(...item.value.events);
        return;
      }
      if (item.status === 'rejected') {
        const label = queryTasks[index]?.label || `query-${index + 1}`;
        failedReasons.push(parseCrossPullTaskError(label, item.reason));
      }
    });

    const deduped = dedupeCrossPullEvents(fetched);
    const selectedEvents = prioritizeCrossPullEvents(deduped);
    if (!selectedEvents.length) {
      if (!succeededTaskCount && failedReasons.length > 0) {
        throw new Error(`横向查询全部失败：${failedReasons.slice(0, 3).join('；')}`);
      }
      if (failedReasons.length > 0) {
        throw new Error(`在当前时间窗口内未检索到关联日志（部分查询失败：${failedReasons.slice(0, 2).join('；')}）`);
      }
      throw new Error('在当前时间窗口内未检索到关联日志');
    }

    return {
      requestId,
      traceId,
      sourceService,
      targetService,
      anchorIso,
      startTime: start_time,
      endTime: end_time,
      selectedEvents,
      failedReasons,
    };
  };

  const handlePullCrossComponentLogs = async () => {
    if (analysisType !== 'log') {
      return;
    }
    setCrossLogLoading(true);
    setError(null);
    setAnalysisAssistNotice('');

    try {
      const crossPull = await fetchCrossComponentLogs(inputText);
      const nextInput = buildCrossPullDraftText(String(inputText || '').trim(), crossPull);
      setInputText(nextInput);
      const partialFailureNotice = crossPull.failedReasons.length > 0
        ? `（${crossPull.failedReasons.length} 路查询失败，已使用可用结果）`
        : '';
      setAnalysisAssistNotice(`已拉取 ${crossPull.selectedEvents.length} 条横向日志到输入框，可编辑后点击“开始分析”。${partialFailureNotice}`);
    } catch (err: unknown) {
      setError(getErrorMessage(err, '横向拉取日志失败'));
    } finally {
      setCrossLogLoading(false);
    }
  };

  const handleInjectCrossLogsToFollowUpDraft = async () => {
    if (analysisType !== 'log') {
      setFollowUpError('仅日志分析模式支持对话框横向日志拉取');
      return;
    }
    setFollowUpCrossLogLoading(true);
    setFollowUpError(null);
    try {
      const baseQuestion = String(followUpQuestion || '').trim();
      const crossPull = await fetchCrossComponentLogs(`${baseQuestion}\n${inputText}`);
      const draftPrefix = baseQuestion || '请基于以下关联日志继续分析请求流程、根因和修复步骤：';
      const followUpDraft = buildCrossPullDraftText(draftPrefix, crossPull);
      setFollowUpQuestion(followUpDraft);
      const partialFailureNotice = crossPull.failedReasons.length > 0
        ? `（${crossPull.failedReasons.length} 路查询失败，已使用可用结果）`
        : '';
      showFollowUpNotice(`已注入 ${crossPull.selectedEvents.length} 条关联日志到追问草稿。${partialFailureNotice}`, 2400);
    } catch (err: unknown) {
      setFollowUpError(getErrorMessage(err, '追问草稿注入关联日志失败'));
    } finally {
      setFollowUpCrossLogLoading(false);
    }
  };

  const handleSendFollowUpWithCrossLogs = async () => {
    if (analysisType !== 'log') {
      setFollowUpError('仅日志分析模式支持“查日志并发送”');
      return;
    }
    if (!followUpQuestion.trim()) {
      setFollowUpError('请先输入追问问题，再执行“查日志并发送”');
      return;
    }
    if (followUpLoading) {
      return;
    }
    setFollowUpCrossLogLoading(true);
    setFollowUpError(null);
    try {
      const crossPull = await fetchCrossComponentLogs(`${followUpQuestion}\n${inputText}`);
      const relatedLogs = crossPull.selectedEvents
        .slice(0, FOLLOWUP_CONTEXT_RELATED_LOG_LIMIT)
        .map((event) => normalizeEventForFollowUpContext(event));
      await handleSubmitFollowUp({
        followupRelatedLogs: relatedLogs,
        followupRelatedLogCount: crossPull.selectedEvents.length,
        followupRelatedMeta: {
          followup_related_anchor_utc: crossPull.anchorIso,
          followup_related_start_time: crossPull.startTime,
          followup_related_end_time: crossPull.endTime,
          followup_related_request_id: crossPull.requestId,
          followup_related_trace_id: crossPull.traceId,
        },
      });
      const partialFailureNotice = crossPull.failedReasons.length > 0
        ? `（${crossPull.failedReasons.length} 路查询失败，已使用可用结果）`
        : '';
      showFollowUpNotice(`已附带 ${relatedLogs.length} 条关联日志并发送追问。${partialFailureNotice}`, 2400);
    } catch (err: unknown) {
      setFollowUpError(getErrorMessage(err, '查日志并发送失败'));
    } finally {
      setFollowUpCrossLogLoading(false);
    }
  };

  const isTopologySyntheticPayload = (logData?: LocationState['logData'] | null): boolean => {
    const source = String(logData?.attributes?.source || '').toLowerCase();
    return TOPOLOGY_AI_SOURCES.has(source);
  };

  const formatEventForAnalysis = (event: EventTextLike): string => {
    const rawMessage = String(event?.message || '').replace(/\s+/g, ' ').trim();
    if (!rawMessage) {
      return '';
    }
    const timestampRaw = String(event?.timestamp || '').trim();
    const timestamp = timestampRaw ? new Date(timestampRaw).toISOString() : new Date().toISOString();
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

    const levelCounter = selected.reduce<Record<string, number>>((acc, item) => {
      const level = String(item?.level || 'UNKNOWN').toUpperCase();
      acc[level] = (acc[level] || 0) + 1;
      return acc;
    }, {});

    const topPatterns = selected
      .reduce<Map<string, number>>((acc, item) => {
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

    const rawLogs = selected.map((item) => ({
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

  const buildLogAnalysisInput = (
    logData: NonNullable<LocationState['logData']>,
  ): { logContent: string; context: Record<string, unknown> } => {
    const baseMessage = String(logData.message || '').trim();
    const baseContext: Record<string, unknown> = { ...(logData.attributes || {}) };

    if (!isTopologySyntheticPayload(logData)) {
      return { logContent: baseMessage, context: baseContext };
    }
    const sourceService = String(baseContext.source_service || logData.service_name || '').trim();
    const targetService = String(baseContext.target_service || '').trim();
    return {
      logContent: baseMessage,
      context: {
        ...baseContext,
        source_service: sourceService || undefined,
        target_service: targetService || undefined,
        topology_time_window: String(baseContext.time_window || ''),
        topology_synthetic: true,
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
        applyHistorySessionToAnalysisRef.current?.(state.historySession);
      } else if (state.historyCase) {
        applyHistoryCaseToAnalysisRef.current?.(state.historyCase);
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
          void handleAnalyzeWithDataRef.current?.(state.logData);
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
              const runTraceAnalysis = runTraceAnalysisRef.current;
              if (!runTraceAnalysis) {
                return;
              }
              const response = await runTraceAnalysis({
                traceId: state.traceId as string,
                service: state.serviceName || undefined,
                useLLM: useLLMRef.current,
              });
              setResult(response);
              buildDefaultContextPillsRef.current?.({
                result: response,
                sessionId: String(response.session_id || ''),
                service: state.serviceName || '',
                traceId: state.traceId as string,
                input: state.traceId as string,
                type: 'trace',
              });
            } catch (err: unknown) {
              setError(parseAnalyzeErrorMessage(err, { useLLM: useLLMRef.current }));
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
  }, [location.state, resetFollowUpConversation]);

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
      const prepared = buildLogAnalysisInput(logData);
      const finalLogContent = prepared.logContent || logData.message;
      const mergedContext: Record<string, unknown> = {
        ...(prepared.context || {}),
        source_log_id: String(logData.id || ''),
        source_log_timestamp: String(logData.timestamp || ''),
        source_service_name: String(logData.service_name || ''),
        source_trace_id: String(logData.trace_id || ''),
        source_request_id: extractRequestIdFromRecord(logData.attributes) || '',
        agent_mode: 'request_flow',
      };
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
    } catch (err: unknown) {
      setError(parseAnalyzeErrorMessage(err, { useLLM }));
    } finally {
      setIsLoading(false);
    }
  };
  handleAnalyzeWithDataRef.current = handleAnalyzeWithData;

  const handleLoadServiceErrorSnapshot = async () => {
    setServiceErrorSnapshotError(null);
    setLoadingServiceErrorSnapshot(true);
    try {
      const snapshot = await buildServiceErrorSnapshot(serviceName);
      setServiceErrorSnapshot(snapshot);
      setServiceErrorSnapshotLoadedAt(new Date().toISOString());
    } catch (err: unknown) {
      setServiceErrorSnapshot(null);
      setServiceErrorSnapshotLoadedAt('');
      setServiceErrorSnapshotError(getErrorMessage(err, '加载服务报错信息失败'));
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
    } catch (err: unknown) {
      setError(parseAnalyzeErrorMessage(err, { useLLM }));
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
        const preparedInput = inputText;
        const mergedContext: Record<string, unknown> = {
          ...(snapshotContext || {}),
          source_log_timestamp: String(sourceLogData?.timestamp || ''),
          source_service_name: String(sourceLogData?.service_name || serviceName || ''),
          source_trace_id: String(sourceLogData?.trace_id || ''),
          source_request_id: extractRequestId(inputText),
          agent_mode: 'request_flow',
        };
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
    } catch (err: unknown) {
      setError(parseAnalyzeErrorMessage(err, { useLLM }));
    } finally {
      setIsLoading(false);
    }
  };

  const handleFindSimilarCases = async (
    logContent: string,
    problemType?: string,
    serviceNameOverride?: string,
    context?: Record<string, unknown>,
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
    } catch (err: unknown) {
      console.error('Failed to load similar case detail:', err);
      setError(getErrorMessage(err, '加载相似知识库详情失败'));
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
      const saveContext: Record<string, unknown> = {};
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
                subgoals: Array.isArray(msg.metadata.subgoals) ? msg.metadata.subgoals : [],
                reflection: (msg.metadata.reflection && typeof msg.metadata.reflection === 'object')
                  ? msg.metadata.reflection
                  : {},
                actions: Array.isArray(msg.metadata.actions) ? normalizeFollowUpActions(msg.metadata.actions) : [],
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

  const normalizeCommandMatchKey = (command: string): string =>
    normalizeFollowUpCommandMatchKey(command);

  const mergeFollowUpMetadataItems = (baseItems: unknown, streamedItems: unknown): Array<Record<string, unknown>> => {
    const merged = [
      ...(Array.isArray(baseItems) ? baseItems : []),
      ...(Array.isArray(streamedItems) ? streamedItems : []),
    ];
    const deduped: Array<Record<string, unknown>> = [];
    const seen = new Set<string>();
    merged.forEach((item) => {
      if (!item || typeof item !== 'object') {
        return;
      }
      const payload = asObject(item) as Record<string, unknown>;
      const actionId = String(payload.action_id || '').trim();
      const commandKey = normalizeCommandMatchKey(String(payload.command || ''));
      const status = String(payload.status || '').trim().toLowerCase();
      const streamEvent = String(payload.stream_event || '').trim().toLowerCase();
      const stream = String(payload.stream || '').trim().toLowerCase();
      const message = String(payload.text || payload.message || payload.detail || '').trim();
      const dedupeKey = [
        actionId,
        commandKey,
        status,
        streamEvent,
        stream,
        message,
      ].join('::');
      if (seen.has(dedupeKey)) {
        return;
      }
      seen.add(dedupeKey);
      deduped.push(payload);
    });
    return deduped;
  };

  const parseCommandExecutionQuestion = (question: string): string => {
    const text = String(question || '').trim();
    if (!text) {
      return '';
    }
    const match = text.match(FOLLOWUP_COMMAND_EXEC_PREFIX_REGEX);
    if (!match?.[1]) {
      return '';
    }
    return normalizeExecutableCommand(match[1]);
  };

  const formatCommandExecutionMessage = (
    payload: Record<string, unknown>,
    fallbackCommand: string,
  ): string => {
    const status = String(payload.status || 'unknown');
    const command = String(payload.command || fallbackCommand || '').trim();
    const message = String(payload.message || '').trim();
    const stdout = String(payload.stdout || '').trim();
    const stderr = String(payload.stderr || '').trim();
    const exitCode = Number(payload.exit_code);
    const durationMs = Number(payload.duration_ms);
    const outputTruncated = Boolean(payload.output_truncated);
    const lines: string[] = [
      `命令执行状态: ${status}`,
      command ? `command: ${command}` : '',
      Number.isFinite(exitCode) ? `exit_code: ${exitCode}` : '',
      Number.isFinite(durationMs) ? `duration_ms: ${durationMs}` : '',
      message ? `message: ${message}` : '',
    ].filter(Boolean);

    if (stdout) {
      lines.push(`stdout:\n${stdout}`);
    }
    if (stderr) {
      lines.push(`stderr:\n${stderr}`);
    }
    if (outputTruncated) {
      lines.push('note: 输出较长，已截断');
    }
    return lines.join('\n').trim();
  };

  const executeFollowUpCommandWithRecovery = useCallback(async (
    sessionId: string,
    messageId: string,
    params: {
      command: string;
      purpose?: string;
      title?: string;
      confirmed?: boolean;
      elevated?: boolean;
      confirmation_ticket?: string;
      timeout_seconds?: number;
      command_spec?: Record<string, unknown>;
    },
  ): Promise<Record<string, unknown>> => {
    const buildPayload = (override?: {
      command?: string;
      commandSpec?: UnknownObject;
    }) => ({
      ...params,
      command: String(override?.command || params.command || '').trim(),
      command_spec: override?.commandSpec || params.command_spec,
    });
    let response = await api.executeFollowUpCommand(
      sessionId,
      messageId,
      buildPayload(),
    ) as Record<string, unknown>;
    const status = String(response.status || '').trim().toLowerCase();
    if (status !== 'blocked' && status !== 'waiting_user_input') {
      return response;
    }
    const recoveryPayload = asObject(asObject(response.error).recovery || response.recovery);
    const recovery = parseCommandSpecRecovery(recoveryPayload);
    if (!recovery.suggestedCommandSpec) {
      return response;
    }
    response = await api.executeFollowUpCommand(
      sessionId,
      messageId,
      buildPayload({
        command: recovery.suggestedCommand || params.command,
        commandSpec: recovery.suggestedCommandSpec,
      }),
    ) as Record<string, unknown>;
    return response;
  }, []);

  const appendFollowUpAssistantMessage = useCallback(
    (content: string, metadataExtra?: Record<string, unknown>) => {
      const normalized = String(content || '').trim();
      if (!normalized) {
        return;
      }
      setFollowUpMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: normalized,
          timestamp: new Date().toISOString(),
          metadata: {
            command_execution: true,
            ...(metadataExtra || {}),
          },
        },
      ]);
    },
    [],
  );

  const upsertFollowUpAssistantMessage = useCallback((message: FollowUpMessage) => {
    const messageId = String(message.message_id || '').trim();
    if (!messageId) {
      return;
    }
    setFollowUpMessages((prev) => {
      const index = prev.findIndex((item) => String(item.message_id || '').trim() === messageId);
      if (index < 0) {
        return [...prev, message];
      }
      const next = [...prev];
      const current = next[index];
      next[index] = {
        ...current,
        ...message,
        metadata: {
          ...((current?.metadata && typeof current.metadata === 'object') ? current.metadata : {}),
          ...((message.metadata && typeof message.metadata === 'object') ? message.metadata : {}),
        },
      };
      return next;
    });
  }, []);

  const removeFollowUpMessageById = useCallback((messageId: string) => {
    const normalized = String(messageId || '').trim();
    if (!normalized) {
      return;
    }
    setFollowUpMessages((prev) => prev.filter((item) => String(item.message_id || '').trim() !== normalized));
  }, []);

  const isAgentRuntimeUnavailableError = useCallback((error: unknown): boolean => {
    const payload = asObject(error);
    const response = asObject(payload.response);
    const status = Number(response.status);
    const message = getErrorMessage(error, '').toLowerCase();
    return (
      status === 404
      || status === 501
      || status === 503
      || message.includes('run not found')
      || message.includes('network error')
      || message.includes('failed to fetch')
      || message.includes('ai runtime stream')
    );
  }, []);

  const syncFollowUpRuntimeMessage = useCallback((runId: string) => {
    const session = followUpRuntimeSessionsRef.current[runId];
    if (!session) {
      return;
    }
    const thoughtTimeline = buildRuntimeThoughtTimeline({
      runtimeState: session.state,
      truncateText: truncateFollowUpThoughtText,
      normalizePhase: normalizeFollowUpThoughtPhase,
      normalizeTimeline: normalizeFollowUpThoughtTimeline,
      maxItems: FOLLOWUP_THOUGHT_RENDER_MAX,
    });
    upsertFollowUpAssistantMessage(buildRuntimeAnalysisFollowUpMessage({
      session,
      thoughtTimeline,
    }));
  }, [upsertFollowUpAssistantMessage]);

  const ensureFollowUpRuntimeSession = useCallback(async (params: {
    runId: string;
    title?: string;
    sourceMessageId?: string;
  }): Promise<FollowUpAnalysisRuntimeSession> => {
    const runId = String(params.runId || '').trim();
    if (!runId) {
      throw new Error('runtime run id is required');
    }
    const existing = followUpRuntimeSessionsRef.current[runId];
    if (existing) {
      return existing;
    }

    const snapshotResponse = await api.getAIRun(runId);
    let nextState = agentRunReducer(createInitialAgentRunState(), {
      type: 'hydrate_snapshot',
      payload: { run: snapshotResponse.run },
    });
    const eventsResponse = await api.getAIRunEvents(runId, { afterSeq: 0, limit: 5000 });
    if (eventsResponse.events.length > 0) {
      nextState = agentRunReducer(nextState, {
        type: 'hydrate_events',
        payload: { events: eventsResponse.events },
      });
    }
    const session: FollowUpAnalysisRuntimeSession = {
      runId,
      messageId: snapshotResponse.run.assistant_message_id,
      state: nextState,
      sourceMessageId: params.sourceMessageId,
      title: String(params.title || snapshotResponse.run.question || 'AI 追问分析').trim() || 'AI 追问分析',
      question: String(snapshotResponse.run.question || '').trim() || undefined,
    };
    followUpRuntimeSessionsRef.current[runId] = session;
    syncFollowUpRuntimeMessage(runId);
    return session;
  }, [syncFollowUpRuntimeMessage]);

  const streamFollowUpRuntimeSession = useCallback(async (runId: string) => {
    const session = followUpRuntimeSessionsRef.current[runId];
    if (!session) {
      return;
    }
    const existingController = followUpRuntimeControllersRef.current[runId];
    if (existingController) {
      existingController.abort();
      delete followUpRuntimeControllersRef.current[runId];
    }
    const controller = new AbortController();
    followUpRuntimeControllersRef.current[runId] = controller;
    session.state = agentRunReducer(session.state, {
      type: 'set_streaming',
      payload: { streaming: true },
    });
    syncFollowUpRuntimeMessage(runId);

    try {
      await api.streamAIRun(runId, {
        afterSeq: session.state.lastSeq,
        signal: controller.signal,
        onEvent: ({ data }) => {
          const envelope = normalizeAgentRunEventEnvelope(data);
          if (!envelope) {
            return;
          }
          const activeSession = followUpRuntimeSessionsRef.current[runId];
          if (!activeSession) {
            return;
          }
          activeSession.state = agentRunReducer(activeSession.state, {
            type: 'append_event',
            payload: { event: envelope as AgentRunEventEnvelope },
          });
          syncFollowUpRuntimeMessage(runId);
          const eventType = String(envelope.event_type || '').trim().toLowerCase();
          if (
            eventType === 'run_finished'
            || eventType === 'run_failed'
            || eventType === 'run_cancelled'
            || eventType === 'action_waiting_user_input'
          ) {
            controller.abort();
          }
        },
      });
    } catch (error: unknown) {
      const aborted = controller.signal.aborted || String(asObject(error).name || '') === 'AbortError';
      if (!aborted) {
        session.state = agentRunReducer(session.state, {
          type: 'set_stream_error',
          payload: { error: getErrorMessage(error, 'AI 运行流式执行失败') },
        });
        syncFollowUpRuntimeMessage(runId);
      }
    } finally {
      const activeSession = followUpRuntimeSessionsRef.current[runId];
      if (activeSession) {
        const hasPendingApprovals = activeSession.state.entities.approvalOrder.some((approvalId) => (
          activeSession.state.entities.approvalsById[approvalId]?.status === 'pending'
        ));
        const waitingUserInput = String(activeSession.state.runMeta?.status || '').trim().toLowerCase() === 'waiting_user_input';
        if (
          String(activeSession.state.runMeta?.status || '').trim().toLowerCase() !== 'completed'
          && String(activeSession.state.runMeta?.status || '').trim().toLowerCase() !== 'failed'
          && String(activeSession.state.runMeta?.status || '').trim().toLowerCase() !== 'cancelled'
          && String(activeSession.state.runMeta?.status || '').trim().toLowerCase() !== 'blocked'
          && !hasPendingApprovals
          && !waitingUserInput
        ) {
          try {
            const reconciled = await reconcileAIRunState(runId, activeSession.state, {
              stopWhenWaitingApproval: true,
            });
            activeSession.state = reconciled.state;
          } catch (_error) {
            // Ignore reconcile failure and preserve the last streamed state.
          }
        }
        activeSession.state = agentRunReducer(activeSession.state, {
          type: 'set_streaming',
          payload: { streaming: false },
        });
        syncFollowUpRuntimeMessage(runId);
      }
      if (followUpRuntimeControllersRef.current[runId] === controller) {
        delete followUpRuntimeControllersRef.current[runId];
      }
    }
  }, [syncFollowUpRuntimeMessage]);

  const runFollowUpAnalysisRuntime = useCallback(async (params: {
    question: string;
    pendingMessages: FollowUpMessage[];
    followUpContext: Record<string, unknown>;
  }) => {
    let createdSession: FollowUpAnalysisRuntimeSession | null = null;
    try {
      const created = await api.createAIRun({
        session_id: analysisSessionId || undefined,
        question: params.question,
        analysis_context: {
          ...params.followUpContext,
          agent_mode: 'followup_analysis_runtime',
          runtime_mode: 'followup_analysis',
        },
        runtime_options: {
          mode: 'followup_analysis',
          use_llm: useLLM,
          show_thought: FOLLOWUP_SHOW_THOUGHT_ENABLED,
          reset: false,
          conversation_id: conversationId || undefined,
          history: params.pendingMessages.map((msg) => ({
            role: msg.role,
            content: String(msg.content || ''),
            timestamp: msg.timestamp,
            message_id: msg.message_id,
            metadata: (msg.metadata && typeof msg.metadata === 'object') ? msg.metadata : {},
          })),
        },
      });
      const initialState = agentRunReducer(createInitialAgentRunState(), {
        type: 'hydrate_snapshot',
        payload: { run: created.run },
      });
      createdSession = {
        runId: created.run.run_id,
        messageId: created.run.assistant_message_id,
        state: agentRunReducer(initialState, {
          type: 'set_streaming',
          payload: { streaming: true },
        }),
        title: params.question,
        question: params.question,
      };
      followUpRuntimeSessionsRef.current[createdSession.runId] = createdSession;
      syncFollowUpRuntimeMessage(createdSession.runId);
      await streamFollowUpRuntimeSession(createdSession.runId);

      const activeSession = followUpRuntimeSessionsRef.current[createdSession.runId] || createdSession;
      const thoughtTimeline = buildRuntimeThoughtTimeline({
        runtimeState: activeSession.state,
        truncateText: truncateFollowUpThoughtText,
        normalizePhase: normalizeFollowUpThoughtPhase,
        normalizeTimeline: normalizeFollowUpThoughtTimeline,
        maxItems: FOLLOWUP_THOUGHT_RENDER_MAX,
      });
      const runtimeMessage = buildRuntimeAnalysisFollowUpMessage({
        session: activeSession,
        thoughtTimeline,
      });
      const runtimeMetadata = asObject(runtimeMessage.metadata);
      return {
        analysis_session_id: String(runtimeMetadata.analysis_session_id || activeSession.state.runMeta?.sessionId || analysisSessionId || ''),
        conversation_id: String(runtimeMetadata.conversation_id || conversationId || ''),
        analysis_method: String(runtimeMetadata.analysis_method || (useLLM ? 'agent-runtime' : 'rule-based')),
        llm_enabled: useLLM,
        llm_requested: useLLM,
        answer: runtimeMessage.content,
        history: [...params.pendingMessages, runtimeMessage],
        references: Array.isArray(runtimeMetadata.references) ? runtimeMetadata.references : [],
        context_pills: Array.isArray(runtimeMetadata.context_pills) ? runtimeMetadata.context_pills : [],
        history_compacted: Boolean(runtimeMetadata.history_compacted),
        conversation_summary: String(runtimeMetadata.conversation_summary || ''),
        token_budget: Number.isFinite(Number(runtimeMetadata.token_budget)) ? Number(runtimeMetadata.token_budget) : undefined,
        token_estimate: Number.isFinite(Number(runtimeMetadata.token_estimate)) ? Number(runtimeMetadata.token_estimate) : undefined,
        token_remaining: Number.isFinite(Number(runtimeMetadata.token_remaining)) ? Number(runtimeMetadata.token_remaining) : undefined,
        token_warning: Boolean(runtimeMetadata.token_warning),
        llm_timeout_fallback: Boolean(runtimeMetadata.llm_timeout_fallback),
        followup_engine: String(runtimeMetadata.followup_engine || 'agent-runtime'),
        subgoals: Array.isArray(runtimeMetadata.subgoals) ? runtimeMetadata.subgoals : [],
        reflection: asObject(runtimeMetadata.reflection),
        actions: Array.isArray(runtimeMetadata.actions) ? runtimeMetadata.actions : [],
        action_observations: Array.isArray(runtimeMetadata.action_observations) ? runtimeMetadata.action_observations : [],
        react_loop: asObject(runtimeMetadata.react_loop),
        react_iterations: Array.isArray(runtimeMetadata.react_iterations) ? runtimeMetadata.react_iterations : [],
        thoughts: thoughtTimeline,
        runtime_run_id: createdSession.runId,
      };
    } catch (error: unknown) {
      if (createdSession && isAgentRuntimeUnavailableError(error)) {
        const controller = followUpRuntimeControllersRef.current[createdSession.runId];
        if (controller) {
          controller.abort();
          delete followUpRuntimeControllersRef.current[createdSession.runId];
        }
        delete followUpRuntimeSessionsRef.current[createdSession.runId];
        removeFollowUpMessageById(createdSession.messageId);
      }
      throw error;
    }
  }, [
    analysisSessionId,
    conversationId,
    isAgentRuntimeUnavailableError,
    removeFollowUpMessageById,
    streamFollowUpRuntimeSession,
    syncFollowUpRuntimeMessage,
    useLLM,
  ]);

  const syncAgentRuntimeCommandMessage = useCallback((runId: string) => {
    const session = agentRuntimeCommandSessionsRef.current[runId];
    if (!session) {
      return;
    }
    const thoughtTimeline = buildRuntimeThoughtTimeline({
      runtimeState: session.state,
      truncateText: truncateFollowUpThoughtText,
      normalizePhase: normalizeFollowUpThoughtPhase,
      normalizeTimeline: normalizeFollowUpThoughtTimeline,
      maxItems: FOLLOWUP_THOUGHT_RENDER_MAX,
    });
    upsertFollowUpAssistantMessage(buildRuntimeFollowUpMessage({
      session,
      thoughtTimeline,
      formatCommandExecutionMessage,
    }));
  }, [upsertFollowUpAssistantMessage]);

  const agentRuntimeSourceTraceId = extractTraceId(inputText);

  const {
    ensureSession: ensureAgentRuntimeCommandSession,
    refreshSession: refreshAgentRuntimeCommandSession,
    streamSession: streamAgentRuntimeCommandSession,
    runCommandFlow: runAgentRuntimeCommandFlow,
    resumeApprovalFlow: resumeAgentRuntimeApprovalFlow,
    cancelRun: cancelAgentRuntimeCommandRun,
  } = useAgentRuntimeCommandFlow({
    sessionsRef: agentRuntimeCommandSessionsRef,
    controllersRef: agentRuntimeCommandControllersRef,
    analysisSessionId,
    analysisType,
    serviceName,
    traceId: agentRuntimeSourceTraceId,
    runtimeEnabled: FOLLOWUP_AGENT_RUNTIME_COMMANDS_ENABLED,
    classifyCommand: classifyFollowUpCommand,
    syncSessionMessage: syncAgentRuntimeCommandMessage,
    removeMessageById: removeFollowUpMessageById,
    isUnavailableError: isAgentRuntimeUnavailableError,
  });

  const openApprovalDialog = useCallback((candidate: FollowUpApprovalCandidate) => {
    const command = normalizeExecutableCommand(candidate.command);
    if (!command) {
      setFollowUpError('审批命令为空，无法执行');
      return;
    }
    setApprovalDialog({
      ...candidate,
      command,
    });
  }, []);

  const closeApprovalDialog = useCallback(() => {
    if (approvalDialogSubmitting) {
      return;
    }
    setApprovalDialog(null);
  }, [approvalDialogSubmitting]);

  useEffect(() => {
    if (!approvalDialog) {
      return undefined;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') {
        return;
      }
      event.preventDefault();
      closeApprovalDialog();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [approvalDialog, closeApprovalDialog]);

  const { runtimePanelRuns: followUpRuntimePanelRuns, resetRuntimeSessions: resetFollowUpRuntimeSessions } = useRuntimeCommandSessions({
    followUpMessages,
    sessionsRef: followUpRuntimeSessionsRef,
    controllersRef: followUpRuntimeControllersRef,
    ensureSession: ensureFollowUpRuntimeSession,
    streamSession: streamFollowUpRuntimeSession,
    syncSessionMessage: syncFollowUpRuntimeMessage,
    isUnavailableError: isAgentRuntimeUnavailableError,
    buildThoughtTimeline: (runtimeState: AgentRunState) => buildRuntimeThoughtTimeline({
      runtimeState,
      truncateText: truncateFollowUpThoughtText,
      normalizePhase: normalizeFollowUpThoughtPhase,
      normalizeTimeline: normalizeFollowUpThoughtTimeline,
      maxItems: FOLLOWUP_THOUGHT_RENDER_MAX,
    }),
    formatTimestamp: toLocaleTime,
  });

  const { runtimePanelRuns: commandRuntimePanelRuns, resetRuntimeSessions: resetCommandRuntimeSessions } = useRuntimeCommandSessions({
    followUpMessages,
    sessionsRef: agentRuntimeCommandSessionsRef,
    controllersRef: agentRuntimeCommandControllersRef,
    ensureSession: ensureAgentRuntimeCommandSession,
    streamSession: streamAgentRuntimeCommandSession,
    syncSessionMessage: syncAgentRuntimeCommandMessage,
    isUnavailableError: isAgentRuntimeUnavailableError,
    buildThoughtTimeline: (runtimeState: AgentRunState) => buildRuntimeThoughtTimeline({
      runtimeState,
      truncateText: truncateFollowUpThoughtText,
      normalizePhase: normalizeFollowUpThoughtPhase,
      normalizeTimeline: normalizeFollowUpThoughtTimeline,
      maxItems: FOLLOWUP_THOUGHT_RENDER_MAX,
    }),
    formatTimestamp: toLocaleTime,
  });
  const runtimePanelRuns = useMemo(() => (
    [...followUpRuntimePanelRuns, ...commandRuntimePanelRuns]
      .sort((left, right) => {
        const leftTime = left.updatedAt ? new Date(left.updatedAt).getTime() : 0;
        const rightTime = right.updatedAt ? new Date(right.updatedAt).getTime() : 0;
        return rightTime - leftTime;
      })
  ), [commandRuntimePanelRuns, followUpRuntimePanelRuns]);
  const resetRuntimeSessions = useCallback(() => {
    resetFollowUpRuntimeSessions();
    resetCommandRuntimeSessions();
  }, [resetCommandRuntimeSessions, resetFollowUpRuntimeSessions]);
  resetRuntimeSessionsRef.current = resetRuntimeSessions;

  useEffect(() => () => {
    resetRuntimeSessions();
  }, [resetRuntimeSessions]);

  const executeApprovalDialogCommand = useCallback(async () => {
    if (!approvalDialog) {
      return;
    }
    const messageId = String(approvalDialog.message_id || '').trim();
    const runtimeRunId = String(approvalDialog.runtime_run_id || '').trim();
    const runtimeApprovalId = String(approvalDialog.runtime_approval_id || '').trim();
    if (!runtimeRunId && !analysisSessionId) {
      setFollowUpError('缺少分析会话，无法执行审批命令');
      return;
    }
    if (!runtimeRunId && !messageId) {
      setFollowUpError('缺少命令来源消息，无法执行审批命令');
      return;
    }
    const command = normalizeExecutableCommand(String(approvalDialog.command || ''));
    if (!command) {
      setFollowUpError('审批命令为空，无法执行');
      return;
    }

    setApprovalDialogSubmitting(true);
    setFollowUpError(null);
    try {
      if (runtimeRunId && runtimeApprovalId) {
        try {
          await resumeAgentRuntimeApprovalFlow({
            runId: runtimeRunId,
            approvalId: runtimeApprovalId,
            command,
            title: approvalDialog.title || command,
            sourceMessageId: messageId || undefined,
            actionId: approvalDialog.action_id,
            elevated: Boolean(approvalDialog.requires_elevation),
          });
          setApprovalDialog(null);
          showFollowUpNotice('审批命令已提交执行', 2200);
          return;
        } catch (runtimeError: unknown) {
          if (!isAgentRuntimeUnavailableError(runtimeError)) {
            throw runtimeError;
          }
        }
      }

      const runtimeHandled = await runAgentRuntimeCommandFlow({
        question: `审批执行命令: ${command}`,
        command,
        sourceMessageId: messageId || undefined,
        actionId: approvalDialog.action_id,
        title: approvalDialog.title || command,
        autoApprove: true,
        elevated: Boolean(approvalDialog.requires_elevation),
      });
      if (runtimeHandled) {
        setApprovalDialog(null);
        showFollowUpNotice('审批命令已提交执行', 2200);
        return;
      }

      const precheck = await executeFollowUpCommandWithRecovery(
        analysisSessionId,
        messageId,
        {
          command,
          confirmed: false,
          elevated: false,
        },
      );
      const precheckStatus = String(precheck.status || '').toLowerCase();
      if (precheckStatus === 'permission_required' || precheckStatus === 'blocked' || precheckStatus === 'waiting_user_input') {
        appendFollowUpAssistantMessage(
          `[审批执行结果]\n${formatCommandExecutionMessage(precheck as Record<string, unknown>, command)}`,
          {
            approval_execution: true,
            source_message_id: messageId,
            command,
          },
        );
        setApprovalDialog(null);
        return;
      }

      const needElevation = precheckStatus === 'elevation_required'
        || Boolean(precheck.requires_elevation)
        || Boolean(approvalDialog.requires_elevation);
      const needConfirmation = precheckStatus === 'confirmation_required'
        || needElevation
        || Boolean(precheck.requires_confirmation)
        || Boolean(approvalDialog.requires_confirmation);
      const confirmationTicket = String(
        precheck.confirmation_ticket || approvalDialog.confirmation_ticket || '',
      ).trim();

      let executionPayload: Record<string, unknown> = precheck as Record<string, unknown>;
      if (needConfirmation) {
        executionPayload = await executeFollowUpCommandWithRecovery(
          analysisSessionId,
          messageId,
          {
            command,
            confirmed: true,
            elevated: Boolean(needElevation),
            confirmation_ticket: confirmationTicket || undefined,
          },
        ) as Record<string, unknown>;
      }

      const executionStatus = String(executionPayload.status || '').toLowerCase();
      if (executionStatus === 'blocked' || executionStatus === 'waiting_user_input') {
        appendFollowUpAssistantMessage(
          `[审批执行]\n${formatCommandExecutionMessage(executionPayload, command)}`,
          {
            approval_execution: true,
            source_message_id: messageId,
            command,
            action_id: approvalDialog.action_id,
          },
        );
        setApprovalDialog(null);
        return;
      }

      appendFollowUpAssistantMessage(
        `[审批执行]\n${formatCommandExecutionMessage(executionPayload, command)}`,
        {
          approval_execution: true,
          source_message_id: messageId,
          command,
          action_id: approvalDialog.action_id,
        },
      );
      setApprovalDialog(null);
      showFollowUpNotice('审批命令已提交执行', 2200);
    } catch (err: unknown) {
      const failureMessage = getErrorMessage(err, '审批命令执行失败，请稍后重试');
      if (runtimeRunId) {
        try {
          const refreshedSession = await refreshAgentRuntimeCommandSession({
            runId: runtimeRunId,
            command,
            purpose: approvalDialog.title || command,
            title: approvalDialog.title || command,
            sourceMessageId: messageId || undefined,
            actionId: approvalDialog.action_id,
          });
          const latestPendingApproval = selectPendingApprovals(refreshedSession.state).slice(-1)[0] || null;
          if (!latestPendingApproval) {
            setApprovalDialog(null);
            setFollowUpError(`${failureMessage}；审批状态已变化，已同步关闭审批窗口。`);
            return;
          }
          const latestApprovalId = String(latestPendingApproval.approvalId || '').trim();
          if (latestApprovalId && latestApprovalId !== runtimeApprovalId) {
            setApprovalDialog((current) => {
              if (!current) {
                return current;
              }
              return {
                ...current,
                runtime_approval_id: latestApprovalId,
                confirmation_ticket: latestApprovalId,
                command: String(latestPendingApproval.command || current.command).trim() || current.command,
                command_type: String(latestPendingApproval.commandType || current.command_type || '').trim() || current.command_type,
                risk_level: String(latestPendingApproval.riskLevel || current.risk_level || '').trim() || current.risk_level,
                requires_confirmation: latestPendingApproval.requiresConfirmation,
                requires_elevation: latestPendingApproval.requiresElevation,
                message: String(latestPendingApproval.message || current.message || '').trim() || current.message,
                title: String(latestPendingApproval.title || current.title || '').trim() || current.title,
              };
            });
            setFollowUpError('审批单已更新为最新待审批项，请再次确认后执行。');
            return;
          }
        } catch (_refreshError) {
          // Ignore refresh failures and surface original approval error.
        }
      }
      setFollowUpError(failureMessage);
    } finally {
      setApprovalDialogSubmitting(false);
    }
  }, [
    analysisSessionId,
    appendFollowUpAssistantMessage,
    approvalDialog,
    isAgentRuntimeUnavailableError,
    refreshAgentRuntimeCommandSession,
    resumeAgentRuntimeApprovalFlow,
    runAgentRuntimeCommandFlow,
    showFollowUpNotice,
    executeFollowUpCommandWithRecovery,
  ]);

  const buildAutoExecCommandKey = (sessionId: string, messageId: string, command: string): string => {
    const commandKey = normalizeCommandMatchKey(command);
    return `${sessionId}::${messageId}::${commandKey}`;
  };

  const autoExecuteQueryActionsFromAssistantMessage = async (sessionId: string, assistantMessage: FollowUpMessage | null | undefined) => {
    if (!FOLLOWUP_AUTO_EXEC_QUERY_ENABLED) {
      return;
    }
    if (!sessionId || !assistantMessage?.message_id) {
      return;
    }
    if (String(assistantMessage.metadata?.runtime_run_id || '').trim()) {
      return;
    }
    const actions = normalizeFollowUpActions(assistantMessage?.metadata?.actions);
    if (!actions.length) {
      return;
    }
    const candidates = actions
      .filter((action) => action.executable && action.command)
      .map((action) => {
        const command = normalizeExecutableCommand(String(action.command || ''));
        const classified = command ? classifyFollowUpCommand(command) : null;
        const commandType = String(action.command_type || classified?.commandType || '').toLowerCase();
        const requiresWritePermission = action.requires_write_permission !== undefined
          ? Boolean(action.requires_write_permission)
          : commandType === 'repair';
        return {
          command,
          commandType,
          requiresWritePermission,
        };
      })
      .filter((item) => item.command && item.commandType === 'query' && !item.requiresWritePermission);

    if (!candidates.length) {
      return;
    }

    const dedupe = new Set<string>();
    for (const item of candidates) {
      const commandKey = normalizeCommandMatchKey(item.command);
      if (!commandKey || dedupe.has(commandKey)) {
        continue;
      }
      dedupe.add(commandKey);
      const autoKey = buildAutoExecCommandKey(sessionId, assistantMessage.message_id, item.command);
      if (autoExecutedQueryCommandKeysRef.current.has(autoKey)) {
        continue;
      }
      autoExecutedQueryCommandKeysRef.current.add(autoKey);

      try {
        const runtimeHandled = await runAgentRuntimeCommandFlow({
          question: `自动执行查询: ${item.command}`,
          command: item.command,
          sourceMessageId: assistantMessage.message_id,
          title: item.command,
        });
        if (runtimeHandled) {
          continue;
        }

        const precheck = await executeFollowUpCommandWithRecovery(
          sessionId,
          assistantMessage.message_id,
          {
            command: item.command,
            confirmed: false,
            elevated: false,
          },
        );

        let executionPayload = precheck as Record<string, unknown>;
        const status = String(precheck.status || '').toLowerCase();
        if (status === 'blocked' || status === 'waiting_user_input' || status === 'permission_required') {
          appendFollowUpAssistantMessage(
            `[自动执行查询]\n${formatCommandExecutionMessage(executionPayload, item.command)}`,
            {
              auto_execution: true,
              source_message_id: assistantMessage.message_id,
              command: item.command,
            },
          );
          continue;
        }
        if (status === 'confirmation_required') {
          const confirmationTicket = String(precheck.confirmation_ticket || '').trim();
          executionPayload = await executeFollowUpCommandWithRecovery(
            sessionId,
            assistantMessage.message_id,
            {
              command: item.command,
              confirmed: true,
              elevated: false,
              confirmation_ticket: confirmationTicket || undefined,
            },
          ) as Record<string, unknown>;
        }

        appendFollowUpAssistantMessage(
          `[自动执行查询]\n${formatCommandExecutionMessage(executionPayload, item.command)}`,
          {
            auto_execution: true,
            source_message_id: assistantMessage.message_id,
            command: item.command,
          },
        );
      } catch (err: unknown) {
        appendFollowUpAssistantMessage(
          `[自动执行查询失败]\ncommand: ${item.command}\nerror: ${getErrorMessage(err, 'unknown')}`,
          {
            auto_execution: true,
            source_message_id: assistantMessage.message_id,
            command: item.command,
          },
        );
      }

      if (dedupe.size >= FOLLOWUP_AUTO_EXEC_QUERY_MAX_ACTIONS) {
        break;
      }
    }
  };

  const findAssistantMessageForCommand = (command: string): FollowUpMessage | null => {
    const targetKey = normalizeCommandMatchKey(command);
    if (!targetKey) {
      return null;
    }
    for (let i = followUpMessages.length - 1; i >= 0; i -= 1) {
      const item = followUpMessages[i];
      if (item.role !== 'assistant' || !item.message_id) {
        continue;
      }
      const commands = extractExecutableCommandsFromText(item.content);
      const metadataActionCommands = Array.isArray(item.metadata?.actions)
        ? normalizeFollowUpActions(item.metadata.actions)
          .map((action) => normalizeCommandMatchKey(String(action.command || '')))
          .filter(Boolean)
        : [];
      const matched = commands.some((candidate) => normalizeCommandMatchKey(candidate.command) === targetKey)
        || metadataActionCommands.includes(targetKey);
      if (matched) {
        return item;
      }
    }
    return null;
  };

  const executeCommandFromFollowUpConversation = async (
    question: string,
    command: string,
    options?: { reuseTailUser?: boolean },
  ) => {
    const normalizedCommand = normalizeExecutableCommand(command);
    const commandMatchKey = normalizeCommandMatchKey(command);
    if (!normalizedCommand || !commandMatchKey) {
      setFollowUpError('命令为空，请使用 `/exec <command>` 或 `执行命令: <command>`');
      return;
    }
    if (!analysisSessionId) {
      setFollowUpError('缺少分析会话，无法执行命令。请先发送一次普通追问。');
      return;
    }
    const sourceMessage = findAssistantMessageForCommand(normalizedCommand);
    if (!sourceMessage?.message_id) {
      setFollowUpError('命令必须来自 AI 最近回复。请先让 AI 生成命令，再用 `/exec` 执行。');
      return;
    }
    const sourceActions = Array.isArray(sourceMessage.metadata?.actions)
      ? normalizeFollowUpActions(sourceMessage.metadata.actions)
      : [];
    const matchedAction = sourceActions.find((item) => (
      normalizeCommandMatchKey(String(item.command || '')) === commandMatchKey
    )) || null;
    const sourceApprovals = Array.isArray(sourceMessage.metadata?.approval_required)
      ? sourceMessage.metadata.approval_required as Array<Record<string, unknown>>
      : [];
    const matchedApproval = sourceApprovals.find((item) => (
      normalizeCommandMatchKey(String((item as UnknownObject).command || '')) === commandMatchKey
    )) || null;
    const runtimeRunId = String(
      (matchedApproval as UnknownObject | null)?.runtime_run_id
      || sourceMessage.metadata?.runtime_run_id
      || '',
    ).trim();
    const runtimeApprovalId = String(
      (matchedApproval as UnknownObject | null)?.runtime_approval_id
      || (matchedApproval as UnknownObject | null)?.approval_id
      || '',
    ).trim();
    const requiresElevation = Boolean(
      matchedAction?.requires_elevation
      || (matchedApproval as UnknownObject | null)?.requires_elevation
      || classifyFollowUpCommand(normalizedCommand).commandType === 'repair',
    );
    const requiresConfirmation = Boolean(
      matchedAction?.requires_confirmation
      || (matchedApproval as UnknownObject | null)?.requires_confirmation
      || requiresElevation,
    );
    const actionTitle = String(
      matchedAction?.title
      || (matchedApproval as UnknownObject | null)?.title
      || normalizedCommand,
    ).trim() || normalizedCommand;

    const tailMessage = followUpMessages.length > 0 ? followUpMessages[followUpMessages.length - 1] : null;
    const canReuseTailUser = Boolean(
      options?.reuseTailUser
      && tailMessage
      && tailMessage.role === 'user'
      && String(tailMessage.content || '').trim() === question,
    );
    const pendingMessages: FollowUpMessage[] = canReuseTailUser
      ? [...followUpMessages]
      : [
          ...followUpMessages,
          {
            role: 'user',
            content: question,
            timestamp: new Date().toISOString(),
          },
        ];
    if (!canReuseTailUser) {
      setFollowUpAutoScrollEnabled(true);
      setFollowUpHasUnseenUpdate(false);
      setFollowUpMessages(pendingMessages);
    }
    setFollowUpQuestion('');
    setFollowUpLoading(true);
    setFollowUpError(null);

    try {
      let alreadyConfirmed = false;
      if (requiresConfirmation) {
        const confirmationText = requiresElevation
          ? `该命令需要提权确认后执行：\n${normalizedCommand}`
          : `确认执行命令？\n${normalizedCommand}`;
        if (!window.confirm(confirmationText)) {
          appendFollowUpAssistantMessage(`命令执行已取消。\ncommand: ${normalizedCommand}`);
          return;
        }
        alreadyConfirmed = true;
      }

      if (runtimeRunId && runtimeApprovalId) {
        await resumeAgentRuntimeApprovalFlow({
          runId: runtimeRunId,
          approvalId: runtimeApprovalId,
          command: normalizedCommand,
          title: actionTitle,
          sourceMessageId: sourceMessage.message_id,
          actionId: matchedAction?.id,
          elevated: requiresElevation,
        });
        return;
      }

      const runtimeHandled = await runAgentRuntimeCommandFlow({
        question,
        command: normalizedCommand,
        commandSpec: matchedAction?.command_spec,
        sourceMessageId: sourceMessage.message_id,
        actionId: matchedAction?.id,
        title: actionTitle,
        autoApprove: alreadyConfirmed,
        elevated: requiresElevation,
      });
      if (runtimeHandled) {
        return;
      }

      const precheck = await executeFollowUpCommandWithRecovery(
        analysisSessionId,
        sourceMessage.message_id,
        {
          command: normalizedCommand,
          command_spec: matchedAction?.command_spec,
          confirmed: false,
          elevated: false,
        },
      );

      if (precheck.status === 'permission_required' || precheck.status === 'blocked' || precheck.status === 'waiting_user_input') {
        appendFollowUpAssistantMessage(formatCommandExecutionMessage(precheck as Record<string, unknown>, normalizedCommand));
        return;
      }

      const needElevation = precheck.status === 'elevation_required' || Boolean(precheck.requires_elevation);
      const confirmationTicket = String(precheck.confirmation_ticket || '').trim();
      if (precheck.status === 'confirmation_required' || needElevation) {
        if (!alreadyConfirmed) {
          const confirmationText = String(
            precheck.confirmation_message
            || (needElevation
              ? `该命令需要提权确认后执行：\n${normalizedCommand}`
              : `确认执行命令？\n${normalizedCommand}`),
          );
          const confirmed = window.confirm(confirmationText);
          if (!confirmed) {
            appendFollowUpAssistantMessage(`命令执行已取消。\ncommand: ${normalizedCommand}`);
            return;
          }
          alreadyConfirmed = true;
        }
      }

      const executed = await executeFollowUpCommandWithRecovery(
        analysisSessionId,
        sourceMessage.message_id,
        {
          command: normalizedCommand,
          command_spec: matchedAction?.command_spec,
          confirmed: true,
          elevated: Boolean(needElevation || requiresElevation),
          confirmation_ticket: confirmationTicket || undefined,
        },
      );
      appendFollowUpAssistantMessage(formatCommandExecutionMessage(executed as Record<string, unknown>, normalizedCommand));
    } catch (err: unknown) {
      setFollowUpError(getErrorMessage(err, '命令执行失败，请稍后重试'));
    } finally {
      setFollowUpLoading(false);
    }
  };

  const handleSubmitFollowUp = async (options?: {
    overrideQuestion?: string;
    retryLastUser?: boolean;
    followupRelatedLogs?: Array<Record<string, unknown>>;
    followupRelatedLogCount?: number;
    followupRelatedMeta?: Record<string, unknown>;
  }) => {
    const question = String(options?.overrideQuestion ?? followUpQuestion).trim();
    if (!question) {
      return;
    }
    const commandForExecution = parseCommandExecutionQuestion(question);
    if (commandForExecution) {
      await executeCommandFromFollowUpConversation(question, commandForExecution, {
        reuseTailUser: Boolean(options?.retryLastUser),
      });
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
      setFollowUpAutoScrollEnabled(true);
      setFollowUpHasUnseenUpdate(false);
      setFollowUpMessages(pendingMessages);
    }
    if (!options?.overrideQuestion) {
      setFollowUpQuestion('');
    }
    setFollowUpLoading(true);
    setFollowUpError(null);

    try {
      const detectedTraceId = extractTraceId(inputText);
      const detectedRequestId = extractRequestId(question) || extractRequestId(inputText);
      const sourceAttributes = asObject(sourceLogData?.attributes);
      const followUpContext = buildRuntimeFollowUpContext({
        analysisSessionId,
        analysisType,
        serviceName,
        inputText,
        question,
        llmInfo: llmInfo || {},
        result,
        detectedTraceId,
        detectedRequestId,
        sourceLogTimestamp: String(sourceLogData?.timestamp || ''),
        sourceTraceId: String(sourceLogData?.trace_id || ''),
        sourceRequestId: (
          extractRequestIdFromRecord(sourceAttributes)
          || extractRequestIdFromRecord(asObject(sourceAttributes.log_meta))
          || extractRequestId(inputText)
        ),
        followupRelatedLogs: options?.followupRelatedLogs,
        followupRelatedLogCount: options?.followupRelatedLogCount,
        followupRelatedMeta: options?.followupRelatedMeta,
      });

      const requestPayload = {
        question,
        analysis_session_id: analysisSessionId || undefined,
        conversation_id: conversationId || undefined,
        use_llm: useLLM,
        show_thought: FOLLOWUP_SHOW_THOUGHT_ENABLED,
        analysis_context: followUpContext,
        history: pendingMessages,
        reset: false,
      };

      let streamPlaceholderMessageId = '';
      let streamCurrentMessageId = '';
      let streamMessageSnapshot: FollowUpMessage | null = null;
      let response;
      let usedRuntimeFlow = false;
      if (FOLLOWUP_ANALYSIS_RUNTIME_ENABLED) {
        try {
          response = await runFollowUpAnalysisRuntime({
            question,
            pendingMessages,
            followUpContext,
          });
          usedRuntimeFlow = true;
        } catch (runtimeError: unknown) {
          if (!isAgentRuntimeUnavailableError(runtimeError)) {
            throw runtimeError;
          }
        }
      }
      if (!usedRuntimeFlow && FOLLOWUP_STREAM_ENABLED) {
        streamPlaceholderMessageId = `local-stream-${Date.now()}-${Math.floor(Math.random() * 1000)}`;
        streamCurrentMessageId = streamPlaceholderMessageId;
        const placeholderMessage: FollowUpMessage = {
          message_id: streamPlaceholderMessageId,
          role: 'assistant',
          content: '',
          timestamp: new Date().toISOString(),
          metadata: {
            stream_loading: true,
            actions: [],
            action_observations: [],
            stream_timeline: [],
          },
        };
        streamMessageSnapshot = placeholderMessage;
        setFollowUpMessages([...pendingMessages, placeholderMessage]);
        const suppressPlanningForCurrentTurn = pendingMessages.some((item) => item.role === 'assistant');

        const patchStreamMessage = (
          updater: (msg: FollowUpMessage) => FollowUpMessage,
          options?: { nextMessageId?: string },
        ) => {
          const lookupMessageId = streamCurrentMessageId || streamPlaceholderMessageId;
          setFollowUpMessages((prev) => prev.map((msg) => (
            msg.message_id === lookupMessageId
              ? (() => {
                  const updated = updater(msg);
                  const nextMessageId = String(
                    options?.nextMessageId || updated.message_id || lookupMessageId,
                  ).trim();
                  if (nextMessageId) {
                    streamCurrentMessageId = nextMessageId;
                  }
                  streamMessageSnapshot = updated;
                  return updated;
                })()
              : msg
          )));
        };

        response = await api.followUpAnalysisStreamV2(requestPayload, {
          onEvent: ({ event, data }: { event: string; data: Record<string, unknown> }) => {
            const eventName = String(event || '').toLowerCase();
            const buildTimeline = (metadata: Record<string, unknown>): FollowUpThoughtItem[] => {
              if (!FOLLOWUP_SHOW_THOUGHT_ENABLED) {
                return filterPlanningThoughtTimeline(
                  normalizeFollowUpThoughtTimeline(metadata.stream_timeline),
                  suppressPlanningForCurrentTurn,
                );
              }
              const thought = buildFollowUpThoughtFromStreamEvent(eventName, data);
              return appendFollowUpThoughtTimeline(metadata, thought, {
                suppressPlanning: suppressPlanningForCurrentTurn,
              });
            };
            if (eventName === 'token') {
              const textChunk = String(data.text || '');
              if (!textChunk) {
                return;
              }
              patchStreamMessage((msg) => ({
                ...msg,
                content: `${String(msg.content || '')}${textChunk}`,
                metadata: {
                  ...(msg.metadata || {}),
                  stream_loading: true,
                },
              }));
              return;
            }
            if (eventName === 'action') {
              const actions = normalizeFollowUpActions(data.actions);
              const eventMessageId = String(data.message_id || '').trim();
              patchStreamMessage((msg) => ({
                ...msg,
                message_id: eventMessageId || msg.message_id,
                metadata: {
                  ...(msg.metadata || {}),
                  stream_message_id: eventMessageId || (msg.metadata as UnknownObject)?.stream_message_id,
                  actions,
                  stream_timeline: buildTimeline((msg.metadata as Record<string, unknown>) || {}),
                },
              }), {
                nextMessageId: eventMessageId,
              });
              return;
            }
            if (eventName === 'observation') {
              patchStreamMessage((msg) => {
                const metadata = (msg.metadata && typeof msg.metadata === 'object') ? msg.metadata : {};
                const existing = Array.isArray((metadata as UnknownObject).action_observations)
                  ? (metadata as UnknownObject).action_observations as Array<Record<string, unknown>>
                  : [];
                return {
                  ...msg,
                  metadata: {
                    ...metadata,
                    action_observations: [...existing, data as Record<string, unknown>],
                    stream_timeline: buildTimeline(metadata as Record<string, unknown>),
                  },
                };
              });
              return;
            }
            if (eventName === 'approval_required') {
              const approvalPayload = data as Record<string, unknown>;
              const commandText = String(approvalPayload.command || '').trim();
              patchStreamMessage((msg) => {
                const metadata = (msg.metadata && typeof msg.metadata === 'object') ? msg.metadata : {};
                const existingApprovals = Array.isArray((metadata as UnknownObject).approval_required)
                  ? (metadata as UnknownObject).approval_required as Array<Record<string, unknown>>
                  : [];
                const actionId = String(approvalPayload.action_id || '').trim();
                const dedupeKey = actionId || normalizeCommandMatchKey(commandText);
                const deduped = existingApprovals.filter((item) => {
                  const itemActionId = String((item as UnknownObject).action_id || '').trim();
                  const itemCommand = String((item as UnknownObject).command || '').trim();
                  const itemKey = itemActionId || normalizeCommandMatchKey(itemCommand);
                  return itemKey !== dedupeKey;
                });
                const existingObservations = Array.isArray((metadata as UnknownObject).action_observations)
                  ? (metadata as UnknownObject).action_observations as Array<Record<string, unknown>>
                  : [];
                const observationLike = {
                  status: String(approvalPayload.status || 'elevation_required'),
                  command: commandText,
                  message: String(approvalPayload.message || '检测到需要审批的命令'),
                  action_id: actionId,
                  requires_elevation: Boolean(approvalPayload.requires_elevation),
                  requires_confirmation: Boolean(approvalPayload.requires_confirmation),
                };
                return {
                  ...msg,
                  metadata: {
                    ...metadata,
                    approval_required: [...deduped, approvalPayload],
                    action_observations: [...existingObservations, observationLike],
                    stream_timeline: buildTimeline(metadata as Record<string, unknown>),
                  },
                };
              });
              if (commandText) {
                showFollowUpNotice(`检测到需审批命令：${truncateFollowUpThoughtText(commandText, 42)}`, 2600);
              } else {
                showFollowUpNotice('检测到需提权审批的动作，回复完成后可执行确认', 2600);
              }
              return;
            }
            if (eventName === 'replan') {
              const reactLoop = (data.react_loop && typeof data.react_loop === 'object')
                ? data.react_loop as Record<string, unknown>
                : {};
              patchStreamMessage((msg) => ({
                ...msg,
                metadata: {
                  ...(msg.metadata || {}),
                  react_loop: reactLoop,
                  stream_timeline: buildTimeline((msg.metadata as Record<string, unknown>) || {}),
                },
              }));
              return;
            }
            if (eventName === 'thought') {
              patchStreamMessage((msg) => {
                const metadata = (msg.metadata && typeof msg.metadata === 'object') ? msg.metadata : {};
                return {
                  ...msg,
                  metadata: {
                    ...metadata,
                    stream_timeline: buildTimeline(metadata as Record<string, unknown>),
                  },
                };
              });
              return;
            }
            if (eventName === 'plan') {
              const stage = String(data.stage || '').trim();
              if (!stage) {
                return;
              }
              patchStreamMessage((msg) => {
                const metadata = (msg.metadata && typeof msg.metadata === 'object') ? msg.metadata : {};
                return {
                  ...msg,
                  metadata: {
                    ...metadata,
                    stream_stage: stage,
                    stream_timeline: buildTimeline(metadata as Record<string, unknown>),
                  },
                };
              });
            }
          },
        });
      } else if (!response) {
        response = await api.followUpAnalysis(requestPayload);
      }
      const responseActions = normalizeFollowUpActions(response.actions);
      const resolvedSessionId = String(response.analysis_session_id || analysisSessionId || '');
      setAnalysisSessionId(resolvedSessionId);
      setConversationId(response.conversation_id || conversationId);
      if (Array.isArray(response.context_pills) && response.context_pills.length > 0) {
        setContextPills(response.context_pills);
      } else {
        buildDefaultContextPills({
          result,
          sessionId: resolvedSessionId,
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
      setAnalysisAssistNotice(methodNotice);
      setLLMInfo((prev) => ({
        method: String(response.analysis_method || (response.llm_enabled ? 'llm' : 'rule-based')),
        model: prev?.model,
        cached: prev?.cached,
        latency_ms: prev?.latency_ms,
      }));
      let autoExecAssistantMessage: FollowUpMessage | null = null;
      const normalizedHistory = normalizeFollowUpMessages(response.history);
      if (normalizedHistory.length > 0) {
        let mergedHistory = normalizedHistory;
        if (streamMessageSnapshot && streamPlaceholderMessageId) {
          const streamMetadata = (streamMessageSnapshot.metadata && typeof streamMessageSnapshot.metadata === 'object')
            ? streamMessageSnapshot.metadata
            : {};
          const streamTimeline = normalizeFollowUpThoughtTimeline((streamMetadata as UnknownObject).stream_timeline);
          if (streamTimeline.length > 0 || String(streamMessageSnapshot.content || '').trim()) {
            const lastAssistantIndex = [...mergedHistory]
              .map((item, idx) => ({ item, idx }))
              .reverse()
              .find(({ item }) => item.role === 'assistant')?.idx;
            if (typeof lastAssistantIndex === 'number') {
              const target = mergedHistory[lastAssistantIndex];
              const metadata = (target?.metadata && typeof target.metadata === 'object')
                ? target.metadata
                : {};
              let mergedTimeline = normalizeFollowUpThoughtTimeline(
                (metadata as UnknownObject).stream_timeline ?? (metadata as UnknownObject).thoughts,
              );
              streamTimeline.forEach((item) => {
                mergedTimeline = appendFollowUpThoughtTimeline(
                  { stream_timeline: mergedTimeline },
                  item,
                );
              });
              mergedHistory = [...mergedHistory];
              mergedHistory[lastAssistantIndex] = {
                ...target,
                content: String(target.content || '').trim()
                  ? target.content
                  : String(streamMessageSnapshot.content || ''),
                metadata: {
                  ...(metadata as Record<string, unknown>),
                  action_observations: mergeFollowUpMetadataItems(
                    (metadata as UnknownObject).action_observations,
                    (streamMetadata as UnknownObject).action_observations,
                  ),
                  approval_required: mergeFollowUpMetadataItems(
                    (metadata as UnknownObject).approval_required,
                    (streamMetadata as UnknownObject).approval_required,
                  ),
                  stream_timeline: mergedTimeline,
                  thoughts: normalizeFollowUpThoughtTimeline((metadata as UnknownObject).thoughts).length > 0
                    ? normalizeFollowUpThoughtTimeline((metadata as UnknownObject).thoughts)
                    : mergedTimeline,
                },
              };
            }
          }
        }
        if (responseActions.length > 0) {
          const lastAssistantIndex = [...mergedHistory]
            .map((item, idx) => ({ item, idx }))
            .reverse()
            .find(({ item }) => item.role === 'assistant')?.idx;
          if (typeof lastAssistantIndex === 'number') {
            const target = mergedHistory[lastAssistantIndex];
            const metadata = (target?.metadata && typeof target.metadata === 'object')
              ? target.metadata
              : {};
            const existingActions = normalizeFollowUpActions((metadata as UnknownObject).actions);
            if (existingActions.length === 0) {
              mergedHistory = [...mergedHistory];
              mergedHistory[lastAssistantIndex] = {
                ...target,
                metadata: {
                  ...(metadata as Record<string, unknown>),
                  actions: responseActions,
                },
              };
            }
          }
        }
        autoExecAssistantMessage = [...mergedHistory]
          .reverse()
          .find((item) => item.role === 'assistant' && Boolean(item.message_id)) || null;
        setFollowUpMessages(suppressPlanningInFollowUpHistory(mergedHistory));
      } else {
        const fallbackAssistantMessage: FollowUpMessage = {
          role: 'assistant',
          content: String(response.answer || ''),
          timestamp: new Date().toISOString(),
          metadata: {
            references: Array.isArray(response.references) ? response.references : [],
            context_pills: Array.isArray(response.context_pills) ? response.context_pills : [],
            history_compacted: Boolean(response.history_compacted),
            token_warning: Boolean(response.token_warning),
            subgoals: Array.isArray(response.subgoals) ? response.subgoals : [],
            reflection: (response.reflection && typeof response.reflection === 'object')
              ? response.reflection
              : {},
            actions: responseActions,
            action_observations: Array.isArray(response.action_observations)
              ? response.action_observations
              : [],
            react_loop: (response.react_loop && typeof response.react_loop === 'object')
              ? response.react_loop
              : {},
            react_iterations: Array.isArray(response.react_iterations)
              ? response.react_iterations
              : [],
            thoughts: normalizeFollowUpThoughtTimeline(response.thoughts),
            stream_timeline: normalizeFollowUpThoughtTimeline(response.thoughts),
          },
        };
        setFollowUpMessages(suppressPlanningInFollowUpHistory([...pendingMessages, fallbackAssistantMessage]));
        autoExecAssistantMessage = fallbackAssistantMessage.message_id ? fallbackAssistantMessage : null;
      }
      if (resolvedSessionId && autoExecAssistantMessage?.message_id) {
        void autoExecuteQueryActionsFromAssistantMessage(resolvedSessionId, autoExecAssistantMessage);
      }
    } catch (err: unknown) {
      if (FOLLOWUP_STREAM_ENABLED) {
        setFollowUpMessages(pendingMessages);
      }
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
        warning: Boolean((metadata as UnknownObject)?.token_warning),
        historyCompacted: Boolean((metadata as UnknownObject)?.history_compacted),
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
    } catch (err: unknown) {
      setFollowUpError(getErrorMessage(err, '删除消息失败，请稍后重试'));
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

  const openRuntimeApprovalDialog = useCallback((approval: RuntimeApprovalEntry) => {
    openApprovalDialog({
      id: approval.id,
      message_id: approval.messageId,
      action_id: approval.actionId,
      command: approval.command,
      command_type: approval.commandType,
      risk_level: approval.riskLevel,
      requires_elevation: Boolean(approval.requiresElevation),
      requires_confirmation: Boolean(approval.requiresConfirmation),
      confirmation_ticket: approval.confirmationTicket,
      message: approval.message,
      title: approval.title,
      runtime_run_id: approval.runtimeRunId,
      runtime_approval_id: approval.runtimeApprovalId,
    });
  }, [openApprovalDialog]);

  const handleCancelRuntimeRun = useCallback(async (runId: string) => {
    try {
      await cancelAgentRuntimeCommandRun(runId);
      showFollowUpNotice('运行已取消', 2200);
    } catch (err: unknown) {
      setFollowUpError(getErrorMessage(err, '取消运行失败，请稍后重试'));
    }
  }, [cancelAgentRuntimeCommandRun, showFollowUpNotice]);

  const handleContinueRuntimeUserInput = useCallback(async (params: {
    runId: string;
    text: string;
    source?: string;
  }) => {
    const runId = String(params.runId || '').trim();
    const text = String(params.text || '').trim();
    if (!runId || !text) {
      setFollowUpError('请先补充一句话语义说明。');
      return;
    }

    setFollowUpError(null);
    try {
      const response = await api.continueAIRunWithInput(runId, {
        text,
        source: params.source || 'user',
      });
      const runStatus = String(response.run.status || '').trim().toLowerCase();
      if (followUpRuntimeSessionsRef.current[runId]) {
        const session = followUpRuntimeSessionsRef.current[runId];
        session.state = agentRunReducer(session.state, {
          type: 'hydrate_snapshot',
          payload: { run: response.run },
        });
        const eventsPayload = await api.getAIRunEvents(runId, {
          afterSeq: session.state.lastSeq,
          limit: 500,
        });
        if (eventsPayload.events.length > 0) {
          session.state = agentRunReducer(session.state, {
            type: 'hydrate_events',
            payload: { events: eventsPayload.events },
          });
        }
        session.state = agentRunReducer(session.state, {
          type: 'set_streaming',
          payload: { streaming: false },
        });
        syncFollowUpRuntimeMessage(runId);
        if (
          runStatus
          && runStatus !== 'completed'
          && runStatus !== 'failed'
          && runStatus !== 'cancelled'
          && runStatus !== 'blocked'
        ) {
          await streamFollowUpRuntimeSession(runId);
        }
        showFollowUpNotice('已补充语义，运行继续执行', 2200);
        return;
      }

      const session = await ensureAgentRuntimeCommandSession({ runId });
      session.state = agentRunReducer(session.state, {
        type: 'hydrate_snapshot',
        payload: { run: response.run },
      });
      const eventsPayload = await api.getAIRunEvents(runId, {
        afterSeq: session.state.lastSeq,
        limit: 500,
      });
      if (eventsPayload.events.length > 0) {
        session.state = agentRunReducer(session.state, {
          type: 'hydrate_events',
          payload: { events: eventsPayload.events },
        });
      }
      session.state = agentRunReducer(session.state, {
        type: 'set_streaming',
        payload: { streaming: false },
      });
      syncAgentRuntimeCommandMessage(runId);
      if (
        runStatus
        && runStatus !== 'completed'
        && runStatus !== 'failed'
        && runStatus !== 'cancelled'
        && runStatus !== 'blocked'
      ) {
        await streamAgentRuntimeCommandSession(runId, { stopOnApproval: false });
      }
      showFollowUpNotice('已补充语义，运行继续执行', 2200);
    } catch (err: unknown) {
      setFollowUpError(getErrorMessage(err, '补充语义失败，请稍后重试'));
    }
  }, [
    ensureAgentRuntimeCommandSession,
    streamAgentRuntimeCommandSession,
    streamFollowUpRuntimeSession,
    syncAgentRuntimeCommandMessage,
    syncFollowUpRuntimeMessage,
    showFollowUpNotice,
  ]);

  const handleFollowUpKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      if (!followUpLoading && followUpQuestion.trim()) {
        void handleSubmitFollowUp();
      }
    }
  };

  const buildDefaultContextPills = (
    payload: ContextPillPayload = {},
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
  buildDefaultContextPillsRef.current = buildDefaultContextPills;

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
    } catch (err: unknown) {
      setFollowUpError(getErrorMessage(err, '动作草案生成失败'));
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
                        当前样本服务: {trimmedServiceName} | 采样时间: {toLocaleTime(serviceErrorSnapshotLoadedAt)} | 样本条数: {serviceErrorSnapshot?.rawLogs?.length || 0}
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
                            [{item.level}] {item.timestamp ? `${toLocaleTime(item.timestamp)} ` : ''}{item.message}
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
                <div className="text-xs font-medium text-emerald-700 mb-1">后端 Agent 关联分析</div>
                <div className="text-[11px] text-emerald-700">
                  默认由后端 Agent 在请求时间点 ±{CROSS_COMPONENT_PULL_WINDOW_MINUTES} 分钟窗口检索 logs/trace。若需人工控制上下文，可点击“横向拉取日志”写入输入框后再编辑分析。
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
              {analysisType === 'log' && (
                <button
                  onClick={handlePullCrossComponentLogs}
                  disabled={isLoading || crossLogLoading}
                  className="px-4 py-3 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 transition-colors disabled:bg-gray-400 disabled:cursor-not-allowed flex items-center justify-center"
                >
                  {crossLogLoading ? (
                    <>
                      <Loader2 className="w-5 h-5 mr-2 animate-spin" />
                      拉取中...
                    </>
                  ) : (
                    <>
                      <Link2 className="w-5 h-5 mr-2" />
                      横向拉取日志
                    </>
                  )}
                </button>
              )}
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
                        {item.service_name || 'unknown'} · {item.analysis_type || 'log'} · {toLocaleTime(item.created_at)}
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

                {/* 数据路径分析 */}
                {result.dataFlow && (
                  <div className="rounded-lg border border-cyan-200 bg-cyan-50 p-4">
                    <div className="flex items-center mb-2">
                      <Network className="w-5 h-5 text-cyan-600 mr-2" />
                      <h4 className="font-medium text-cyan-900">数据路径分析</h4>
                    </div>
                    {result.dataFlow.summary && (
                      <p className="text-sm text-cyan-800 mb-2">{result.dataFlow.summary}</p>
                    )}
                    {Array.isArray(result.dataFlow.path) && result.dataFlow.path.length > 0 && (
                      <ol className="space-y-2">
                        {result.dataFlow.path.map((item, index) => (
                          <li key={index} className="rounded border border-cyan-200 bg-white p-2 text-xs">
                            <div className="font-medium text-cyan-900">
                              #{item.step || index + 1} {item.component || 'unknown'}
                            </div>
                            {item.operation && <div className="text-cyan-800 mt-1">操作: {item.operation}</div>}
                            {item.from && item.to && (
                              <div className="text-cyan-700 mt-1">流向: {item.from} → {item.to}</div>
                            )}
                            {item.evidence && <div className="text-cyan-700 mt-1">证据: {item.evidence}</div>}
                            {(item.status || item.latency_ms) && (
                              <div className="text-cyan-700 mt-1">
                                {item.status ? `状态: ${item.status}` : ''}
                                {item.status && item.latency_ms ? ' · ' : ''}
                                {item.latency_ms ? `耗时: ${item.latency_ms}ms` : ''}
                              </div>
                            )}
                          </li>
                        ))}
                      </ol>
                    )}
                    {Array.isArray(result.dataFlow.evidence) && result.dataFlow.evidence.length > 0 && (
                      <div className="mt-3 text-xs text-cyan-800">
                        关键证据: {result.dataFlow.evidence.join('；')}
                      </div>
                    )}
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

                {/* 处理思路 */}
                {result.handlingIdeas && result.handlingIdeas.length > 0 && (
                  <div>
                    <div className="flex items-center mb-3">
                      <MessageCircle className="w-5 h-5 text-indigo-500 mr-2" />
                      <h4 className="font-medium text-gray-900">处理思路</h4>
                    </div>
                    <ul className="space-y-2">
                      {result.handlingIdeas.map((idea, index) => (
                        <li key={index} className="text-sm">
                          <div className="font-medium text-gray-800 mb-1">{idea.title}</div>
                          <div className="text-gray-600">{idea.description}</div>
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
                          setKbRetrievalMode((current) => (checked ? (current === 'local' ? 'hybrid' : current) : 'local'));
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
                        onChange={(e) => setKbRetrievalMode(e.target.value as 'local' | 'hybrid' | 'remote_only')}
                        className="mt-1 w-full rounded border border-indigo-200 bg-white px-2 py-1"
                        disabled={!kbRemoteEnabled}
                      >
                        <option value="local">仅本地</option>
                        <option value="remote_only">仅远端</option>
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
                      <h4 className="text-sm font-medium text-slate-800">联合知识检索候选</h4>
                      <span className="text-xs text-slate-500">
                        local={kbSearchSources.local} / remote={kbSearchSources.external}
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
                      onClick={() => handleKBSearch(inputText, {
                        problemType: result?.overview?.problem,
                        service: serviceName || undefined,
                      })}
                      disabled={!inputText}
                      className="flex items-center gap-2 px-3 py-2 bg-amber-50 hover:bg-amber-100 text-amber-700 rounded-lg transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <BookOpen className="w-4 h-4" />
                      联合知识检索
                    </button>
                    <button
                      onClick={() => handleFindSimilarCases(inputText, result?.overview?.problem, serviceName, {})}
                      disabled={!inputText}
                      className="flex items-center gap-2 px-3 py-2 bg-slate-50 hover:bg-slate-100 text-slate-700 rounded-lg transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <BookOpen className="w-4 h-4" />
                      本地相似案例
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
                      <div>命令执行：查询类计划会自动执行并回填结果；也可在输入框使用 `/exec &lt;command&gt;` 或 `执行命令: &lt;command&gt;` 手动触发。</div>
                      <div>权限策略：`AI_FOLLOWUP_COMMAND_EXEC_ENABLED` 控制总开关，`AI_FOLLOWUP_COMMAND_WRITE_ENABLED` 控制写命令；写命令会触发提权确认。</div>
                      <div>安全与成本：追问内容自动脱敏，长会话会自动摘要压缩并在必要时提醒。</div>
                      <div>提示：先发送一次追问后，AI 回答卡片才会出现“引用片段”和“转工单/Runbook/告警抑制”按钮。</div>
                    </div>
                    <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_22rem]">
                      <div className="min-w-0">
                    <div
                      ref={followUpListRef}
                      onScroll={handleFollowUpListScroll}
                      className="space-y-2 max-h-[28rem] overflow-auto pr-1"
                    >
                      {followUpMessages.length === 0 ? (
                        <div className="text-xs text-gray-500">
                          可基于当前分析结果连续追问，例如“优先排查哪条根因？”、“给出更细的修复步骤”。
                        </div>
                      ) : (
                        followUpMessages.map((msg, index) => {
                          const assistantMessageOrder = msg.role === 'assistant'
                            ? followUpMessages
                              .slice(0, index + 1)
                              .filter((item) => item.role === 'assistant')
                              .length
                            : 0;
                          const suppressPlanningForMessage = assistantMessageOrder > 1;
                          const messageMetadata = (msg.metadata && typeof msg.metadata === 'object')
                            ? msg.metadata
                            : undefined;
                          const messageSubgoals = Array.isArray(messageMetadata?.subgoals)
                            ? messageMetadata.subgoals as FollowUpSubgoal[]
                            : [];
                          const messageReflection = (messageMetadata?.reflection && typeof messageMetadata.reflection === 'object')
                            ? messageMetadata.reflection as FollowUpReflection
                            : undefined;
                          const reflectionActions = Array.isArray(messageReflection?.next_actions)
                            ? messageReflection?.next_actions || []
                            : [];
                          const reflectionGaps = Array.isArray(messageReflection?.gaps)
                            ? messageReflection?.gaps || []
                            : [];
                          const messageActions = Array.isArray(messageMetadata?.actions)
                            ? normalizeFollowUpActions(messageMetadata.actions)
                            : [];
                          const messageThoughtTimeline = msg.role === 'assistant' && FOLLOWUP_SHOW_THOUGHT_ENABLED
                            ? buildFollowUpThoughtTimelineFromMetadata(messageMetadata, {
                              suppressPlanning: suppressPlanningForMessage,
                            })
                            : [];
                          const streamLoading = Boolean(messageMetadata?.stream_loading);
                          const streamStage = String(messageMetadata?.stream_stage || '').trim();
                          const resolvedMessageId = String(
                            msg.message_id || (messageMetadata as UnknownObject)?.stream_message_id || '',
                          ).trim();
                          const messageObservations = Array.isArray(messageMetadata?.action_observations)
                            ? messageMetadata.action_observations as Array<Record<string, unknown>>
                            : [];
                          const messageApprovals = Array.isArray(messageMetadata?.approval_required)
                            ? messageMetadata.approval_required as Array<Record<string, unknown>>
                            : [];
                          const observationsByActionId = new Map<string, Record<string, unknown>>();
                          const observationsByCommand = new Map<string, Record<string, unknown>>();
                          const approvalsByActionId = new Map<string, Record<string, unknown>>();
                          const approvalsByCommand = new Map<string, Record<string, unknown>>();
                          messageObservations.forEach((item) => {
                            if (!item || typeof item !== 'object') {
                              return;
                            }
                            const payload = item as UnknownObject;
                            const actionId = String(payload.action_id || '').trim();
                            const commandText = normalizeCommandMatchKey(String(payload.command || ''));
                            if (actionId) {
                              observationsByActionId.set(actionId, payload as Record<string, unknown>);
                            }
                            if (commandText) {
                              observationsByCommand.set(commandText, payload as Record<string, unknown>);
                            }
                          });
                          messageApprovals.forEach((item) => {
                            if (!item || typeof item !== 'object') {
                              return;
                            }
                            const payload = item as UnknownObject;
                            const actionId = String(payload.action_id || '').trim();
                            const commandText = normalizeCommandMatchKey(String(payload.command || ''));
                            if (actionId) {
                              approvalsByActionId.set(actionId, payload as Record<string, unknown>);
                            }
                            if (commandText) {
                              approvalsByCommand.set(commandText, payload as Record<string, unknown>);
                            }
                          });
                          return (
                            <div
                              key={`${msg.message_id || `${msg.role}-${index}`}-${msg.timestamp || ''}`}
                              className={`rounded p-2 text-sm ${
                                msg.role === 'user'
                                  ? 'bg-blue-50 border border-blue-100 text-blue-800'
                                  : 'bg-white border border-gray-200 text-gray-700'
                              }`}
                            >
                            <div className="text-[11px] mb-1 opacity-70 flex items-center gap-2 flex-wrap">
                              <span>
                                {msg.role === 'user' ? '你' : 'AI'} {msg.timestamp ? `· ${toLocaleTime(msg.timestamp)}` : ''}
                              </span>
                              {msg.role === 'assistant' && streamLoading && (
                                <span className="inline-flex items-center gap-1 text-indigo-600">
                                  <Loader2 className="w-3 h-3 animate-spin" />
                                  流式输出中
                                </span>
                              )}
                              {msg.role === 'assistant' && streamStage && (
                                <span className="inline-flex items-center rounded border border-slate-200 bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">
                                  阶段: {streamStage}
                                </span>
                              )}
                            </div>
                            <div className="min-h-[18px]">
                              {msg.role === 'assistant'
                                ? renderFollowUpRichContent(
                                  String(msg.content || ''),
                                  `${msg.message_id || index}`,
                                  { streamLoading },
                                )
                                : <div className="whitespace-pre-wrap">{msg.content}</div>}
                            </div>
                            {msg.role === 'assistant' && messageThoughtTimeline.length > 0 && (
                              streamLoading ? (
                                <div className="mt-2 rounded border border-indigo-200 bg-indigo-50/60 p-2">
                                  <div className="flex items-center justify-between gap-2 text-[11px] font-medium text-indigo-700">
                                    <span>思考过程（实时）</span>
                                    <span className="inline-flex items-center gap-1">
                                      <Loader2 className="w-3 h-3 animate-spin" />
                                      {messageThoughtTimeline.length}
                                    </span>
                                  </div>
                                  <div className="mt-1.5 space-y-1.5 max-h-56 overflow-auto pr-1">
                                    {messageThoughtTimeline.map((thought, thoughtIndex) => {
                                      const detailText = String(thought.detail || '').trim();
                                      return (
                                        <div
                                          key={`${msg.message_id || index}:thought:${thought.id || thoughtIndex}`}
                                          className="rounded border border-indigo-100 bg-white p-1.5"
                                        >
                                          <div className="flex items-center gap-2 text-[10px] text-slate-600">
                                            <span className={`inline-flex items-center rounded-full border px-1.5 py-0.5 ${getFollowUpThoughtTagClass(thought.status)}`}>
                                              {String(thought.phase || 'system')}
                                            </span>
                                            {typeof thought.iteration === 'number' && thought.iteration > 0 && (
                                              <span>迭代: {thought.iteration}</span>
                                            )}
                                            {thought.timestamp && <span>{toLocaleTime(thought.timestamp)}</span>}
                                          </div>
                                          <div className="mt-1 text-[11px] font-medium text-indigo-900 whitespace-pre-wrap">{thought.title}</div>
                                          <div className="mt-0.5 text-[11px] text-indigo-700 whitespace-pre-wrap">
                                            {detailText || '正在生成该步骤的详细推理...'}
                                          </div>
                                        </div>
                                      );
                                    })}
                                  </div>
                                </div>
                              ) : (
                                <details className="mt-2 rounded border border-indigo-200 bg-indigo-50/60 p-2">
                                  <summary className="cursor-pointer text-[11px] font-medium text-indigo-700">
                                    思考过程 ({messageThoughtTimeline.length})
                                  </summary>
                                  <div className="mt-1.5 space-y-1.5">
                                    {messageThoughtTimeline.map((thought, thoughtIndex) => (
                                      <div
                                        key={`${msg.message_id || index}:thought:${thought.id || thoughtIndex}`}
                                        className="rounded border border-indigo-100 bg-white p-1.5"
                                      >
                                        <div className="flex items-center gap-2 text-[10px] text-slate-600">
                                          <span className={`inline-flex items-center rounded-full border px-1.5 py-0.5 ${getFollowUpThoughtTagClass(thought.status)}`}>
                                            {String(thought.phase || 'system')}
                                          </span>
                                          {typeof thought.iteration === 'number' && thought.iteration > 0 && (
                                            <span>迭代: {thought.iteration}</span>
                                          )}
                                          {thought.timestamp && <span>{toLocaleTime(thought.timestamp)}</span>}
                                        </div>
                                        <div className="mt-1 text-[11px] font-medium text-indigo-900">{thought.title}</div>
                                        {thought.detail && (
                                          <div className="mt-0.5 text-[11px] text-indigo-700 whitespace-pre-wrap">{thought.detail}</div>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                </details>
                              )
                            )}
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
                            {msg.role === 'assistant' && messageSubgoals.length > 0 && (
                              <div className="mt-2 rounded border border-sky-200 bg-sky-50 p-2">
                                <div className="text-[11px] font-medium text-sky-700 mb-1">子目标拆解</div>
                                <div className="space-y-1.5">
                                  {messageSubgoals.map((goal, goalIndex) => (
                                    <div key={`${msg.message_id || index}:subgoal:${goal.id || goalIndex}`} className="rounded border border-sky-100 bg-white p-1.5">
                                      <div className="flex items-center justify-between gap-2">
                                        <div className="text-[11px] font-medium text-sky-900">{goal.title || goal.id || '未命名子目标'}</div>
                                        <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] ${getFollowUpSubgoalStatusTagClass(goal.status)}`}>
                                          {formatFollowUpSubgoalStatus(goal.status)}
                                        </span>
                                      </div>
                                      {goal.reason && (
                                        <div className="mt-1 text-[11px] text-sky-700">
                                          {goal.reason}
                                        </div>
                                      )}
                                      {goal.next_action && (
                                        <div className="mt-1 text-[11px] text-sky-600">
                                          下一步：{goal.next_action}
                                        </div>
                                      )}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                            {msg.role === 'assistant' && messageReflection && (
                              <div className="mt-2 rounded border border-violet-200 bg-violet-50 p-2">
                                <div className="text-[11px] font-medium text-violet-700 mb-1">反思闭环</div>
                                <div className="flex flex-wrap gap-2 text-[11px] text-violet-700">
                                  <span>迭代: {Number(messageReflection.iterations || 0)}</span>
                                  <span>完成: {Number(messageReflection.completed_count || 0)}/{Number(messageReflection.total_count || 0)}</span>
                                  <span>置信度: {formatReflectionConfidence(messageReflection.final_confidence)}</span>
                                </div>
                                {reflectionGaps.length > 0 && (
                                  <div className="mt-1 text-[11px] text-violet-700">
                                    缺口：{reflectionGaps.slice(0, 3).join('；')}
                                  </div>
                                )}
                                {reflectionActions.length > 0 && (
                                  <div className="mt-1 text-[11px] text-violet-700">
                                    下一步：{reflectionActions.slice(0, 3).join('；')}
                                  </div>
                                )}
                              </div>
                            )}
                            {msg.role === 'assistant' && messageActions.length > 0 && (
                              <div className="mt-2 rounded border border-emerald-200 bg-emerald-50 p-2">
                                <div className="text-[11px] font-medium text-emerald-700 mb-1">执行计划（ReAct）</div>
                                <div className="space-y-1.5">
                                  {messageActions.slice(0, 1).map((action, actionIndex) => {
                                    const displayPriority = Number.isFinite(Number(action.priority))
                                      ? Math.max(1, Math.floor(Number(action.priority)))
                                      : actionIndex + 1;
                                    const displayTitle = String(action.title || action.command || action.purpose || '未命名动作').trim();
                                    const actionId = String(action.id || '').trim();
                                    const normalizedCommand = normalizeExecutableCommand(String(action.command || ''));
                                    const commandKey = normalizeCommandMatchKey(normalizedCommand);
                                    const observation = (
                                      (actionId && observationsByActionId.get(actionId))
                                      || (commandKey && observationsByCommand.get(commandKey))
                                      || null
                                    );
                                    const approvalPayload = (
                                      (actionId && approvalsByActionId.get(actionId))
                                      || (commandKey && approvalsByCommand.get(commandKey))
                                      || null
                                    );
                                    const statusRaw = normalizeFollowUpActionStatus(
                                      observation?.status || approvalPayload?.status,
                                    );
                                    const statusLabel = statusRaw === 'unknown' ? '' : formatFollowUpActionStatus(statusRaw);
                                    const requiresElevation = Boolean(
                                      action.requires_elevation
                                      || observation?.requires_elevation
                                      || approvalPayload?.requires_elevation
                                      || statusRaw === 'elevation_required',
                                    );
                                    const requiresConfirmation = Boolean(
                                      action.requires_confirmation
                                      || observation?.requires_confirmation
                                      || approvalPayload?.requires_confirmation
                                      || statusRaw === 'confirmation_required'
                                      || requiresElevation,
                                    );
                                    const resolvedCommandType = String(
                                      observation?.command_type || approvalPayload?.command_type || action.command_type || '',
                                    ).trim().toLowerCase();
                                    const policySkipped = statusRaw === 'permission_required' || statusRaw === 'skipped';
                                    const unknownTypeAction = resolvedCommandType === 'unknown' || action.command_type === 'unknown';
                                    const needsApprovalAction = Boolean(normalizedCommand) && (
                                      requiresElevation
                                      || requiresConfirmation
                                      || policySkipped
                                      || unknownTypeAction
                                    );
                                    const runtimeApprovalRunId = String(
                                      approvalPayload?.runtime_run_id || observation?.runtime_run_id || messageMetadata?.runtime_run_id || '',
                                    ).trim();
                                    const runtimeApprovalId = String(
                                      approvalPayload?.runtime_approval_id || approvalPayload?.approval_id || '',
                                    ).trim();
                                    const approvalContextId = resolvedMessageId || runtimeApprovalRunId;
                                    const canApproveAction = needsApprovalAction
                                      && Boolean(approvalContextId)
                                      && !streamLoading
                                      && !followUpLoading
                                      && (
                                        Boolean(runtimeApprovalRunId)
                                        || !resolvedMessageId.startsWith('local-stream-')
                                      );
                                    const approvalMessage = String(
                                      observation?.message || approvalPayload?.message || action.reason || '',
                                    ).trim();
                                    const approvalCandidate: FollowUpApprovalCandidate | null = (
                                      needsApprovalAction
                                      && approvalContextId
                                      && normalizedCommand
                                    )
                                      ? {
                                          id: `${approvalContextId}:${actionId || actionIndex}`,
                                          message_id: resolvedMessageId || undefined,
                                          action_id: actionId || undefined,
                                          command: normalizedCommand,
                                          command_type: resolvedCommandType || undefined,
                                          risk_level: String(
                                            observation?.risk_level || approvalPayload?.risk_level || action.risk_level || '',
                                          ).trim() || undefined,
                                          requires_elevation: requiresElevation,
                                          requires_confirmation: requiresConfirmation,
                                          confirmation_ticket: String(
                                            approvalPayload?.confirmation_ticket || '',
                                          ).trim() || undefined,
                                          message: approvalMessage || undefined,
                                          title: String(approvalPayload?.title || displayTitle).trim() || displayTitle,
                                          runtime_run_id: runtimeApprovalRunId || undefined,
                                          runtime_approval_id: runtimeApprovalId || undefined,
                                        }
                                      : null;
                                    return (
                                      <div
                                        key={`${msg.message_id || index}:plan:${action.id || actionIndex}`}
                                        className="rounded border border-emerald-100 bg-white p-1.5"
                                      >
                                        <div className="text-[11px] font-medium text-emerald-900">
                                          {`P${displayPriority} · ${displayTitle}`}
                                        </div>
                                        {action.purpose && (
                                          <div className="mt-1 text-[11px] text-emerald-700">
                                            目的：{action.purpose}
                                          </div>
                                        )}
                                        {action.command && (
                                          <div className="mt-1 text-[11px] text-emerald-800 break-all">
                                            <code className="rounded bg-emerald-100 px-1 py-0.5">{action.command}</code>
                                          </div>
                                        )}
                                        <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-emerald-700">
                                          {statusLabel && (
                                            <span className={`inline-flex items-center rounded-full border px-2 py-0.5 ${getFollowUpActionStatusTagClass(statusRaw)}`}>
                                              状态: {statusLabel}
                                            </span>
                                          )}
                                          {action.command_type && <span>类型: {action.command_type}</span>}
                                          {action.risk_level && <span>风险: {action.risk_level}</span>}
                                          {action.requires_write_permission && <span>写权限: 是</span>}
                                          {action.requires_elevation && <span>提权: 必需</span>}
                                        </div>
                                        {action.reason && (
                                          <div className="mt-1 text-[10px] text-emerald-600">
                                            说明：{action.reason}
                                          </div>
                                        )}
                                        {action.executable && action.command && (
                                          <div className="mt-1 text-[10px] text-emerald-600 break-all">
                                            执行：
                                            <code className="ml-1 rounded bg-emerald-100 px-1 py-0.5 text-emerald-700">
                                              /exec {action.command}
                                            </code>
                                          </div>
                                        )}
                                        {approvalMessage && (
                                          <div className="mt-1 text-[10px] text-amber-700 whitespace-pre-wrap">
                                            观察：{approvalMessage}
                                          </div>
                                        )}
                                        {approvalCandidate && (
                                          <div className="mt-1.5 rounded border border-amber-200 bg-amber-50 p-1.5">
                                            <div className="text-[10px] text-amber-800">
                                              {requiresElevation
                                                ? '该动作需要提权审批，确认后才会执行。'
                                                : policySkipped
                                                ? '该动作被策略跳过，需人工确认后执行。'
                                                : unknownTypeAction
                                                ? '该动作类型为 unknown，需人工复核命令语义后执行。'
                                                : '该动作需要确认后执行。'}
                                            </div>
                                            <div className="mt-1">
                                              <button
                                                type="button"
                                                onClick={() => openApprovalDialog(approvalCandidate)}
                                                disabled={!canApproveAction}
                                                className="px-2 py-1 text-[11px] rounded bg-amber-600 text-white border border-amber-600 hover:bg-amber-700 disabled:opacity-50"
                                              >
                                                {requiresElevation || requiresConfirmation ? '审批并执行' : '人工确认执行'}
                                              </button>
                                              {!canApproveAction && (
                                                <span className="ml-2 text-[10px] text-amber-700">
                                                  {streamLoading ? '流式输出中，完成后可审批' : '缺少可执行上下文'}
                                                </span>
                                              )}
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    );
                                  })}
                                </div>
                                {messageActions.length > 1 && (
                                  <div className="mt-1 text-[10px] text-emerald-700">
                                    队列中还有 {messageActions.length - 1} 条动作，按串行策略会在当前动作完成后继续。
                                  </div>
                                )}
                                <div className="mt-1 text-[10px] text-emerald-600">
                                  查询命令默认自动执行；可用 `/exec &lt;command&gt;` 或 `执行命令: &lt;command&gt;` 手动重试/补充执行。
                                </div>
                              </div>
                            )}
                            {msg.role === 'assistant' && messageApprovals.length > 0 && (
                              <div className="mt-2 rounded border border-amber-200 bg-amber-50 p-2">
                                <div className="text-[11px] font-medium text-amber-800 mb-1">待审批动作</div>
                                <div className="space-y-1.5">
                                  {messageApprovals.slice(0, 1).map((item, approvalIndex) => {
                                    const payload = item as UnknownObject;
                                    const actionId = String(payload.action_id || '').trim();
                                    const command = normalizeExecutableCommand(String(payload.command || ''));
                                    const statusRaw = normalizeFollowUpActionStatus(payload.status);
                                    const requiresElevation = Boolean(payload.requires_elevation) || statusRaw === 'elevation_required';
                                    const requiresConfirmation = Boolean(payload.requires_confirmation) || statusRaw === 'confirmation_required' || requiresElevation;
                                    const riskLevel = String(payload.risk_level || '').trim() || undefined;
                                    const approvalMessageText = String(payload.message || '').trim();
                                    const runtimeApprovalRunId = String(payload.runtime_run_id || messageMetadata?.runtime_run_id || '').trim();
                                    const runtimeApprovalId = String(payload.runtime_approval_id || payload.approval_id || '').trim();
                                    const approvalContextId = resolvedMessageId || runtimeApprovalRunId;
                                    const approvalCandidate: FollowUpApprovalCandidate | null = (approvalContextId && command)
                                      ? {
                                          id: `${approvalContextId}:approval:${actionId || approvalIndex}`,
                                          message_id: resolvedMessageId || undefined,
                                          action_id: actionId || undefined,
                                          command,
                                          command_type: String(payload.command_type || '').trim() || undefined,
                                          risk_level: riskLevel,
                                          requires_elevation: requiresElevation,
                                          requires_confirmation: requiresConfirmation,
                                          confirmation_ticket: String(payload.confirmation_ticket || '').trim() || undefined,
                                          message: approvalMessageText || undefined,
                                          title: String(payload.title || `审批动作 ${approvalIndex + 1}`).trim() || `审批动作 ${approvalIndex + 1}`,
                                          runtime_run_id: runtimeApprovalRunId || undefined,
                                          runtime_approval_id: runtimeApprovalId || undefined,
                                        }
                                      : null;
                                    const canApproveAction = Boolean(approvalCandidate)
                                      && !streamLoading
                                      && !followUpLoading
                                      && (
                                        Boolean(runtimeApprovalRunId)
                                        || !resolvedMessageId.startsWith('local-stream-')
                                      );
                                    return (
                                      <div
                                        key={`${msg.message_id || index}:approval:${actionId || approvalIndex}`}
                                        className="rounded border border-amber-100 bg-white p-1.5"
                                      >
                                        <div className="flex flex-wrap items-center gap-2 text-[10px]">
                                          <span className={`inline-flex items-center rounded-full border px-2 py-0.5 ${getFollowUpActionStatusTagClass(statusRaw)}`}>
                                            {formatFollowUpActionStatus(statusRaw)}
                                          </span>
                                          {riskLevel && <span className="text-amber-700">风险: {riskLevel}</span>}
                                          {requiresElevation && <span className="text-amber-700">提权: 必需</span>}
                                        </div>
                                        {command && (
                                          <div className="mt-1 text-[11px] text-amber-900 break-all">
                                            <code className="rounded bg-amber-100 px-1 py-0.5">{command}</code>
                                          </div>
                                        )}
                                        {approvalMessageText && (
                                          <div className="mt-1 text-[10px] text-amber-700 whitespace-pre-wrap">
                                            {approvalMessageText}
                                          </div>
                                        )}
                                        {approvalCandidate && (
                                          <div className="mt-1">
                                            <button
                                              type="button"
                                              onClick={() => openApprovalDialog(approvalCandidate)}
                                              disabled={!canApproveAction}
                                              className="px-2 py-1 text-[11px] rounded bg-amber-600 text-white border border-amber-600 hover:bg-amber-700 disabled:opacity-50"
                                            >
                                              {requiresElevation || requiresConfirmation ? '审批并执行' : '人工确认执行'}
                                            </button>
                                          </div>
                                        )}
                                      </div>
                                    );
                                  })}
                                </div>
                                {messageApprovals.length > 1 && (
                                  <div className="mt-1 text-[10px] text-amber-700">
                                    其余 {messageApprovals.length - 1} 条审批在当前审批完成后串行进入。
                                  </div>
                                )}
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
                        );
                        })
                      )}
                    </div>

                    {followUpHasUnseenUpdate && !followUpAutoScrollEnabled && (
                      <div className="mt-2 flex justify-end">
                        <button
                          type="button"
                          onClick={handleJumpToFollowUpBottom}
                          className="px-2 py-1 text-[11px] rounded border border-indigo-200 bg-indigo-50 text-indigo-700 hover:bg-indigo-100"
                        >
                          回到底部查看最新输出
                        </button>
                      </div>
                    )}

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

                    {analysisType === 'log' && (
                      <div className="mt-3 flex flex-wrap gap-2">
                        <button
                          type="button"
                          onClick={() => {
                            void handleInjectCrossLogsToFollowUpDraft();
                          }}
                          disabled={followUpLoading || followUpCrossLogLoading}
                          className="px-2 py-1 text-xs rounded bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100 disabled:opacity-50"
                        >
                          {followUpCrossLogLoading ? '拉取中...' : '查日志注入草稿'}
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            void handleSendFollowUpWithCrossLogs();
                          }}
                          disabled={followUpLoading || followUpCrossLogLoading || !followUpQuestion.trim()}
                          className="px-2 py-1 text-xs rounded bg-emerald-600 text-white border border-emerald-600 hover:bg-emerald-700 disabled:opacity-50"
                        >
                          {followUpCrossLogLoading ? '发送中...' : '查日志并发送'}
                        </button>
                      </div>
                    )}

                    <div className="mt-2 flex items-start gap-2">
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
                      <div className="xl:sticky xl:top-3 xl:self-start">
                        <RuntimeActivityPanel
                          runs={runtimePanelRuns}
                          disabled={followUpLoading || approvalDialogSubmitting}
                          onApprove={openRuntimeApprovalDialog}
                          onSubmitUserInput={(params) => {
                            return handleContinueRuntimeUserInput({
                              runId: params.runId,
                              text: params.text,
                              source: params.source,
                            });
                          }}
                          onCancelRun={(runId) => {
                            void handleCancelRuntimeRun(runId);
                          }}
                        />
                      </div>
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
      {approvalDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <button
            type="button"
            aria-label="关闭审批弹窗"
            onClick={closeApprovalDialog}
            className="absolute inset-0 bg-black/40"
          />
          <div className="relative z-10 w-full max-w-2xl rounded-lg border border-amber-200 bg-white shadow-xl">
            <div className="flex items-start justify-between gap-3 border-b border-amber-100 px-4 py-3">
              <div>
                <div className="text-xs text-amber-700">人工审批执行</div>
                <h3 className="text-sm font-semibold text-slate-900">
                  {approvalDialog.title || '确认命令执行'}
                </h3>
              </div>
              <button
                type="button"
                onClick={closeApprovalDialog}
                disabled={approvalDialogSubmitting}
                className="inline-flex h-8 w-8 items-center justify-center rounded text-slate-500 hover:bg-slate-100 disabled:opacity-50"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="space-y-2 px-4 py-3 text-sm">
              <div className="flex flex-wrap gap-2 text-[11px]">
                {approvalDialog.command_type && (
                  <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-slate-700">
                    类型: {approvalDialog.command_type}
                  </span>
                )}
                {approvalDialog.risk_level && (
                  <span className={`inline-flex items-center rounded-full border px-2 py-0.5 ${
                    String(approvalDialog.risk_level).toLowerCase() === 'high'
                      ? 'border-rose-200 bg-rose-50 text-rose-700'
                      : 'border-emerald-200 bg-emerald-50 text-emerald-700'
                  }`}>
                    风险: {approvalDialog.risk_level}
                  </span>
                )}
                {approvalDialog.requires_elevation && (
                  <span className="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-amber-800">
                    需要提权
                  </span>
                )}
                {approvalDialog.requires_confirmation && !approvalDialog.requires_elevation && (
                  <span className="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-amber-800">
                    需要确认
                  </span>
                )}
              </div>
              <div className="rounded border border-slate-200 bg-slate-50 p-2 text-[12px] text-slate-800 break-all">
                <code>{approvalDialog.command}</code>
              </div>
              {approvalDialog.message && (
                <div className="rounded border border-amber-200 bg-amber-50 p-2 text-[12px] text-amber-800 whitespace-pre-wrap">
                  {approvalDialog.message}
                </div>
              )}
              <div className="text-[11px] text-slate-500">
                审批执行会先做策略预检；若命令仍被策略拒绝，将回填到对话消息中供人工处理。
              </div>
            </div>
            <div className="flex justify-end gap-2 border-t border-amber-100 px-4 py-3">
              <button
                type="button"
                onClick={closeApprovalDialog}
                disabled={approvalDialogSubmitting}
                className="px-3 py-1.5 text-sm rounded border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => {
                  void executeApprovalDialogCommand();
                }}
                disabled={approvalDialogSubmitting}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-amber-600 text-white text-sm hover:bg-amber-700 disabled:opacity-50"
              >
                {approvalDialogSubmitting ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                审批并执行
              </button>
            </div>
          </div>
        </div>
      )}
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
                    {selectedSimilarCaseDetail.created_at && <span>创建: {toLocaleTime(selectedSimilarCaseDetail.created_at)}</span>}
                    {selectedSimilarCaseDetail.resolved_at && <span>解决: {toLocaleTime(selectedSimilarCaseDetail.resolved_at)}</span>}
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
                                  {Object.keys((history.changes || {}) as Record<string, { before?: unknown; after?: unknown }>).length === 0 ? (
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
                                        查看详情（{Object.keys((history.changes || {}) as Record<string, { before?: unknown; after?: unknown }>).length} 项）
                                      </summary>
                                      <div className="mt-2 max-h-56 overflow-auto space-y-2 pr-1">
                                        {Object.entries((history.changes || {}) as Record<string, { before?: unknown; after?: unknown }>).map(([field, diff]) => (
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
