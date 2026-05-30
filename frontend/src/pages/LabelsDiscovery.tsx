/**
 * 标签发现页面
 * 参考 Datadog 设计风格
 */
import React, { useCallback, useState } from 'react';
import { useEvents } from '../hooks/useApi';
import LoadingState from '../components/common/LoadingState';
import { copyTextToClipboard } from '../utils/clipboard';
import { Tags, Search, RefreshCw, Copy, Check } from 'lucide-react';

interface DiscoveredLabel {
  key: string;
  values: string[];
  count: number;
}

type EventLike = Record<string, unknown> & {
  attributes?: Record<string, unknown>;
};

const LabelsDiscovery: React.FC = () => {
  const [searchQuery, setSearchQuery] = useState('');
  const [discoveredLabels, setDiscoveredLabels] = useState<DiscoveredLabel[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const { data: eventsData, refetch } = useEvents({ limit: 500 });

  // 从事件中提取标签
  const extractLabels = useCallback(() => {
    if (!eventsData?.events) return;

    setLoading(true);
    try {
      const labelMap: Record<string, { values: Set<string>; count: number }> = {};

      eventsData.events.forEach((event) => {
        const eventRecord = event as unknown as EventLike;

        // 基础字段
        ['service_name', 'namespace', 'pod_name', 'level'].forEach((key) => {
          const value = eventRecord[key];
          if (value) {
            if (!labelMap[key]) {
              labelMap[key] = { values: new Set(), count: 0 };
            }
            labelMap[key].values.add(String(value));
            labelMap[key].count++;
          }
        });

        // 属性字段
        const attributes = eventRecord.attributes;
        if (attributes && typeof attributes === 'object') {
          Object.entries(attributes).forEach(([key, value]) => {
            if (typeof value === 'string' || typeof value === 'number') {
              if (!labelMap[key]) {
                labelMap[key] = { values: new Set(), count: 0 };
              }
              labelMap[key].values.add(String(value));
              labelMap[key].count++;
            }
          });
        }
      });

      const labels: DiscoveredLabel[] = Object.entries(labelMap)
        .map(([key, data]) => ({
          key,
          values: Array.from(data.values).slice(0, 10),
          count: data.count,
        }))
        .sort((a, b) => b.count - a.count);

      setDiscoveredLabels(labels);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }, [eventsData?.events]);

  React.useEffect(() => {
    extractLabels();
  }, [extractLabels]);

  const copyToClipboard = async (text: string, key: string) => {
    const copied = await copyTextToClipboard(text);
    if (!copied) {
      setError('复制失败，请检查浏览器剪贴板权限后重试。');
      return;
    }
    setError(null);
    setCopiedKey(key);
    setTimeout(() => setCopiedKey(null), 2000);
  };

  // 过滤标签
  const filteredLabels = discoveredLabels.filter(
    (label) =>
      label.key.toLowerCase().includes(searchQuery.toLowerCase()) ||
      label.values.some((v) => v.toLowerCase().includes(searchQuery.toLowerCase()))
  );

  if (eventsData === null && !loading) {
    return <LoadingState message="加载数据..." />;
  }

  return (
    <div className="flex flex-col h-full">
      {/* 页面标题 */}
      <div className="page-header mb-4">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl flex items-center justify-center" style={{ background: 'var(--brand-primary-soft)', color: 'var(--brand-primary)' }}>
            <Tags size={18} />
          </div>
          <div>
            <h1 className="page-title">标签发现</h1>
            <p className="text-xs mt-0.5" style={{ color: 'var(--app-text-subtle)' }}>发现和分析数据中的标签模式</p>
          </div>
        </div>
        <button
          onClick={() => { refetch(); extractLabels(); }}
          className="btn btn-secondary"
        >
          <RefreshCw size={13} />
          刷新
        </button>
      </div>

      {/* 搜索栏 */}
      <div className="card p-4 mb-4">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--app-text-subtle)' }} />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="搜索标签键或值..."
            className="input pl-9"
          />
        </div>
        {error && (
          <div className="mt-2 text-xs px-3 py-1.5 rounded-lg" style={{ background: 'var(--color-error-soft)', color: 'var(--color-error-dark)' }}>
            {error}
          </div>
        )}
      </div>

      {/* 统计信息 */}
      <div className="grid grid-cols-3 gap-4 mb-4">
        {[
          { label: '发现标签数', value: discoveredLabels.length, tone: 'blue' },
          { label: '总出现次数', value: discoveredLabels.reduce((sum, l) => sum + l.count, 0), tone: 'teal' },
          { label: '分析事件数', value: eventsData?.total || 0, tone: 'purple' },
        ].map(({ label, value, tone }) => (
          <div key={label} className={`kpi-card tone-${tone}`}>
            <div className="kpi-label">{label}</div>
            <div className="kpi-value">{value.toLocaleString()}</div>
          </div>
        ))}
      </div>

      {/* 标签列表 */}
      <div className="card overflow-hidden flex-1">
        <div className="overflow-auto h-full">
          {loading ? (
            <LoadingState message="分析标签..." />
          ) : filteredLabels.length > 0 ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>标签键</th>
                  <th>值示例</th>
                  <th>出现次数</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {filteredLabels.map((label) => (
                  <tr key={label.key} className="hover:bg-gray-50">
                    <td>
                      <span className="font-mono font-semibold text-xs" style={{ color: 'var(--brand-primary)' }}>
                        {label.key}
                      </span>
                    </td>
                    <td>
                      <div className="flex flex-wrap gap-1 max-w-md">
                        {label.values.slice(0, 5).map((value, index) => (
                          <span
                            key={index}
                            className="px-2 py-0.5 rounded-full text-xs truncate max-w-32"
                            style={{ background: 'var(--app-surface-muted)', color: 'var(--app-text-muted)', border: '1px solid var(--app-border)' }}
                            title={value}
                          >
                            {value}
                          </span>
                        ))}
                        {label.values.length > 5 && (
                          <span className="px-2 py-0.5 rounded-full text-xs" style={{ background: 'var(--app-border)', color: 'var(--app-text-subtle)' }}>
                            +{label.values.length - 5}
                          </span>
                        )}
                      </div>
                    </td>
                    <td>
                      <span className="font-semibold text-sm" style={{ color: 'var(--app-text)' }}>{label.count}</span>
                    </td>
                    <td>
                      <button
                        onClick={() => { void copyToClipboard(label.key, label.key); }}
                        className="btn btn-ghost btn-icon"
                        title="复制标签键"
                      >
                        {copiedKey === label.key ? (
                          <Check size={14} style={{ color: 'var(--color-success-dark)' }} />
                        ) : (
                          <Copy size={14} />
                        )}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty-state">
              <div className="empty-state-icon">
                <Tags size={28} style={{ color: 'var(--app-text-subtle)' }} />
              </div>
              <div className="empty-state-title">暂无标签数据</div>
              <div className="empty-state-desc">等待数据收集中…</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default LabelsDiscovery;
