/**
 * 统一导航 Hook
 * 
 * 提供三大核心功能之间的无缝跳转：
 * - 日志分析
 * - 服务拓扑
 * - AI 诊断
 */
import { useCallback } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';

export interface LogData {
  id: string;
  timestamp: string;
  service_name: string;
  level: string;
  message: string;
  pod_name?: string;
  namespace?: string;
  node_name?: string;
  container_name?: string;
  trace_id?: string;
  span_id?: string;
  attributes?: Record<string, unknown>;
}

export interface TopologyNodeData {
  id: string;
  label: string;
  type: string;
  metrics?: Record<string, unknown>;
}

export interface AIAnalysisData {
  logData?: LogData;
  traceId?: string;
  serviceName?: string;
  message?: string;
  autoAnalyze?: boolean;
}

export interface NavigationOptions {
  keepFilters?: boolean;
  openInNewTab?: boolean;
}

export function useNavigation() {
  const navigate = useNavigate();
  const location = useLocation();

  const goToLogs = useCallback((options?: {
    serviceName?: string;
    namespace?: string;
    level?: string;
    search?: string;
    traceId?: string;
    traceIds?: string[];
    requestId?: string;
    requestIds?: string[];
    podName?: string;
    timestamp?: string;
    sourceService?: string;
    targetService?: string;
    sourceNamespace?: string;
    targetNamespace?: string;
    timeWindow?: string;
    anchorTime?: string;
    correlationMode?: 'and' | 'or';
  }) => {
    const params = new URLSearchParams();
    
    if (options?.serviceName) {
      params.set('service', options.serviceName);
    }
    if (options?.namespace) {
      params.set('namespace', options.namespace);
    }
    if (options?.level) {
      params.set('level', options.level);
    }
    if (options?.search) {
      params.set('search', options.search);
    }
    if (options?.traceId) {
      params.set('trace_id', options.traceId);
    }
    if (options?.traceIds?.length) {
      params.set('trace_ids', options.traceIds.map((item) => String(item || '').trim()).filter(Boolean).join(','));
    }
    if (options?.requestId) {
      params.set('request_id', options.requestId);
    }
    if (options?.requestIds?.length) {
      params.set('request_ids', options.requestIds.map((item) => String(item || '').trim()).filter(Boolean).join(','));
    }
    if (options?.podName) {
      params.set('pod', options.podName);
    }
    if (options?.timestamp) {
      params.set('ts', options.timestamp);
      if (!options.anchorTime) {
        params.set('anchor_time', options.timestamp);
      }
    }
    if (options?.sourceService) {
      params.set('source_service', options.sourceService);
    }
    if (options?.targetService) {
      params.set('target_service', options.targetService);
    }
    if (options?.sourceNamespace) {
      params.set('source_namespace', options.sourceNamespace);
    }
    if (options?.targetNamespace) {
      params.set('target_namespace', options.targetNamespace);
    }
    if (options?.timeWindow) {
      params.set('time_window', options.timeWindow);
    }
    if (options?.anchorTime) {
      params.set('anchor_time', options.anchorTime);
    }
    if (options?.correlationMode) {
      params.set('correlation_mode', options.correlationMode);
    }

    const queryString = params.toString();
    navigate(`/logs${queryString ? `?${queryString}` : ''}`);
  }, [navigate]);

  const goToLogDetail = useCallback((log: LogData, options?: NavigationOptions) => {
    const params = new URLSearchParams();
    params.set('id', log.id);
    
    if (options?.keepFilters) {
      const currentParams = new URLSearchParams(location.search);
      currentParams.forEach((value, key) => {
        if (!params.has(key)) {
          params.set(key, value);
        }
      });
    }

    navigate(`/logs?${params.toString()}`);
  }, [navigate, location.search]);

  const goToTopology = useCallback((options?: {
    serviceName?: string;
    namespace?: string;
    timeWindow?: string;
  }) => {
    const params = new URLSearchParams();
    
    if (options?.serviceName) {
      params.set('service', options.serviceName);
    }
    if (options?.namespace) {
      params.set('namespace', options.namespace);
    }
    if (options?.timeWindow) {
      params.set('timeWindow', options.timeWindow);
    }

    const queryString = params.toString();
    navigate(`/topology${queryString ? `?${queryString}` : ''}`);
  }, [navigate]);

  const goToTopologyNode = useCallback((node: TopologyNodeData) => {
    const params = new URLSearchParams();
    const serviceName = String(node.label || node.id || '').trim();
    const nodeId = String(node.id || '').trim();
    if (serviceName) {
      params.set('service', serviceName);
    }
    if (nodeId) {
      params.set('highlight', nodeId);
    }
    const queryString = params.toString();
    navigate(`/topology${queryString ? `?${queryString}` : ''}`);
  }, [navigate]);

  const goToAIAnalysis = useCallback((data: AIAnalysisData) => {
    const shouldAutoAnalyze = data.autoAnalyze === true;
    if (data.logData) {
      navigate('/ai-analysis', {
        state: {
          logData: data.logData,
          mode: 'log',
          autoAnalyze: shouldAutoAnalyze,
        }
      });
    } else if (data.traceId) {
      navigate('/ai-analysis', {
        state: {
          traceId: data.traceId,
          serviceName: data.serviceName,
          mode: 'trace',
          autoAnalyze: shouldAutoAnalyze,
        }
      });
    } else if (data.serviceName || data.message) {
      navigate('/ai-analysis', {
        state: {
          serviceName: data.serviceName,
          message: data.message,
          autoAnalyze: shouldAutoAnalyze,
        }
      });
    } else {
      navigate('/ai-analysis');
    }
  }, [navigate]);

  const goToTraces = useCallback((options?: {
    traceId?: string;
    serviceName?: string;
    mode?: 'observed' | 'inferred';
    sourceService?: string;
    targetService?: string;
  }) => {
    const params = new URLSearchParams();
    
    if (options?.traceId) {
      params.set('trace_id', options.traceId);
    }
    if (options?.serviceName) {
      params.set('service', options.serviceName);
    }
    if (options?.mode) {
      params.set('mode', options.mode);
    }
    if (options?.sourceService) {
      params.set('source_service', options.sourceService);
    }
    if (options?.targetService) {
      params.set('target_service', options.targetService);
    }

    const queryString = params.toString();
    navigate(`/traces${queryString ? `?${queryString}` : ''}`);
  }, [navigate]);

  const goToDashboard = useCallback(() => {
    navigate('/dashboard');
  }, [navigate]);

  const goToAlerts = useCallback((options?: {
    tab?: 'events' | 'rules';
    status?: 'pending' | 'firing' | 'acknowledged' | 'silenced' | 'resolved';
    severity?: 'critical' | 'warning' | 'info';
    serviceName?: string;
    namespace?: string;
    scope?: 'all' | 'edge' | 'service';
    sourceService?: string;
    targetService?: string;
  }) => {
    const params = new URLSearchParams();
    const activeTab = options?.tab;

    if (activeTab) {
      params.set('tab', activeTab);
    }
    if (options?.status) {
      params.set('status', options.status);
    }
    if (options?.severity) {
      params.set('severity', options.severity);
    }
    if (options?.serviceName) {
      params.set('service', options.serviceName);
    }
    if (options?.namespace) {
      params.set('namespace', options.namespace);
    }
    if (options?.scope && options.scope !== 'all') {
      params.set('scope', options.scope);
    }
    if (options?.sourceService) {
      params.set('source_service', options.sourceService);
    }
    if (options?.targetService) {
      params.set('target_service', options.targetService);
    }

    if (activeTab === 'events') {
      if (options?.scope && options.scope !== 'all') {
        params.set('event_scope', options.scope);
      }
      if (options?.namespace) {
        params.set('event_namespace', options.namespace);
      }
      if (options?.serviceName) {
        params.set('event_service', options.serviceName);
      }
      if (options?.sourceService) {
        params.set('event_source_service', options.sourceService);
      }
      if (options?.targetService) {
        params.set('event_target_service', options.targetService);
      }
    }

    if (activeTab === 'rules') {
      if (options?.scope && options.scope !== 'all') {
        params.set('rule_scope', options.scope);
      }
      if (options?.namespace) {
        params.set('rule_namespace', options.namespace);
      }
      if (options?.serviceName) {
        params.set('rule_service', options.serviceName);
      }
      if (options?.sourceService) {
        params.set('rule_source_service', options.sourceService);
      }
      if (options?.targetService) {
        params.set('rule_target_service', options.targetService);
      }
    }

    const queryString = params.toString();
    navigate(`/alerts${queryString ? `?${queryString}` : ''}`);
  }, [navigate]);

  const goBack = useCallback(() => {
    navigate(-1);
  }, [navigate]);

  return {
    goToLogs,
    goToLogDetail,
    goToTopology,
    goToTopologyNode,
    goToAIAnalysis,
    goToTraces,
    goToDashboard,
    goToAlerts,
    goBack,
    currentPath: location.pathname,
    currentSearch: location.search,
  };
}

export default useNavigation;
