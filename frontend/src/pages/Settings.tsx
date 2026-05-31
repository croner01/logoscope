/**
 * 系统设置页面
 * 参考 Datadog 设计风格
 */
import React, { useCallback, useEffect, useState } from 'react';
import { BookOpen, CheckCircle2, Cpu, Database, RefreshCw, Server, Settings as SettingsIcon, Trash2 } from 'lucide-react';

import ErrorState from '../components/common/ErrorState';
import LoadingState from '../components/common/LoadingState';
import { api } from '../utils/api';

interface CacheStats {
  total_keys: number;
  memory_usage: string;
  hit_rate: number;
  keys_by_pattern: Record<string, number>;
}

interface DeduplicationStats {
  total_events: number;
  duplicate_count: number;
  deduplication_rate: number;
  top_duplicates: Array<{ pattern: string; count: number }>;
}

interface LLMRuntimeStatus {
  configured_provider: string;
  configured_model: string;
  llm_enabled: boolean;
  api_key_configured: boolean;
  local_llm_ready: boolean;
  local_llm_api_base: string;
  supported_providers: string[];
  runtime_config_contract: Record<string, string>;
  deployment_persistence: {
    deployment_file: string;
    deployment_file_exists: boolean;
    deployment_file_writable: boolean;
    enabled_by_default: boolean;
  };
  note: string;
}

interface LLMRuntimeForm {
  provider: string;
  model: string;
  api_base: string;
  api_key: string;
  local_model_path: string;
  clear_api_key: boolean;
  persist_to_deployment: boolean;
  extra: string;
}

interface LLMValidateResult {
  status: string;
  validated: boolean;
  runtime: Record<string, unknown>;
  note: string;
}

interface KBRemoteRuntimeStatus {
  configured_provider: string;
  configured_base_url: string;
  api_key_configured: boolean;
  timeout_seconds: number;
  health_path: string;
  search_path: string;
  upsert_path: string;
  outbox_enabled: boolean;
  outbox_poll_seconds: number;
  outbox_max_attempts: number;
  supported_providers: string[];
  runtime_config_contract: Record<string, string>;
  provider_status: {
    remote_available?: boolean;
    remote_configured?: boolean;
    message?: string;
    outbox_queue_total?: number;
    outbox_failed?: number;
    [key: string]: unknown;
  };
  deployment_persistence: {
    deployment_file: string;
    deployment_file_exists: boolean;
    deployment_file_writable: boolean;
    enabled_by_default: boolean;
  };
  note: string;
}

interface KBRemoteRuntimeForm {
  provider: string;
  base_url: string;
  api_key: string;
  timeout_seconds: string;
  health_path: string;
  search_path: string;
  upsert_path: string;
  outbox_enabled: boolean;
  outbox_poll_seconds: string;
  outbox_max_attempts: string;
  clear_api_key: boolean;
  persist_to_deployment: boolean;
  extra: string;
}

interface KBValidateResult {
  status: string;
  validated: boolean;
  runtime: Record<string, unknown>;
  note: string;
}

interface APIHealthStatus {
  status: string;
  service: string;
  version: string;
  checked_at: string;
}

interface BannerMessage {
  type: 'success' | 'error' | 'info';
  text: string;
}

const EMPTY_CACHE_STATS: CacheStats = {
  total_keys: 0,
  memory_usage: 'N/A',
  hit_rate: 0,
  keys_by_pattern: {},
};

const EMPTY_DEDUP_STATS: DeduplicationStats = {
  total_events: 0,
  duplicate_count: 0,
  deduplication_rate: 0,
  top_duplicates: [],
};

const DEFAULT_LLM_RUNTIME: LLMRuntimeStatus = {
  configured_provider: 'openai',
  configured_model: '',
  llm_enabled: false,
  api_key_configured: false,
  local_llm_ready: false,
  local_llm_api_base: '',
  supported_providers: ['openai', 'claude', 'deepseek', 'local'],
  runtime_config_contract: {
    provider: 'openai|claude|deepseek|local',
    model: 'string',
    api_base: 'string(url)',
    api_key: 'string(optional, masked input)',
    local_model_path: 'string(optional)',
    persist_to_deployment: 'bool(default=true)',
    extra: 'object(optional)',
  },
  deployment_persistence: {
    deployment_file: '',
    deployment_file_exists: false,
    deployment_file_writable: false,
    enabled_by_default: true,
  },
  note: '运行时状态不可用',
};

const DEFAULT_LLM_FORM: LLMRuntimeForm = {
  provider: 'openai',
  model: '',
  api_base: '',
  api_key: '',
  local_model_path: '',
  clear_api_key: false,
  persist_to_deployment: true,
  extra: '{\n  "routing": "reserved"\n}',
};

const KB_PROVIDER_PRESETS: Record<string, { health_path: string; search_path: string; upsert_path: string }> = {
  ragflow: {
    health_path: '/api/v1/system/health',
    search_path: '/api/v1/retrieval',
    upsert_path: '/api/v1/kb/upsert',
  },
  generic_rest: {
    health_path: '/health',
    search_path: '/search',
    upsert_path: '/upsert',
  },
  disabled: {
    health_path: '/health',
    search_path: '/search',
    upsert_path: '/upsert',
  },
};

const DEFAULT_KB_RUNTIME: KBRemoteRuntimeStatus = {
  configured_provider: 'ragflow',
  configured_base_url: '',
  api_key_configured: false,
  timeout_seconds: 5,
  health_path: KB_PROVIDER_PRESETS.ragflow.health_path,
  search_path: KB_PROVIDER_PRESETS.ragflow.search_path,
  upsert_path: KB_PROVIDER_PRESETS.ragflow.upsert_path,
  outbox_enabled: true,
  outbox_poll_seconds: 5,
  outbox_max_attempts: 5,
  supported_providers: ['ragflow', 'generic_rest', 'disabled'],
  runtime_config_contract: {
    provider: 'ragflow|generic_rest|disabled',
    base_url: 'string(url)',
    api_key: 'string(optional, masked input)',
    timeout_seconds: 'int(default=5)',
    health_path: 'string(path)',
    search_path: 'string(path)',
    upsert_path: 'string(path)',
    outbox_enabled: 'bool(default=true)',
    outbox_poll_seconds: 'int(default=5)',
    outbox_max_attempts: 'int(default=5)',
    persist_to_deployment: 'bool(default=true)',
    extra: 'object(optional)',
  },
  provider_status: {
    remote_available: false,
    remote_configured: false,
    message: '远端知识库状态不可用',
    outbox_queue_total: 0,
    outbox_failed: 0,
  },
  deployment_persistence: {
    deployment_file: '',
    deployment_file_exists: false,
    deployment_file_writable: false,
    enabled_by_default: true,
  },
  note: '默认采用 RAGFlow provider，可按需改成 generic_rest 或 disabled。',
};

