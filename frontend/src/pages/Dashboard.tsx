/**
 * Dashboard — Logoscope Observability Command Center
 * Professional, clean layout with data-driven design
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  AlertTriangle,
  ArrowRight,
  Bell,
  BellOff,
  Clock,
  RefreshCw,
  Server,
  ShieldAlert,
  Network,
  TrendingUp,
  TrendingDown,
  Activity,
  Layers,
  Zap,
  CheckCircle2,
  XCircle,
  BarChart3,
  Eye,
  ChevronRight,
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
const STORAGE_KEY = 'dashboard.hotspot.excluded_services';

function loadExcluded(): string[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return Array.from(new Set(
      parsed.map(i => resolveCanonicalServiceName(i)).filter(i => i && i !== 'unknown')
    ));
  } catch { return []; }
}

/* ══════════════════════════════════════════════════════════════════════════ */
const Dashboard: React.FC = () => {
  const navigate = useNavigate();
  const navigation = useNavigation();

  const { data: eventsData,   loading: eventsLoading,   error: eventsError,   refetch: refetchEvents }     = useEvents({ limit: 40, time_window: HOTSPOT_TIME_WINDOW, exclude_health_check: true });
  const { data: logsStatsData,loading: logsStatsLoading,error: logsStatsError,refetch: refetchLogsStats }   = useLogsStats({ time_window: HOTSPOT_TIME_WINDOW });
  const { data: traceStatsData,loading: traceStatsLoading,error: traceStatsError,refetch: refetchTraceStats }= useTraceStats({ time_window: HOTSPOT_TIME_WINDOW });
  const { data: topologyData, loading: topologyLoading, error: topologyError }                              = useHybridTopology({ time_window: HOTSPOT_TIME_WINDOW });
  const { data: alertsData,   loading: alertsLoading,   error: alertsError,   refetch: refetchAlerts }      = useAlertEvents({ limit: 8, status: 'firing' });
  const { data: iqData,       loading: iqLoading,       error: iqError,       refetch: refetchIQ }          = useInferenceQuality({ time_window: HOTSPOT_TIME_WINDOW });
  const { data: iaData,       loading: iaLoading,       error: iaError,       refetch: refetchIA }          = useInferenceQualityAlerts({ time_window: HOTSPOT_TIME_WINDOW, min_coverage: 0.20, max_inferred_ratio: 0.80, max_false_positive_rate: 0.30 });

  const [suppressingMetric, setSuppressingMetric] = useState<string | null>(null);
  const [refreshingAll, setRefreshingAll] = useState(false);
  const [excludeInput, setExcludeInput] = useState('');
  const [excludedServices, setExcludedServices] = useState<string[]>(loadExcluded);

  const events       = useMemo(() => eventsData?.events || [], [eventsData]);
  const alerts       = useMemo(() => alertsData?.events || [], [alertsData]);
  const topoNodes    = useMemo(() => topologyData?.nodes || [], [topologyData]);
  const topoEdges    = useMemo(() => topologyData?.edges || [], [topologyData]);
  const levelBuckets = useMemo(() => logsStatsData?.byLevel || {}, [logsStatsData]);
  const svcBuckets   = useMemo(() => logsStatsData?.byService || {}, [logsStatsData]);
  const svcErrBuckets= useMemo(() => logsStatsData?.byServiceErrors || {}, [logsStatsData]);

  const totalEvents    = Number(logsStatsData?.total || eventsData?.total || 0);
  const warnCount      = Number(levelBuckets.WARN || 0) + Number(levelBuckets.WARNING || 0);
  const errorCount     = Number(levelBuckets.ERROR || 0) + Number(levelBuckets.FATAL || 0);
  const abnormalCount  = warnCount + errorCount;
  const debugTraceCount= Number(levelBuckets.DEBUG || 0) + Number(levelBuckets.TRACE || 0);
  const activeLogSvcs  = Object.keys(svcBuckets).length;
  const topSvcEntry    = Object.entries(svcBuckets).sort((a,b) => Number(b[1]||0)-Number(a[1]||0))[0];
  const topSvcName     = topSvcEntry?.[0] || '--';
  const topSvcCount    = Number(topSvcEntry?.[1] || 0);
  const topSvcRatio    = totalEvents > 0 ? (topSvcCount / totalEvents) * 100 : 0;
  const errorRate      = totalEvents > 0 ? (errorCount / totalEvents) * 100 : 0;
  const abnormalRate   = totalEvents > 0 ? (abnormalCount / totalEvents) * 100 : 0;
  const debugTraceRate = totalEvents > 0 ? (debugTraceCount / totalEvents) * 100 : 0;
  const firingAlerts   = alertsData?.total || 0;
  const topoSvcCount   = topoNodes.filter(n => {
    const t = String((n as unknown as LooseRecord)?.type||'').toLowerCase();
    return !t || t === 'service' || t.includes('service');
  }).length;
  const activeServices = topoSvcCount > 0 ? topoSvcCount : topoNodes.length;
  const avgDuration    = Number(traceStatsData?.avg_duration ?? traceStatsData?.avg_latency ?? 0);

  const excludedSet = useMemo(() => new Set(excludedServices.map(s => s.toLowerCase())), [excludedServices]);

  const servicePulseBase = useMemo<ServicePulse[]>(() => {
    const buckets: Record<string, { total: number; errors: number }> = {};
    const ingest = (sv: unknown, tv: unknown, ev: unknown) => {
      const svc = resolveCanonicalServiceName(sv);
      if (!svc || svc === 'unknown') return;
      const total = Math.max(Number(tv)||0, 0);
      const errors= Math.max(Number(ev)||0, 0);
      if (total<=0 && errors<=0) return;
      if (!buckets[svc]) buckets[svc] = { total:0, errors:0 };
      buckets[svc].total  += total;
      buckets[svc].errors += errors;
    };
    topoNodes.forEach(n => {
      const nr = n as unknown as LooseRecord;
      const m = (nr?.metrics as LooseRecord|undefined) || {};
      if (m?.log_count === undefined) return;
      const sm = nr?.service as LooseRecord|undefined;
      ingest(m?.service_name||sm?.name||nr?.label||nr?.id, m?.log_count, m?.error_count??0);
    });
    if (Object.keys(buckets).length === 0) {
      Object.entries(svcBuckets).forEach(([svc,total]) => ingest(svc,total,svcErrBuckets[svc]||0));
    }
    return Object.entries(buckets)
      .map(([svc, v]) => ({ service:svc, total:v.total, errors:v.errors, errorRate: v.total>0 ? (v.errors/v.total)*100 : (v.errors>0?100:0) }))
      .sort((a,b) => b.errorRate!==a.errorRate ? b.errorRate-a.errorRate : b.total-a.total);
  }, [svcBuckets, svcErrBuckets, topoNodes]);

  const servicePulse = useMemo(() => servicePulseBase.filter(i => !excludedSet.has(i.service.toLowerCase())).slice(0,6), [excludedSet,servicePulseBase]);
  const allExcluded = servicePulseBase.length>0 && servicePulse.length===0;

  const hotspotPath = useMemo(() => `/logs?time_window=${HOTSPOT_TIME_WINDOW}&exclude_health_check=true`, []);

  const addExcluded = (raw: string) => {
    const tokens = raw.split(/[\n,;]+/).map(i=>resolveCanonicalServiceName(i)).filter(i=>i&&i!=='unknown');
    if (!tokens.length) return;
    setExcludedServices(prev => Array.from(new Set([...prev,...tokens])).sort((a,b)=>a.localeCompare(b,'zh-CN',{sensitivity:'base'})));
    setExcludeInput('');
  };
  const removeExcluded = (svc: string) => setExcludedServices(prev=>prev.filter(i=>i!==svc));

  const jumpToLogs = (svc: string) => {
    const params = new URLSearchParams({ service: resolveCanonicalServiceName(svc), time_window: HOTSPOT_TIME_WINDOW, exclude_health_check: 'true' });
    navigate(`/logs?${params}`);
  };

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(excludedServices));
  }, [excludedServices]);

  const latestTime = useMemo(() => {
    const t = events[0]?.timestamp;
    return t ? formatTime(t) : '暂无实时数据';
  }, [events]);

  const iqMetrics     = iqData?.metrics || {};
  const coverage      = Number(iqMetrics.coverage      || 0);
  const inferredRatio = Number(iqMetrics.inferred_ratio || 0);
  const fpRate        = Number(iqMetrics.false_positive_rate || 0);
  const fpState       = String(iqMetrics.false_positive_rate_state || 'ok').toLowerCase();
  const fpUnavail     = fpState === 'unknown';
  const fpReason      = String(iqMetrics.false_positive_rate_reason || '');
  const fpMinSample   = Number(iqMetrics.false_positive_rate_min_sample || 0);
  const fpHint        = fpReason === 'insufficient_inferred_sample' ? `推断样本不足（<${fpMinSample||1}）` : '当前窗口无观测基线';

  const handleRefreshAll = async () => {
    setRefreshingAll(true);
    try { await Promise.all([refetchEvents(),refetchLogsStats(),refetchTraceStats(),refetchAlerts(),refetchIQ(),refetchIA()]); }
    finally { setRefreshingAll(false); }
  };

  const handleToggleSuppression = async (metric: string, suppressed: boolean) => {
    try {
      setSuppressingMetric(metric);
      await api.setInferenceAlertSuppression(metric, !suppressed);
      refetchIA(); refetchIQ();
    } catch(e) { console.error(e); }
    finally { setSuppressingMetric(null); }
  };

  /* ── Render ─────────────────────────────────────────────────────────── */
  return (
    <div className="p-6 space-y-6 max-w-[1600px] mx-auto animate-fade-in">

      {/* ══ Hero Banner ══════════════════════════════════════════════════════ */}
      <section
        className="relative rounded-2xl overflow-hidden"
        style={{
          background: 'linear-gradient(135deg, #0c1422 0%, #0f2234 40%, #0d4a42 100%)',
          boxShadow: '0 8px 32px rgba(13,148,136,0.18)',
        }}
      >
        {/* Decorative elements */}
        <div className="absolute inset-0 pointer-events-none overflow-hidden">
          <div style={{ position:'absolute', top:-60, right:-60, width:300, height:300, borderRadius:'50%', background:'radial-gradient(circle, rgba(13,148,136,0.15) 0%, transparent 70%)' }} />
          <div style={{ position:'absolute', bottom:-40, left:'30%', width:200, height:200, borderRadius:'50%', background:'radial-gradient(circle, rgba(99,102,241,0.08) 0%, transparent 70%)' }} />
          {/* Grid dots */}
          <svg className="absolute inset-0 w-full h-full opacity-[0.04]" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <pattern id="dots" x="0" y="0" width="24" height="24" patternUnits="userSpaceOnUse">
                <circle cx="2" cy="2" r="1" fill="white"/>
              </pattern>
            </defs>
            <rect width="100%" height="100%" fill="url(#dots)"/>
          </svg>
        </div>

        <div className="relative px-6 py-5 md:px-8 md:py-6">
          <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
            {/* Title block */}
            <div>
              <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold tracking-wide mb-3"
                style={{ background:'rgba(13,148,136,0.2)', border:'1px solid rgba(13,148,136,0.35)', color:'#5eead4' }}>
                <Activity size={11} />
                OBSERVABILITY COMMAND CENTER
              </div>
              <h1 className="text-xl md:text-2xl font-bold text-white tracking-tight">仪表盘总览</h1>
              <p className="mt-1 text-sm" style={{ color:'rgba(226,232,240,0.65)' }}>
                面向故障定位与稳定性运营 · 最近 1 小时聚合视图
              </p>
            </div>

            {/* Action buttons */}
            <div className="flex items-center gap-2 flex-shrink-0">
              <button
                onClick={handleRefreshAll}
                disabled={refreshingAll}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-white disabled:opacity-50 transition-colors duration-150"
                style={{ background:'rgba(255,255,255,0.1)', border:'1px solid rgba(255,255,255,0.15)' }}
                onMouseEnter={e=>(e.currentTarget.style.background='rgba(255,255,255,0.16)')}
                onMouseLeave={e=>(e.currentTarget.style.background='rgba(255,255,255,0.1)')}
              >
                <RefreshCw size={13} className={refreshingAll ? 'animate-spin' : ''} />
                全量刷新
              </button>
              <Link to="/alerts"
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors duration-150"
                style={{ background:'#0d9488', color:'white', border:'1px solid rgba(255,255,255,0.15)' }}
                onMouseEnter={e=>((e.currentTarget as HTMLElement).style.background='#0f766e')}
                onMouseLeave={e=>((e.currentTarget as HTMLElement).style.background='#0d9488')}
              >
                告警中心
                <ArrowRight size={12} />
              </Link>
            </div>
          </div>

          {/* Hero stats row */}
          <div className="mt-5 grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-3">
            {[
              { label:'最新数据点',  value: latestTime,              icon: <Clock size={13} />,       mono: true },
              { label:'活跃服务',    value: String(activeServices),  icon: <Server size={13} /> },
              { label:'拓扑连接',    value: String(topoEdges.length),icon: <Network size={13} /> },
              { label:'触发告警',    value: String(firingAlerts),    icon: <ShieldAlert size={13} />, warn: firingAlerts > 0 },
              { label:'错误率',      value: `${errorRate.toFixed(2)}%`, icon: <AlertTriangle size={13} />, warn: errorRate >= 5 },
            ].map(s => (
              <div
                key={s.label}
                className="rounded-xl px-3 py-2.5"
                style={{
                  background: s.warn ? 'rgba(239,68,68,0.15)' : 'rgba(255,255,255,0.08)',
                  border: `1px solid ${s.warn ? 'rgba(239,68,68,0.3)' : 'rgba(255,255,255,0.12)'}`,
                }}
              >
                <div className="flex items-center gap-1.5 mb-1" style={{ color: s.warn ? '#fca5a5' : 'rgba(226,232,240,0.6)', fontSize: 11 }}>
                  {s.icon}
                  {s.label}
                </div>
                <div className={`font-bold text-white ${s.mono ? 'text-xs' : 'text-base'} truncate`}>
                  {s.value}
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ══ KPI Cards ════════════════════════════════════════════════════════ */}
      <section className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
        <KpiCard
          icon={<Layers size={18} />}
          title="总事件"
          value={totalEvents.toLocaleString()}
          sub="最近 1 小时"
          tone="blue"
          loading={logsStatsLoading}
          error={logsStatsError?.message}
        />
        <KpiCard
          icon={<XCircle size={18} />}
          title="错误事件"
          value={errorCount.toLocaleString()}
          sub={errorRate >= 5 ? `错误率 ${errorRate.toFixed(1)}% · 高于阈值` : `错误率 ${errorRate.toFixed(1)}% · 正常`}
          tone={errorRate >= 5 ? 'red' : 'green'}
          loading={logsStatsLoading}
          error={logsStatsError?.message}
          trend={errorRate >= 5 ? 'up' : 'down'}
        />
        <KpiCard
          icon={<Zap size={18} />}
          title="平均响应"
          value={`${avgDuration.toFixed(0)} ms`}
          sub={avgDuration > 800 ? '高于 800ms 警戒线' : 'Trace 聚合统计'}
          tone={avgDuration > 800 ? 'amber' : 'green'}
          loading={traceStatsLoading}
          error={traceStatsError?.message}
          trend={avgDuration > 800 ? 'up' : 'down'}
        />
        <KpiCard
          icon={<Network size={18} />}
          title="拓扑节点"
          value={topoNodes.length.toLocaleString()}
          sub={`${activeServices} 个服务节点`}
          tone="purple"
          loading={topologyLoading}
          error={topologyError?.message}
        />
      </section>

      {/* ══ Main Content: Service Hotspot + Alerts ════════════════════════ */}
      <section className="grid grid-cols-1 xl:grid-cols-5 gap-5">

        {/* Service Health Hotspot */}
        <div className="xl:col-span-3 card overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b" style={{ borderColor:'var(--app-border-subtle)' }}>
            <div>
              <div className="flex items-center gap-2">
                <BarChart3 size={15} style={{ color:'var(--brand-primary)' }} />
                <h2 className="text-sm font-semibold" style={{ color:'var(--app-text)' }}>服务健康热区</h2>
              </div>
              <p className="text-xs mt-0.5" style={{ color:'var(--app-text-subtle)' }}>
                按错误率 + 流量排序 · 最近 1 小时
              </p>
            </div>
            <Link to={hotspotPath} className="flex items-center gap-1 text-xs font-medium transition-colors"
              style={{ color:'var(--brand-primary)' }}>
              日志明细 <ChevronRight size={12} />
            </Link>
          </div>

          {/* Filter bar */}
          <div className="px-5 py-3 border-b" style={{ borderColor:'var(--app-border-subtle)', background:'var(--app-surface-muted)' }}>
            <div className="flex gap-2">
              <input
                type="text"
                value={excludeInput}
                onChange={e => setExcludeInput(e.target.value)}
                onKeyDown={e => { if (e.key==='Enter') { e.preventDefault(); addExcluded(excludeInput); }}}
                placeholder="输入服务名屏蔽（逗号分隔）…"
                className="flex-1 h-7 px-2.5 text-xs rounded-lg outline-none transition-colors"
                style={{ background:'var(--app-surface)', border:'1px solid var(--app-border)', color:'var(--app-text)' }}
                onFocus={e=>(e.currentTarget.style.borderColor='var(--brand-primary)')}
                onBlur={e=>(e.currentTarget.style.borderColor='var(--app-border)')}
              />
              <button
                onClick={() => addExcluded(excludeInput)}
                className="h-7 px-3 text-xs font-medium rounded-lg transition-colors"
                style={{ background:'var(--app-text)', color:'var(--app-text-inverse)' }}
                onMouseEnter={e=>((e.currentTarget as HTMLElement).style.opacity='0.85')}
                onMouseLeave={e=>((e.currentTarget as HTMLElement).style.opacity='1')}
              >
                屏蔽
              </button>
              {excludedServices.length > 0 && (
                <button
                  onClick={() => setExcludedServices([])}
                  className="h-7 px-3 text-xs rounded-lg transition-colors"
                  style={{ border:'1px solid var(--app-border)', color:'var(--app-text-muted)' }}
                  onMouseEnter={e=>((e.currentTarget as HTMLElement).style.background='var(--app-surface-hover)')}
                  onMouseLeave={e=>((e.currentTarget as HTMLElement).style.background='')}
                >
                  清空
                </button>
              )}
            </div>
            {excludedServices.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-2">
                {excludedServices.map(svc => (
                  <button
                    key={svc}
                    onClick={() => removeExcluded(svc)}
                    className="flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] transition-colors"
                    style={{ background:'var(--app-surface)', border:'1px solid var(--app-border)', color:'var(--app-text-muted)' }}
                  >
                    {svc}
                    <span style={{ color:'var(--app-text-subtle)' }}>×</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Service list */}
          <div className="p-5">
            {topologyLoading ? (
              <div className="space-y-3">
                {[1,2,3].map(i => <div key={i} className="skeleton h-14 w-full" />)}
              </div>
            ) : topologyError ? (
              <InlineError text={topologyError.message || '服务健康计算失败'} />
            ) : allExcluded ? (
              <EmptyState title="热区服务已全部屏蔽" description="请移除部分屏蔽服务以查看热区排行" />
            ) : servicePulse.length === 0 ? (
              <EmptyState title="暂无服务健康样本" description="等待日志数据进入分析窗口…" />
            ) : (
              <div className="space-y-2.5">
                {servicePulse.map((item, idx) => {
                  const isHot = item.errorRate >= 20;
                  const isWarn = item.errorRate >= 8;
                  const barColor = isHot ? '#ef4444' : isWarn ? '#f59e0b' : '#10b981';
                  const bgTint   = isHot ? 'rgba(239,68,68,0.04)' : isWarn ? 'rgba(245,158,11,0.04)' : 'rgba(16,185,129,0.04)';
                  return (
                    <div
                      key={item.service}
                      className="rounded-xl overflow-hidden transition-all duration-150 card-hover"
                      style={{ border:'1px solid var(--app-border)', background: bgTint }}
                    >
                      <button
                        className="w-full px-4 py-3 text-left"
                        onClick={() => jumpToLogs(item.service)}
                      >
                        <div className="flex items-center justify-between mb-2">
                          <div className="flex items-center gap-2">
                            <span
                              className="w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold text-white flex-shrink-0"
                              style={{ background: isHot ? '#ef4444' : isWarn ? '#f59e0b' : '#10b981' }}
                            >
                              {idx + 1}
                            </span>
                            <span className="text-sm font-semibold truncate" style={{ color:'var(--app-text)' }}>
                              {item.service}
                            </span>
                          </div>
                          <div className="flex items-center gap-3 text-xs flex-shrink-0 ml-2">
                            <span style={{ color:'var(--app-text-subtle)' }}>
                              {item.errors.toLocaleString()} / {item.total.toLocaleString()}
                            </span>
                            <span
                              className="font-bold"
                              style={{ color: barColor }}
                            >
                              {item.errorRate.toFixed(1)}%
                            </span>
                          </div>
                        </div>
                        {/* Progress bar */}
                        <div className="h-1.5 rounded-full overflow-hidden" style={{ background:'var(--app-border)' }}>
                          <div
                            className="h-full rounded-full transition-all duration-300"
                            style={{ width:`${Math.min(item.errorRate,100)}%`, background: barColor }}
                          />
                        </div>
                      </button>
                      <div className="px-4 py-1.5 border-t flex items-center justify-between" style={{ borderColor:'var(--app-border-subtle)' }}>
                        <span className="text-[11px]" style={{ color:'var(--app-text-subtle)' }}>
                          错误率 {item.errorRate.toFixed(2)}%
                        </span>
                        <button
                          onClick={() => addExcluded(item.service)}
                          className="text-[11px] transition-colors"
                          style={{ color:'var(--app-text-subtle)' }}
                          onMouseEnter={e=>((e.currentTarget as HTMLElement).style.color='var(--app-text-muted)')}
                          onMouseLeave={e=>((e.currentTarget as HTMLElement).style.color='var(--app-text-subtle)')}
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

        {/* Alert Stream */}
        <div className="xl:col-span-2 card overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b" style={{ borderColor:'var(--app-border-subtle)' }}>
            <div className="flex items-center gap-2">
              <div className="w-2 h-2 rounded-full animate-pulse" style={{ background: firingAlerts > 0 ? 'var(--color-error)' : 'var(--color-success)' }} />
              <h2 className="text-sm font-semibold" style={{ color:'var(--app-text)' }}>实时告警流</h2>
              {firingAlerts > 0 && (
                <span className="badge badge-error">{firingAlerts}</span>
              )}
            </div>
            <Link to="/alerts?tab=events&status=firing"
              className="flex items-center gap-1 text-xs font-medium"
              style={{ color:'var(--brand-primary)' }}>
              全部 <ChevronRight size={12} />
            </Link>
          </div>
          <div className="p-4">
            {alertsLoading ? (
              <div className="space-y-2">
                {[1,2,3].map(i => <div key={i} className="skeleton h-16 w-full" />)}
              </div>
            ) : alertsError ? (
              <InlineError text={alertsError.message || '告警加载失败'} />
            ) : alerts.length > 0 ? (
              <div className="space-y-2">
                {alerts.slice(0,6).map(alert => (
                  <button
                    key={alert.id}
                    className="w-full rounded-xl px-3.5 py-3 text-left transition-all duration-150 card-hover"
                    style={{ border:'1px solid var(--app-border)', background:'var(--app-surface)' }}
                    onClick={() => {
                      const src = String(alert.source_service||'').trim();
                      const tgt = String(alert.target_service||'').trim();
                      navigation.goToAlerts({ tab:'events', status:'firing', severity:alert.severity, serviceName:alert.service_name||undefined, namespace:alert.namespace||undefined, scope: src||tgt?'edge':'service', sourceService:src||undefined, targetService:tgt||undefined });
                    }}
                  >
                    <div className="flex items-start gap-2.5">
                      <span
                        className="mt-0.5 flex-shrink-0 w-2 h-2 rounded-full"
                        style={{ background: formatColor(alert.severity), marginTop: 5 }}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center justify-between gap-2 mb-0.5">
                          <span className="text-xs font-semibold truncate" style={{ color:'var(--app-text)' }}>
                            {alert.rule_name}
                          </span>
                          <span
                            className="badge flex-shrink-0"
                            style={{ background:`${formatColor(alert.severity)}22`, color:formatColor(alert.severity), border:`1px solid ${formatColor(alert.severity)}44` }}
                          >
                            {alert.severity}
                          </span>
                        </div>
                        <p className="text-[11px] line-clamp-2" style={{ color:'var(--app-text-muted)' }}>
                          {alert.message}
                        </p>
                        <div className="mt-1 flex items-center gap-2 text-[10px]" style={{ color:'var(--app-text-subtle)' }}>
                          <span>{alert.service_name}</span>
                          <span>·</span>
                          <span>{formatTime(alert.fired_at)}</span>
                        </div>
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <div className="flex flex-col items-center py-8 gap-3">
                <div className="w-12 h-12 rounded-full flex items-center justify-center" style={{ background:'var(--color-success-soft)' }}>
                  <CheckCircle2 size={24} style={{ color:'var(--color-success)' }} />
                </div>
                <div className="text-center">
                  <div className="text-sm font-semibold" style={{ color:'var(--app-text)' }}>当前无触发告警</div>
                  <div className="text-xs mt-0.5" style={{ color:'var(--app-text-subtle)' }}>系统状态稳定运行中</div>
                </div>
              </div>
            )}
          </div>
        </div>
      </section>

      {/* ══ Quality Panels ═══════════════════════════════════════════════════ */}
      <section className="grid grid-cols-1 xl:grid-cols-2 gap-5">

        {/* Inference Quality */}
        <div className="card overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b" style={{ borderColor:'var(--app-border-subtle)' }}>
            <div>
              <div className="flex items-center gap-1.5">
                <Eye size={14} style={{ color:'var(--brand-secondary)' }} />
                <h2 className="text-sm font-semibold" style={{ color:'var(--app-text)' }}>拓扑推断质量</h2>
                <Tooltip
                  title="推断质量指标说明"
                  lines={['Coverage：推断边覆盖率，建议长期保持在 20% 以上。','Inferred Ratio：推断边占比，过高可能代表观测信号不足。','False Positive：误报率，持续上升时建议回看推断模式与阈值。']}
                  widthClass="w-[300px]"
                />
              </div>
              <p className="text-xs mt-0.5" style={{ color:'var(--app-text-subtle)' }}>面向拓扑推断可靠性</p>
            </div>
            <Link to="/alerts" className="flex items-center gap-1 text-xs font-medium" style={{ color:'var(--brand-primary)' }}>
              质量告警 <ChevronRight size={12} />
            </Link>
          </div>

          <div className="grid grid-cols-3 gap-3 p-5 border-b" style={{ borderColor:'var(--app-border-subtle)' }}>
            <QualityBlock title="Coverage"       value={formatPct(coverage)}      status={coverage >= 0.2 ? 'ok' : 'warning'} />
            <QualityBlock title="Inferred Ratio" value={formatPct(inferredRatio)} status={inferredRatio <= 0.8 ? 'ok' : 'warning'} />
            <QualityBlock
              title="False Positive"
              value={fpUnavail ? 'N/A' : formatPct(fpRate)}
              status={fpUnavail ? 'neutral' : (fpRate <= 0.3 ? 'ok' : 'warning')}
              hint={fpUnavail ? fpHint : undefined}
            />
          </div>

          <div className="p-5">
            {iqLoading || iaLoading ? (
              <InlineLoading text="加载推断质量中…" />
            ) : iqError || iaError ? (
              <InlineError text={iqError?.message || iaError?.message || '推断质量加载失败'} />
            ) : iaData?.alerts?.length ? (
              <div className="space-y-2">
                {iaData.alerts.slice(0,4).map((a: unknown) => {
                  const ar = a as LooseRecord;
                  const metric    = String(ar.metric    || '');
                  const expression= String(ar.expression|| '');
                  const value     = Number(ar.value     || 0);
                  const suppressed= Boolean(ar.suppressed);
                  return (
                    <div key={`${metric}-${expression}`}
                      className="flex items-center justify-between rounded-xl px-3.5 py-2.5"
                      style={{ border:'1px solid var(--app-border)', background:'var(--app-surface-muted)' }}>
                      <div>
                        <div className="text-xs font-semibold" style={{ color:'var(--app-text)' }}>{metric}</div>
                        <div className="text-[11px] mt-0.5" style={{ color:'var(--app-text-subtle)' }}>
                          {expression} · 当前 {formatPct(value)}
                        </div>
                      </div>
                      <button
                        onClick={() => handleToggleSuppression(metric, suppressed)}
                        disabled={!metric || suppressingMetric === metric}
                        className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors disabled:opacity-50"
                        style={{ background:'var(--app-surface)', border:'1px solid var(--app-border)', color:'var(--app-text-muted)' }}
                      >
                        {suppressed ? <Bell size={11} /> : <BellOff size={11} />}
                        {suppressed ? '取消抑制' : '抑制'}
                      </button>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="flex items-center gap-2.5 rounded-xl px-3.5 py-3"
                style={{ background:'var(--color-success-soft)', border:'1px solid rgba(16,185,129,0.2)' }}>
                <CheckCircle2 size={15} style={{ color:'var(--color-success)' }} />
                <span className="text-xs font-medium" style={{ color:'#065f46' }}>当前未触发推断质量告警</span>
              </div>
            )}
          </div>
        </div>

        {/* Log Quality Metrics */}
        <div className="card overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b" style={{ borderColor:'var(--app-border-subtle)' }}>
            <div>
              <div className="flex items-center gap-1.5">
                <TrendingUp size={14} style={{ color:'var(--brand-primary)' }} />
                <h2 className="text-sm font-semibold" style={{ color:'var(--app-text)' }}>日志质量指标</h2>
                <Tooltip
                  title="日志质量指标口径"
                  lines={['仅基于日志数据统计，不含发布回归/质量门禁口径。','错误率 = (ERROR + FATAL) / 总日志量。','告警级占比 = (WARN + ERROR + FATAL) / 总日志量。']}
                  widthClass="w-[300px]"
                />
              </div>
              <p className="text-xs mt-0.5" style={{ color:'var(--app-text-subtle)' }}>快速识别日志异常密度与服务覆盖</p>
            </div>
            <button
              onClick={refetchLogsStats}
              className="text-xs px-2.5 py-1.5 rounded-lg transition-colors"
              style={{ background:'var(--app-surface-muted)', border:'1px solid var(--app-border)', color:'var(--app-text-muted)' }}
              onMouseEnter={e=>((e.currentTarget as HTMLElement).style.background='var(--app-surface-hover)')}
              onMouseLeave={e=>((e.currentTarget as HTMLElement).style.background='var(--app-surface-muted)')}
            >
              刷新
            </button>
          </div>

          <div className="p-5">
            {logsStatsLoading ? (
              <div className="grid grid-cols-2 gap-3">
                {[1,2,3,4,5,6,7,8].map(i => <div key={i} className="skeleton h-14 w-full" />)}
              </div>
            ) : logsStatsError ? (
              <InlineError text={logsStatsError.message || '日志质量指标加载失败'} />
            ) : (
              <div className="grid grid-cols-2 gap-3">
                <MetricTile title="日志总量"          value={totalEvents.toLocaleString()}           tone={totalEvents>0?'good':'neutral'} />
                <MetricTile title="错误日志数"         value={errorCount.toLocaleString()}            tone={errorRate<=5?'good':'warning'} />
                <MetricTile title="错误日志占比"        value={`${errorRate.toFixed(2)}%`}            tone={errorRate<=5?'good':'warning'} />
                <MetricTile title="告警级占比 WARN+"   value={`${abnormalRate.toFixed(2)}%`}         tone={abnormalRate<=10?'good':'warning'} />
                <MetricTile title="调试日志占比"        value={`${debugTraceRate.toFixed(2)}%`}       tone={debugTraceRate<=30?'good':'warning'} />
                <MetricTile title="产生日志服务数"      value={activeLogSvcs.toLocaleString()}        tone={activeLogSvcs>0?'good':'neutral'} />
                <MetricTile title="Top 服务"          value={`${topSvcName}`}                       tone={topSvcCount>0?'good':'neutral'} sub={topSvcCount.toLocaleString()} />
                <MetricTile title="Top 服务占比"       value={`${topSvcRatio.toFixed(2)}%`}          tone={topSvcRatio<=40?'good':'warning'} />
              </div>
            )}
          </div>
        </div>
      </section>

      {/* ══ Recent Logs Table ════════════════════════════════════════════════ */}
      <section className="card overflow-hidden">
        <div className="flex items-center justify-between px-5 py-4 border-b" style={{ borderColor:'var(--app-border-subtle)' }}>
          <div className="flex items-center gap-2">
            <Activity size={14} style={{ color:'var(--brand-primary)' }} />
            <h2 className="text-sm font-semibold" style={{ color:'var(--app-text)' }}>最新日志快览</h2>
          </div>
          <Link to="/logs" className="flex items-center gap-1 text-xs font-medium" style={{ color:'var(--brand-primary)' }}>
            进入日志中心 <ChevronRight size={12} />
          </Link>
        </div>

        {eventsLoading ? (
          <div className="p-5 space-y-2">
            {[1,2,3,4,5].map(i => <div key={i} className="skeleton h-10 w-full" />)}
          </div>
        ) : eventsError ? (
          <div className="p-5"><InlineError text={eventsError.message || '日志加载失败'} /></div>
        ) : events.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th className="w-40">时间</th>
                  <th className="w-36">服务</th>
                  <th className="w-20">级别</th>
                  <th>消息</th>
                </tr>
              </thead>
              <tbody>
                {events.slice(0,12).map(event => (
                  <tr
                    key={event.id}
                    className="cursor-pointer"
                    onClick={() => navigate(`/logs?service=${encodeURIComponent(event.service_name)}&level=${event.level}`)}
                  >
                    <td className="whitespace-nowrap font-mono text-xs" style={{ color:'var(--app-text-subtle)' }}>
                      {formatTime(event.timestamp)}
                    </td>
                    <td className="whitespace-nowrap font-medium text-xs" style={{ color:'var(--brand-primary)' }}>
                      {event.service_name}
                    </td>
                    <td>
                      <LevelBadge level={event.level} />
                    </td>
                    <td className="text-xs truncate max-w-[500px]" style={{ color:'var(--app-text-muted)' }}>
                      {event.message}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="p-8">
            <EmptyState title="暂无日志数据" description="等待日志数据收集中…" />
          </div>
        )}
      </section>
    </div>
  );
};

/* ─── Sub-components ──────────────────────────────────────────────────────── */

function KpiCard(p: {
  icon: React.ReactNode;
  title: string;
  value: string;
  sub: string;
  tone: 'blue'|'green'|'red'|'amber'|'purple';
  loading: boolean;
  error?: string;
  trend?: 'up'|'down';
}): JSX.Element {
  const cfg: Record<string, { icon: string; text: string; bg: string; border: string }> = {
    blue:   { icon:'#3b82f6', text:'#1e40af', bg:'#eff6ff', border:'#bfdbfe' },
    green:  { icon:'#10b981', text:'#065f46', bg:'#ecfdf5', border:'#a7f3d0' },
    red:    { icon:'#ef4444', text:'#991b1b', bg:'#fef2f2', border:'#fecaca' },
    amber:  { icon:'#f59e0b', text:'#92400e', bg:'#fffbeb', border:'#fde68a' },
    purple: { icon:'#8b5cf6', text:'#4c1d95', bg:'#f5f3ff', border:'#ddd6fe' },
  };
  const c = cfg[p.tone];
  return (
    <div
      className="rounded-2xl p-5 transition-all duration-200 hover:shadow-md"
      style={{ background: c.bg, border:`1px solid ${c.border}` }}
    >
      <div className="flex items-start justify-between mb-3">
        <div
          className="w-9 h-9 rounded-xl flex items-center justify-center"
          style={{ background:'white', color: c.icon, boxShadow:`0 2px 8px ${c.icon}22` }}
        >
          {p.icon}
        </div>
        {p.trend && (
          <span style={{ color: p.trend==='up' ? '#ef4444' : '#10b981' }}>
            {p.trend==='up' ? <TrendingUp size={14}/> : <TrendingDown size={14}/>}
          </span>
        )}
      </div>
      <div className="text-2xl font-bold mb-1" style={{ color: c.text }}>
        {p.loading ? <div className="skeleton h-8 w-24" /> : p.error ? '--' : p.value}
      </div>
      <div className="text-xs font-medium" style={{ color: c.text, opacity: 0.7 }}>
        {p.title}
      </div>
      <div className="text-[11px] mt-1 opacity-60" style={{ color: c.text }}>
        {p.error ? p.error : p.sub}
      </div>
    </div>
  );
}

function QualityBlock(p: { title: string; value: string; status: 'ok'|'warning'|'neutral'; hint?: string }): JSX.Element {
  const cfg = {
    ok:      { color:'#065f46', bg:'var(--color-success-soft)', border:'rgba(16,185,129,0.2)',  valueColor:'#059669' },
    warning: { color:'#92400e', bg:'var(--color-warning-soft)', border:'rgba(245,158,11,0.2)',  valueColor:'#d97706' },
    neutral: { color:'#475569', bg:'var(--app-surface-muted)',  border:'var(--app-border)',      valueColor:'#64748b' },
  }[p.status];
  return (
    <div className="rounded-xl p-3.5" style={{ background: cfg.bg, border:`1px solid ${cfg.border}` }}>
      <div className="text-[11px] font-semibold uppercase tracking-wider mb-2" style={{ color: cfg.color, opacity:0.7 }}>
        {p.title}
      </div>
      <div className="text-xl font-bold" style={{ color: cfg.valueColor }}>{p.value}</div>
      {p.hint && <div className="text-[10px] mt-1 opacity-60" style={{ color: cfg.color }}>{p.hint}</div>}
    </div>
  );
}

function MetricTile(p: { title: string; value: string; tone: 'good'|'warning'|'neutral'; sub?: string }): JSX.Element {
  const valueColor = p.tone==='good' ? '#059669' : p.tone==='warning' ? '#d97706' : 'var(--app-text-muted)';
  return (
    <div className="rounded-xl p-3 transition-all duration-150 card-hover" style={{ border:'1px solid var(--app-border)', background:'var(--app-surface)' }}>
      <div className="text-[11px] truncate mb-1" style={{ color:'var(--app-text-subtle)' }}>{p.title}</div>
      <div className="text-sm font-bold truncate" style={{ color: valueColor }}>{p.value}</div>
      {p.sub && <div className="text-[10px] mt-0.5 truncate" style={{ color:'var(--app-text-subtle)' }}>{p.sub}</div>}
    </div>
  );
}

function LevelBadge({ level }: { level: string }): JSX.Element {
  const cfg: Record<string, { bg: string; color: string }> = {
    TRACE: { bg:'#f1f5f9', color:'#64748b' },
    DEBUG: { bg:'#eef2ff', color:'#4338ca' },
    INFO:  { bg:'#f0fdfa', color:'#0d9488' },
    WARN:  { bg:'#fffbeb', color:'#d97706' },
    ERROR: { bg:'#fef2f2', color:'#dc2626' },
    FATAL: { bg:'#7f1d1d', color:'#fecaca' },
  };
  const c = cfg[level] || cfg.TRACE;
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold"
      style={{ background: c.bg, color: c.color }}>
      {level}
    </span>
  );
}

function InlineLoading({ text }: { text: string }): JSX.Element {
  return (
    <div className="flex items-center gap-2 py-2 text-xs" style={{ color:'var(--app-text-subtle)' }}>
      <RefreshCw size={13} className="animate-spin" />
      {text}
    </div>
  );
}

function InlineError({ text }: { text: string }): JSX.Element {
  return (
    <div className="flex items-center gap-2 rounded-xl px-3.5 py-3 text-xs"
      style={{ background:'var(--color-error-soft)', border:'1px solid rgba(239,68,68,0.2)', color:'#991b1b' }}>
      <XCircle size={13} />
      {text}
    </div>
  );
}

function formatPct(v: number): string {
  const s = Number.isFinite(v) ? v : 0;
  return `${(s * 100).toFixed(1)}%`;
}

export default Dashboard;
