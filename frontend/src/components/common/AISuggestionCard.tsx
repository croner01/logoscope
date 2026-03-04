/**
 * AI 建议卡片组件
 * 显示 AI 分析的结果和建议
 */

import React from 'react';
import { Clock, Sparkles } from 'lucide-react';

interface AISuggestionCardProps {
  loading?: boolean;
  error?: string;
  analysisLabel?: string;
  suggestion?: {
    overview?: {
      problem: string;
      severity: string;
      description: string;
      confidence: number;
    };
    rootCauses?: Array<{
      title: string;
      description: string;
    }>;
    solutions?: Array<{
      title: string;
      description: string;
      steps: string[];
    }>;
    similarCases?: Array<{
      title: string;
      description: string;
    }>;
    analysis_method?: string;
    model?: string;
    cached?: boolean;
    latency_ms?: number;
  };
  onAnalyze: () => void;
}

/**
 * AI 建议卡片组件
 */
export const AISuggestionCard: React.FC<AISuggestionCardProps> = ({
  loading = false,
  error = null,
  analysisLabel = '日志',
  suggestion = null,
  onAnalyze,
}) => {
  const handleAnalyze = () => {
    onAnalyze();
  };

  return (
    <div className="p-4 space-y-4">
      {/* 卡片头部 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Sparkles className="text-purple-500" />
          <span className="font-medium text-gray-700">AI 智能分析</span>
        </div>
        {(suggestion || error) && (
          <button
            onClick={handleAnalyze}
            disabled={loading}
            className="px-3 py-1.5 text-xs font-medium bg-purple-100 hover:bg-purple-200 text-purple-700 rounded-lg transition-colors disabled:opacity-50"
          >
            {loading ? '分析中...' : '重新分析'}
          </button>
        )}
      </div>

      {/* 加载状态 */}
      {loading ? (
        <div className="p-8 text-center">
          <div className="animate-pulse flex flex-col items-center gap-3">
            <Clock className="text-purple-600" size={24} />
            <p className="text-sm text-gray-600">AI 正在分析{analysisLabel}...</p>
          </div>
        </div>
      ) : error ? (
        <div className="p-6 text-center bg-red-50 border border-red-200 rounded-lg">
          <p className="text-sm text-red-600 mb-2">{error}</p>
          <button
            onClick={handleAnalyze}
            className="text-xs text-red-700 hover:text-red-800 font-medium"
          >
            点击重试
          </button>
        </div>
      ) : !suggestion ? (
        /* 无数据时的提示 */
        <div className="text-center text-gray-500 py-8">
          <Sparkles className="text-gray-400 mx-auto mb-3" size={32} />
          <p className="text-sm mb-4">点击上方按钮开始 AI 分析</p>
          <button
            onClick={handleAnalyze}
            className="px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white text-sm font-medium rounded-lg transition-colors"
          >
            开始分析
          </button>
        </div>
      ) : (
        /* 建议内容 */
        <div className="space-y-4">
          {/* 问题概述 */}
          {suggestion.overview && (
            <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
              <div className="px-4 py-3 bg-gray-50 border-b border-gray-200">
                <div className="flex items-center justify-between gap-2">
                  <div className="font-semibold text-gray-700">问题分析</div>
                  {(suggestion.analysis_method || suggestion.model || suggestion.latency_ms) && (
                    <div className="text-[11px] text-gray-500 flex items-center gap-1.5">
                      {suggestion.analysis_method && (
                        <span className="px-1.5 py-0.5 rounded bg-purple-50 text-purple-600">
                          {suggestion.analysis_method}
                        </span>
                      )}
                      {suggestion.model && <span>{suggestion.model}</span>}
                      {typeof suggestion.latency_ms === 'number' && <span>{suggestion.latency_ms}ms</span>}
                      {suggestion.cached && <span className="text-green-600">cache</span>}
                    </div>
                  )}
                </div>
              </div>
              <div className="p-4">
                <p className="text-sm text-gray-700 mb-3">{suggestion.overview.description || suggestion.overview.problem}</p>
                <div className="flex items-center gap-2 text-xs text-gray-500">
                  <span>置信度: {Math.round((suggestion.overview.confidence || 0) * 100)}%</span>
                  <span>•</span>
                  <span>级别: {suggestion.overview.severity}</span>
                </div>
              </div>
            </div>
          )}

          {/* 根因分析 */}
          {suggestion.rootCauses && suggestion.rootCauses.length > 0 && (
            <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
              <div className="px-4 py-3 bg-red-50 border-b border-red-200">
                <div className="font-semibold text-red-700">根因分析</div>
              </div>
              <div className="p-4">
                <ul className="space-y-2">
                  {suggestion.rootCauses.map((cause, idx) => (
                    <li key={idx} className="text-sm text-gray-700">
                      <div className="font-medium mb-1">{cause.title}</div>
                      <div className="text-gray-600">{cause.description}</div>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          {/* 解决方案 */}
          {suggestion.solutions && suggestion.solutions.length > 0 && (
            <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
              <div className="px-4 py-3 bg-blue-50 border-b border-blue-200">
                <div className="font-semibold text-blue-700">解决建议</div>
              </div>
              <div className="p-4">
                <ul className="space-y-3">
                  {suggestion.solutions.map((solution, idx) => (
                    <li key={idx} className="text-sm">
                      <div className="font-medium text-gray-800 mb-1">{solution.title}</div>
                      <div className="text-gray-600 mb-2">{solution.description}</div>
                      {solution.steps && solution.steps.length > 0 && (
                        <ol className="ml-4 space-y-1">
                          {solution.steps.map((step, stepIdx) => (
                            <li key={stepIdx} className="text-gray-700 text-xs">{step}</li>
                          ))}
                        </ol>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          {/* 相似知识条目 */}
          {suggestion.similarCases && suggestion.similarCases.length > 0 && (
            <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
              <div className="px-4 py-3 bg-purple-50 border-b border-purple-200">
                <div className="font-semibold text-purple-700">相似知识条目</div>
              </div>
              <div className="p-4">
                <ul className="space-y-2">
                  {suggestion.similarCases.map((caseItem, idx) => (
                    <li key={idx} className="text-sm text-gray-700">
                      <div className="font-medium mb-1">{caseItem.title}</div>
                      <div className="text-gray-600">{caseItem.description}</div>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
