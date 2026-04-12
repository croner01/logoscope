import React from 'react';

interface SkillBadgeProps {
  skillName: string;
  displayName?: string;
  riskLevel?: string;
  compact?: boolean;
}

const SKILL_COLORS: Record<string, string> = {
  k8s_pod_diagnostics: 'bg-blue-100 text-blue-800 border-blue-200',
  clickhouse_log_query: 'bg-orange-100 text-orange-800 border-orange-200',
  network_connectivity: 'bg-purple-100 text-purple-800 border-purple-200',
  resource_usage: 'bg-green-100 text-green-800 border-green-200',
};

const RISK_COLORS: Record<string, string> = {
  low: 'bg-green-100 text-green-700 border-green-200',
  medium: 'bg-yellow-100 text-yellow-700 border-yellow-200',
  high: 'bg-red-100 text-red-700 border-red-200',
};

const SKILL_ICONS: Record<string, string> = {
  k8s_pod_diagnostics: '☸',
  clickhouse_log_query: '🗄',
  network_connectivity: '🌐',
  resource_usage: '📊',
};

const SkillBadge: React.FC<SkillBadgeProps> = ({
  skillName,
  displayName,
  riskLevel,
  compact = false,
}) => {
  const colorClass = SKILL_COLORS[skillName] ?? 'bg-slate-100 text-slate-700 border-slate-200';
  const icon = SKILL_ICONS[skillName] ?? '⚙';
  const label = displayName || skillName;

  if (compact) {
    return (
      <span
        className={`inline-flex items-center gap-0.5 rounded border px-1 py-0.5 text-[9px] font-medium ${colorClass}`}
        title={label}
      >
        <span>{icon}</span>
        <span className="max-w-[80px] truncate">{label}</span>
      </span>
    );
  }

  const riskColor = riskLevel ? (RISK_COLORS[riskLevel.toLowerCase()] ?? RISK_COLORS.medium) : null;

  return (
    <span className="inline-flex items-center gap-1">
      <span
        className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium ${colorClass}`}
      >
        <span>{icon}</span>
        <span>{label}</span>
      </span>
      {riskColor && riskLevel && (
        <span
          className={`inline-flex items-center rounded border px-1 py-0.5 text-[9px] ${riskColor}`}
        >
          {riskLevel}
        </span>
      )}
    </span>
  );
};

export default SkillBadge;