const DEFAULT_KB_FORM: KBRemoteRuntimeForm = {
  provider: 'ragflow',
  base_url: '',
  api_key: '',
  timeout_seconds: '5',
  health_path: KB_PROVIDER_PRESETS.ragflow.health_path,
  search_path: KB_PROVIDER_PRESETS.ragflow.search_path,
  upsert_path: KB_PROVIDER_PRESETS.ragflow.upsert_path,
  outbox_enabled: true,
  outbox_poll_seconds: '5',
  outbox_max_attempts: '5',
  clear_api_key: false,
  persist_to_deployment: true,
  extra: '{\n  "dataset_id": ""\n}',
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

function asNumber(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function asPercent(value: number): number {
  const normalized = value <= 1 ? value * 100 : value;
  return Number(Math.max(normalized, 0).toFixed(1));
}

function asString(value: unknown, fallback = ''): string {
  if (typeof value !== 'string') {
    return fallback;
  }
  const text = value.trim();
  return text || fallback;
}

function normalizeCacheStats(raw: Record<string, unknown>): CacheStats {
  const totalEntries = asNumber(raw?.total_entries ?? raw?.total_keys, 0);
  const activeEntries = asNumber(raw?.active_entries, totalEntries);
  const expiredEntries = asNumber(raw?.expired_entries, 0);
  const rawHitRate = asNumber(raw?.hit_rate, Number.NaN);

  const keysByPattern: Record<string, number> = {};
  if (raw?.keys_by_pattern && typeof raw.keys_by_pattern === 'object') {
    Object.entries(raw.keys_by_pattern).forEach(([pattern, count]) => {
      keysByPattern[String(pattern)] = asNumber(count, 0);
    });
  }

  return {
    total_keys: totalEntries,
    memory_usage: asString(raw?.memory_usage, 'N/A'),
    hit_rate: Number.isFinite(rawHitRate)
      ? asPercent(rawHitRate)
      : (totalEntries > 0 ? Number(((activeEntries / totalEntries) * 100).toFixed(1)) : 0),
    keys_by_pattern: Object.keys(keysByPattern).length > 0
      ? keysByPattern
      : {
          active_entries: activeEntries,
          expired_entries: expiredEntries,
        },
  };
}

function normalizeDedupStats(raw: Record<string, unknown>): DeduplicationStats {
  const totalEvents = asNumber(raw?.total_processed ?? raw?.total_events, 0);
  const duplicateCount = asNumber(raw?.duplicates_found ?? raw?.duplicate_count, 0);
  const duplicateRate = asNumber(raw?.duplicate_rate, 0);

  const topDuplicates = Array.isArray(raw?.top_duplicates)
    ? raw.top_duplicates
        .map((item) => {
          const itemRecord = asRecord(item);
          return {
            pattern: asString(itemRecord.pattern ?? itemRecord.name, 'LooseAny'),
            count: asNumber(itemRecord.count, 0),
          };
        })
        .map((item) => ({
          pattern: item.pattern,
          count: item.count,
        }))
        .filter((item: { pattern: string; count: number }) => item.count > 0)
    : [
        { pattern: '按 ID 去重', count: asNumber(raw?.duplicates_by_id, 0) },
        { pattern: '按语义去重', count: asNumber(raw?.duplicates_by_semantic, 0) },
      ].filter((item) => item.count > 0);

  return {
    total_events: totalEvents,
    duplicate_count: duplicateCount,
    deduplication_rate: asPercent(duplicateRate),
    top_duplicates: topDuplicates,
  };
}

function normalizeLLMRuntime(raw: Record<string, unknown>): LLMRuntimeStatus {
  const providers = Array.isArray(raw?.supported_providers)
    ? raw.supported_providers.map((item) => asString(item)).filter(Boolean)
    : [];
  const deploymentPersistence = asRecord(raw?.deployment_persistence);

  const contractRaw = raw?.runtime_config_contract;
  const runtimeContract: Record<string, string> = {};
  if (contractRaw && typeof contractRaw === 'object') {
    Object.entries(contractRaw).forEach(([key, value]) => {
      runtimeContract[String(key)] = asString(value, '');
    });
  }

  return {
    configured_provider: asString(raw?.configured_provider, 'openai'),
    configured_model: asString(raw?.configured_model, ''),
    llm_enabled: Boolean(raw?.llm_enabled),
    api_key_configured: Boolean(raw?.api_key_configured),
    local_llm_ready: Boolean(raw?.local_llm_ready),
    local_llm_api_base: asString(raw?.local_llm_api_base, ''),
    supported_providers: providers.length > 0 ? providers : DEFAULT_LLM_RUNTIME.supported_providers,
    runtime_config_contract: Object.keys(runtimeContract).length > 0
      ? runtimeContract
      : DEFAULT_LLM_RUNTIME.runtime_config_contract,
    deployment_persistence: {
      deployment_file: asString(deploymentPersistence.deployment_file, ''),
      deployment_file_exists: Boolean(deploymentPersistence.deployment_file_exists),
      deployment_file_writable: Boolean(deploymentPersistence.deployment_file_writable),
      enabled_by_default: Boolean(deploymentPersistence.enabled_by_default ?? true),
    },
    note: asString(raw?.note, ''),
  };
}

function normalizeKBRemoteRuntime(raw: Record<string, unknown>): KBRemoteRuntimeStatus {
  const providers = Array.isArray(raw?.supported_providers)
    ? raw.supported_providers.map((item) => asString(item)).filter(Boolean)
    : [];
  const provider = asString(raw?.configured_provider, 'ragflow');
  const preset = KB_PROVIDER_PRESETS[provider] || KB_PROVIDER_PRESETS.ragflow;
  const deploymentPersistence = asRecord(raw?.deployment_persistence);

  const contractRaw = raw?.runtime_config_contract;
  const runtimeContract: Record<string, string> = {};
  if (contractRaw && typeof contractRaw === 'object') {
    Object.entries(contractRaw).forEach(([key, value]) => {
      runtimeContract[String(key)] = asString(value, '');
    });
  }

  const providerStatusRaw = raw?.provider_status;
  const providerStatus = asRecord(providerStatusRaw);

  return {
    configured_provider: provider,
    configured_base_url: asString(raw?.configured_base_url, ''),
    api_key_configured: Boolean(raw?.api_key_configured),
    timeout_seconds: Math.max(1, asNumber(raw?.timeout_seconds, 5)),
    health_path: asString(raw?.health_path, preset.health_path),
    search_path: asString(raw?.search_path, preset.search_path),
    upsert_path: asString(raw?.upsert_path, preset.upsert_path),
    outbox_enabled: Boolean(raw?.outbox_enabled ?? true),
    outbox_poll_seconds: Math.max(1, asNumber(raw?.outbox_poll_seconds, 5)),
    outbox_max_attempts: Math.max(1, asNumber(raw?.outbox_max_attempts, 5)),
    supported_providers: providers.length > 0 ? providers : DEFAULT_KB_RUNTIME.supported_providers,
    runtime_config_contract: Object.keys(runtimeContract).length > 0
      ? runtimeContract
      : DEFAULT_KB_RUNTIME.runtime_config_contract,
    provider_status: {
      remote_available: Boolean(providerStatus.remote_available),
      remote_configured: Boolean(providerStatus.remote_configured),
      message: asString(providerStatus.message, ''),
      outbox_queue_total: asNumber(providerStatus.outbox_queue_total, 0),
      outbox_failed: asNumber(providerStatus.outbox_failed, 0),
      ...providerStatus,
    },
    deployment_persistence: {
      deployment_file: asString(deploymentPersistence.deployment_file, ''),
      deployment_file_exists: Boolean(deploymentPersistence.deployment_file_exists),
      deployment_file_writable: Boolean(deploymentPersistence.deployment_file_writable),
      enabled_by_default: Boolean(deploymentPersistence.enabled_by_default ?? true),
    },
    note: asString(raw?.note, ''),
  };
}

function getErrorMessage(error: unknown, fallback: string): string {
  const errorRecord = asRecord(error);
  const responseRecord = asRecord(errorRecord.response);
  const dataRecord = asRecord(responseRecord.data);
  const detail = dataRecord.detail;
  if (typeof detail === 'string' && detail.trim()) {
    return detail.trim();
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0];
    if (typeof first === 'string') {
      return first;
    }
    if (first && typeof first.msg === 'string') {
      return first.msg;
    }
  }
  if (typeof errorRecord.message === 'string' && errorRecord.message.trim()) {
    return errorRecord.message.trim();
  }
  return fallback;
}

function formatCheckedAt(isoTime: string): string {
  if (!isoTime) {
    return '-';
  }
  const date = new Date(isoTime);
  if (Number.isNaN(date.getTime())) {
    return isoTime;
  }
  return date.toLocaleString('zh-CN', { hour12: false });
}

const Settings: React.FC = () => {
  const [cacheStats, setCacheStats] = useState<CacheStats>(EMPTY_CACHE_STATS);
  const [dedupStats, setDedupStats] = useState<DeduplicationStats>(EMPTY_DEDUP_STATS);
  const [llmRuntime, setLlmRuntime] = useState<LLMRuntimeStatus>(DEFAULT_LLM_RUNTIME);
  const [kbRuntime, setKbRuntime] = useState<KBRemoteRuntimeStatus>(DEFAULT_KB_RUNTIME);

  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [banner, setBanner] = useState<BannerMessage | null>(null);

  const [clearingCache, setClearingCache] = useState(false);
  const [clearingDedupCache, setClearingDedupCache] = useState(false);
  const [checkingApiHealth, setCheckingApiHealth] = useState(false);
  const [refreshingLLMRuntime, setRefreshingLLMRuntime] = useState(false);
  const [validatingLLMRuntime, setValidatingLLMRuntime] = useState(false);
  const [updatingLLMRuntime, setUpdatingLLMRuntime] = useState(false);
  const [refreshingKBRuntime, setRefreshingKBRuntime] = useState(false);
  const [validatingKBRuntime, setValidatingKBRuntime] = useState(false);
  const [updatingKBRuntime, setUpdatingKBRuntime] = useState(false);

  const [apiHealth, setApiHealth] = useState<APIHealthStatus | null>(null);
  const [llmForm, setLlmForm] = useState<LLMRuntimeForm>(DEFAULT_LLM_FORM);
  const [llmValidateResult, setLlmValidateResult] = useState<LLMValidateResult | null>(null);
  const [kbForm, setKbForm] = useState<KBRemoteRuntimeForm>(DEFAULT_KB_FORM);
  const [kbValidateResult, setKbValidateResult] = useState<KBValidateResult | null>(null);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);

  const fetchRuntimeStatus = useCallback(async (showNotice = false) => {
    setRefreshingLLMRuntime(true);
    try {
      const raw = await api.getLLMRuntimeStatus();
      const normalized = normalizeLLMRuntime(raw || {});
      setLlmRuntime(normalized);
      setLlmForm((prev) => ({
        ...prev,
        provider: normalized.configured_provider || prev.provider,
        model: normalized.configured_model || prev.model,
        api_base: normalized.local_llm_api_base || prev.api_base,
      }));
      if (showNotice) {
        setBanner({ type: 'success', text: 'LLM 运行时状态已刷新' });
      }
    } catch (error) {
      console.error('Failed to fetch llm runtime status:', error);
      if (showNotice) {
        setBanner({ type: 'error', text: getErrorMessage(error, '获取 LLM 运行时状态失败') });
      }
    } finally {
      setRefreshingLLMRuntime(false);
    }
  }, []);

  const loadModelList = useCallback(async (provider: string) => {
    if (!provider) {
      setAvailableModels([]);
      return;
    }
    setLoadingModels(true);
    try {
      const data = await api.getLLMAvailableModels(provider);
      if (data?.models && Array.isArray(data.models)) {
        setAvailableModels(data.models);
      } else {
        setAvailableModels([]);
      }
    } catch {
      setAvailableModels([]);
    } finally {
      setLoadingModels(false);
    }
  }, []);

  const fetchKBRuntimeStatus = useCallback(async (showNotice = false) => {
    setRefreshingKBRuntime(true);
    try {
      const raw = await api.getKBRuntimeStatus();
      const normalized = normalizeKBRemoteRuntime(raw || {});
      setKbRuntime(normalized);
      setKbForm((prev) => ({
        ...prev,
        provider: normalized.configured_provider || prev.provider,
        base_url: normalized.configured_base_url || prev.base_url,
        timeout_seconds: String(normalized.timeout_seconds || 5),
        health_path: normalized.health_path || prev.health_path,
        search_path: normalized.search_path || prev.search_path,
        upsert_path: normalized.upsert_path || prev.upsert_path,
        outbox_enabled: normalized.outbox_enabled,
        outbox_poll_seconds: String(normalized.outbox_poll_seconds || 5),
        outbox_max_attempts: String(normalized.outbox_max_attempts || 5),
      }));
      if (showNotice) {
        setBanner({ type: 'success', text: '远端知识库运行时状态已刷新' });
      }
    } catch (error) {
      console.error('Failed to fetch kb runtime status:', error);
      if (showNotice) {
        setBanner({ type: 'error', text: getErrorMessage(error, '获取远端知识库运行时状态失败') });
      }
    } finally {
      setRefreshingKBRuntime(false);
    }
  }, []);

  const fetchSettingsData = useCallback(async (options?: { initial?: boolean; silentSuccess?: boolean }) => {
    const initial = Boolean(options?.initial);
    if (initial) {
      setLoading(true);
    } else {
      setRefreshing(true);
    }
    setLoadError(null);

    try {
      const [cacheResult, dedupResult, llmResult, kbResult] = await Promise.allSettled([
        api.getCacheStats(),
        api.getDeduplicationStats(),
        api.getLLMRuntimeStatus(),
        api.getKBRuntimeStatus(),
      ]);

      let failedCount = 0;

      if (cacheResult.status === 'fulfilled') {
        setCacheStats(normalizeCacheStats(cacheResult.value || {}));
      } else {
        failedCount += 1;
        setCacheStats(EMPTY_CACHE_STATS);
      }

      if (dedupResult.status === 'fulfilled') {
        setDedupStats(normalizeDedupStats(dedupResult.value || {}));
      } else {
        failedCount += 1;
        setDedupStats(EMPTY_DEDUP_STATS);
      }

      if (llmResult.status === 'fulfilled') {
        const normalized = normalizeLLMRuntime(llmResult.value || {});
        setLlmRuntime(normalized);
        setLlmForm((prev) => ({
          ...prev,
          provider: normalized.configured_provider || prev.provider,
          model: normalized.configured_model || prev.model,
          api_base: normalized.local_llm_api_base || prev.api_base,
        }));
      } else {
        failedCount += 1;
        setLlmRuntime(DEFAULT_LLM_RUNTIME);
      }

      if (kbResult.status === 'fulfilled') {
        const normalized = normalizeKBRemoteRuntime(kbResult.value || {});
        setKbRuntime(normalized);
        setKbForm((prev) => ({
          ...prev,
          provider: normalized.configured_provider || prev.provider,
          base_url: normalized.configured_base_url || prev.base_url,
          timeout_seconds: String(normalized.timeout_seconds || 5),
          health_path: normalized.health_path || prev.health_path,
          search_path: normalized.search_path || prev.search_path,
          upsert_path: normalized.upsert_path || prev.upsert_path,
          outbox_enabled: normalized.outbox_enabled,
          outbox_poll_seconds: String(normalized.outbox_poll_seconds || 5),
          outbox_max_attempts: String(normalized.outbox_max_attempts || 5),
        }));
      } else {
        failedCount += 1;
        setKbRuntime(DEFAULT_KB_RUNTIME);
      }

      if (failedCount === 4) {
        setLoadError('系统设置加载失败，请检查后端服务是否可用。');
      } else if (failedCount > 0) {
        setBanner({ type: 'error', text: '部分设置项加载失败，已展示可用数据。' });
      } else if (!initial && !options?.silentSuccess) {
        setBanner({ type: 'success', text: '系统设置已刷新' });
      }
    } finally {
      if (initial) {
        setLoading(false);
      } else {
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    fetchSettingsData({ initial: true });
  }, [fetchSettingsData]);

  useEffect(() => {
    if (llmForm.provider) {
      loadModelList(llmForm.provider);
    }
  }, [llmForm.provider, loadModelList]);

  const handleRefreshAll = async () => {
    await fetchSettingsData({ initial: false });
  };

  const handleClearCache = async () => {
    if (!window.confirm('确定要清除全部缓存吗？')) {
      return;
    }

    setClearingCache(true);
    try {
      const result = await api.clearCache();
      setBanner({ type: 'success', text: `缓存清除成功，清理 ${result?.cleared ?? 0} 条` });
      await fetchSettingsData({ initial: false, silentSuccess: true });
    } catch (error) {
      console.error('Failed to clear cache:', error);
      setBanner({ type: 'error', text: getErrorMessage(error, '缓存清除失败') });
    } finally {
      setClearingCache(false);
    }
  };

  const handleClearDedupCache = async () => {
    if (!window.confirm('确定要清除去重缓存吗？')) {
      return;
    }

    setClearingDedupCache(true);
    try {
      await api.clearDeduplicationCache();
      setBanner({ type: 'success', text: '去重缓存清除成功' });
      await fetchSettingsData({ initial: false, silentSuccess: true });
    } catch (error) {
      console.error('Failed to clear deduplication cache:', error);
      setBanner({ type: 'error', text: getErrorMessage(error, '去重缓存清除失败') });
    } finally {
      setClearingDedupCache(false);
    }
  };

  const handleCheckApiHealth = async () => {
    setCheckingApiHealth(true);
    try {
      const result = await api.health();
      setApiHealth({
        status: asString(result?.status, 'LooseAny'),
        service: asString(result?.service, 'LooseAny'),
        version: asString(result?.version, 'LooseAny'),
        checked_at: new Date().toISOString(),
      });
      setBanner({ type: 'success', text: 'API 连通性检查成功' });
    } catch (error) {
      console.error('Failed to check api health:', error);
      setBanner({ type: 'error', text: getErrorMessage(error, 'API 连通性检查失败') });
    } finally {
      setCheckingApiHealth(false);
    }
  };

  const buildLLMRuntimePayload = (): {
    provider: string;
    model?: string;
    api_base?: string;
    api_key?: string;
    local_model_path?: string;
    clear_api_key?: boolean;
    persist_to_deployment?: boolean;
    extra: Record<string, unknown>;
  } | null => {
    let extra: Record<string, unknown> = {};
    const extraRaw = llmForm.extra.trim();

    if (extraRaw) {
      try {
        const parsed = JSON.parse(extraRaw);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          setBanner({ type: 'error', text: 'extra 必须是 JSON 对象' });
          return null;
        }
        extra = parsed as Record<string, unknown>;
      } catch (error) {
        setBanner({ type: 'error', text: `extra JSON 解析失败: ${getErrorMessage(error, '格式错误')}` });
        return null;
      }
    }

    return {
      provider: llmForm.provider,
      model: llmForm.model.trim() || undefined,
      api_base: llmForm.api_base.trim() || undefined,
      api_key: llmForm.api_key.trim() || undefined,
      local_model_path: llmForm.local_model_path.trim() || undefined,
      clear_api_key: llmForm.clear_api_key,
      persist_to_deployment: llmForm.persist_to_deployment,
      extra,
    };
  };

  const handleValidateLLMRuntime = async () => {
    const payload = buildLLMRuntimePayload();
    if (!payload) {
      return;
    }

    setValidatingLLMRuntime(true);
    try {
      const result = await api.validateLLMRuntimeConfig(payload);

      setLlmValidateResult({
        status: asString(result?.status, 'LooseAny'),
        validated: Boolean(result?.validated),
        runtime: result?.runtime && typeof result.runtime === 'object' ? result.runtime : {},
        note: asString(result?.note, ''),
      });
      setBanner({ type: 'success', text: 'LLM 参数校验通过' });
    } catch (error) {
      console.error('Failed to validate llm runtime config:', error);
      setLlmValidateResult(null);
      setBanner({ type: 'error', text: getErrorMessage(error, 'LLM 参数校验失败') });
    } finally {
      setValidatingLLMRuntime(false);
    }
  };

  const handleUpdateLLMRuntime = async () => {
    const payload = buildLLMRuntimePayload();
    if (!payload) {
      return;
    }

    if (payload.api_key && payload.clear_api_key) {
      setBanner({ type: 'error', text: 'API Key 输入与“清空现有 API Key”不能同时使用' });
      return;
    }

    setUpdatingLLMRuntime(true);
    try {
      const result = await api.updateLLMRuntimeConfig(payload);
      const runtimeStatus = normalizeLLMRuntime(result?.runtime_status || {});
      setLlmRuntime(runtimeStatus);
      setLlmForm((prev) => ({
        ...prev,
        provider: runtimeStatus.configured_provider || prev.provider,
        model: runtimeStatus.configured_model || prev.model,
        api_base: runtimeStatus.local_llm_api_base || prev.api_base,
        api_key: '',
        clear_api_key: false,
      }));
      const persisted = Boolean(result?.deployment_persistence?.persisted);
      const persistError = asString(result?.deployment_persistence?.error, '');
      const successText = payload.persist_to_deployment
        ? (persisted
            ? 'API Key 与 LLM 运行时配置已更新，并已同步到部署文件'
            : `API Key 与 LLM 运行时配置已更新，但部署文件持久化失败: ${persistError || 'LooseAny'}`)
        : 'API Key 与 LLM 运行时配置已更新（仅当前进程生效）';
      setBanner({ type: persisted || !payload.persist_to_deployment ? 'success' : 'info', text: successText });
      await fetchSettingsData({ initial: false, silentSuccess: true });
    } catch (error) {
      console.error('Failed to update llm runtime config:', error);
      setBanner({ type: 'error', text: getErrorMessage(error, '更新 LLM 运行时配置失败') });
    } finally {
      setUpdatingLLMRuntime(false);
    }
  };

  const buildKBRuntimePayload = (): {
    provider: string;
    base_url?: string;
    api_key?: string;
    timeout_seconds: number;
    health_path: string;
    search_path: string;
    upsert_path: string;
    outbox_enabled: boolean;
    outbox_poll_seconds: number;
    outbox_max_attempts: number;
    clear_api_key: boolean;
    persist_to_deployment: boolean;
    extra: Record<string, unknown>;
  } | null => {
    let extra: Record<string, unknown> = {};
    const extraRaw = kbForm.extra.trim();

    if (extraRaw) {
      try {
        const parsed = JSON.parse(extraRaw);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          setBanner({ type: 'error', text: 'KB extra 必须是 JSON 对象' });
          return null;
        }
        extra = parsed as Record<string, unknown>;
      } catch (error) {
        setBanner({ type: 'error', text: `KB extra JSON 解析失败: ${getErrorMessage(error, '格式错误')}` });
        return null;
      }
    }

    const timeoutSeconds = Math.max(1, asNumber(kbForm.timeout_seconds, 5));
    const outboxPollSeconds = Math.max(1, asNumber(kbForm.outbox_poll_seconds, 5));
    const outboxMaxAttempts = Math.max(1, asNumber(kbForm.outbox_max_attempts, 5));

    return {
      provider: kbForm.provider,
      base_url: kbForm.base_url.trim() || undefined,
      api_key: kbForm.api_key.trim() || undefined,
      timeout_seconds: timeoutSeconds,
      health_path: kbForm.health_path.trim() || '/health',
      search_path: kbForm.search_path.trim() || '/search',
      upsert_path: kbForm.upsert_path.trim() || '/upsert',
      outbox_enabled: kbForm.outbox_enabled,
      outbox_poll_seconds: outboxPollSeconds,
      outbox_max_attempts: outboxMaxAttempts,
      clear_api_key: kbForm.clear_api_key,
      persist_to_deployment: kbForm.persist_to_deployment,
      extra,
    };
  };

  const handleValidateKBRuntime = async () => {
    const payload = buildKBRuntimePayload();
    if (!payload) {
      return;
    }

    setValidatingKBRuntime(true);
    try {
      const result = await api.validateKBRuntimeConfig(payload);
      setKbValidateResult({
        status: asString(result?.status, 'LooseAny'),
        validated: Boolean(result?.validated),
        runtime: result?.runtime && typeof result.runtime === 'object' ? result.runtime : {},
        note: asString(result?.note, ''),
      });
      setBanner({ type: 'success', text: '远端知识库参数校验通过' });
    } catch (error) {
      console.error('Failed to validate kb runtime config:', error);
      setKbValidateResult(null);
      setBanner({ type: 'error', text: getErrorMessage(error, '远端知识库参数校验失败') });
    } finally {
      setValidatingKBRuntime(false);
    }
  };

  const handleUpdateKBRuntime = async () => {
    const payload = buildKBRuntimePayload();
    if (!payload) {
      return;
    }

    if (payload.provider !== 'disabled' && !payload.base_url) {
      setBanner({ type: 'error', text: '启用远端知识库时必须填写 Base URL' });
      return;
    }
    if (payload.api_key && payload.clear_api_key) {
      setBanner({ type: 'error', text: 'KB API Key 输入与“清空现有 API Key”不能同时使用' });
      return;
    }

    setUpdatingKBRuntime(true);
    try {
      const result = await api.updateKBRuntimeConfig(payload);
      const runtimeStatus = normalizeKBRemoteRuntime(result?.runtime_status || {});
      setKbRuntime(runtimeStatus);
      setKbForm((prev) => ({
        ...prev,
        provider: runtimeStatus.configured_provider || prev.provider,
        base_url: runtimeStatus.configured_base_url || prev.base_url,
        api_key: '',
        clear_api_key: false,
        timeout_seconds: String(runtimeStatus.timeout_seconds || 5),
        health_path: runtimeStatus.health_path || prev.health_path,
        search_path: runtimeStatus.search_path || prev.search_path,
        upsert_path: runtimeStatus.upsert_path || prev.upsert_path,
        outbox_enabled: runtimeStatus.outbox_enabled,
        outbox_poll_seconds: String(runtimeStatus.outbox_poll_seconds || 5),
        outbox_max_attempts: String(runtimeStatus.outbox_max_attempts || 5),
      }));
      const persisted = Boolean(result?.deployment_persistence?.persisted);
      const persistError = asString(result?.deployment_persistence?.error, '');
      const successText = payload.persist_to_deployment
        ? (persisted
            ? '远端知识库运行时配置已更新，并已同步到部署文件'
            : `远端知识库运行时配置已更新，但部署文件持久化失败: ${persistError || 'LooseAny'}`)
        : '远端知识库运行时配置已更新（仅当前进程生效）';
      setBanner({ type: persisted || !payload.persist_to_deployment ? 'success' : 'info', text: successText });
      await fetchSettingsData({ initial: false, silentSuccess: true });
    } catch (error) {
      console.error('Failed to update kb runtime config:', error);
      setBanner({ type: 'error', text: getErrorMessage(error, '更新远端知识库运行时配置失败') });
    } finally {
      setUpdatingKBRuntime(false);
    }
  };

  if (loading) {
    return <LoadingState message="加载系统设置..." />;
  }

  if (loadError) {
    return <ErrorState message={loadError} onRetry={() => fetchSettingsData({ initial: true })} />;
  }

  const cacheHasData = cacheStats.total_keys > 0;
  const dedupHasData = dedupStats.total_events > 0 || dedupStats.duplicate_count > 0;

  return (
    <div className="flex flex-col h-full">
      {/* 页头 */}
      <div className="flex-shrink-0 px-6 py-4 border-b animate-fade-in" style={{ background: 'var(--app-surface)', borderColor: 'var(--app-border)' }}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl flex items-center justify-center" style={{ background: 'var(--brand-secondary-soft)', color: 'var(--brand-secondary)' }}>
              <SettingsIcon size={18} />
            </div>
            <div>
              <h1 className="text-base font-bold" style={{ color: 'var(--app-text)' }}>系统设置</h1>
              <p className="text-xs" style={{ color: 'var(--app-text-subtle)' }}>管理系统配置、缓存、LLM 与远端知识库运行时</p>
            </div>
          </div>
          <button
            onClick={handleRefreshAll}
            disabled={refreshing}
            className="btn btn-secondary"
          >
            <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} />
            刷新全部
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-auto p-6">

      {banner && (
        <div
          className="mb-5 px-4 py-3 rounded-xl text-sm flex items-center gap-2"
          style={{
            background: banner.type === 'success' ? 'var(--color-success-soft)' : banner.type === 'error' ? 'var(--color-error-soft)' : 'var(--color-info-soft)',
            border: `1px solid ${banner.type === 'success' ? '#a7f3d0' : banner.type === 'error' ? '#fca5a5' : '#bfdbfe'}`,
            color: banner.type === 'success' ? 'var(--color-success-dark)' : banner.type === 'error' ? 'var(--color-error-dark)' : 'var(--color-info-dark)',
          }}
        >
          <CheckCircle2 size={14} />
          {banner.text}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <div className="card overflow-hidden">
          <div className="card-header">
            <div className="card-title">
              <Database size={14} style={{ color: 'var(--color-info)' }} />
              缓存管理
            </div>
            <button
              onClick={handleClearCache}
              disabled={clearingCache}
              className="btn btn-danger"
            >
              {clearingCache ? <RefreshCw size={12} className="animate-spin" /> : <Trash2 size={12} />}
              清空缓存
            </button>
          </div>
          <div className="card-body">
            <div className="space-y-4">
              <div className="grid grid-cols-3 gap-3">
                <div className="text-center p-3 rounded-xl" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                  <div className="text-2xl font-bold" style={{ color: 'var(--app-text)' }}>
                    {cacheStats.total_keys.toLocaleString()}
                  </div>
                  <div className="text-[11px] mt-1" style={{ color: 'var(--app-text-subtle)' }}>缓存键数</div>
                </div>
                <div className="text-center p-3 rounded-xl" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                  <div className="text-2xl font-bold" style={{ color: 'var(--app-text)' }}>{cacheStats.memory_usage}</div>
                  <div className="text-[11px] mt-1" style={{ color: 'var(--app-text-subtle)' }}>内存使用</div>
                </div>
                <div className="text-center p-3 rounded-xl" style={{ background: 'var(--color-success-soft)', border: '1px solid #a7f3d0' }}>
                  <div className="text-2xl font-bold" style={{ color: 'var(--color-success-dark)' }}>{cacheStats.hit_rate}%</div>
                  <div className="text-[11px] mt-1" style={{ color: 'var(--color-success-dark)', opacity: 0.7 }}>活跃率</div>
                </div>
              </div>

              <div>
                <div className="section-label mb-2">按模式分布</div>
                <div className="space-y-1.5">
                  {cacheHasData && Object.entries(cacheStats.keys_by_pattern).length > 0 ? (
                    Object.entries(cacheStats.keys_by_pattern).map(([pattern, count]) => (
                      <div key={pattern} className="flex items-center justify-between py-1.5 px-2.5 rounded-lg" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                        <code className="text-xs" style={{ color: 'var(--app-text-muted)' }}>{pattern}</code>
                        <span className="text-xs font-semibold" style={{ color: 'var(--app-text)' }}>{count.toLocaleString()}</span>
                      </div>
                    ))
                  ) : (
                    <div className="text-xs py-2" style={{ color: 'var(--app-text-subtle)' }}>
                      {cacheHasData ? '暂无可用分布数据' : '当前缓存暂无数据（缓存尚未命中或已过期）'}
                    </div>
                  )}
                </div>
              </div>

              <p className="text-[11px] pt-2" style={{ color: 'var(--app-text-subtle)', borderTop: '1px solid var(--app-border-subtle)', paddingTop: '10px' }}>
                仅保留清空全部缓存，避免误导性配置。
              </p>
            </div>
          </div>
        </div>

        <div className="card overflow-hidden">
          <div className="card-header">
            <div className="card-title">
              <Server size={14} style={{ color: 'var(--color-success)' }} />
              去重统计
            </div>
            <button
              onClick={handleClearDedupCache}
              disabled={clearingDedupCache}
              className="btn btn-secondary"
            >
              {clearingDedupCache ? <RefreshCw size={12} className="animate-spin" /> : <Trash2 size={12} />}
              清除去重缓存
            </button>
          </div>
          <div className="card-body">
            <div className="space-y-4">
              <div className="grid grid-cols-3 gap-3">
                <div className="text-center p-3 rounded-xl" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                  <div className="text-2xl font-bold" style={{ color: 'var(--app-text)' }}>
                    {dedupStats.total_events.toLocaleString()}
                  </div>
                  <div className="text-[11px] mt-1" style={{ color: 'var(--app-text-subtle)' }}>总事件数</div>
                </div>
                <div className="text-center p-3 rounded-xl" style={{ background: 'var(--color-warning-soft)', border: '1px solid #fde68a' }}>
                  <div className="text-2xl font-bold" style={{ color: 'var(--color-warning-dark)' }}>
                    {dedupStats.duplicate_count.toLocaleString()}
                  </div>
                  <div className="text-[11px] mt-1" style={{ color: 'var(--color-warning-dark)', opacity: 0.7 }}>重复数</div>
                </div>
                <div className="text-center p-3 rounded-xl" style={{ background: 'var(--color-info-soft)', border: '1px solid #bfdbfe' }}>
                  <div className="text-2xl font-bold" style={{ color: 'var(--color-info-dark)' }}>
                    {dedupStats.deduplication_rate}%
                  </div>
                  <div className="text-[11px] mt-1" style={{ color: 'var(--color-info-dark)', opacity: 0.7 }}>去重率</div>
                </div>
              </div>

              <div>
                <div className="section-label mb-2">常见重复模式</div>
                <div className="space-y-1.5">
                  {dedupStats.top_duplicates.length > 0 ? (
                    dedupStats.top_duplicates.map((item, index) => (
                      <div
                        key={`${item.pattern}-${index}`}
                        className="flex items-center justify-between py-1.5 px-2.5 rounded-lg"
                        style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}
                      >
                        <span className="text-xs truncate" style={{ color: 'var(--app-text-muted)' }}>{item.pattern}</span>
                        <span className="text-xs font-bold ml-3 flex-shrink-0" style={{ color: 'var(--app-text)' }}>
                          {item.count.toLocaleString()} 次
                        </span>
                      </div>
                    ))
                  ) : (
                    <div className="text-xs py-2" style={{ color: 'var(--app-text-subtle)' }}>
                      {dedupHasData ? '暂无重复模式样本' : '去重统计暂无数据（尚未写入样本或去重器未启用）'}
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>

        <div className="card overflow-hidden lg:col-span-2">
          <div className="card-header">
            <div className="card-title">
              <BookOpen size={14} style={{ color: 'var(--brand-primary)' }} />
              远端知识库运行时
              <span className="badge badge-neutral ml-1">RAGFlow</span>
            </div>
            <button
              onClick={() => fetchKBRuntimeStatus(true)}
              disabled={refreshingKBRuntime}
              className="btn btn-secondary"
            >
              <RefreshCw size={12} className={refreshingKBRuntime ? 'animate-spin' : ''} />
              刷新状态
            </button>
          </div>

          <div className="card-body space-y-4">
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              {[
                { label: 'Provider', value: kbRuntime.configured_provider || 'LooseAny', status: 'neutral' as const },
                { label: '远端可用性', value: kbRuntime.provider_status.remote_available ? '可用' : '不可用', status: (kbRuntime.provider_status.remote_available ? 'ok' : 'warn') as 'ok'|'warn'|'neutral' },
                { label: 'API Key', value: kbRuntime.api_key_configured ? '已配置' : '未配置', status: (kbRuntime.api_key_configured ? 'ok' : 'warn') as 'ok'|'warn'|'neutral' },
                { label: 'Outbox 积压', value: String(asNumber(kbRuntime.provider_status.outbox_queue_total, 0)), status: 'neutral' as const },
                { label: 'Outbox 失败', value: String(asNumber(kbRuntime.provider_status.outbox_failed, 0)), status: (asNumber(kbRuntime.provider_status.outbox_failed, 0) > 0 ? 'warn' : 'ok') as 'ok'|'warn'|'neutral' },
              ].map(item => (
                <div key={item.label} className="p-3 rounded-xl" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                  <div className="section-label mb-1.5">{item.label}</div>
                  <div className="text-sm font-semibold" style={{ color: item.status === 'ok' ? 'var(--color-success-dark)' : item.status === 'warn' ? 'var(--color-error-dark)' : 'var(--app-text)' }}>{item.value}</div>
                </div>
              ))}
            </div>

            <div className="space-y-2 text-xs">
              <div className="flex items-start gap-2 py-2 px-3 rounded-lg" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                <span className="font-semibold flex-shrink-0" style={{ color: 'var(--app-text-muted)' }}>Base URL</span>
                <code className="flex-1 truncate" style={{ color: 'var(--brand-primary)' }}>{kbRuntime.configured_base_url || '未配置'}</code>
              </div>
              <div className="flex items-start gap-2 py-2 px-3 rounded-lg" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                <span className="font-semibold flex-shrink-0" style={{ color: 'var(--app-text-muted)' }}>Provider 状态</span>
                <span style={{ color: 'var(--app-text)' }}>{asString(kbRuntime.provider_status.message, '未知')}</span>
              </div>
              <div className="flex items-start gap-2 py-2 px-3 rounded-lg" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                <span className="font-semibold flex-shrink-0" style={{ color: 'var(--app-text-muted)' }}>支持 Provider</span>
                <span style={{ color: 'var(--app-text)' }}>{kbRuntime.supported_providers.join(', ') || '-'}</span>
              </div>
              <div className="flex items-start gap-2 py-2 px-3 rounded-lg" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                <span className="font-semibold flex-shrink-0" style={{ color: 'var(--app-text-muted)' }}>部署文件</span>
                <code className="flex-1 truncate" style={{ color: 'var(--app-text)' }}>{kbRuntime.deployment_persistence.deployment_file || '未配置'}</code>
                <span className="text-xs px-1.5 py-0.5 rounded" style={{ background: kbRuntime.deployment_persistence.deployment_file_exists ? (kbRuntime.deployment_persistence.deployment_file_writable ? 'var(--color-success-soft)' : 'var(--color-warning-soft)') : 'var(--app-surface-muted)', color: kbRuntime.deployment_persistence.deployment_file_exists ? (kbRuntime.deployment_persistence.deployment_file_writable ? 'var(--color-success-dark)' : 'var(--color-warning-dark)') : 'var(--app-text-subtle)' }}>
                  {kbRuntime.deployment_persistence.deployment_file_exists ? (kbRuntime.deployment_persistence.deployment_file_writable ? '可写' : '只读') : '不存在'}
                </span>
              </div>
              {kbRuntime.note && (
                <div className="py-2 px-3 rounded-lg text-xs" style={{ background: 'var(--color-warning-soft)', color: 'var(--color-warning-dark)', border: '1px solid #fde68a' }}>{kbRuntime.note}</div>
              )}
            </div>

            <div className="pt-4" style={{ borderTop: '1px solid var(--app-border)' }}>
              <div className="section-label mb-3">配置参数</div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Provider</label>
                  <select
                    value={kbForm.provider}
                    onChange={(e) => {
                      const nextProvider = e.target.value;
                      const preset = KB_PROVIDER_PRESETS[nextProvider] || KB_PROVIDER_PRESETS.generic_rest;
                      setKbForm((prev) => ({
                        ...prev,
                        provider: nextProvider,
                        health_path: preset.health_path,
                        search_path: preset.search_path,
                        upsert_path: preset.upsert_path,
                      }));
                    }}
                    className="input"
                  >
                    {(kbRuntime.supported_providers.length > 0
                      ? kbRuntime.supported_providers
                      : DEFAULT_KB_RUNTIME.supported_providers).map((provider) => (
                      <option key={provider} value={provider}>{provider}</option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Base URL</label>
                  <input
                    type="text"
                    value={kbForm.base_url}
                    onChange={(e) => setKbForm((prev) => ({ ...prev, base_url: e.target.value }))}
                    placeholder="例如: http://ragflow:9380"
                    className="input"
                    disabled={kbForm.provider === 'disabled'}
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>API Key（留空不更新）</label>
                  <input
                    type="password"
                    value={kbForm.api_key}
                    onChange={(e) => setKbForm((prev) => ({ ...prev, api_key: e.target.value }))}
                    placeholder="输入新的 API Key"
                    className="input"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>超时秒数</label>
                  <input
                    type="number"
                    min={1}
                    value={kbForm.timeout_seconds}
                    onChange={(e) => setKbForm((prev) => ({ ...prev, timeout_seconds: e.target.value }))}
                    className="input"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Health Path</label>
                  <input
                    type="text"
                    value={kbForm.health_path}
                    onChange={(e) => setKbForm((prev) => ({ ...prev, health_path: e.target.value }))}
                    className="input"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Search Path</label>
                  <input
                    type="text"
                    value={kbForm.search_path}
                    onChange={(e) => setKbForm((prev) => ({ ...prev, search_path: e.target.value }))}
                    className="input"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Upsert Path</label>
                  <input
                    type="text"
                    value={kbForm.upsert_path}
                    onChange={(e) => setKbForm((prev) => ({ ...prev, upsert_path: e.target.value }))}
                    className="input"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Outbox 轮询秒数</label>
                  <input
                    type="number"
                    min={1}
                    value={kbForm.outbox_poll_seconds}
                    onChange={(e) => setKbForm((prev) => ({ ...prev, outbox_poll_seconds: e.target.value }))}
                    className="input"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Outbox 最大重试</label>
                  <input
                    type="number"
                    min={1}
                    value={kbForm.outbox_max_attempts}
                    onChange={(e) => setKbForm((prev) => ({ ...prev, outbox_max_attempts: e.target.value }))}
                    className="input"
                  />
                </div>

                <div className="md:col-span-2 flex flex-wrap gap-4">
                  {[
                    { key: 'outbox_enabled' as const, label: '启用 Outbox 异步重试同步', checked: kbForm.outbox_enabled, onChange: (v: boolean) => setKbForm((p) => ({ ...p, outbox_enabled: v })) },
                    { key: 'clear_api_key' as const, label: '清空现有 KB API Key（与上方输入二选一）', checked: kbForm.clear_api_key, onChange: (v: boolean) => setKbForm((p) => ({ ...p, clear_api_key: v })) },
                    { key: 'persist_to_deployment' as const, label: '同步写入部署文件 (deploy/ai-service.yaml)', checked: kbForm.persist_to_deployment, onChange: (v: boolean) => setKbForm((p) => ({ ...p, persist_to_deployment: v })) },
                  ].map(item => (
                    <label key={item.key} className="inline-flex items-center gap-2 text-xs cursor-pointer" style={{ color: 'var(--app-text-muted)' }}>
                      <input
                        type="checkbox"
                        checked={item.checked}
                        onChange={(e) => item.onChange(e.target.checked)}
                        className="rounded"
                        style={{ accentColor: 'var(--brand-primary)' }}
                      />
                      {item.label}
                    </label>
                  ))}
                </div>

                <div className="md:col-span-2">
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Extra (JSON object)</label>
                  <textarea
                    value={kbForm.extra}
                    onChange={(e) => setKbForm((prev) => ({ ...prev, extra: e.target.value }))}
                    rows={4}
                    className="input font-mono text-xs"
                  />
                </div>
              </div>

              <div className="mt-4 flex items-center justify-between gap-3">
                <div className="text-xs" style={{ color: 'var(--app-text-subtle)' }}>
                  契约: provider({kbRuntime.runtime_config_contract.provider}),
                  base_url({kbRuntime.runtime_config_contract.base_url}),
                  api_key({kbRuntime.runtime_config_contract.api_key || 'string(optional)'})
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleValidateKBRuntime}
                    disabled={validatingKBRuntime || updatingKBRuntime}
                    className="btn btn-secondary"
                  >
                    {validatingKBRuntime ? <RefreshCw size={12} className="animate-spin" /> : <CheckCircle2 size={12} />}
                    校验参数
                  </button>
                  <button
                    onClick={handleUpdateKBRuntime}
                    disabled={updatingKBRuntime || validatingKBRuntime}
                    className="btn btn-primary"
                  >
                    <RefreshCw size={12} className={updatingKBRuntime ? 'animate-spin' : ''} />
                    更新 KB 运行时
                  </button>
                </div>
              </div>
              <div className="mt-3 px-3 py-2 rounded-lg text-xs" style={{ background: 'var(--color-warning-soft)', color: 'var(--color-warning-dark)', border: '1px solid #fde68a' }}>
                默认 provider 为 RAGFlow。若部署了 RAGFlow 兼容网关，可直接使用默认 path；
                若接口路径不同，请按网关契约改写 Health / Search / Upsert Path。
              </div>

              {kbValidateResult && (
                <div className="mt-4 p-3 rounded-xl text-xs" style={{ background: 'var(--color-success-soft)', border: '1px solid #a7f3d0' }}>
                  <div className="font-semibold mb-1" style={{ color: 'var(--color-success-dark)' }}>
                    校验结果: {kbValidateResult.validated ? '✓ 通过' : '✗ 未通过'}
                    <span className="ml-2 font-normal" style={{ color: 'var(--color-success-dark)', opacity: 0.7 }}>status: {kbValidateResult.status}</span>
                  </div>
                  {kbValidateResult.note && <div style={{ color: 'var(--color-success-dark)' }}>{kbValidateResult.note}</div>}
                  <pre className="mt-2 rounded-lg p-2 overflow-auto max-h-44 font-mono" style={{ background: 'var(--app-surface)', border: '1px solid var(--app-border)', color: 'var(--app-text)' }}>
                    {JSON.stringify(kbValidateResult.runtime, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="card overflow-hidden lg:col-span-2">
          <div className="card-header">
            <div className="card-title">
              <Cpu size={14} style={{ color: 'var(--brand-secondary)' }} />
              LLM 运行时
              <span className="badge badge-neutral ml-1">{llmRuntime.configured_provider || 'openai'}</span>
            </div>
            <button
              onClick={() => fetchRuntimeStatus(true)}
              disabled={refreshingLLMRuntime}
              className="btn btn-secondary"
            >
              <RefreshCw size={12} className={refreshingLLMRuntime ? 'animate-spin' : ''} />
              刷新状态
            </button>
          </div>

          <div className="card-body space-y-4">
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              {[
                { label: 'Provider', value: llmRuntime.configured_provider || '未配置', status: 'neutral' as const },
                { label: 'Model', value: llmRuntime.configured_model || '未配置', status: 'neutral' as const },
                { label: 'LLM 状态', value: llmRuntime.llm_enabled ? '已启用' : '未启用', status: (llmRuntime.llm_enabled ? 'ok' : 'warn') as 'ok'|'warn'|'neutral' },
                { label: 'API Key', value: llmRuntime.api_key_configured ? '已配置' : '未配置', status: (llmRuntime.api_key_configured ? 'ok' : 'warn') as 'ok'|'warn'|'neutral' },
                { label: '本地 LLM', value: llmRuntime.local_llm_ready ? '就绪' : '未就绪', status: (llmRuntime.local_llm_ready ? 'ok' : 'neutral') as 'ok'|'warn'|'neutral' },
              ].map(item => (
                <div key={item.label} className="p-3 rounded-xl" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                  <div className="section-label mb-1.5">{item.label}</div>
                  <div className="text-sm font-semibold truncate" style={{ color: item.status === 'ok' ? 'var(--color-success-dark)' : item.status === 'warn' ? 'var(--color-error-dark)' : 'var(--app-text)' }}>{item.value}</div>
                </div>
              ))}
            </div>

            <div className="space-y-2 text-xs">
              <div className="flex items-start gap-2 py-2 px-3 rounded-lg" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                <span className="font-semibold flex-shrink-0" style={{ color: 'var(--app-text-muted)' }}>本地 API Base</span>
                <code className="flex-1 truncate" style={{ color: 'var(--brand-primary)' }}>{llmRuntime.local_llm_api_base || '未配置'}</code>
              </div>
              <div className="flex items-start gap-2 py-2 px-3 rounded-lg" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                <span className="font-semibold flex-shrink-0" style={{ color: 'var(--app-text-muted)' }}>支持 Provider</span>
                <span style={{ color: 'var(--app-text)' }}>{llmRuntime.supported_providers.join(', ') || '-'}</span>
              </div>
              <div className="flex items-start gap-2 py-2 px-3 rounded-lg" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                <span className="font-semibold flex-shrink-0" style={{ color: 'var(--app-text-muted)' }}>部署文件</span>
                <code className="flex-1 truncate" style={{ color: 'var(--app-text)' }}>{llmRuntime.deployment_persistence.deployment_file || '未配置'}</code>
                <span className="text-xs px-1.5 py-0.5 rounded" style={{ background: llmRuntime.deployment_persistence.deployment_file_exists ? (llmRuntime.deployment_persistence.deployment_file_writable ? 'var(--color-success-soft)' : 'var(--color-warning-soft)') : 'var(--app-surface-muted)', color: llmRuntime.deployment_persistence.deployment_file_exists ? (llmRuntime.deployment_persistence.deployment_file_writable ? 'var(--color-success-dark)' : 'var(--color-warning-dark)') : 'var(--app-text-subtle)' }}>
                  {llmRuntime.deployment_persistence.deployment_file_exists ? (llmRuntime.deployment_persistence.deployment_file_writable ? '可写' : '只读') : '不存在'}
                </span>
              </div>
              {llmRuntime.note && (
                <div className="py-2 px-3 rounded-lg" style={{ background: 'var(--color-warning-soft)', color: 'var(--color-warning-dark)', border: '1px solid #fde68a' }}>{llmRuntime.note}</div>
              )}
            </div>

            <div className="pt-4" style={{ borderTop: '1px solid var(--app-border)' }}>
              <div className="section-label mb-3">配置参数</div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Provider</label>
                  <select
                    value={llmForm.provider}
                    onChange={(e) => {
                      const newProvider = e.target.value;
                      setLlmForm((prev) => ({ ...prev, provider: newProvider }));
                      loadModelList(newProvider);
                    }}
                    className="input"
                  >
                    {(llmRuntime.supported_providers.length > 0
                      ? llmRuntime.supported_providers
                      : DEFAULT_LLM_RUNTIME.supported_providers).map((provider) => (
                      <option key={provider} value={provider}>{provider}</option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Model</label>
                  <input
                    type="text"
                    value={llmForm.model}
                    onChange={(e) => setLlmForm((prev) => ({ ...prev, model: e.target.value }))}
                    placeholder="例如: gpt-4o-mini / claude-3-5-sonnet"
                    list="llm-model-suggestions"
                    className="input"
                  />
                  <datalist id="llm-model-suggestions">
                    {availableModels.map((m) => (
                      <option key={m} value={m} />
                    ))}
                  </datalist>
                  {loadingModels && <span className="text-xs text-gray-400 ml-2">加载中...</span>}
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>API Base</label>
                  <input
                    type="text"
                    value={llmForm.api_base}
                    onChange={(e) => setLlmForm((prev) => ({ ...prev, api_base: e.target.value }))}
                    placeholder="例如: http://127.0.0.1:11434/v1"
                    className="input"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>API Key（留空不更新）</label>
                  <input
                    type="password"
                    value={llmForm.api_key}
                    onChange={(e) => setLlmForm((prev) => ({ ...prev, api_key: e.target.value }))}
                    placeholder="输入新的 API Key"
                    className="input"
                  />
                </div>

                <div>
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Local Model Path</label>
                  <input
                    type="text"
                    value={llmForm.local_model_path}
                    onChange={(e) => setLlmForm((prev) => ({ ...prev, local_model_path: e.target.value }))}
                    placeholder="例如: /models/qwen2.5"
                    className="input"
                  />
                </div>

                <div className="md:col-span-2 flex flex-wrap gap-4">
                  {[
                    { key: 'clear_api_key' as const, label: '清空现有 API Key（与上方输入二选一）', checked: llmForm.clear_api_key, onChange: (v: boolean) => setLlmForm((p) => ({ ...p, clear_api_key: v })) },
                    { key: 'persist_to_deployment' as const, label: '同步写入部署文件 (deploy/semantic-engine.yaml)', checked: llmForm.persist_to_deployment, onChange: (v: boolean) => setLlmForm((p) => ({ ...p, persist_to_deployment: v })) },
                  ].map(item => (
                    <label key={item.key} className="inline-flex items-center gap-2 text-xs cursor-pointer" style={{ color: 'var(--app-text-muted)' }}>
                      <input
                        type="checkbox"
                        checked={item.checked}
                        onChange={(e) => item.onChange(e.target.checked)}
                        className="rounded"
                        style={{ accentColor: 'var(--brand-primary)' }}
                      />
                      {item.label}
                    </label>
                  ))}
                </div>

                <div className="md:col-span-2">
                  <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>Extra (JSON object)</label>
                  <textarea
                    value={llmForm.extra}
                    onChange={(e) => setLlmForm((prev) => ({ ...prev, extra: e.target.value }))}
                    rows={5}
                    className="input font-mono text-xs"
                  />
                </div>
              </div>

              <div className="mt-4 flex items-center justify-between gap-3">
                <div className="text-xs" style={{ color: 'var(--app-text-subtle)' }}>
                  契约: provider({llmRuntime.runtime_config_contract.provider}),
                  model({llmRuntime.runtime_config_contract.model}),
                  api_key({llmRuntime.runtime_config_contract.api_key || 'string(optional)'})
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleValidateLLMRuntime}
                    disabled={validatingLLMRuntime || updatingLLMRuntime}
                    className="btn btn-secondary"
                  >
                    {validatingLLMRuntime ? <RefreshCw size={12} className="animate-spin" /> : <CheckCircle2 size={12} />}
                    校验参数
                  </button>
                  <button
                    onClick={handleUpdateLLMRuntime}
                    disabled={updatingLLMRuntime || validatingLLMRuntime}
                    className="btn btn-primary"
                  >
                    <RefreshCw size={12} className={updatingLLMRuntime ? 'animate-spin' : ''} />
                    更新 API Key / 运行时
                  </button>
                </div>
              </div>
              <div className="mt-3 px-3 py-2 rounded-lg text-xs" style={{ background: 'var(--color-warning-soft)', color: 'var(--color-warning-dark)', border: '1px solid #fde68a' }}>
                默认会尝试将非敏感 LLM 配置（Provider / Model / API Base / Local Path）同步写入部署文件；
                若部署文件不可访问则自动回退为仅当前进程生效。API Key 仍建议通过 Secret 管理。
              </div>

              {llmValidateResult && (
                <div className="mt-4 p-3 rounded-xl text-xs" style={{ background: 'var(--color-success-soft)', border: '1px solid #a7f3d0' }}>
                  <div className="font-semibold mb-1" style={{ color: 'var(--color-success-dark)' }}>
                    校验结果: {llmValidateResult.validated ? '✓ 通过' : '✗ 未通过'}
                    <span className="ml-2 font-normal" style={{ color: 'var(--color-success-dark)', opacity: 0.7 }}>status: {llmValidateResult.status}</span>
                  </div>
                  {llmValidateResult.note && <div style={{ color: 'var(--color-success-dark)' }}>{llmValidateResult.note}</div>}
                  <pre className="mt-2 rounded-lg p-2 overflow-auto max-h-44 font-mono" style={{ background: 'var(--app-surface)', border: '1px solid var(--app-border)', color: 'var(--app-text)' }}>
                    {JSON.stringify(llmValidateResult.runtime, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="card overflow-hidden lg:col-span-2">
          <div className="card-header">
            <div className="card-title">
              <SettingsIcon size={14} style={{ color: 'var(--brand-accent)' }} />
              API 配置
            </div>
            <button
              onClick={handleCheckApiHealth}
              disabled={checkingApiHealth}
              className="btn btn-primary"
            >
              <RefreshCw size={12} className={checkingApiHealth ? 'animate-spin' : ''} />
              连通性检查
            </button>
          </div>
          <div className="card-body space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>API 基础 URL</label>
                <input
                  type="text"
                  value={import.meta.env.VITE_API_URL || '相对路径 (/api/v1/*)'}
                  disabled
                  className="input"
                  style={{ opacity: 0.6, cursor: 'not-allowed' }}
                />
              </div>
              <div>
                <label className="block text-xs font-medium mb-1" style={{ color: 'var(--app-text-muted)' }}>API 版本</label>
                <input
                  type="text"
                  value="v1"
                  disabled
                  className="input"
                  style={{ opacity: 0.6, cursor: 'not-allowed' }}
                />
              </div>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {[
                { label: '引擎状态', value: apiHealth?.status || '未检查', ok: apiHealth?.status === 'healthy' },
                { label: '服务名称', value: apiHealth?.service || '-', ok: null },
                { label: '版本号', value: apiHealth?.version || '-', ok: null },
                { label: '最后检查', value: formatCheckedAt(apiHealth?.checked_at || ''), ok: null },
              ].map(item => (
                <div key={item.label} className="p-3 rounded-xl" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                  <div className="section-label mb-1.5">{item.label}</div>
                  <div className="text-sm font-semibold truncate" style={{ color: item.ok === true ? 'var(--color-success-dark)' : item.ok === false ? 'var(--color-error-dark)' : 'var(--app-text)' }}>
                    {item.value}
                  </div>
                </div>
              ))}
            </div>

            <div className="px-3 py-2 rounded-lg text-xs" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)', color: 'var(--app-text-subtle)' }}>
              API 基础地址通过环境变量设置（VITE_API_URL / 代理规则）。生产环境请保持与 Nginx 路由一致。
            </div>
          </div>
        </div>
      </div>
      </div>
    </div>
  );
};

export default Settings;
