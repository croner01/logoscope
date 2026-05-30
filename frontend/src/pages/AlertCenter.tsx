/**
 * 告警中心页面
 * 参考 Datadog 设计风格
 * 添加页面联动功能
 * 增强告警管理和 AI 分析关联
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useAlertRules, useAlertEvents, useAlertStats } from '../hooks/useApi';
import { useNavigation } from '../hooks/useNavigation';
import { api } from '../utils/api';
import type { AlertEvent, AlertNotification, AlertRule, AlertRuleTemplate, LogsFacetBucket } from '../utils/api';
import LoadingState from '../components/common/LoadingState';
import ErrorState from '../components/common/ErrorState';
import EmptyState from '../components/common/EmptyState';
import { formatTime, formatColor } from '../utils/formatters';
import {
  Bell,
  RefreshCw,
  Trash2,
  Play,
  Pause,
  PlusCircle,
  Megaphone,
  BrainCircuit,
  Network,
  FileText,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react';

const normalizeFacetOptions = (items: LogsFacetBucket[]): string[] => (
  Array.from(
    new Set(
      (items || [])
        .map((item) => String(item?.value || '').trim())
        .filter(Boolean),
    ),
  )
    .sort((a, b) => a.localeCompare(b, 'zh-CN', { sensitivity: 'base' }))
);

const DEFAULT_TEMPLATE_FORM = {
  template_id: '',
  namespace: '',
  service_name: '',
  source_service: '',
  target_service: '',
  threshold: '',
  duration: '',
  severity: '',
  min_occurrence_count: '1',
  notification_cooldown_seconds: '300',
};

const EDGE_METRIC_NAMES = new Set([
  'edge_error_rate_5m',
  'edge_error_count_5m',
  'edge_call_count_5m',
  'edge_p95_ms_5m',
  'edge_p99_ms_5m',
  'edge_timeout_rate_5m',
  'edge_retries_per_call_5m',
  'edge_pending_per_call_5m',
  'edge_dlq_per_call_5m',
]);

const formatConditionLabel = (condition: string): string => {
  switch (condition) {
    case 'gt':
      return '>';
    case 'gte':
      return '>=';
    case 'lt':
      return '<';
    case 'lte':
      return '<=';
    case 'eq':
      return '=';
    default:
      return condition;
  }
};

const resolveMetricCategory = (metricName?: string | null, labels?: Record<string, string> | null): string => {
  const category = String(labels?.category || '').trim();
  if (category) {
    return category;
  }
  const metric = String(metricName || '').trim().toLowerCase();
  if (!metric) {
    return 'general';
  }
  if (metric.includes('latency') || metric.includes('_p95_') || metric.includes('_p99_')) {
    return 'performance';
  }
  if (metric.includes('error') || metric.includes('timeout')) {
    return 'availability';
  }
  if (metric.includes('call_count') || metric.includes('traffic')) {
    return 'traffic';
  }
  if (metric.includes('retry') || metric.includes('pending') || metric.includes('dlq')) {
    return 'queue';
  }
  return 'general';
};

const formatCategoryLabel = (category: string): string => {
  switch (category) {
    case 'edge':
      return '链路';
    case 'trace':
      return '链路追踪';
    case 'logs':
      return '日志';
    case 'availability':
      return '可用性';
    case 'performance':
      return '性能';
    case 'traffic':
      return '流量';
    case 'queue':
      return '队列';
    default:
      return category || '通用';
  }
};

const categoryBadgeClassName = (category: string): string => {
  switch (category) {
    case 'edge':
    case 'trace':
      return 'bg-sky-100 text-sky-700';
    case 'logs':
      return 'bg-amber-100 text-amber-700';
    case 'availability':
      return 'bg-rose-100 text-rose-700';
    case 'performance':
      return 'bg-violet-100 text-violet-700';
    case 'traffic':
      return 'bg-emerald-100 text-emerald-700';
    case 'queue':
      return 'bg-cyan-100 text-cyan-700';
    default:
      return 'bg-gray-100 text-gray-700';
  }
};

const isEdgeTemplate = (template?: AlertRuleTemplate | null): boolean => {
  if (!template) {
    return false;
  }
  return String(template?.labels?.scope || '').trim().toLowerCase() === 'edge'
    || EDGE_METRIC_NAMES.has(String(template.metric_name || '').trim());
};

const isEdgeRule = (rule?: { metric_name?: string | null; source_service?: string | null; target_service?: string | null; labels?: Record<string, string> | null } | null): boolean => {
  if (!rule) {
    return false;
  }
  return EDGE_METRIC_NAMES.has(String(rule.metric_name || '').trim())
    || Boolean(String(rule.source_service || '').trim())
    || Boolean(String(rule.target_service || '').trim())
    || String(rule?.labels?.scope || '').trim().toLowerCase() === 'edge';
};

const buildEdgeServiceLabel = (source?: string | null, target?: string | null, fallback?: string | null): string => {
  const normalizedSource = String(source || '').trim();
  const normalizedTarget = String(target || '').trim();
  const normalizedFallback = String(fallback || '').trim();
  if (normalizedSource && normalizedTarget) {
    return `${normalizedSource} -> ${normalizedTarget}`;
  }
  if (normalizedSource) {
    return normalizedSource;
  }
  if (normalizedTarget) {
    return normalizedTarget;
  }
  return normalizedFallback || '*';
};

const buildTemplateFormState = (template?: AlertRuleTemplate | null, overrides?: Partial<typeof DEFAULT_TEMPLATE_FORM>) => ({
  ...DEFAULT_TEMPLATE_FORM,
  template_id: template?.id || '',
  threshold: template ? String(template.threshold ?? '') : DEFAULT_TEMPLATE_FORM.threshold,
  duration: template ? String(template.duration ?? '') : DEFAULT_TEMPLATE_FORM.duration,
  severity: template ? String(template.severity ?? 'warning') : DEFAULT_TEMPLATE_FORM.severity,
  ...overrides,
});

type ScopeFilter = 'all' | 'edge' | 'service';

const normalizeFilterValue = (value?: string | null): string => {
  const normalized = String(value || '').trim();
  return normalized || 'all';
};

const parseScopeFilter = (value?: string | null): ScopeFilter => {
  const normalized = String(value || '').trim().toLowerCase();
  if (normalized === 'edge' || normalized === 'service') {
    return normalized;
  }
  return 'all';
};

const parseAlertTab = (value?: string | null): 'events' | 'rules' => {
  return String(value || '').trim().toLowerCase() === 'rules' ? 'rules' : 'events';
};

const resolveInitialFilterValue = (
  params: URLSearchParams,
  primaryKey: string,
  fallbackKey?: string,
): string => normalizeFilterValue(params.get(primaryKey) || (fallbackKey ? params.get(fallbackKey) : null));

const AlertCenter: React.FC = () => {
  const navigation = useNavigation();
  const initialQueryParamsRef = useRef<URLSearchParams | null>(null);
  if (!initialQueryParamsRef.current) {
    initialQueryParamsRef.current = new URLSearchParams(navigation.currentSearch);
  }
  const initialQueryParams = initialQueryParamsRef.current;
  const [activeTab, setActiveTab] = useState<'events' | 'rules'>(() => parseAlertTab(initialQueryParams.get('tab')));
  const [statusFilter, setStatusFilter] = useState<string>(() => resolveInitialFilterValue(initialQueryParams, 'status'));
  const [severityFilter, setSeverityFilter] = useState<string>(() => resolveInitialFilterValue(initialQueryParams, 'severity'));
  const [eventScopeFilter, setEventScopeFilter] = useState<ScopeFilter>(() => parseScopeFilter(initialQueryParams.get('event_scope') || initialQueryParams.get('scope')));
  const [eventNamespaceFilter, setEventNamespaceFilter] = useState<string>(() => resolveInitialFilterValue(initialQueryParams, 'event_namespace', 'namespace'));
  const [eventServiceFilter, setEventServiceFilter] = useState<string>(() => resolveInitialFilterValue(initialQueryParams, 'event_service', 'service'));
  const [edgeSourceFilter, setEdgeSourceFilter] = useState<string>(() => resolveInitialFilterValue(initialQueryParams, 'event_source_service', 'source_service'));
  const [edgeTargetFilter, setEdgeTargetFilter] = useState<string>(() => resolveInitialFilterValue(initialQueryParams, 'event_target_service', 'target_service'));
  const [ruleScopeFilter, setRuleScopeFilter] = useState<ScopeFilter>(() => parseScopeFilter(initialQueryParams.get('rule_scope')));
  const [ruleNamespaceFilter, setRuleNamespaceFilter] = useState<string>(() => resolveInitialFilterValue(initialQueryParams, 'rule_namespace', 'namespace'));
  const [ruleServiceFilter, setRuleServiceFilter] = useState<string>(() => resolveInitialFilterValue(initialQueryParams, 'rule_service', 'service'));
  const [ruleEdgeSourceFilter, setRuleEdgeSourceFilter] = useState<string>(() => resolveInitialFilterValue(initialQueryParams, 'rule_source_service'));
  const [ruleEdgeTargetFilter, setRuleEdgeTargetFilter] = useState<string>(() => resolveInitialFilterValue(initialQueryParams, 'rule_target_service'));
  const [actionError, setActionError] = useState<string | null>(null);
  const [evaluating, setEvaluating] = useState<boolean>(false);
  const [creatingRule, setCreatingRule] = useState<boolean>(false);
  const [editingRuleId, setEditingRuleId] = useState<string | null>(null);
  const [editingRuleSnapshot, setEditingRuleSnapshot] = useState<AlertRule | null>(null);
  const [currentCursor, setCurrentCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<string[]>([]);
  const [templates, setTemplates] = useState<AlertRuleTemplate[]>([]);
  const [notifications, setNotifications] = useState<AlertNotification[]>([]);
  const [availableServices, setAvailableServices] = useState<string[]>([]);
  const [availableNamespaces, setAvailableNamespaces] = useState<string[]>([]);
  const [templateForm, setTemplateForm] = useState({ ...DEFAULT_TEMPLATE_FORM });
  const templateIdRef = useRef(templateForm.template_id);

  const { data: rulesData, loading: rulesLoading, error: rulesError, refetch: refetchRules } = useAlertRules();
  const { data: eventsData, loading: eventsLoading, error: eventsError, refetch: refetchEvents } = useAlertEvents({
    status: statusFilter === 'all' ? undefined : statusFilter,
    severity: severityFilter === 'all' ? undefined : severityFilter,
    service_name: eventServiceFilter === 'all' ? undefined : eventServiceFilter,
    source_service: edgeSourceFilter === 'all' ? undefined : edgeSourceFilter,
    target_service: edgeTargetFilter === 'all' ? undefined : edgeTargetFilter,
    namespace: eventNamespaceFilter === 'all' ? undefined : eventNamespaceFilter,
    scope: eventScopeFilter,
    cursor: currentCursor || undefined,
    limit: 50,
  });
  const { data: statsData, refetch: refetchStats } = useAlertStats();
  const selectedTemplate = useMemo(() => (
    templates.find((template) => template.id === templateForm.template_id) || null
  ), [templates, templateForm.template_id]);
  const selectedTemplateIsEdge = useMemo(() => isEdgeTemplate(selectedTemplate), [selectedTemplate]);

  useEffect(() => {
    setCurrentCursor(null);
    setCursorHistory([]);
  }, [statusFilter, severityFilter, eventScopeFilter, eventNamespaceFilter, eventServiceFilter, edgeSourceFilter, edgeTargetFilter]);

  useEffect(() => {
    templateIdRef.current = templateForm.template_id;
  }, [templateForm.template_id]);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }
    const params = new URLSearchParams();
    if (activeTab !== 'events') params.set('tab', activeTab);
    if (statusFilter !== 'all') params.set('status', statusFilter);
    if (severityFilter !== 'all') params.set('severity', severityFilter);
    if (eventScopeFilter !== 'all') params.set('event_scope', eventScopeFilter);
    if (eventNamespaceFilter !== 'all') params.set('event_namespace', eventNamespaceFilter);
    if (eventServiceFilter !== 'all') params.set('event_service', eventServiceFilter);
    if (edgeSourceFilter !== 'all') params.set('event_source_service', edgeSourceFilter);
    if (edgeTargetFilter !== 'all') params.set('event_target_service', edgeTargetFilter);
    if (ruleScopeFilter !== 'all') params.set('rule_scope', ruleScopeFilter);
    if (ruleNamespaceFilter !== 'all') params.set('rule_namespace', ruleNamespaceFilter);
    if (ruleServiceFilter !== 'all') params.set('rule_service', ruleServiceFilter);
    if (ruleEdgeSourceFilter !== 'all') params.set('rule_source_service', ruleEdgeSourceFilter);
    if (ruleEdgeTargetFilter !== 'all') params.set('rule_target_service', ruleEdgeTargetFilter);

    const nextSearch = params.toString();
    const currentSearch = window.location.search.replace(/^\?/, '');
    if (nextSearch === currentSearch) {
      return;
    }
    const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ''}`;
    window.history.replaceState(window.history.state, '', nextUrl);
  }, [
    activeTab,
    statusFilter,
    severityFilter,
    eventScopeFilter,
    eventNamespaceFilter,
    eventServiceFilter,
    edgeSourceFilter,
    edgeTargetFilter,
    ruleScopeFilter,
    ruleNamespaceFilter,
    ruleServiceFilter,
    ruleEdgeSourceFilter,
    ruleEdgeTargetFilter,
  ]);

  const applyTemplateDefaults = useCallback((templateId: string, templateList: AlertRuleTemplate[]) => {
    const matched = templateList.find((tpl) => tpl.id === templateId);
    if (!matched) return;
    setTemplateForm(prev => buildTemplateFormState(matched, {
      namespace: prev.namespace,
      service_name: prev.service_name,
      source_service: prev.source_service,
      target_service: prev.target_service,
      min_occurrence_count: prev.min_occurrence_count,
      notification_cooldown_seconds: prev.notification_cooldown_seconds,
    }));
  }, []);

  const resetRuleForm = useCallback((templateList: AlertRuleTemplate[] = templates) => {
    setEditingRuleId(null);
    setEditingRuleSnapshot(null);
    const fallbackTemplate = templateList.find((template) => template.id === templateIdRef.current) || templateList[0] || null;
    setTemplateForm(buildTemplateFormState(fallbackTemplate));
  }, [templates]);

  const findTemplateForRule = useCallback((rule: AlertRule, templateList: AlertRuleTemplate[]) => {
    const templateIdFromLabels = String(rule.labels?.template_id || '').trim();
    if (templateIdFromLabels) {
      const exactTemplate = templateList.find((template) => template.id === templateIdFromLabels);
      if (exactTemplate) {
        return exactTemplate;
      }
    }

    const edgeRule = isEdgeRule(rule);
    return templateList.find((template) => (
      String(template.metric_name || '').trim() === String(rule.metric_name || '').trim()
      && isEdgeTemplate(template) === edgeRule
    )) || null;
  }, []);

  const startEditRule = useCallback((rule: AlertRule) => {
    const matchedTemplate = findTemplateForRule(rule, templates);
    setEditingRuleId(rule.id);
    setEditingRuleSnapshot(rule);
    setTemplateForm(buildTemplateFormState(matchedTemplate, {
      namespace: String(rule.namespace || rule.labels?.namespace || '').trim(),
      service_name: String(rule.service_name || '').trim(),
      source_service: String(rule.source_service || '').trim(),
      target_service: String(rule.target_service || '').trim(),
      threshold: String(rule.threshold ?? ''),
      duration: String(rule.duration ?? ''),
      severity: String(rule.severity || matchedTemplate?.severity || 'warning'),
      min_occurrence_count: String(rule.min_occurrence_count ?? 1),
      notification_cooldown_seconds: String(rule.notification_cooldown_seconds ?? 300),
    }));
    setActionError(null);
    setActiveTab('rules');
  }, [findTemplateForRule, templates]);

  const loadPhaseCData = useCallback(async () => {
    const [templateResult, notificationResult, facetsResult] = await Promise.all([
      api.getAlertRuleTemplates(),
      api.getAlertNotifications({ limit: 8 }),
      api.getLogFacets({ time_window: '1 HOUR', limit_services: 300, limit_namespaces: 300, limit_levels: 20 }),
    ]);
    const templateList = templateResult?.templates || [];
    setTemplates(templateList);
    setNotifications(notificationResult?.notifications || []);
    setAvailableServices(normalizeFacetOptions(facetsResult?.services || []));
    setAvailableNamespaces(normalizeFacetOptions(facetsResult?.namespaces || []));
    if (!editingRuleId && !templateIdRef.current && templateList.length > 0) {
      setTemplateForm(buildTemplateFormState(templateList[0]));
    }
  }, [editingRuleId]);

  useEffect(() => {
    loadPhaseCData().catch((error) => {
      console.error('Failed to load phase-C alert data:', error);
    });
  }, [loadPhaseCData]);

  const toggleRule = async (ruleId: string, enabled: boolean) => {
    try {
      setActionError(null);
      await api.updateAlertRule(ruleId, { enabled: !enabled });
      await Promise.all([refetchRules(), refetchStats(), loadPhaseCData()]);
    } catch (error) {
      console.error('Failed to toggle rule:', error);
      setActionError('切换规则状态失败，请稍后重试。');
    }
  };

  const deleteRule = async (ruleId: string) => {
    if (!confirm('确定要删除此告警规则吗？')) return;
    try {
      setActionError(null);
      await api.deleteAlertRule(ruleId);
      if (editingRuleId === ruleId) {
        resetRuleForm();
      }
      await Promise.all([refetchRules(), refetchStats(), loadPhaseCData()]);
    } catch (error) {
      console.error('Failed to delete rule:', error);
      setActionError('删除规则失败，请稍后重试。');
    }
  };

  const handleRefresh = async () => {
    setActionError(null);
    try {
      if (activeTab === 'events') {
        await Promise.all([refetchEvents(), refetchStats(), loadPhaseCData()]);
      } else {
        await Promise.all([refetchRules(), refetchStats(), loadPhaseCData()]);
      }
    } catch (error) {
      console.error('Failed to refresh alert center:', error);
      setActionError('刷新失败，请检查后端服务状态。');
    }
  };

  const handleEvaluateNow = async () => {
    setActionError(null);
    try {
      setEvaluating(true);
      await api.evaluateAlertRules();
      await Promise.all([refetchEvents(), refetchStats(), loadPhaseCData()]);
    } catch (error) {
      console.error('Failed to evaluate alert rules:', error);
      setActionError('手动评估失败，请稍后重试。');
    } finally {
      setEvaluating(false);
    }
  };

  const handleAcknowledge = async (eventId: string) => {
    try {
      setActionError(null);
      await api.acknowledgeAlertEvent(eventId);
      await Promise.all([refetchEvents(), refetchStats(), loadPhaseCData()]);
    } catch (error) {
      console.error('Failed to acknowledge alert event:', error);
      setActionError('确认告警失败，请稍后重试。');
    }
  };

  const handleSilence = async (eventId: string, seconds = 3600) => {
    try {
      setActionError(null);
      await api.silenceAlertEvent(eventId, seconds);
      await Promise.all([refetchEvents(), refetchStats(), loadPhaseCData()]);
    } catch (error) {
      console.error('Failed to silence alert event:', error);
      setActionError('静默告警失败，请稍后重试。');
    }
  };

  const handleResolve = async (eventId: string) => {
    try {
      setActionError(null);
      await api.resolveAlertEvent(eventId, 'resolved from alert center');
      await Promise.all([refetchEvents(), refetchStats(), loadPhaseCData()]);
    } catch (error) {
      console.error('Failed to resolve alert event:', error);
      setActionError('关闭告警失败，请稍后重试。');
    }
  };

  const handleNextPage = () => {
    if (!eventsData?.next_cursor) return;
    setCursorHistory(prev => [...prev, currentCursor || '']);
    setCurrentCursor(eventsData.next_cursor);
  };

  const handlePrevPage = () => {
    setCursorHistory(prev => {
      if (!prev.length) {
        return prev;
      }
      const nextHistory = [...prev];
      const previousCursor = nextHistory.pop() || '';
      setCurrentCursor(previousCursor || null);
      return nextHistory;
    });
  };

  const handleAnalyzeAlert = (alert: AlertEvent) => {
    navigation.goToAIAnalysis({
      message: `分析告警: ${alert.rule_name}\n服务: ${alert.service_name}\n消息: ${alert.message}`,
      serviceName: alert.service_name,
    });
  };

  const handleSubmitRuleForm = async () => {
    if (!templateForm.template_id || !selectedTemplate) {
      setActionError('请选择规则模板。');
      return;
    }

    if (selectedTemplateIsEdge) {
      const sourceService = templateForm.source_service.trim();
      const targetService = templateForm.target_service.trim();
      if (!sourceService || !targetService) {
        setActionError('边级规则需要同时选择 source_service 和 target_service。');
        return;
      }
    }

    try {
      setCreatingRule(true);
      setActionError(null);

      if (editingRuleId && editingRuleSnapshot) {
        const updatePayload: Partial<AlertRule> = {
          namespace: templateForm.namespace.trim(),
          service_name: selectedTemplateIsEdge ? undefined : templateForm.service_name.trim(),
          source_service: selectedTemplateIsEdge ? templateForm.source_service.trim() : undefined,
          target_service: selectedTemplateIsEdge ? templateForm.target_service.trim() : undefined,
          threshold: Number(templateForm.threshold || selectedTemplate.threshold || 0),
          duration: Math.max(0, Number(templateForm.duration || selectedTemplate.duration || 0)),
          severity: (templateForm.severity || selectedTemplate.severity || 'warning') as AlertRule['severity'],
          min_occurrence_count: Math.max(1, Number(templateForm.min_occurrence_count || 1)),
          notification_cooldown_seconds: Math.max(0, Number(templateForm.notification_cooldown_seconds || 0)),
          labels: {
            ...(editingRuleSnapshot.labels || {}),
            template_id: selectedTemplate.id,
          },
        };
        await api.updateAlertRule(editingRuleId, updatePayload);
      } else {
        const payload: Parameters<typeof api.createAlertRuleFromTemplate>[0] = {
          template_id: templateForm.template_id,
          namespace: templateForm.namespace.trim() || undefined,
          service_name: selectedTemplateIsEdge ? undefined : (templateForm.service_name.trim() || undefined),
          source_service: selectedTemplateIsEdge ? (templateForm.source_service.trim() || undefined) : undefined,
          target_service: selectedTemplateIsEdge ? (templateForm.target_service.trim() || undefined) : undefined,
          severity: templateForm.severity || undefined,
          min_occurrence_count: Math.max(1, Number(templateForm.min_occurrence_count || 1)),
          notification_cooldown_seconds: Math.max(0, Number(templateForm.notification_cooldown_seconds || 0)),
          notification_enabled: true,
          notification_channels: ['inapp'],
          labels: { template_id: selectedTemplate.id },
        };

        if (templateForm.threshold.trim() !== '') {
          payload.threshold = Number(templateForm.threshold);
        }
        if (templateForm.duration.trim() !== '') {
          payload.duration = Math.max(0, Number(templateForm.duration));
        }

        await api.createAlertRuleFromTemplate(payload);
      }

      await Promise.all([refetchRules(), refetchStats(), loadPhaseCData()]);
      resetRuleForm();
      setActiveTab('rules');
    } catch (error) {
      console.error('Failed to submit alert rule form:', error);
      setActionError(editingRuleId ? '规则更新失败，请检查参数后重试。' : '模板建规则失败，请检查参数后重试。');
    } finally {
      setCreatingRule(false);
    }
  };

  const isLoading = activeTab === 'events' ? eventsLoading : rulesLoading;
  const hasError = activeTab === 'events' ? eventsError : rulesError;
  const refetch = activeTab === 'events' ? refetchEvents : refetchRules;
  const firingCount = Number(statsData?.firing ?? statsData?.firing_events ?? 0);
  const resolvedCount = Number(statsData?.resolved ?? statsData?.resolved_events ?? 0);
  const currentPage = cursorHistory.length + 1;
  const totalNotifications = Number(statsData?.total_notifications ?? notifications.length ?? 0);
  const filteredRules = useMemo(() => {
    const rules = rulesData?.rules || [];
    return rules.filter((rule: AlertRule) => {
      const ruleNamespace = String(rule?.namespace || rule?.labels?.namespace || '').trim();
      const ruleSourceService = String(rule?.source_service || '').trim();
      const ruleTargetService = String(rule?.target_service || '').trim();
      const edgeRule = isEdgeRule(rule);
      if (ruleScopeFilter === 'edge' && !edgeRule) {
        return false;
      }
      if (ruleScopeFilter === 'service' && edgeRule) {
        return false;
      }
      if (ruleNamespaceFilter !== 'all' && ruleNamespace !== ruleNamespaceFilter) {
        return false;
      }
      if (ruleServiceFilter !== 'all') {
        const matchesSelectedService = [
          String(rule?.service_name || '').trim(),
          ruleSourceService,
          ruleTargetService,
        ].filter(Boolean).includes(ruleServiceFilter);
        if (!matchesSelectedService) {
          return false;
        }
      }
      if (ruleEdgeSourceFilter !== 'all' && ruleSourceService !== ruleEdgeSourceFilter) {
        return false;
      }
      if (ruleEdgeTargetFilter !== 'all' && ruleTargetService !== ruleEdgeTargetFilter) {
        return false;
      }
      return true;
    });
  }, [ruleScopeFilter, ruleNamespaceFilter, ruleServiceFilter, ruleEdgeSourceFilter, ruleEdgeTargetFilter, rulesData?.rules]);

  const templateGroups = useMemo(() => {
    const edgeTemplates = templates
      .filter((template) => isEdgeTemplate(template))
      .sort((a, b) => String(a?.name || '').localeCompare(String(b?.name || ''), 'zh-CN', { sensitivity: 'base' }));
    const serviceTemplates = templates
      .filter((template) => !isEdgeTemplate(template))
      .sort((a, b) => String(a?.name || '').localeCompare(String(b?.name || ''), 'zh-CN', { sensitivity: 'base' }));

    return [
      { label: `边级模板 (${edgeTemplates.length})`, templates: edgeTemplates },
      { label: `服务模板 (${serviceTemplates.length})`, templates: serviceTemplates },
    ].filter((group) => group.templates.length > 0);
  }, [templates]);

  const eventSummary = useMemo(() => {
    const currentEvents = eventsData?.events || [];
    return currentEvents.reduce((summary, event) => {
      const edgeEvent = isEdgeRule({
        metric_name: event.metric_name,
        source_service: event.source_service,
        target_service: event.target_service,
        labels: event.labels || null,
      });
      summary.total += 1;
      if (edgeEvent) {
        summary.edge += 1;
      } else {
        summary.service += 1;
      }
      if (event.status === 'firing') {
        summary.firing += 1;
      }
      if (event.status !== 'resolved') {
        summary.open += 1;
      }
      if (event.severity === 'critical') {
        summary.critical += 1;
      }
      return summary;
    }, {
      total: 0,
      edge: 0,
      service: 0,
      firing: 0,
      open: 0,
      critical: 0,
    });
  }, [eventsData?.events]);

  const ruleSummary = useMemo(() => filteredRules.reduce((summary, rule) => {
    const edgeRule = isEdgeRule(rule);
    summary.total += 1;
    if (edgeRule) {
      summary.edge += 1;
    } else {
      summary.service += 1;
    }
    if (rule.enabled) {
      summary.enabled += 1;
    } else {
      summary.disabled += 1;
    }
    if (rule.severity === 'critical') {
      summary.critical += 1;
    }
    return summary;
  }, {
    total: 0,
    edge: 0,
    service: 0,
    enabled: 0,
    disabled: 0,
    critical: 0,
  }), [filteredRules]);

  const eventSummaryCards = [
    { label: '当前页事件', value: eventSummary.total, tone: 'text-gray-900' },
    { label: '未恢复', value: eventSummary.open, tone: 'text-amber-700' },
    { label: '触发中', value: eventSummary.firing, tone: 'text-rose-700' },
    { label: '边级事件', value: eventSummary.edge, tone: 'text-sky-700' },
    { label: '严重事件', value: eventSummary.critical, tone: 'text-red-700' },
  ];

  const ruleSummaryCards = [
    { label: '筛选规则', value: ruleSummary.total, tone: 'text-gray-900' },
    { label: '已启用', value: ruleSummary.enabled, tone: 'text-emerald-700' },
    { label: '已禁用', value: ruleSummary.disabled, tone: 'text-gray-600' },
    { label: '边级规则', value: ruleSummary.edge, tone: 'text-sky-700' },
    { label: '严重规则', value: ruleSummary.critical, tone: 'text-red-700' },
  ];

  return (
    <div className="flex flex-col h-full">
      {/* 头部 */}
      <div className="bg-white border-b border-gray-200 px-6 py-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Bell className="w-6 h-6 text-amber-500" />
            <h1 className="text-xl font-semibold text-gray-900">告警中心</h1>
          </div>

          <div className="flex items-center gap-3">
            {/* 统计信息 */}
            {statsData && (
              <div className="flex items-center gap-4 text-sm">
                <div className="flex items-center gap-1">
                  <div className="w-2 h-2 rounded-full bg-red-500" />
                  <span className="text-gray-600">触发: {firingCount}</span>
                </div>
                <div className="flex items-center gap-1">
                  <div className="w-2 h-2 rounded-full bg-green-500" />
                  <span className="text-gray-600">已解决: {resolvedCount}</span>
                </div>
                <div className="flex items-center gap-1">
                  <div className="w-2 h-2 rounded-full bg-blue-500" />
                  <span className="text-gray-600">通知: {totalNotifications}</span>
                </div>
              </div>
            )}

            <button
              onClick={handleEvaluateNow}
              disabled={evaluating}
              className="px-3 py-2 text-xs bg-amber-50 text-amber-700 rounded-lg hover:bg-amber-100 disabled:opacity-50 transition-colors"
              title="手动评估告警规则"
            >
              {evaluating ? '评估中...' : '立即评估'}
            </button>
            <button
              onClick={handleRefresh}
              className="p-2 hover:bg-gray-100 text-gray-600 rounded-lg transition-colors"
              title="刷新"
            >
              <RefreshCw className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>

      {/* 主内容 */}
      <div className="flex-1 overflow-auto p-6">
        {isLoading ? (
          <LoadingState message="加载中..." />
        ) : hasError ? (
          <ErrorState message={hasError?.message || String(hasError)} onRetry={() => refetch()} />
        ) : (
          <div className="max-w-6xl mx-auto">
            {actionError && (
              <div className="mb-4 px-3 py-2 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">
                {actionError}
              </div>
            )}
            {/* Tab 切换 */}
            <div className="flex gap-4 mb-6">
              <button
                onClick={() => setActiveTab('events')}
                className={`px-4 py-2 rounded-lg font-medium transition-colors ${
                  activeTab === 'events'
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                }`}
              >
                告警事件
              </button>
              <button
                onClick={() => setActiveTab('rules')}
                className={`px-4 py-2 rounded-lg font-medium transition-colors ${
                  activeTab === 'rules'
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                }`}
              >
                告警规则
              </button>
            </div>

            {activeTab === 'events' ? (
              <>
                {/* 过滤器 */}
                <div className="mb-2 text-[11px] text-gray-500">
                  事件过滤支持通用服务筛选，也支持对边级事件按 source_service / target_service 精确命中。
                </div>
                <div className="mb-4 grid grid-cols-1 gap-3 md:grid-cols-7">
                  <select
                    value={statusFilter}
                    onChange={(e) => setStatusFilter(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部状态</option>
                    <option value="pending">待触发</option>
                    <option value="firing">触发中</option>
                    <option value="acknowledged">已确认</option>
                    <option value="silenced">已静默</option>
                    <option value="resolved">已解决</option>
                  </select>
                  <select
                    value={eventScopeFilter}
                    onChange={(e) => setEventScopeFilter(e.target.value as 'all' | 'edge' | 'service')}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部范围</option>
                    <option value="edge">仅边级</option>
                    <option value="service">仅服务级</option>
                  </select>
                  <select
                    value={severityFilter}
                    onChange={(e) => setSeverityFilter(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部级别</option>
                    <option value="critical">严重</option>
                    <option value="warning">警告</option>
                    <option value="info">信息</option>
                  </select>
                  <select
                    value={eventNamespaceFilter}
                    onChange={(e) => setEventNamespaceFilter(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部命名空间</option>
                    {availableNamespaces.map((namespace) => (
                      <option key={`ns-${namespace}`} value={namespace}>
                        {namespace}
                      </option>
                    ))}
                  </select>
                  <select
                    value={eventServiceFilter}
                    onChange={(e) => setEventServiceFilter(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部服务</option>
                    {availableServices.map((serviceName) => (
                      <option key={`svc-${serviceName}`} value={serviceName}>
                        {serviceName}
                      </option>
                    ))}
                  </select>
                  <select
                    value={edgeSourceFilter}
                    onChange={(e) => setEdgeSourceFilter(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部 source</option>
                    {availableServices.map((serviceName) => (
                      <option key={`edge-source-${serviceName}`} value={serviceName}>
                        {serviceName}
                      </option>
                    ))}
                  </select>
                  <select
                    value={edgeTargetFilter}
                    onChange={(e) => setEdgeTargetFilter(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部 target</option>
                    {availableServices.map((serviceName) => (
                      <option key={`edge-target-${serviceName}`} value={serviceName}>
                        {serviceName}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-5">
                  {eventSummaryCards.map((item) => (
                    <div key={item.label} className="rounded-lg border border-gray-200 bg-white px-3 py-3 shadow-sm">
                      <div className="text-[11px] uppercase tracking-wide text-gray-500">{item.label}</div>
                      <div className={`mt-1 text-xl font-semibold ${item.tone}`}>{item.value}</div>
                    </div>
                  ))}
                </div>

                <div className="mb-4 rounded-lg border border-blue-100 bg-blue-50/60 p-3">
                  <div className="flex items-center gap-2 text-sm font-medium text-blue-800">
                    <Megaphone className="h-4 w-4" />
                    最近通知
                  </div>
                  {notifications.length > 0 ? (
                    <div className="mt-2 space-y-1 text-xs text-blue-900">
                      {notifications.slice(0, 4).map((notice) => (
                        <div key={notice.id} className="flex items-center justify-between rounded bg-white/80 px-2 py-1">
                          <span className="truncate pr-3">
                            [{notice.channel}] {notice.rule_name} · {notice.delivery_status}
                          </span>
                          <span className="text-[11px] text-blue-700">{formatTime(notice.created_at)}</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="mt-2 text-xs text-blue-700">暂无通知记录</div>
                  )}
                </div>

                {/* 事件列表 */}
                {eventsData?.events && eventsData.events.length > 0 ? (
                  <div className="space-y-3">
                    {eventsData.events.map((event) => {
                      const eventNamespace = event.namespace || event.labels?.namespace || 'unknown';
                      const edgeEvent = isEdgeRule(event);
                      const eventCategory = resolveMetricCategory(event.metric_name, event.labels);
                      const eventServiceLabel = edgeEvent
                        ? buildEdgeServiceLabel(event.source_service, event.target_service, event.service_name)
                        : (event.service_name || '*');

                      return (
                        <div
                          key={event.id}
                          className="border border-gray-200 rounded-lg p-4 hover:bg-gray-50 transition-colors"
                        >
                          <div className="flex items-start justify-between">
                            <div className="flex-1">
                              <div className="flex items-center space-x-2">
                                <span
                                  className="w-2 h-2 rounded-full"
                                  style={{ backgroundColor: formatColor(event.status) }}
                                />
                                <span className="font-medium text-gray-900">{event.rule_name}</span>
                                <span
                                  className="px-2 py-0.5 text-xs rounded-full text-white"
                                  style={{ backgroundColor: formatColor(event.severity) }}
                                >
                                  {event.severity}
                                </span>
                                <span className={`rounded-full px-2 py-0.5 text-[11px] ${categoryBadgeClassName(eventCategory)}`}>
                                  {formatCategoryLabel(eventCategory)}
                                </span>
                                {edgeEvent && (
                                  <span className="rounded-full bg-sky-100 px-2 py-0.5 text-[11px] text-sky-700">
                                    边级
                                  </span>
                                )}
                              </div>
                              <p className="text-sm text-gray-600 mt-1">{event.message}</p>
                              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500">
                                <span>服务: {eventServiceLabel}</span>
                                <span>命名空间: {eventNamespace}</span>
                                <span>指标: {event.metric_name}</span>
                                <span>次数: {event.occurrence_count || 1}</span>
                                <span>通知: {event.notification_count || 0}</span>
                              </div>
                            </div>
                            <div className="text-right">
                              <div className="text-xs text-gray-500">{formatTime(event.fired_at)}</div>
                              <span
                                className={`inline-flex items-center mt-1 px-2 py-0.5 text-xs rounded ${
                                  event.status === 'firing'
                                    ? 'bg-red-100 text-red-700'
                                    : event.status === 'pending'
                                      ? 'bg-amber-100 text-amber-700'
                                      : event.status === 'acknowledged'
                                        ? 'bg-blue-100 text-blue-700'
                                        : event.status === 'silenced'
                                          ? 'bg-gray-100 text-gray-700'
                                          : 'bg-green-100 text-green-700'
                                }`}
                              >
                                {event.status === 'firing'
                                  ? '触发中'
                                  : event.status === 'pending'
                                    ? '待触发'
                                    : event.status === 'acknowledged'
                                      ? '已确认'
                                      : event.status === 'silenced'
                                        ? '已静默'
                                        : '已解决'}
                              </span>
                            </div>
                          </div>

                          {/* 操作按钮 */}
                          <div className="flex items-center gap-2 mt-3 pt-3 border-t border-gray-100">
                            <button
                              onClick={() => navigation.goToLogs({
                                serviceName: edgeEvent ? undefined : event.service_name,
                                namespace: eventNamespace === 'unknown' ? undefined : eventNamespace,
                                level: 'ERROR',
                                sourceService: event.source_service || undefined,
                                targetService: event.target_service || undefined,
                              })}
                              className="flex items-center gap-1 px-2 py-1 text-xs bg-blue-50 text-blue-600 rounded hover:bg-blue-100 transition-colors"
                            >
                              <FileText className="w-3 h-3" />
                              查看日志
                            </button>
                            <button
                              onClick={() => navigation.goToTopology({
                                serviceName: edgeEvent ? (event.source_service || event.service_name) : event.service_name,
                                namespace: eventNamespace === 'unknown' ? undefined : eventNamespace,
                              })}
                              className="flex items-center gap-1 px-2 py-1 text-xs bg-green-50 text-green-600 rounded hover:bg-green-100 transition-colors"
                            >
                              <Network className="w-3 h-3" />
                              查看拓扑
                            </button>
                            <button
                              onClick={() => handleAnalyzeAlert(event)}
                              className="flex items-center gap-1 px-2 py-1 text-xs bg-purple-50 text-purple-600 rounded hover:bg-purple-100 transition-colors"
                            >
                              <BrainCircuit className="w-3 h-3" />
                              AI 分析
                            </button>
                            {event.status !== 'resolved' && (
                              <button
                                onClick={() => handleResolve(event.id)}
                                className="px-2 py-1 text-xs bg-emerald-50 text-emerald-700 rounded hover:bg-emerald-100 transition-colors"
                              >
                                关闭
                              </button>
                            )}
                            {(event.status === 'pending' || event.status === 'firing') && (
                              <>
                                <button
                                  onClick={() => handleAcknowledge(event.id)}
                                  className="px-2 py-1 text-xs bg-sky-50 text-sky-700 rounded hover:bg-sky-100 transition-colors"
                                >
                                  确认
                                </button>
                                <button
                                  onClick={() => handleSilence(event.id)}
                                  className="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200 transition-colors"
                                >
                                  静默1h
                                </button>
                              </>
                            )}
                          </div>
                        </div>
                      );
                    })}
                    <div className="flex items-center justify-between pt-2">
                      <div className="text-xs text-gray-500">
                        第 {currentPage} 页 · 共 {eventsData.total} 条
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          onClick={handlePrevPage}
                          disabled={!cursorHistory.length}
                          className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-100 disabled:opacity-50"
                        >
                          <ChevronLeft className="w-3.5 h-3.5" />
                          上一页
                        </button>
                        <button
                          onClick={handleNextPage}
                          disabled={!eventsData.has_more}
                          className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-100 disabled:opacity-50"
                        >
                          下一页
                          <ChevronRight className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>
                  </div>
                ) : (
                  <EmptyState title="暂无告警事件" description="系统运行正常，没有触发告警" />
                )}
              </>
            ) : (
              <>
                <div className="mb-4 rounded-lg border border-emerald-100 bg-emerald-50/60 p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2 text-sm font-medium text-emerald-800">
                      <PlusCircle className="h-4 w-4" />
                      {editingRuleSnapshot ? '编辑规则' : '模板建规则'}
                    </div>
                    {selectedTemplate && (
                      <div className="text-[11px] text-emerald-700">
                        默认条件: {formatConditionLabel(selectedTemplate.condition)} {selectedTemplate.threshold} · 持续 {selectedTemplate.duration}s
                      </div>
                    )}
                  </div>
                  {editingRuleSnapshot && (
                    <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                      <div>
                        正在编辑: {editingRuleSnapshot.name} · {editingRuleSnapshot.metric_name}
                      </div>
                      <button
                        onClick={() => resetRuleForm()}
                        className="rounded border border-amber-300 bg-white px-2 py-1 text-[11px] text-amber-700 hover:bg-amber-100"
                      >
                        取消编辑
                      </button>
                    </div>
                  )}
                  <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-emerald-700">
                    {templateGroups.map((group) => (
                      <span key={group.label} className="rounded-full bg-white px-2 py-1 ring-1 ring-emerald-200">
                        {group.label}
                      </span>
                    ))}
                  </div>
                  <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
                    {templateGroups.map((group) => (
                      <div key={group.label} className="rounded-lg border border-emerald-200 bg-white/70 p-3">
                        <div className="text-xs font-medium text-emerald-800">{group.label}</div>
                        <div className="mt-2 flex flex-wrap gap-2">
                          {group.templates.map((tpl) => {
                            const active = tpl.id === templateForm.template_id;
                            return (
                              <button
                                key={tpl.id}
                                type="button"
                                onClick={() => {
                                  setTemplateForm(prev => ({ ...prev, template_id: tpl.id }));
                                  applyTemplateDefaults(tpl.id, templates);
                                }}
                                disabled={Boolean(editingRuleId)}
                                className={`rounded-full px-2.5 py-1 text-[11px] transition-colors ${
                                  active
                                    ? 'bg-emerald-600 text-white'
                                    : 'bg-emerald-50 text-emerald-700 hover:bg-emerald-100'
                                } disabled:cursor-not-allowed disabled:opacity-60`}
                              >
                                {tpl.name}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    ))}
                  </div>
                  {selectedTemplate && (
                    <div className="mt-3 rounded-lg border border-emerald-200 bg-white/80 p-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                          selectedTemplateIsEdge
                            ? 'bg-sky-100 text-sky-700'
                            : 'bg-emerald-100 text-emerald-700'
                        }`}>
                          {selectedTemplateIsEdge ? '边级模板' : '服务级模板'}
                        </span>
                        <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[11px] text-emerald-700">
                          {selectedTemplate.metric_name}
                        </span>
                        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-600">
                          默认级别 {selectedTemplate.severity}
                        </span>
                      </div>
                      <div className="mt-2 text-xs text-emerald-900">{selectedTemplate.description}</div>
                    </div>
                  )}
                  <datalist id="alert-service-options">
                    {availableServices.map((serviceName) => (
                      <option key={`tpl-svc-${serviceName}`} value={serviceName} />
                    ))}
                  </datalist>
                  <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-4">
                    <select
                      value={templateForm.template_id}
                      onChange={(e) => {
                        const nextId = e.target.value;
                        setTemplateForm(prev => ({ ...prev, template_id: nextId }));
                        applyTemplateDefaults(nextId, templates);
                      }}
                      disabled={Boolean(editingRuleId)}
                      className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs disabled:cursor-not-allowed disabled:bg-gray-100"
                    >
                      {templateGroups.map((group) => (
                        <optgroup key={group.label} label={group.label}>
                          {group.templates.map((tpl) => (
                            <option key={tpl.id} value={tpl.id}>
                              [{String(tpl.labels?.category || 'general')}] {tpl.name}
                            </option>
                          ))}
                        </optgroup>
                      ))}
                    </select>
                    <select
                      value={templateForm.namespace}
                      onChange={(e) => setTemplateForm(prev => ({ ...prev, namespace: e.target.value }))}
                      className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs"
                    >
                      <option value="">namespace(可选)</option>
                      {availableNamespaces.map((namespace) => (
                        <option key={`tpl-ns-${namespace}`} value={namespace}>
                          {namespace}
                        </option>
                      ))}
                    </select>
                    <select
                      value={templateForm.severity}
                      onChange={(e) => setTemplateForm(prev => ({ ...prev, severity: e.target.value }))}
                      className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs"
                    >
                      <option value="critical">critical</option>
                      <option value="warning">warning</option>
                      <option value="info">info</option>
                    </select>
                    {selectedTemplateIsEdge ? (
                      <input
                        list="alert-service-options"
                        value={templateForm.source_service}
                        onChange={(e) => setTemplateForm(prev => ({ ...prev, source_service: e.target.value }))}
                        placeholder="source_service"
                        className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs"
                      />
                    ) : (
                      <input
                        list="alert-service-options"
                        value={templateForm.service_name}
                        onChange={(e) => setTemplateForm(prev => ({ ...prev, service_name: e.target.value }))}
                        placeholder="service_name(可选)"
                        className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs md:col-span-2"
                      />
                    )}
                    {selectedTemplateIsEdge && (
                      <input
                        list="alert-service-options"
                        value={templateForm.target_service}
                        onChange={(e) => setTemplateForm(prev => ({ ...prev, target_service: e.target.value }))}
                        placeholder="target_service"
                        className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs"
                      />
                    )}
                  </div>
                  {selectedTemplateIsEdge && (
                    <div className="mt-2 text-[11px] text-emerald-700">
                      边级模板会按固定链路 source_service 到 target_service 评估，适合做链路错误率、延迟、超时率和调用量下降告警。
                    </div>
                  )}
                  <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-4">
                    <input
                      value={templateForm.threshold}
                      onChange={(e) => setTemplateForm(prev => ({ ...prev, threshold: e.target.value }))}
                      placeholder="threshold"
                      className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs"
                    />
                    <input
                      value={templateForm.duration}
                      onChange={(e) => setTemplateForm(prev => ({ ...prev, duration: e.target.value }))}
                      placeholder="duration(秒)"
                      className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs"
                    />
                    <input
                      value={templateForm.min_occurrence_count}
                      onChange={(e) => setTemplateForm(prev => ({ ...prev, min_occurrence_count: e.target.value }))}
                      placeholder="最小触发次数"
                      className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs"
                    />
                    <input
                      value={templateForm.notification_cooldown_seconds}
                      onChange={(e) => setTemplateForm(prev => ({ ...prev, notification_cooldown_seconds: e.target.value }))}
                      placeholder="通知冷却(秒)"
                      className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs"
                    />
                  </div>
                  <div className="mt-3 flex items-center justify-end gap-2">
                    {editingRuleSnapshot && (
                      <button
                        onClick={() => resetRuleForm()}
                        className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50"
                      >
                        取消
                      </button>
                    )}
                    <button
                      onClick={handleSubmitRuleForm}
                      disabled={creatingRule || templates.length === 0}
                      className="rounded bg-emerald-600 px-3 py-1.5 text-xs text-white hover:bg-emerald-700 disabled:opacity-50"
                    >
                      {creatingRule ? (editingRuleSnapshot ? '保存中...' : '创建中...') : (editingRuleSnapshot ? '保存规则' : '创建规则')}
                    </button>
                  </div>
                </div>

                <div className="mb-4 grid grid-cols-1 gap-3 md:grid-cols-5">
                  <select
                    value={ruleScopeFilter}
                    onChange={(e) => setRuleScopeFilter(e.target.value as 'all' | 'edge' | 'service')}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部规则范围</option>
                    <option value="edge">仅边级规则</option>
                    <option value="service">仅服务级规则</option>
                  </select>
                  <select
                    value={ruleNamespaceFilter}
                    onChange={(e) => setRuleNamespaceFilter(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部命名空间</option>
                    {availableNamespaces.map((namespace) => (
                      <option key={`rule-ns-${namespace}`} value={namespace}>
                        {namespace}
                      </option>
                    ))}
                  </select>
                  <select
                    value={ruleServiceFilter}
                    onChange={(e) => setRuleServiceFilter(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部服务</option>
                    {availableServices.map((serviceName) => (
                      <option key={`rule-svc-${serviceName}`} value={serviceName}>
                        {serviceName}
                      </option>
                    ))}
                  </select>
                  <select
                    value={ruleEdgeSourceFilter}
                    onChange={(e) => setRuleEdgeSourceFilter(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部规则 source</option>
                    {availableServices.map((serviceName) => (
                      <option key={`rule-edge-source-${serviceName}`} value={serviceName}>
                        {serviceName}
                      </option>
                    ))}
                  </select>
                  <select
                    value={ruleEdgeTargetFilter}
                    onChange={(e) => setRuleEdgeTargetFilter(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部规则 target</option>
                    {availableServices.map((serviceName) => (
                      <option key={`rule-edge-target-${serviceName}`} value={serviceName}>
                        {serviceName}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-5">
                  {ruleSummaryCards.map((item) => (
                    <div key={item.label} className="rounded-lg border border-gray-200 bg-white px-3 py-3 shadow-sm">
                      <div className="text-[11px] uppercase tracking-wide text-gray-500">{item.label}</div>
                      <div className={`mt-1 text-xl font-semibold ${item.tone}`}>{item.value}</div>
                    </div>
                  ))}
                </div>

                {/* 规则列表 */}
                {filteredRules.length > 0 ? (
                  <div className="bg-white rounded-lg shadow overflow-hidden">
                    <table className="min-w-full divide-y divide-gray-200">
                      <thead className="bg-gray-50">
                        <tr>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">规则名称</th>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">项目范围</th>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">严重级别</th>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">降噪策略</th>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">通知策略</th>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">状态</th>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">操作</th>
                        </tr>
                      </thead>
                      <tbody className="bg-white divide-y divide-gray-200">
                        {filteredRules.map((rule) => {
                          const edgeRule = isEdgeRule(rule);
                          const ruleCategory = resolveMetricCategory(rule.metric_name, rule.labels);
                          const scopeLabel = edgeRule
                            ? buildEdgeServiceLabel(rule.source_service, rule.target_service, rule.service_name)
                            : (rule.service_name || '*');

                          return (
                            <tr key={rule.id}>
                              <td className="px-6 py-4 whitespace-nowrap">
                                <div className="flex items-center gap-2">
                                  <div className="text-sm font-medium text-gray-900">{rule.name}</div>
                                  <span className={`rounded-full px-2 py-0.5 text-[11px] ${categoryBadgeClassName(ruleCategory)}`}>
                                    {formatCategoryLabel(ruleCategory)}
                                  </span>
                                  {edgeRule && (
                                    <span className="rounded-full bg-sky-100 px-2 py-0.5 text-[11px] text-sky-700">
                                      边级
                                    </span>
                                  )}
                                </div>
                                <div className="text-sm text-gray-500">{rule.description}</div>
                                <div className="mt-1 text-xs text-gray-500">
                                  {rule.metric_name} {formatConditionLabel(rule.condition)} {rule.threshold}
                                </div>
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap">
                                <div className="text-xs text-gray-700">ns: {rule.namespace || rule.labels?.namespace || '*'}</div>
                                <div className="text-xs text-gray-500">{edgeRule ? 'edge' : 'svc'}: {scopeLabel}</div>
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap">
                                <span className={`px-2 py-1 text-xs rounded-full ${
                                  rule.severity === 'critical' ? 'bg-red-100 text-red-700' :
                                  rule.severity === 'warning' ? 'bg-orange-100 text-orange-700' :
                                  'bg-blue-100 text-blue-700'
                                }`}>
                                  {rule.severity}
                                </span>
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap">
                                <div className="text-xs text-gray-700">最小触发: {rule.min_occurrence_count || 1}</div>
                                <div className="text-xs text-gray-500">持续: {rule.duration || 0}s</div>
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap">
                                <div className="text-xs text-gray-700">{rule.notification_enabled === false ? '关闭' : '开启'}</div>
                                <div className="text-xs text-gray-500">
                                  {(rule.notification_channels || ['inapp']).join(',')} · 冷却{rule.notification_cooldown_seconds ?? 0}s
                                </div>
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap">
                                <span className={`px-2 py-1 text-xs rounded-full ${
                                  rule.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-700'
                                }`}>
                                  {rule.enabled ? '已启用' : '已禁用'}
                                </span>
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap text-sm">
                                <button
                                  onClick={() => startEditRule(rule)}
                                  className="mr-3 text-amber-600 hover:text-amber-900"
                                >
                                  编辑
                                </button>
                                <button
                                  onClick={() => toggleRule(rule.id, rule.enabled)}
                                  className="text-blue-600 hover:text-blue-900 mr-3"
                                >
                                  {rule.enabled ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
                                </button>
                                <button
                                  onClick={() => deleteRule(rule.id)}
                                  className="text-red-600 hover:text-red-900"
                                >
                                  <Trash2 className="w-4 h-4" />
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <EmptyState title="暂无告警规则" description="请放宽项目筛选或点击上方按钮创建新规则" />
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

export default AlertCenter;
