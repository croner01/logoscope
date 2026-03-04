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
  attributes?: Record<string, any>;
}

export interface TopologyNodeData {
  id: string;
  label: string;
  type: string;
  metrics?: Record<string, any>;
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
    level?: string;
    search?: string;
    traceId?: string;
    podName?: string;
    timestamp?: string;
    sourceService?: string;
    targetService?: string;
    timeWindow?: string;
  }) => {
    const params = new URLSearchParams();
    
    if (options?.serviceName) {
      params.set('service', options.serviceName);
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
    if (options?.podName) {
      params.set('pod', options.podName);
    }
    if (options?.timestamp) {
      params.set('ts', options.timestamp);
    }
    if (options?.sourceService) {
      params.set('source_service', options.sourceService);
    }
    if (options?.targetService) {
      params.set('target_service', options.targetService);
    }
    if (options?.timeWindow) {
      params.set('time_window', options.timeWindow);
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
    navigate(`/topology?service=${encodeURIComponent(node.id)}&highlight=${encodeURIComponent(node.id)}`);
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
    status?: 'firing' | 'resolved';
    severity?: 'critical' | 'warning' | 'info';
  }) => {
    const params = new URLSearchParams();
    
    if (options?.status) {
      params.set('status', options.status);
    }
    if (options?.severity) {
      params.set('severity', options.severity);
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
