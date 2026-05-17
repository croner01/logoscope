/**
 * EmptyState — Logoscope Design System
 */
import React from 'react';
import { Inbox } from 'lucide-react';

interface EmptyStateProps {
  icon?: React.ReactNode;
  title?: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
  compact?: boolean;
}

const EmptyState: React.FC<EmptyStateProps> = ({
  icon,
  title = '暂无数据',
  description,
  action,
  className = '',
  compact = false,
}) => {
  return (
    <div className={`flex flex-col items-center justify-center text-center ${compact ? 'py-6' : 'py-12'} ${className}`}>
      <div
        className={`flex items-center justify-center rounded-2xl mb-4 ${compact ? 'w-10 h-10' : 'w-14 h-14'}`}
        style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}
      >
        {icon
          ? <span style={{ color: 'var(--app-text-subtle)' }}>{icon}</span>
          : <Inbox size={compact ? 18 : 24} style={{ color: 'var(--app-text-subtle)' }} />
        }
      </div>
      <h3 className={`font-semibold ${compact ? 'text-sm' : 'text-base'}`} style={{ color: 'var(--app-text)' }}>
        {title}
      </h3>
      {description && (
        <p className="mt-1.5 text-xs max-w-xs" style={{ color: 'var(--app-text-subtle)' }}>
          {description}
        </p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
};

export default EmptyState;
