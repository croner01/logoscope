/**
 * Dashboard 页面 - 指挥台增强版
 * 优化目标：更快感知加载、更清晰业务态势、更接近业内可观测性看板风格
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  AlertTriangle,
  ArrowRight,
  Bell,
  BellOff,
  Clock3,
  Gauge,
  Network,
  RefreshCw,
  Server,
  ShieldAlert,
} from 'lucide-react';

import EmptyState from '../components/common/EmptyState';
import Tooltip from '../components/common/Tooltip';
import { api } from '../utils/api';
import { useNavigation } from '../hooks/useNavigation';
import {
  useAlertEvents,
  useEvents,
  useHybridTopology,
  useInferenceQuality,
  useInferenceQualityAlerts,
  useLogsStats,
  useTraceStats,
} from '../hooks/useApi';
import { formatColor, formatTime } from '../utils/formatters';
import { resolveCanonicalServiceName } from '../utils/serviceName';

interface ServicePulse {
  service: string;
  total: number;
  errors: number;
  errorRate: number;
}

type LooseRecord = Record<string, unknown>;

const HOTSPOT_TIME_WINDOW = '1 HOUR';
const HOTSPOT_EXCLUDED_SERVICES_STORAGE_KEY = 'dashboard.hotspot.excluded_services';

function loadExcludedHotspotServices(): string[] {
  if (typeof window === 'undefined') {
    return [];
  }
  const raw = window.localStorage.getItem(HOTSPOT_EXCLUDED_SERVICES_STORAGE_KEY);
  if (!raw) {
    return [];
  }
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return Array.from(
      new Set(
        parsed
          .map((item) => resolveCanonicalServiceName(item))
          .filter((item) => item && item !== 'unknown'),
      ),
    );
  } catch (error) {
    console.warn('Failed to parse hotspot excluded services:', error);
    return [];
  }
}

const Dashboard: React.FC = () => {
  const navigate = useNavigate();
  const navigation = useNavigation();

  // 减少单次请求体积，优先提升首页响应速度
  const {
    data: eventsData,
    loading: eventsLoading,
    error: eventsError,
    refetch: refetchEvents,
  } = useEvents({ limit: 40, time_window: HOTSPOT_TIME_WINDOW, exclude_health_check: true });
  const {
    data: logsStatsData,
    loading: logsStatsLoading,
    error: logsStatsError,
    refetch: refetchLogsStats,
  } = useLogsStats({ time_window: HOTSPOT_TIME_WINDOW });
  const {
    data: traceStatsData,
    loading: traceStatsLoading,
    error: traceStatsError,
    refetch: refetchTraceStats,
  } = useTraceStats({ time_window: HOTSPOT_TIME_WINDOW });
  const {
    data: topologyData,
    loading: topologyLoading,
    error: topologyError,
  } = useHybridTopology({ time_window: HOTSPOT_TIME_WINDOW });
  const {
    data: alertsData,
    loading: alertsLoading,
    error: alertsError,
    refetch: refetchAlerts,
  } = useAlertEvents({ limit: 8, status: 'firing' });

  const {
    data: inferenceQualityData,
    loading: inferenceQualityLoading,
    error: inferenceQualityError,
    refetch: refetchInferenceQuality,
  } = useInferenceQuality({ time_window: HOTSPOT_TIME_WINDOW });
  const {
    data: inferenceAlertsData,
    loading: inferenceAlertsLoading,
    error: inferenceAlertsError,
    refetch: refetchInferenceAlerts,
  } = useInferenceQualityAlerts({
    time_window: HOTSPOT_TIME_WINDOW,
    min_coverage: 0.20,
    max_inferred_ratio: 0.80,
    max_false_positive_rate: 0.30,
  });

  const [suppressingMetric, setSuppressingMetric] = useState<string | null>(null);
  const [refreshingAll, setRefreshingAll] = useState<boolean>(false);
  const [hotspotExcludeInput, setHotspotExcludeInput] = useState<string>('');
  const [excludedHotspotServices, setExcludedHotspotServices] = useState<string[]>(() => loadExcludedHotspotServices());

  const events = useMemo(() => eventsData?.events || [], [eventsData?.events]);
  const alerts = useMemo(() => alertsData?.events || [], [alertsData?.events]);
  const topologyNodes = useMemo(() => topologyData?.nodes || [], [topologyData?.nodes]);
  const topologyEdges = useMemo(() => topologyData?.edges || [], [topologyData?.edges]);

  const levelBuckets = useMemo(() => logsStatsData?.byLevel || {}, [logsStatsData?.byLevel]);
  const serviceBuckets = useMemo(() => logsStatsData?.byService || {}, [logsStatsData?.byService]);
  const serviceErrorBuckets = useMemo(
    () => logsStatsData?.byServiceErrors || {},
    [logsStatsData?.byServiceErrors],
  );
  const totalEvents = Number(logsStatsData?.total || eventsData?.total || 0);
  const warnCount = Number(levelBuckets.WARN || 0) + Number(levelBuckets.WARNING || 0);
  const errorCount = Number(levelBuckets.ERROR || 0) + Number(levelBuckets.FATAL || 0);
  const abnormalCount = warnCount + errorCount;
  const debugTraceCount = Number(levelBuckets.DEBUG || 0) + Number(levelBuckets.TRACE || 0);
  const activeLogServices = Object.keys(serviceBuckets).length;
  const topLogServiceEntry = Object.entries(serviceBuckets).sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))[0];
  const topLogServiceName = topLogServiceEntry?.[0] || '--';
  const topLogServiceCount = Number(topLogServiceEntry?.[1] || 0);
  const topLogServiceRatio = totalEvents > 0 ? (topLogServiceCount / totalEvents) * 100 : 0;
  const errorRate = totalEvents > 0 ? (errorCount / totalEvents) * 100 : 0;
  const abnormalRate = totalEvents > 0 ? (abnormalCount / totalEvents) * 100 : 0;
  const debugTraceRate = totalEvents > 0 ? (debugTraceCount / totalEvents) * 100 : 0;
  const firingAlerts = alertsData?.total || 0;
  const topologyNodeCount = topologyNodes.length;
  const serviceNodeCount = topologyNodes.filter((node) => {
    const nodeRecord = node as unknown as LooseRecord;
    const type = String(nodeRecord?.type || '').trim().toLowerCase();
    if (!type) {
      return true;
    }
    return type === 'service' || type.includes('service');
  }).length;
  const activeServices = serviceNodeCount > 0 ? serviceNodeCount : topologyNodeCount;

  const avgDuration = Number(traceStatsData?.avg_duration ?? traceStatsData?.avg_latency ?? 0);
  const excludedHotspotServiceSet = useMemo(
    () => new Set(excludedHotspotServices.map((service) => service.toLowerCase())),
    [excludedHotspotServices],
  );

  const servicePulseBaseline = useMemo<ServicePulse[]>(() => {
    const buckets: Record<string, { total: number; errors: number }> = {};
    const ingest = (serviceValue: unknown, totalValue: unknown, errorValue: unknown): void => {
      const service = resolveCanonicalServiceName(serviceValue);
      if (!service || service === 'unknown') {
        return;
      }
      const totalNum = Number(totalValue);
      const errorNum = Number(errorValue);
      const total = Number.isFinite(totalNum) ? Math.max(totalNum, 0) : 0;
      const errors = Number.isFinite(errorNum) ? Math.max(errorNum, 0) : 0;
      if (total <= 0 && errors <= 0) {
        return;
      }
      if (!buckets[service]) {
        buckets[service] = { total: 0, errors: 0 };
      }
      buckets[service].total += total;
      buckets[service].errors += errors;
    };

    topologyNodes.forEach((node) => {
      const nodeRecord = node as unknown as LooseRecord;
      const metrics = (nodeRecord?.metrics as LooseRecord | undefined) || {};
      const hasLogCount = metrics?.log_count !== undefined && metrics?.log_count !== null;
      if (!hasLogCount) {
        return;
      }
      const serviceMeta = nodeRecord?.service as LooseRecord | undefined;
        ingest(
          metrics?.service_name || serviceMeta?.name || nodeRecord?.label || nodeRecord?.id || 'unknown',
          metrics?.log_count,
          metrics?.error_count ?? 0,
        );
      });

    // 当拓扑侧没有日志口径字段时，回退到 logs stats 聚合口径，避免受 events limit 采样影响。
    if (Object.keys(buckets).length === 0) {
      Object.entries(serviceBuckets).forEach(([service, total]) => {
        ingest(service, total, serviceErrorBuckets[service] || 0);
      });
    }

    return Object.entries(buckets)
      .map(([service, v]) => ({
        service,
        total: v.total,
        errors: v.errors,
        errorRate: v.total > 0 ? (v.errors / v.total) * 100 : (v.errors > 0 ? 100 : 0),
      }))
      .sort((a, b) => {
        if (b.errorRate !== a.errorRate) {
          return b.errorRate - a.errorRate;
        }
        return b.total - a.total;
      });
  }, [serviceBuckets, serviceErrorBuckets, topologyNodes]);

  const servicePulse = useMemo<ServicePulse[]>(
    () => servicePulseBaseline
      .filter((item) => !excludedHotspotServiceSet.has(item.service.toLowerCase()))
      .slice(0, 6),
    [excludedHotspotServiceSet, servicePulseBaseline],
  );

  const hotspotLogsPath = useMemo(() => {
    const params = new URLSearchParams();
    params.set('time_window', HOTSPOT_TIME_WINDOW);
    params.set('exclude_health_check', 'true');
    return `/logs?${params.toString()}`;
  }, []);

  const addExcludedHotspotServices = (rawValue: string): void => {
    const tokens = rawValue
      .split(/[\n,;]+/)
      .map((item) => resolveCanonicalServiceName(item))
      .filter((item) => item && item !== 'unknown');
    if (tokens.length === 0) {
      return;
    }
    setExcludedHotspotServices((prev) => Array.from(new Set([...prev, ...tokens])).sort((a, b) =>
      a.localeCompare(b, 'zh-CN', { sensitivity: 'base' }),
    ));
    setHotspotExcludeInput('');
  };

  const removeExcludedHotspotService = (service: string): void => {
    setExcludedHotspotServices((prev) => prev.filter((item) => item !== service));
  };

  const jumpToHotspotLogs = (service: string): void => {
    const normalizedService = resolveCanonicalServiceName(service);
    const params = new URLSearchParams();
    params.set('service', normalizedService);
    params.set('time_window', HOTSPOT_TIME_WINDOW);
    params.set('exclude_health_check', 'true');
    navigate(`/logs?${params.toString()}`);
  };

  const allHotspotServicesExcluded = servicePulseBaseline.length > 0 && servicePulse.length === 0;

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }
    window.localStorage.setItem(
      HOTSPOT_EXCLUDED_SERVICES_STORAGE_KEY,
      JSON.stringify(excludedHotspotServices),
    );
  }, [excludedHotspotServices]);

  const latestEventTime = useMemo(() => {
    const first = events[0]?.timestamp;
    if (!first) {
      return '暂无实时数据';
    }
    return formatTime(first);
  }, [events]);

  const inferenceMetrics = inferenceQualityData?.metrics || {};
  const coverage = Number(inferenceMetrics.coverage || 0);
  const inferredRatio = Number(inferenceMetrics.inferred_ratio || 0);
  const falsePositiveRate = Number(inferenceMetrics.false_positive_rate || 0);
  const falsePositiveRateState = String(inferenceMetrics.false_positive_rate_state || 'ok').toLowerCase();
  const falsePositiveUnavailable = falsePositiveRateState === 'unknown';
  const falsePositiveReason = String(inferenceMetrics.false_positive_rate_reason || '');
  const falsePositiveMinSample = Number(inferenceMetrics.false_positive_rate_min_sample || 0);
  const falsePositiveHint = falsePositiveReason === 'insufficient_inferred_sample'
    ? `推断样本不足（<${falsePositiveMinSample || 1}）`
    : '当前窗口无观测基线';

  const handleRefreshAll = async () => {
    try {
      setRefreshingAll(true);
      await Promise.all([
        refetchEvents(),
        refetchLogsStats(),
        refetchTraceStats(),
        refetchAlerts(),
        refetchInferenceQuality(),
        refetchInferenceAlerts(),
      ]);
    } finally {
      setRefreshingAll(false);
    }
  };

  const handleToggleInferenceSuppression = async (metric: string, suppressed: boolean) => {
    try {
      setSuppressingMetric(metric);
      await api.setInferenceAlertSuppression(metric, !suppressed);
      refetchInferenceAlerts();
      refetchInferenceQuality();
    } catch (error) {
      console.error('Failed to toggle inference suppression:', error);
    } finally {
      setSuppressingMetric(null);
    }
  };

  return (
    <div className="space-y-6 pb-2">
      <section className="rounded-2xl border border-slate-200 bg-[linear-gradient(120deg,#0f172a_0%,#1e293b_45%,#0f766e_100%)] text-white shadow-lg">
        <div className="px-6 py-6 md:px-8 md:py-7">
          <div className="flex flex-col gap-5 md:flex-row md:items-end md:justify-between">
            <div>
              <div className="inline-flex items-center gap-2 rounded-full border border-white/20 bg-white/10 px-3 py-1 text-xs tracking-wide">
                <Gauge className="h-3.5 w-3.5" />
                Observability Command Deck
              </div>
              <h1 className="mt-3 text-2xl font-bold tracking-tight md:text-3xl">仪表盘总览</h1>
              <p className="mt-2 text-sm text-slate-200">
                面向故障定位与稳定性运营，聚焦异常密度、服务健康和日志质量。
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={handleRefreshAll}
                disabled={refreshingAll}
                className="inline-flex items-center gap-2 rounded-lg bg-white/15 px-3 py-2 text-xs font-medium text-white hover:bg-white/25 disabled:opacity-60"
              >
                <RefreshCw className={`h-4 w-4 ${refreshingAll ? 'animate-spin' : ''}`} />
                全量刷新
              </button>
              <Link
                to="/alerts"
                className="inline-flex items-center gap-2 rounded-lg bg-amber-400 px-3 py-2 text-xs font-semibold text-slate-900 hover:bg-amber-300"
              >
                告警中心
                <ArrowRight className="h-4 w-4" />
              </Link>
            </div>
          </div>

          <div className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-5">
            <HeroStat title="最新数据点" value={latestEventTime} icon={<Clock3 className="h-4 w-4" />} />
            <HeroStat title="活跃服务" value={`${activeServices}`} icon={<Server className="h-4 w-4" />} />
            <HeroStat title="拓扑连接" value={`${topologyEdges.length}`} icon={<Network className="h-4 w-4" />} />
            <HeroStat title="触发告警" value={`${firingAlerts}`} icon={<ShieldAlert className="h-4 w-4" />} />
            <HeroStat title="错误率" value={`${errorRate.toFixed(2)}%`} icon={<AlertTriangle className="h-4 w-4" />} />
          </div>
        </div>
      </section>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <PrimaryKpi
          title="总事件"
          value={totalEvents.toLocaleString()}
          hint="最近 1 小时聚合统计"
          loading={logsStatsLoading}
          error={logsStatsError?.message}
          tone="blue"
        />
        <PrimaryKpi
          title="错误事件"
          value={errorCount.toLocaleString()}
          hint={errorRate >= 5 ? '高于建议阈值 5%' : '处于可控区间'}
          loading={logsStatsLoading}
          error={logsStatsError?.message}
          tone={errorRate >= 5 ? 'red' : 'green'}
        />
        <PrimaryKpi
          title="平均响应"
          value={`${avgDuration.toFixed(1)} ms`}
          hint="来自 Trace 聚合统计"
          loading={traceStatsLoading}
          error={traceStatsError?.message}
          tone={avgDuration > 800 ? 'amber' : 'green'}
        />
        <PrimaryKpi
          title="拓扑节点"
          value={topologyNodeCount.toLocaleString()}
          hint="服务映射规模"
          loading={topologyLoading}
          error={topologyError?.message}
          tone="slate"
        />
      </section>

      <section className="grid grid-cols-1 gap-6 xl:grid-cols-5">
        <div className="xl:col-span-3 rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
            <div>
              <h2 className="text-sm font-semibold text-slate-900">服务健康热区</h2>
              <p className="text-xs text-slate-500">按错误率 + 流量排序，统计窗口：最近 1 小时日志</p>
            </div>
            <Link to={hotspotLogsPath} className="text-xs font-medium text-teal-700 hover:text-teal-800">查看日志明细</Link>
          </div>
          <div className="p-4">
            <div className="mb-4 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <input
                  type="text"
                  value={hotspotExcludeInput}
                  onChange={(e) => setHotspotExcludeInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      addExcludedHotspotServices(hotspotExcludeInput);
                    }
                  }}
                  placeholder="屏蔽服务，支持逗号分隔"
                  className="h-8 min-w-[220px] flex-1 rounded-md border border-slate-300 bg-white px-2.5 text-xs text-slate-700 placeholder:text-slate-400"
                />
                <button
                  onClick={() => addExcludedHotspotServices(hotspotExcludeInput)}
                  className="h-8 rounded-md bg-slate-800 px-3 text-xs font-medium text-white hover:bg-slate-700"
                >
                  添加屏蔽
                </button>
                {excludedHotspotServices.length > 0 ? (
                  <button
                    onClick={() => setExcludedHotspotServices([])}
                    className="h-8 rounded-md border border-slate-300 px-3 text-xs text-slate-600 hover:bg-white"
                  >
                    清空
                  </button>
                ) : null}
              </div>
              {excludedHotspotServices.length > 0 ? (
                <div className="mt-2 flex flex-wrap gap-2">
                  {excludedHotspotServices.map((service) => (
                    <button
                      key={`excluded-${service}`}
                      onClick={() => removeExcludedHotspotService(service)}
                      className="rounded-full border border-slate-300 bg-white px-2.5 py-0.5 text-[11px] text-slate-600 hover:border-slate-400"
                    >
                      {service} · 移除
                    </button>
                  ))}
                </div>
              ) : (
                <div className="mt-2 text-[11px] text-slate-500">未配置屏蔽服务，热区将展示全部服务。</div>
              )}
            </div>

            {topologyLoading ? (
              <InlineLoading text="分析服务健康中..." />
            ) : topologyError ? (
              <InlineError text={topologyError.message || '服务健康计算失败'} />
            ) : allHotspotServicesExcluded ? (
              <EmptyState title="热区服务已全部屏蔽" description="请移除部分屏蔽服务后再查看热区排行" />
            ) : servicePulse.length === 0 ? (
              <EmptyState title="暂无服务健康样本" description="等待日志数据进入分析窗口" />
            ) : (
              <div className="space-y-3">
                {servicePulse.map((item) => {
                  const barColor = item.errorRate >= 20
                    ? 'bg-red-500'
                    : item.errorRate >= 8
                      ? 'bg-amber-500'
                      : 'bg-emerald-500';
                  return (
                    <div key={item.service} className="rounded-lg border border-slate-200">
                      <button
                        className="w-full px-3 py-2 text-left hover:bg-slate-50"
                        onClick={() => jumpToHotspotLogs(item.service)}
                      >
                        <div className="mb-1 flex items-center justify-between text-xs">
                          <span className="font-semibold text-slate-800">{item.service}</span>
                          <span className="text-slate-500">{item.errors}/{item.total} error</span>
                        </div>
                        <div className="h-2 rounded bg-slate-100">
                          <div className={`h-2 rounded ${barColor}`} style={{ width: `${Math.min(item.errorRate, 100)}%` }} />
                        </div>
                        <div className="mt-1 text-[11px] text-slate-500">错误率 {item.errorRate.toFixed(1)}%</div>
                      </button>
                      <div className="border-t border-slate-100 px-3 py-1.5">
                        <button
                          onClick={() => addExcludedHotspotServices(item.service)}
                          className="text-[11px] text-slate-500 hover:text-slate-700"
                        >
                          屏蔽此服务
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        <div className="xl:col-span-2 rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
            <h2 className="text-sm font-semibold text-slate-900">实时告警流</h2>
            <Link to="/alerts?tab=events&status=firing" className="text-xs font-medium text-teal-700 hover:text-teal-800">全部告警</Link>
          </div>
          <div className="p-4">
            {alertsLoading ? (
              <InlineLoading text="加载告警中..." />
            ) : alertsError ? (
              <InlineError text={alertsError.message || '告警加载失败'} />
            ) : alerts.length > 0 ? (
              <div className="space-y-2">
                {alerts.slice(0, 6).map((alert) => (
                  <button
                    key={alert.id}
                    className="w-full rounded-lg border border-slate-200 px-3 py-2 text-left hover:bg-slate-50"
                    onClick={() => {
                      const sourceService = String(alert.source_service || '').trim();
                      const targetService = String(alert.target_service || '').trim();
                      const edgeScope = sourceService || targetService ? 'edge' : 'service';
                      navigation.goToAlerts({
                        tab: 'events',
                        status: 'firing',
                        severity: alert.severity,
                        serviceName: alert.service_name || undefined,
                        namespace: alert.namespace || undefined,
                        scope: edgeScope,
                        sourceService: sourceService || undefined,
                        targetService: targetService || undefined,
                      });
                    }}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-xs font-semibold text-slate-800">{alert.rule_name}</span>
                      <span
                        className="rounded px-1.5 py-0.5 text-[10px] font-semibold text-white"
                        style={{ backgroundColor: formatColor(alert.severity) }}
                      >
                        {alert.severity}
                      </span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-[11px] text-slate-600">{alert.message}</p>
                    <div className="mt-1 text-[10px] text-slate-500">{alert.service_name} · {formatTime(alert.fired_at)}</div>
                  </button>
                ))}
              </div>
            ) : (
              <EmptyState title="当前无触发告警" description="系统状态稳定" />
            )}
          </div>
        </div>
      </section>

      <section className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <div className="rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
            <div>
              <div className="flex items-center gap-1">
                <h2 className="text-sm font-semibold text-slate-900">推断质量面板</h2>
                <Tooltip
                  title="推断质量指标说明"
                  lines={[
                    'Coverage：推断边覆盖率，建议长期保持在 20% 以上。',
                    'Inferred Ratio：推断边占比，过高可能代表观测信号不足。',
                    'False Positive：误报率，持续上升时建议回看推断模式与阈值。',
                  ]}
                  widthClass="w-[320px]"
                />
              </div>
              <p className="text-xs text-slate-500">面向拓扑推断可靠性</p>
            </div>
            <Link to="/alerts" className="text-xs font-medium text-teal-700 hover:text-teal-800">质量告警</Link>
          </div>

          <div className="grid grid-cols-1 gap-3 p-4 md:grid-cols-3">
            <QualityBlock title="Coverage" value={formatPercent(coverage)} status={coverage >= 0.2 ? 'ok' : 'warning'} />
            <QualityBlock title="Inferred Ratio" value={formatPercent(inferredRatio)} status={inferredRatio <= 0.8 ? 'ok' : 'warning'} />
            <QualityBlock
              title="False Positive"
              value={falsePositiveUnavailable ? 'N/A' : formatPercent(falsePositiveRate)}
              status={falsePositiveUnavailable ? 'neutral' : (falsePositiveRate <= 0.3 ? 'ok' : 'warning')}
              hint={falsePositiveUnavailable ? falsePositiveHint : undefined}
            />
          </div>

          <div className="border-t border-slate-100 px-4 py-3">
            {inferenceQualityLoading || inferenceAlertsLoading ? (
              <InlineLoading text="加载推断质量中..." />
            ) : inferenceQualityError || inferenceAlertsError ? (
              <InlineError text={inferenceQualityError?.message || inferenceAlertsError?.message || '推断质量加载失败'} />
            ) : inferenceAlertsData?.alerts?.length ? (
              <div className="space-y-2">
                {inferenceAlertsData.alerts.slice(0, 4).map((alert: unknown) => {
                  const alertRecord = alert as LooseRecord;
                  const metric = String(alertRecord.metric || '');
                  const expression = String(alertRecord.expression || '');
                  const value = Number(alertRecord.value || 0);
                  const suppressed = Boolean(alertRecord.suppressed);
                  return (
                    <div key={`${metric}-${expression}`} className="flex items-center justify-between rounded-lg border border-slate-200 px-3 py-2">
                      <div>
                        <div className="text-xs font-semibold text-slate-800">{metric}</div>
                        <div className="text-[11px] text-slate-500">{expression} · 当前 {formatPercent(value)}</div>
                      </div>
                      <button
                        onClick={() => handleToggleInferenceSuppression(metric, suppressed)}
                        disabled={!metric || suppressingMetric === metric}
                        className="inline-flex items-center gap-1 rounded-md bg-slate-100 px-2 py-1 text-[11px] text-slate-700 hover:bg-slate-200 disabled:opacity-50"
                      >
                        {suppressed ? <Bell className="h-3 w-3" /> : <BellOff className="h-3 w-3" />}
                        {suppressed ? '取消抑制' : '抑制'}
                      </button>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
                当前未触发推断质量告警
              </div>
            )}
          </div>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
            <div>
              <div className="flex items-center gap-1">
                <h2 className="text-sm font-semibold text-slate-900">日志质量指标（最近 1 小时）</h2>
                <Tooltip
                  title="日志质量指标口径"
                  lines={[
                    '仅基于日志数据统计，不包含发布回归/质量门禁口径。',
                    '错误率 = (ERROR + FATAL) / 总日志量。',
                    '告警级占比 = (WARN + ERROR + FATAL) / 总日志量。',
                  ]}
                  widthClass="w-[320px]"
                />
              </div>
              <p className="text-xs text-slate-500">用于快速识别日志异常密度、服务覆盖与集中度</p>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={refetchLogsStats}
                className="rounded-md bg-slate-100 px-2.5 py-1.5 text-xs text-slate-700 hover:bg-slate-200"
              >
                刷新
              </button>
            </div>
          </div>

          <div className="p-4">
            {logsStatsLoading ? (
              <InlineLoading text="加载日志质量指标中..." />
            ) : logsStatsError ? (
              <InlineError text={logsStatsError.message || '日志质量指标加载失败'} />
            ) : (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <ValueLine title="日志总量" value={totalEvents.toLocaleString()} tone={totalEvents > 0 ? 'good' : 'neutral'} />
                <ValueLine title="错误日志数" value={errorCount.toLocaleString()} tone={errorRate <= 5 ? 'good' : 'warning'} />
                <ValueLine title="错误日志占比" value={`${errorRate.toFixed(2)}%`} tone={errorRate <= 5 ? 'good' : 'warning'} />
                <ValueLine title="告警级占比 (WARN+)" value={`${abnormalRate.toFixed(2)}%`} tone={abnormalRate <= 10 ? 'good' : 'warning'} />
                <ValueLine title="调试日志占比 (TRACE+DEBUG)" value={`${debugTraceRate.toFixed(2)}%`} tone={debugTraceRate <= 30 ? 'good' : 'warning'} />
                <ValueLine title="产生日志服务数" value={activeLogServices.toLocaleString()} tone={activeLogServices > 0 ? 'good' : 'neutral'} />
                <ValueLine title="Top 服务" value={`${topLogServiceName} (${topLogServiceCount.toLocaleString()})`} tone={topLogServiceCount > 0 ? 'good' : 'neutral'} />
                <ValueLine title="Top 服务占比" value={`${topLogServiceRatio.toFixed(2)}%`} tone={topLogServiceRatio <= 40 ? 'good' : 'warning'} />
              </div>
            )}
          </div>
        </div>
      </section>

      <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-900">最新日志（快速跳转）</h2>
          <Link to="/logs" className="text-xs font-medium text-teal-700 hover:text-teal-800">进入日志中心</Link>
        </div>

        <div className="overflow-x-auto">
          {eventsLoading ? (
            <div className="p-4">
              <InlineLoading text="加载日志中..." />
            </div>
          ) : eventsError ? (
            <div className="p-4">
              <InlineError text={eventsError.message || '日志加载失败'} />
            </div>
          ) : events.length > 0 ? (
            <table className="min-w-full divide-y divide-slate-100">
              <thead className="bg-slate-50">
                <tr className="text-left text-xs text-slate-500">
                  <th className="px-4 py-2">时间</th>
                  <th className="px-4 py-2">服务</th>
                  <th className="px-4 py-2">级别</th>
                  <th className="px-4 py-2">消息</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {events.slice(0, 12).map((event) => (
                  <tr
                    key={event.id}
                    className="cursor-pointer hover:bg-slate-50"
                    onClick={() => navigate(`/logs?service=${encodeURIComponent(event.service_name)}&level=${event.level}`)}
                  >
                    <td className="whitespace-nowrap px-4 py-2 text-xs text-slate-500">{formatTime(event.timestamp)}</td>
                    <td className="whitespace-nowrap px-4 py-2 text-xs font-medium text-teal-700">{event.service_name}</td>
                    <td className="px-4 py-2">
                      <span className="rounded-full px-2 py-1 text-[10px] font-semibold text-white" style={{ backgroundColor: getLevelColor(event.level) }}>
                        {event.level}
                      </span>
                    </td>
                    <td className="max-w-[560px] truncate px-4 py-2 text-xs text-slate-600">{event.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="p-6">
              <EmptyState title="暂无日志数据" description="等待日志数据收集中..." />
            </div>
          )}
        </div>
      </section>
    </div>
  );
};

function HeroStat(props: { title: string; value: string; icon: React.ReactNode }): JSX.Element {
  return (
    <div className="rounded-lg border border-white/20 bg-white/10 px-3 py-2">
      <div className="flex items-center gap-1 text-[11px] text-slate-200">
        {props.icon}
        {props.title}
      </div>
      <div className="mt-1 truncate text-sm font-semibold text-white">{props.value}</div>
    </div>
  );
}

function PrimaryKpi(props: {
  title: string;
  value: string;
  hint: string;
  loading: boolean;
  error?: string;
  tone: 'blue' | 'green' | 'red' | 'amber' | 'slate';
}): JSX.Element {
  const toneClass: Record<string, string> = {
    blue: 'text-blue-700 bg-blue-50 border-blue-100',
    green: 'text-emerald-700 bg-emerald-50 border-emerald-100',
    red: 'text-red-700 bg-red-50 border-red-100',
    amber: 'text-amber-700 bg-amber-50 border-amber-100',
    slate: 'text-slate-700 bg-slate-50 border-slate-100',
  };

  return (
    <div className={`rounded-xl border p-4 shadow-sm ${toneClass[props.tone]}`}>
      <div className="text-xs font-medium">{props.title}</div>
      <div className="mt-2 text-2xl font-bold">
        {props.loading ? '...' : props.error ? '--' : props.value}
      </div>
      <div className="mt-1 text-[11px] opacity-80">
        {props.error ? props.error : props.hint}
      </div>
    </div>
  );
}

function QualityBlock(props: { title: string; value: string; status: 'ok' | 'warning' | 'neutral'; hint?: string }): JSX.Element {
  const valueClass =
    props.status === 'ok'
      ? 'text-emerald-600'
      : props.status === 'warning'
        ? 'text-amber-600'
        : 'text-slate-500';
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
      <div className="text-[11px] text-slate-500">{props.title}</div>
      <div className={`mt-1 text-xl font-bold ${valueClass}`}>
        {props.value}
      </div>
      {props.hint ? <div className="mt-1 text-[10px] text-slate-500">{props.hint}</div> : null}
    </div>
  );
}

function ValueLine(props: { title: string; value: string; tone: 'good' | 'warning' | 'neutral' }): JSX.Element {
  const valueClass =
    props.tone === 'good'
      ? 'text-emerald-700'
      : props.tone === 'warning'
        ? 'text-amber-700'
        : 'text-slate-700';
  return (
    <div className="rounded-lg border border-slate-200 px-3 py-2">
      <div className="text-xs text-slate-500">{props.title}</div>
      <div className={`mt-1 text-sm font-semibold ${valueClass}`}>
        {props.value}
      </div>
    </div>
  );
}

function InlineLoading(props: { text: string }): JSX.Element {
  return (
    <div className="flex items-center gap-2 text-xs text-slate-500">
      <RefreshCw className="h-4 w-4 animate-spin" />
      {props.text}
    </div>
  );
}

function InlineError(props: { text: string }): JSX.Element {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
      {props.text}
    </div>
  );
}

function formatPercent(value: number): string {
  const safe = Number.isFinite(value) ? value : 0;
  return `${(safe * 100).toFixed(1)}%`;
}

function getLevelColor(level: string): string {
  const colors: Record<string, string> = {
    TRACE: '#64748B',
    DEBUG: '#0EA5E9',
    INFO: '#14B8A6',
    WARN: '#F59E0B',
    ERROR: '#EF4444',
    FATAL: '#B91C1C',
  };
  return colors[level] || '#64748B';
}

export default Dashboard;
