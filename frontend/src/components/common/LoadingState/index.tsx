/**
 * LoadingState — Logoscope Design System
 */
import React from 'react';

interface LoadingStateProps {
  message?: string;
  className?: string;
  compact?: boolean;
}

const LoadingState: React.FC<LoadingStateProps> = ({
  message = '加载中…',
  className = '',
  compact = false,
}) => {
  return (
    <div className={`flex flex-col items-center justify-center ${compact ? 'py-6 gap-2' : 'py-12 gap-4'} ${className}`}>
      {/* Spinner */}
      <div className="relative" style={{ width: compact ? 28 : 40, height: compact ? 28 : 40 }}>
        <svg
          className="animate-spin"
          viewBox="0 0 40 40"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          style={{ width: '100%', height: '100%' }}
        >
          {/* Track */}
          <circle cx="20" cy="20" r="16" stroke="var(--app-border)" strokeWidth="3" />
          {/* Spinner arc */}
          <circle
            cx="20" cy="20" r="16"
            stroke="var(--brand-primary)"
            strokeWidth="3"
            strokeLinecap="round"
            strokeDasharray="60 40"
            style={{ transformOrigin: 'center' }}
          />
        </svg>
      </div>
      <p className="text-xs font-medium" style={{ color: 'var(--app-text-subtle)' }}>
        {message}
      </p>
    </div>
  );
};

export default LoadingState;
