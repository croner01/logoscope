/**
 * 过滤器面板组件
 * 参考 Datadog 设计风格
 */
import React, { useState } from 'react';
import { Filter, X, Plus } from 'lucide-react';

export interface FilterItem {
  key: string;
  value: string;
  operator: 'eq' | 'ne' | 'contains' | 'gt' | 'lt';
}

interface FilterPanelProps {
  filters: FilterItem[];
  onChange: (filters: FilterItem[]) => void;
  availableKeys?: string[];
  className?: string;
}

const OPERATOR_LABELS: Record<string, string> = {
  eq: '=',
  ne: '≠',
  contains: '包含',
  gt: '>',
  lt: '<',
};

const FilterPanel: React.FC<FilterPanelProps> = ({
  filters,
  onChange,
  availableKeys = ['service_name', 'level', 'namespace', 'pod_name', 'trace_id'],
  className = '',
}) => {
  const [isAdding, setIsAdding] = useState(false);
  const [newFilter, setNewFilter] = useState<Partial<FilterItem>>({});

  const handleAddFilter = () => {
    if (newFilter.key && newFilter.value && newFilter.operator) {
      onChange([...filters, newFilter as FilterItem]);
      setNewFilter({});
      setIsAdding(false);
    }
  };

  const handleRemoveFilter = (index: number) => {
    const newFilters = filters.filter((_, i) => i !== index);
    onChange(newFilters);
  };

  return (
    <div className={`bg-white border border-gray-200 rounded-lg ${className}`}>
      {/* 过滤器列表 */}
      <div className="p-3 flex flex-wrap gap-2">
        {filters.map((filter, index) => (
          <div
            key={index}
            className="flex items-center bg-gray-100 rounded-full px-3 py-1 text-sm"
          >
            <span className="font-medium text-gray-700">{filter.key}</span>
            <span className="mx-1 text-gray-500">{OPERATOR_LABELS[filter.operator]}</span>
            <span className="text-gray-900">{filter.value}</span>
            <button
              onClick={() => handleRemoveFilter(index)}
              className="ml-2 text-gray-400 hover:text-gray-600"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        ))}

        {/* 添加过滤器按钮 */}
        {!isAdding ? (
          <button
            onClick={() => setIsAdding(true)}
            className="flex items-center text-sm text-blue-600 hover:text-blue-700 px-2 py-1"
          >
            <Plus className="w-4 h-4 mr-1" />
            添加过滤器
          </button>
        ) : (
          <div className="flex items-center space-x-2 bg-gray-50 rounded-lg p-2">
            {/* 选择键 */}
            <select
              value={newFilter.key || ''}
              onChange={(e) => setNewFilter({ ...newFilter, key: e.target.value })}
              className="text-sm border border-gray-300 rounded px-2 py-1"
            >
              <option value="">选择字段</option>
              {availableKeys.map((key) => (
                <option key={key} value={key}>
                  {key}
                </option>
              ))}
            </select>

            {/* 选择操作符 */}
            <select
              value={newFilter.operator || 'eq'}
              onChange={(e) => setNewFilter({ ...newFilter, operator: e.target.value as FilterItem['operator'] })}
              className="text-sm border border-gray-300 rounded px-2 py-1"
            >
              {Object.entries(OPERATOR_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>

            {/* 输入值 */}
            <input
              type="text"
              value={newFilter.value || ''}
              onChange={(e) => setNewFilter({ ...newFilter, value: e.target.value })}
              placeholder="值"
              className="text-sm border border-gray-300 rounded px-2 py-1 w-32"
            />

            {/* 确认/取消按钮 */}
            <button
              onClick={handleAddFilter}
              className="text-sm bg-blue-600 text-white px-3 py-1 rounded hover:bg-blue-700"
            >
              确认
            </button>
            <button
              onClick={() => {
                setIsAdding(false);
                setNewFilter({});
              }}
              className="text-sm text-gray-500 hover:text-gray-700"
            >
              取消
            </button>
          </div>
        )}
      </div>

      {/* 如果没有过滤器，显示提示 */}
      {filters.length === 0 && !isAdding && (
        <div className="px-3 pb-3">
          <p className="text-sm text-gray-500 flex items-center">
            <Filter className="w-4 h-4 mr-1" />
            点击上方按钮添加过滤条件
          </p>
        </div>
      )}
    </div>
  );
};

export default FilterPanel;
