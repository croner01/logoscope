/**
 * 告警中心页面
 * 参考 Datadog 设计风格
 * 添加页面联动功能
 * 增强告警管理和 AI 分析关联
 */
import React, { useEffect, useState } from 'react';
import { useAlertRules, useAlertEvents, useAlertStats } from '../hooks/useApi';
import { useNavigation } from '../hooks/useNavigation';
import { api } from '../utils/api';
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

const AlertCenter: React.FC = () => {
  const navigation = useNavigation();
  const [activeTab, setActiveTab] = useState<'events' | 'rules'>('events');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [severityFilter, setSeverityFilter] = useState<string>('all');
  const [actionError, setActionError] = useState<string | null>(null);
  const [evaluating, setEvaluating] = useState<boolean>(false);
  const [creatingRule, setCreatingRule] = useState<boolean>(false);
  const [currentCursor, setCurrentCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<string[]>([]);
  const [templates, setTemplates] = useState<any[]>([]);
  const [notifications, setNotifications] = useState<any[]>([]);
  const [templateForm, setTemplateForm] = useState({
    template_id: '',
    service_name: '',
    threshold: '',
    duration: '',
    severity: '',
    min_occurrence_count: '1',
    notification_cooldown_seconds: '300',
  });

  const { data: rulesData, loading: rulesLoading, error: rulesError, refetch: refetchRules } = useAlertRules();
  const { data: eventsData, loading: eventsLoading, error: eventsError, refetch: refetchEvents } = useAlertEvents({
    status: statusFilter === 'all' ? undefined : statusFilter,
    severity: severityFilter === 'all' ? undefined : severityFilter,
    cursor: currentCursor || undefined,
    limit: 50,
  });
  const { data: statsData, refetch: refetchStats } = useAlertStats();

  useEffect(() => {
    setCurrentCursor(null);
    setCursorHistory([]);
  }, [statusFilter, severityFilter]);

  const applyTemplateDefaults = (templateId: string, templateList: any[]) => {
    const matched = templateList.find((tpl: any) => tpl.id === templateId);
    if (!matched) return;
    setTemplateForm(prev => ({
      ...prev,
      template_id: matched.id,
      threshold: String(matched.threshold ?? ''),
      duration: String(matched.duration ?? ''),
      severity: String(matched.severity ?? 'warning'),
    }));
  };

  const loadPhaseCData = async () => {
    const [templateResult, notificationResult] = await Promise.all([
      api.getAlertRuleTemplates(),
      api.getAlertNotifications({ limit: 8 }),
    ]);
    const templateList = templateResult?.templates || [];
    setTemplates(templateList);
    setNotifications(notificationResult?.notifications || []);
    if (!templateForm.template_id && templateList.length > 0) {
      applyTemplateDefaults(templateList[0].id, templateList);
    }
  };

  useEffect(() => {
    loadPhaseCData().catch((error) => {
      console.error('Failed to load phase-C alert data:', error);
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  const handleAnalyzeAlert = (alert: any) => {
    navigation.goToAIAnalysis({
      message: `分析告警: ${alert.rule_name}\n服务: ${alert.service_name}\n消息: ${alert.message}`,
      serviceName: alert.service_name,
    });
  };

  const handleCreateRuleFromTemplate = async () => {
    if (!templateForm.template_id) {
      setActionError('请选择规则模板。');
      return;
    }

    const payload: any = {
      template_id: templateForm.template_id,
      service_name: templateForm.service_name.trim() || undefined,
      severity: templateForm.severity || undefined,
      min_occurrence_count: Math.max(1, Number(templateForm.min_occurrence_count || 1)),
      notification_cooldown_seconds: Math.max(0, Number(templateForm.notification_cooldown_seconds || 0)),
      notification_enabled: true,
      notification_channels: ['inapp'],
    };

    if (templateForm.threshold.trim() !== '') {
      payload.threshold = Number(templateForm.threshold);
    }
    if (templateForm.duration.trim() !== '') {
      payload.duration = Math.max(0, Number(templateForm.duration));
    }

    try {
      setCreatingRule(true);
      setActionError(null);
      await api.createAlertRuleFromTemplate(payload);
      await Promise.all([refetchRules(), refetchStats(), loadPhaseCData()]);
      setActiveTab('rules');
    } catch (error) {
      console.error('Failed to create alert rule from template:', error);
      setActionError('模板建规则失败，请检查参数后重试。');
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
                <div className="flex gap-4 mb-4">
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
                    value={severityFilter}
                    onChange={(e) => setSeverityFilter(e.target.value)}
                    className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="all">全部级别</option>
                    <option value="critical">严重</option>
                    <option value="warning">警告</option>
                    <option value="info">信息</option>
                  </select>
                </div>

                <div className="mb-4 rounded-lg border border-blue-100 bg-blue-50/60 p-3">
                  <div className="flex items-center gap-2 text-sm font-medium text-blue-800">
                    <Megaphone className="h-4 w-4" />
                    最近通知
                  </div>
                  {notifications.length > 0 ? (
                    <div className="mt-2 space-y-1 text-xs text-blue-900">
                      {notifications.slice(0, 4).map((notice: any) => (
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
                    {eventsData.events.map((event: any) => (
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
                            </div>
                            <p className="text-sm text-gray-600 mt-1">{event.message}</p>
                            <div className="flex items-center space-x-4 mt-2 text-xs text-gray-500">
                              <span>服务: {event.service_name}</span>
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
                            onClick={() => navigation.goToLogs({ serviceName: event.service_name, level: 'ERROR' })}
                            className="flex items-center gap-1 px-2 py-1 text-xs bg-blue-50 text-blue-600 rounded hover:bg-blue-100 transition-colors"
                          >
                            <FileText className="w-3 h-3" />
                            查看日志
                          </button>
                          <button
                            onClick={() => navigation.goToTopology({ serviceName: event.service_name })}
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
                    ))}
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
                  <div className="flex items-center gap-2 text-sm font-medium text-emerald-800">
                    <PlusCircle className="h-4 w-4" />
                    模板建规则
                  </div>
                  <div className="mt-2 hidden grid-cols-6 gap-3 px-1 text-[11px] font-medium text-emerald-700 md:grid">
                    <div>规则模板</div>
                    <div>服务名</div>
                    <div>阈值</div>
                    <div>持续时间(秒)</div>
                    <div>最小触发次数</div>
                    <div>通知冷却(秒)</div>
                  </div>
                  <div className="mt-2 text-[11px] text-emerald-700 md:hidden">
                    字段顺序：规则模板 / 服务名 / 阈值 / 持续时间(秒) / 最小触发次数 / 通知冷却(秒)
                  </div>
                  <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-6">
                    <select
                      value={templateForm.template_id}
                      onChange={(e) => {
                        const nextId = e.target.value;
                        setTemplateForm(prev => ({ ...prev, template_id: nextId }));
                        applyTemplateDefaults(nextId, templates);
                      }}
                      className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs"
                    >
                      {templates.map((tpl: any) => (
                        <option key={tpl.id} value={tpl.id}>
                          {tpl.name}
                        </option>
                      ))}
                    </select>
                    <input
                      value={templateForm.service_name}
                      onChange={(e) => setTemplateForm(prev => ({ ...prev, service_name: e.target.value }))}
                      placeholder="service_name(可选)"
                      className="rounded border border-emerald-200 bg-white px-2 py-2 text-xs"
                    />
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
                  <div className="mt-3 flex items-center justify-end">
                    <button
                      onClick={handleCreateRuleFromTemplate}
                      disabled={creatingRule || templates.length === 0}
                      className="rounded bg-emerald-600 px-3 py-1.5 text-xs text-white hover:bg-emerald-700 disabled:opacity-50"
                    >
                      {creatingRule ? '创建中...' : '创建规则'}
                    </button>
                  </div>
                </div>

                {/* 规则列表 */}
                {rulesData?.rules && rulesData.rules.length > 0 ? (
                  <div className="bg-white rounded-lg shadow overflow-hidden">
                    <table className="min-w-full divide-y divide-gray-200">
                      <thead className="bg-gray-50">
                        <tr>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">规则名称</th>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">严重级别</th>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">降噪策略</th>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">通知策略</th>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">状态</th>
                          <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">操作</th>
                        </tr>
                      </thead>
                      <tbody className="bg-white divide-y divide-gray-200">
                        {rulesData.rules.map((rule: any) => (
                          <tr key={rule.id}>
                            <td className="px-6 py-4 whitespace-nowrap">
                              <div className="text-sm font-medium text-gray-900">{rule.name}</div>
                              <div className="text-sm text-gray-500">{rule.description}</div>
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
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <EmptyState title="暂无告警规则" description="点击上方按钮创建新规则" />
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
