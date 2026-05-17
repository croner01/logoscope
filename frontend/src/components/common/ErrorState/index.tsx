/**
 * ErrorState — Logoscope Design System
 */
import React from 'react';
import { AlertCircle, RefreshCw } from 'lucide-react';

interface ErrorStateProps {
  message?: string;
  onRetry?: () => void;
  className?: string;
  compact?: boolean;
}

const ErrorState: React.FC<ErrorStateProps> = ({
  message = '加载失败，请稍后重试',
  onRetry,
  className = '',
  compact = false,
}) => {
  return (
    <div className={`flex flex-col items-center justify-center text-center ${compact ? 'py-6 gap-3' : 'py-12 gap-4'} ${className}`}>
      <div
        className={`flex items-center justify-center rounded-2xl ${compact ? 'w-10 h-10' : 'w-14 h-14'}`}
        style={{ background: 'var(--color-error-soft)', border: '1px solid rgba(239,68,68,0.2)' }}
      >
        <AlertCircle
          size={compact ? 18 : 24}
          style={{ color: 'var(--color-error)' }}
        />
      </div>

      <div>
        <p className={`font-semibold ${compact ? 'text-sm' : 'text-base'}`} style={{ color: 'var(--app-text)' }}>
          数据加载失败
        </p>
        <p className="text-xs mt-1 max-w-xs" style={{ color: 'var(--app-text-subtle)' }}>
          {message}
        </p>
      </div>

      {onRetry && (
        <button
          onClick={onRetry}
          className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-semibold text-white transition-colors duration-150"
          style={{ background: 'var(--brand-primary)' }}
          onMouseEnter={e => ((e.currentTarget as HTMLElement).style.background = 'var(--brand-primary-dark)')}
          onMouseLeave={e => ((e.currentTarget as HTMLElement).style.background = 'var(--brand-primary)')}
        >
          <RefreshCw size={12} />
          重新加载
        </button>
      )}
    </div>
  );
};

export default ErrorState;
