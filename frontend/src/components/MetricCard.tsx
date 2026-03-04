/**
 * 指标卡片组件 - 参考 Datadog 设计风格
 */
import React from 'react';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';
import { formatNumber } from '../utils/formatters';

interface MetricCardProps {
  title: string;
  value: number;
  unit?: string;
  trend?: number;
  trendType?: 'up' | 'down' | 'neutral';
  color?: string;
  icon?: React.ReactNode;
  className?: string;
}

export const MetricCard: React.FC<MetricCardProps> = ({
  title,
  value,
  unit,
  trend,
  trendType = 'neutral',
  color = '#4CAF50',
  icon,
  className,
}) => {
  const getTrendIcon = () => {
    switch (trendType) {
      case 'up':
        return <TrendingUp className="w-4 h-4" style={{ color: '#4CAF50' }} />;
      case 'down':
        return <TrendingDown className="w-4 h-4" style={{ color: '#F44336' }} />;
      default:
        return <Minus className="w-4 h-4" style={{ color: '#9E9E9E' }} />;
    }
  };

  const getTrendText = () => {
    if (trend === undefined) return null;
    return (
      <span className="text-xs font-medium" style={{ color: trendType === 'up' ? '#4CAF50' : trendType === 'down' ? '#F44336' : '#9E9E9E' }}>
        {trendType === 'up' ? '+' : ''}{trend}%
      </span>
    );
  };

  return (
    <div
      className={`bg-white rounded-lg shadow-md p-4 transition-all duration-200 hover:shadow-lg ${className}`}
      style={{ borderLeft: `4px solid ${color}` }}
    >
      <div className="flex justify-between items-start">
        <div className="flex items-start space-x-3">
          {icon && (
            <div
              className="p-2 rounded-lg"
              style={{ backgroundColor: `${color}20` }}
            >
              <div style={{ color }}>{icon}</div>
            </div>
          )}
          <div>
            <div className="text-xs font-medium text-gray-500 mb-1">{title}</div>
            <div className="text-2xl font-bold text-gray-900">
              {formatNumber(value)}
              {unit && <span className="text-sm ml-1 text-gray-500">{unit}</span>}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {getTrendIcon()}
          {getTrendText()}
        </div>
      </div>
    </div>
  );
};

export default MetricCard;
