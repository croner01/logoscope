/**
 * 标签发现页面
 * 参考 Datadog 设计风格
 */
import React, { useCallback, useState } from 'react';
import { useEvents } from '../hooks/useApi';
import LoadingState from '../components/common/LoadingState';
import EmptyState from '../components/common/EmptyState';
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
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">标签发现</h1>
          <p className="text-gray-500 mt-1">发现和分析数据中的标签模式</p>
        </div>
        <button
          onClick={() => { refetch(); extractLabels(); }}
          className="flex items-center px-3 py-2 text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
        >
          <RefreshCw className="w-4 h-4 mr-2" />
          刷新
        </button>
      </div>

      {/* 搜索栏 */}
      <div className="bg-white rounded-lg shadow-md p-4 mb-4">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-gray-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="搜索标签键或值..."
            className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        {error && <div className="mt-2 text-xs text-red-600">{error}</div>}
      </div>

      {/* 统计信息 */}
      <div className="grid grid-cols-3 gap-4 mb-4">
        <div className="bg-white rounded-lg shadow-md p-4">
          <div className="text-sm text-gray-500">发现标签数</div>
          <div className="text-2xl font-bold text-gray-900">{discoveredLabels.length}</div>
        </div>
        <div className="bg-white rounded-lg shadow-md p-4">
          <div className="text-sm text-gray-500">总出现次数</div>
          <div className="text-2xl font-bold text-gray-900">
            {discoveredLabels.reduce((sum, l) => sum + l.count, 0)}
          </div>
        </div>
        <div className="bg-white rounded-lg shadow-md p-4">
          <div className="text-sm text-gray-500">分析事件数</div>
          <div className="text-2xl font-bold text-gray-900">{eventsData?.total || 0}</div>
        </div>
      </div>

      {/* 标签列表 */}
      <div className="bg-white rounded-lg shadow-md overflow-hidden flex-1">
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
                    <td className="font-medium text-blue-600">{label.key}</td>
                    <td>
                      <div className="flex flex-wrap gap-1 max-w-md">
                        {label.values.slice(0, 5).map((value, index) => (
                          <span
                            key={index}
                            className="px-2 py-0.5 bg-gray-100 text-gray-700 text-xs rounded-full truncate max-w-32"
                            title={value}
                          >
                            {value}
                          </span>
                        ))}
                        {label.values.length > 5 && (
                          <span className="px-2 py-0.5 bg-gray-200 text-gray-500 text-xs rounded-full">
                            +{label.values.length - 5}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="text-gray-500">{label.count}</td>
                    <td>
                      <button
                        onClick={() => {
                          void copyToClipboard(label.key, label.key);
                        }}
                        className="p-1 text-gray-400 hover:text-gray-600 rounded"
                        title="复制标签键"
                      >
                        {copiedKey === label.key ? (
                          <Check className="w-4 h-4 text-green-500" />
                        ) : (
                          <Copy className="w-4 h-4" />
                        )}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState
              icon={<Tags className="w-12 h-12 text-gray-400" />}
              title="暂无标签数据"
              description="等待数据收集中..."
            />
          )}
        </div>
      </div>
    </div>
  );
};

export default LabelsDiscovery;
