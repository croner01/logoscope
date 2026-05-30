/**
 * 本地相似案例展示组件
 *
 * 展示本地知识库内与当前日志相似的历史案例，包括：
 * - 相似度评分
 * - 匹配特征
 * - 解决方案
 */
import React from 'react';
import { CheckCircle, Tag, ChevronRight, BookOpen } from 'lucide-react';

export interface SimilarCase {
  id: string;
  problem_type: string;
  severity: string;
  summary: string;
  service_name: string;
  root_causes: string[];
  solutions: Array<{
    title: string;
    description: string;
    steps: string[];
  }>;
  resolved: boolean;
  resolution: string;
  tags: string[];
  similarity_score: number;
  matched_features: string[];
  relevance_reason: string;
  content_update_history_count?: number;
  content_update_history_recent?: Array<{
    version?: number;
    updated_at?: string;
    changed_fields?: string[];
  }>;
}

interface SimilarCasesProps {
  cases: SimilarCase[];
  loading?: boolean;
  onSelectCase?: (caseItem: SimilarCase) => void;
}

const SEVERITY_COLORS: Record<string, { bg: string; text: string; border: string }> = {
  critical: { bg: 'bg-red-100', text: 'text-red-700', border: 'border-red-200' },
  high: { bg: 'bg-orange-100', text: 'text-orange-700', border: 'border-orange-200' },
  medium: { bg: 'bg-yellow-100', text: 'text-yellow-700', border: 'border-yellow-200' },
  low: { bg: 'bg-green-100', text: 'text-green-700', border: 'border-green-200' },
};

const SimilarCases: React.FC<SimilarCasesProps> = ({ cases, loading, onSelectCase }) => {
  if (loading) {
    return (
      <div className="animate-pulse space-y-3">
        {[1, 2, 3].map((i) => (
          <div key={i} className="bg-gray-100 rounded-lg h-32"></div>
        ))}
      </div>
    );
  }

  if (!cases || cases.length === 0) {
    return (
      <div className="text-center py-8 text-gray-500">
        <BookOpen className="w-12 h-12 mx-auto mb-3 text-gray-300" />
        <p>未找到本地相似案例</p>
        <p className="text-sm mt-1">本地知识库中暂无相似的历史问题</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between mb-3">
        <h4 className="font-medium text-gray-900">本地相似案例推荐</h4>
        <span className="text-sm text-gray-500">{cases.length} 个相关条目</span>
      </div>

      {cases.map((caseItem) => {
        const severityColors = SEVERITY_COLORS[caseItem.severity] || SEVERITY_COLORS.medium;
        const similarityPercent = Math.round(caseItem.similarity_score * 100);

        return (
          <div
            key={caseItem.id}
            onClick={() => onSelectCase?.(caseItem)}
            className="bg-white border border-gray-200 rounded-lg p-4 cursor-pointer hover:border-blue-300 hover:shadow-md transition-all"
          >
            {/* 头部：相似度和状态 */}
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <div
                  className="px-2 py-1 rounded text-xs font-medium"
                  style={{
                    backgroundColor: `rgba(59, 130, 246, ${similarityPercent / 100 * 0.2})`,
                    color: `rgb(${59 + (100 - similarityPercent) * 1.5}, ${130 + similarityPercent * 0.5}, 246)`,
                  }}
                >
                  {similarityPercent}% 相似
                </div>
                <span className={`px-2 py-1 rounded text-xs font-medium ${severityColors.bg} ${severityColors.text}`}>
                  {caseItem.severity}
                </span>
                {caseItem.resolved && (
                  <span className="flex items-center gap-1 text-xs text-green-600">
                    <CheckCircle className="w-3 h-3" />
                    已解决
                  </span>
                )}
              </div>
              <ChevronRight className="w-4 h-4 text-gray-400" />
            </div>

            {/* 摘要 */}
            <h5 className="font-medium text-gray-900 mb-2">{caseItem.summary}</h5>

            {/* 服务和问题类型 */}
            <div className="flex items-center gap-3 text-sm text-gray-600 mb-3">
              <span>服务: {caseItem.service_name || '未知'}</span>
              <span>•</span>
              <span>类型: {caseItem.problem_type}</span>
            </div>

            {/* 相关性原因 */}
            <div className="text-xs text-blue-600 mb-3">
              匹配: {caseItem.relevance_reason}
            </div>

            {Number(caseItem.content_update_history_count || 0) > 0 && (
              <div className="text-xs text-slate-500 mb-3">
                更新历史: {caseItem.content_update_history_count} 次
              </div>
            )}

            {/* 标签 */}
            {caseItem.tags && caseItem.tags.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {caseItem.tags.slice(0, 4).map((tag, idx) => (
                  <span
                    key={idx}
                    className="inline-flex items-center gap-1 px-2 py-0.5 bg-gray-100 text-gray-600 text-xs rounded"
                  >
                    <Tag className="w-2.5 h-2.5" />
                    {tag}
                  </span>
                ))}
              </div>
            )}

            {/* 解决方案预览 */}
            {caseItem.resolved && caseItem.resolution && (
              <div className="mt-3 pt-3 border-t border-gray-100">
                <div className="flex items-start gap-2">
                  <CheckCircle className="w-4 h-4 text-green-500 mt-0.5 shrink-0" />
                  <div className="text-sm text-gray-700">
                    <span className="font-medium text-green-700">解决方案: </span>
                    {caseItem.resolution}
                  </div>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};

export default SimilarCases;
