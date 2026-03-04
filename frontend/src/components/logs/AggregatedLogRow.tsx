/**
 * 聚合日志行组件
 * 
 * 显示日志 Pattern 模板和聚合信息
 * 可展开查看示例日志
 */
import React, { useState } from 'react';
import { ChevronDown, ChevronRight, Layers, Clock, Server, Copy, Check } from 'lucide-react';
import { formatTime } from '../../utils/formatters';
import { copyTextToClipboard } from '../../utils/clipboard';
import type { LogPattern, Event } from '../../utils/api';

const LEVEL_COLORS: Record<string, { bg: string; text: string; dot: string; solid: string }> = {
  TRACE: { bg: 'bg-gray-100', text: 'text-gray-600', dot: 'bg-gray-400', solid: '#9ca3af' },
  DEBUG: { bg: 'bg-indigo-100', text: 'text-indigo-700', dot: 'bg-indigo-500', solid: '#6366f1' },
  INFO: { bg: 'bg-blue-100', text: 'text-blue-700', dot: 'bg-blue-500', solid: '#3b82f6' },
  WARN: { bg: 'bg-amber-100', text: 'text-amber-700', dot: 'bg-amber-500', solid: '#f59e0b' },
  ERROR: { bg: 'bg-red-100', text: 'text-red-700', dot: 'bg-red-500', solid: '#ef4444' },
  FATAL: { bg: 'bg-red-200', text: 'text-red-800', dot: 'bg-red-600', solid: '#dc2626' },
};

interface AggregatedLogRowProps {
  pattern: LogPattern;
  onSelectLog?: (event: Event) => void;
  defaultExpanded?: boolean;
}

const AggregatedLogRow: React.FC<AggregatedLogRowProps> = ({
  pattern,
  onSelectLog,
  defaultExpanded = false,
}) => {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);
  const [copied, setCopied] = useState(false);
  
  const levelColors = LEVEL_COLORS[pattern.level] || LEVEL_COLORS.INFO;
  
  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    const copiedSuccessfully = await copyTextToClipboard(pattern.pattern);
    if (copiedSuccessfully) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };
  
  const highlightVariables = (text: string) => {
    const parts = text.split(/(\{[^}]+\})/g);
    return parts.map((part, index) => {
      if (part.match(/^\{[^}]+\}$/)) {
        return (
          <span
            key={index}
            className="bg-purple-100 text-purple-700 px-1 py-0.5 rounded text-xs font-mono"
          >
            {part}
          </span>
        );
      }
      return part;
    });
  };

  return (
    <div className="border-b border-gray-100 last:border-0">
      <div
        className="flex items-center gap-3 px-4 py-3 hover:bg-gray-50 cursor-pointer transition-colors"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <button className="text-gray-400 hover:text-gray-600 transition-colors">
          {isExpanded ? (
            <ChevronDown className="w-4 h-4" />
          ) : (
            <ChevronRight className="w-4 h-4" />
          )}
        </button>
        
        <div className="flex items-center gap-2 shrink-0">
          <span
            className="w-2 h-2 rounded-full"
            style={{ backgroundColor: levelColors.solid }}
          />
          <span className={`text-xs font-medium ${levelColors.text}`}>
            {pattern.level}
          </span>
        </div>
        
        <div className="flex items-center gap-2 shrink-0">
          <Layers className="w-4 h-4 text-purple-500" />
          <span className="bg-purple-100 text-purple-700 text-xs font-semibold px-2 py-0.5 rounded-full">
            {pattern.count.toLocaleString()}
          </span>
        </div>
        
        <div className="flex-1 min-w-0">
          <pre className="text-sm text-gray-800 font-mono truncate">
            {highlightVariables(pattern.pattern)}
          </pre>
        </div>
        
        <div className="flex items-center gap-2 shrink-0 text-xs text-gray-400">
          <Clock className="w-3 h-3" />
          <span>{formatTime(pattern.first_seen)}</span>
          <span>~</span>
          <span>{formatTime(pattern.last_seen)}</span>
        </div>
        
        <button
          onClick={handleCopy}
          className="p-1 text-gray-400 hover:text-gray-600 transition-colors"
          title="复制 Pattern"
        >
          {copied ? (
            <Check className="w-4 h-4 text-green-500" />
          ) : (
            <Copy className="w-4 h-4" />
          )}
        </button>
      </div>
      
      {isExpanded && (
        <div className="bg-gray-50 border-t border-gray-100">
          {pattern.variables.length > 0 && (
            <div className="px-4 py-2 border-b border-gray-100 flex items-center gap-3 flex-wrap">
              <span className="text-xs text-gray-500">变量:</span>
              {pattern.variables.map((variable) => (
                <div key={variable} className="flex items-center gap-1">
                  <span className="bg-purple-100 text-purple-700 text-xs px-1.5 py-0.5 rounded font-mono">
                    {`{${variable}}`}
                  </span>
                  {pattern.variable_examples[variable] && (
                    <span className="text-xs text-gray-400">
                      {pattern.variable_examples[variable].slice(0, 3).join(', ')}
                      {pattern.variable_examples[variable].length > 3 && '...'}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
          
          {pattern.service_names.length > 0 && (
            <div className="px-4 py-2 border-b border-gray-100 flex items-center gap-3 flex-wrap">
              <span className="text-xs text-gray-500 flex items-center gap-1">
                <Server className="w-3 h-3" />
                服务:
              </span>
              {pattern.service_names.map((service) => (
                <span
                  key={service}
                  className="bg-blue-100 text-blue-700 text-xs px-1.5 py-0.5 rounded"
                >
                  {service}
                </span>
              ))}
            </div>
          )}
          
          <div className="px-4 py-2">
            <div className="text-xs text-gray-500 mb-2">
              示例日志 ({pattern.samples.length} 条):
            </div>
            <div className="space-y-1">
              {pattern.samples.map((sample, index) => (
                <div
                  key={sample.id || index}
                  className="bg-white border border-gray-200 rounded-lg p-2 hover:border-blue-300 cursor-pointer transition-colors"
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelectLog?.(sample);
                  }}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xs text-gray-400 font-mono">
                      {formatTime(sample.timestamp)}
                    </span>
                    <span
                      className="w-1.5 h-1.5 rounded-full"
                      style={{ backgroundColor: levelColors.solid }}
                    />
                    <span className="text-xs font-medium text-gray-600">
                      {sample.level}
                    </span>
                    <span className="text-xs text-gray-400">
                      [{sample.service_name}]
                    </span>
                    {sample.pod_name && (
                      <span className="text-xs text-gray-400 font-mono">
                        {sample.pod_name}
                      </span>
                    )}
                  </div>
                  <div className="text-sm text-gray-700 font-mono whitespace-pre-wrap break-words leading-5">
                    {sample.message}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default AggregatedLogRow;
