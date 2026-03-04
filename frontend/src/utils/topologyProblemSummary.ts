export type RiskLevel = '高风险' | '中风险' | '低风险';

export interface ProblemSummary {
  hasIssue: boolean;
  riskLevel: RiskLevel;
  issueScore: number;
  reasons: string[];
  headline: string;
  suggestion: string;
}

function toNum(value: any, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function normalizeRiskLevel(value: any): RiskLevel {
  if (value === '高风险' || value === '中风险' || value === '低风险') {
    return value;
  }
  return '低风险';
}

function defaultSummary(headline = ''): ProblemSummary {
  return {
    hasIssue: false,
    riskLevel: '低风险',
    issueScore: 0,
    reasons: [],
    headline,
    suggestion: '',
  };
}

export function resolveEdgeProblemSummary(edge: any): ProblemSummary {
  const raw = edge?.problem_summary || edge?.metrics?.problem_summary;
  if (raw && typeof raw === 'object') {
    return {
      hasIssue: Boolean(raw.has_issue ?? raw.hasIssue),
      riskLevel: normalizeRiskLevel(raw.risk_level ?? raw.riskLevel),
      issueScore: toNum(raw.issue_score ?? raw.issueScore, 0),
      reasons: Array.isArray(raw.reasons) ? raw.reasons.map((item: any) => String(item)) : [],
      headline: String(raw.headline || ''),
      suggestion: String(raw.suggestion || ''),
    };
  }

  const metrics = edge?.metrics || {};
  const errorRate = toNum(metrics.error_rate, 0);
  const timeoutRate = toNum(metrics.timeout_rate ?? edge?.timeout_rate, 0);
  const p95 = toNum(metrics.p95 ?? edge?.p95, 0);
  const p99 = toNum(metrics.p99 ?? edge?.p99, 0);
  const qualityScore = toNum(metrics.quality_score ?? edge?.quality_score, 100);
  const evidence = String(metrics.evidence_type || edge?.evidence_type || 'observed').toLowerCase();

  const latencyScore = Math.min((p95 + p99) / 2500, 1) * 30;
  const qualityPenalty = Math.max(0, (70 - qualityScore) / 70) * 30;
  const timeoutScore = Math.min(timeoutRate * 100, 1) * 25;
  const errorScore = Math.min(errorRate * 100, 1) * 50;
  const evidencePenalty = evidence === 'inferred' ? 3 : 0;
  const issueScore = Math.round((errorScore + timeoutScore + latencyScore + qualityPenalty + evidencePenalty) * 100) / 100;

  const riskLevel: RiskLevel = issueScore >= 70 ? '高风险' : issueScore >= 35 ? '中风险' : '低风险';
  const hasIssue = riskLevel !== '低风险' || errorRate >= 0.03 || timeoutRate >= 0.02 || p99 >= 650 || qualityScore < 80;

  return {
    hasIssue,
    riskLevel,
    issueScore,
    reasons: [],
    headline: '',
    suggestion: '',
  };
}

export function resolveNodeProblemSummary(node: any): ProblemSummary {
  const raw = node?.problem_summary || node?.metrics?.problem_summary;
  if (raw && typeof raw === 'object') {
    return {
      hasIssue: Boolean(raw.has_issue ?? raw.hasIssue),
      riskLevel: normalizeRiskLevel(raw.risk_level ?? raw.riskLevel),
      issueScore: toNum(raw.issue_score ?? raw.issueScore, 0),
      reasons: Array.isArray(raw.reasons) ? raw.reasons.map((item: any) => String(item)) : [],
      headline: String(raw.headline || ''),
      suggestion: String(raw.suggestion || ''),
    };
  }

  const metrics = node?.metrics || {};
  const errorCount = toNum(metrics.error_count, 0);
  const errorRate = toNum(metrics.error_rate, 0);
  const timeoutRate = toNum(metrics.timeout_rate, 0);
  const qualityScore = toNum(metrics.quality_score ?? node?.quality_score, 100);

  const issueScore = Math.round(
    (
      Math.min(errorCount, 8) * 4 +
      Math.min(errorRate * 100, 1) * 40 +
      Math.min(timeoutRate * 100, 1) * 20 +
      Math.max(0, (85 - qualityScore) / 85) * 25
    ) * 100
  ) / 100;
  const riskLevel: RiskLevel = issueScore >= 70 ? '高风险' : issueScore >= 35 ? '中风险' : '低风险';
  const hasIssue = riskLevel !== '低风险' || errorCount > 0 || errorRate >= 0.03 || timeoutRate >= 0.02 || qualityScore < 80;

  return {
    hasIssue,
    riskLevel,
    issueScore,
    reasons: [],
    headline: '',
    suggestion: '',
  };
}

export function resolveIssueSummary(
  nodes: any[],
  edges: any[],
  metadata?: Record<string, any> | null
): {
  unhealthyNodes: number;
  unhealthyEdges: number;
  highRiskNodes: number;
  mediumRiskNodes: number;
  highRiskEdges: number;
  mediumRiskEdges: number;
} {
  const raw = metadata?.issue_summary;
  if (raw && typeof raw === 'object') {
    const unhealthyNodes = toNum(raw.unhealthy_nodes, 0);
    const unhealthyEdges = toNum(raw.unhealthy_edges, 0);
    const highRiskNodes = toNum(raw.high_risk_nodes, 0);
    const mediumRiskNodes = toNum(raw.medium_risk_nodes, 0);
    const highRiskEdges = toNum(raw.high_risk_edges, 0);
    const mediumRiskEdges = toNum(raw.medium_risk_edges, 0);
    return {
      unhealthyNodes,
      unhealthyEdges,
      highRiskNodes,
      mediumRiskNodes,
      highRiskEdges,
      mediumRiskEdges,
    };
  }

  let highRiskNodes = 0;
  let mediumRiskNodes = 0;
  for (const node of nodes || []) {
    const summary = resolveNodeProblemSummary(node);
    if (!summary.hasIssue) {
      continue;
    }
    if (summary.riskLevel === '高风险') {
      highRiskNodes += 1;
    } else if (summary.riskLevel === '中风险') {
      mediumRiskNodes += 1;
    }
  }

  let highRiskEdges = 0;
  let mediumRiskEdges = 0;
  for (const edge of edges || []) {
    const summary = resolveEdgeProblemSummary(edge);
    if (!summary.hasIssue) {
      continue;
    }
    if (summary.riskLevel === '高风险') {
      highRiskEdges += 1;
    } else if (summary.riskLevel === '中风险') {
      mediumRiskEdges += 1;
    }
  }

  return {
    unhealthyNodes: highRiskNodes + mediumRiskNodes,
    unhealthyEdges: highRiskEdges + mediumRiskEdges,
    highRiskNodes,
    mediumRiskNodes,
    highRiskEdges,
    mediumRiskEdges,
  };
}

export function resolveEdgeIssueScore(edge: any): number {
  return resolveEdgeProblemSummary(edge).issueScore;
}

export function buildProblemBadgeClass(riskLevel: RiskLevel): string {
  if (riskLevel === '高风险') {
    return 'bg-rose-500/20 text-rose-200';
  }
  if (riskLevel === '中风险') {
    return 'bg-amber-500/20 text-amber-200';
  }
  return 'bg-emerald-500/20 text-emerald-200';
}

export function safeSummary(summary: ProblemSummary | null | undefined, fallbackHeadline = ''): ProblemSummary {
  if (!summary) {
    return defaultSummary(fallbackHeadline);
  }
  return summary;
}
