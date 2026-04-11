/**
 * 拓扑图计算工具
 * 用于 Focus/Depth、证据过滤、问题边排序等前端高频计算。
 */

export interface TopologyNodeLike {
  id: string;
}

export interface TopologyEdgeLike {
  id?: string;
  source: string;
  target: string;
  evidence_type?: string;
  metrics?: Record<string, unknown>;
  timeout_rate?: number;
  p95?: number;
  p99?: number;
  quality_score?: number;
  coverage?: number;
  problem_summary?: unknown;
}

export type EvidenceMode = 'all' | 'observed' | 'inferred';

interface GraphFilterResult<TNode, TEdge> {
  nodes: TNode[];
  edges: TEdge[];
}

const VALID_DEPTHS = new Set([1, 2, 3]);

export function resolveEdgeEvidence(edge: TopologyEdgeLike): 'observed' | 'inferred' {
  const evidenceType = String(edge?.metrics?.evidence_type || edge?.evidence_type || '').toLowerCase();
  const source = String(edge?.metrics?.source || '').toLowerCase();
  if (evidenceType === 'inferred' || source === 'inferred') {
    return 'inferred';
  }
  return 'observed';
}

export function filterByEvidenceMode<TNode extends TopologyNodeLike, TEdge extends TopologyEdgeLike>(
  nodes: TNode[],
  edges: TEdge[],
  evidenceMode: EvidenceMode
): GraphFilterResult<TNode, TEdge> {
  if (evidenceMode === 'all') {
    return { nodes, edges };
  }

  const filteredEdges = edges.filter((edge) => resolveEdgeEvidence(edge) === evidenceMode);
  const keptNodeIds = new Set<string>();
  filteredEdges.forEach((edge) => {
    keptNodeIds.add(edge.source);
    keptNodeIds.add(edge.target);
  });
  const filteredNodes = nodes.filter((node) => keptNodeIds.has(node.id));

  return {
    nodes: filteredNodes,
    edges: filteredEdges,
  };
}

export function filterWeakEvidenceEdges<TNode extends TopologyNodeLike, TEdge extends TopologyEdgeLike>(
  nodes: TNode[],
  edges: TEdge[],
  enabled: boolean
): GraphFilterResult<TNode, TEdge> {
  if (!enabled) {
    return { nodes, edges };
  }

  const filteredEdges = edges.filter((edge) => !isWeakEvidenceEdge(edge));
  const keptNodeIds = new Set<string>();
  filteredEdges.forEach((edge) => {
    keptNodeIds.add(edge.source);
    keptNodeIds.add(edge.target);
  });
  const filteredNodes = nodes.filter((node) => keptNodeIds.has(node.id));

  return {
    nodes: filteredNodes,
    edges: filteredEdges,
  };
}

export function filterGraphByFocusDepth<TNode extends TopologyNodeLike, TEdge extends TopologyEdgeLike>(
  nodes: TNode[],
  edges: TEdge[],
  focusNodeId: string | null,
  depth: number
): GraphFilterResult<TNode, TEdge> {
  if (!focusNodeId) {
    return { nodes, edges };
  }
  const normalizedDepth = VALID_DEPTHS.has(depth) ? depth : 1;
  const adjacency = buildAdjacency(edges);
  const visited = new Set<string>([focusNodeId]);
  const queue: Array<{ id: string; d: number }> = [{ id: focusNodeId, d: 0 }];

  while (queue.length > 0) {
    const current = queue.shift();
    if (!current || current.d >= normalizedDepth) {
      continue;
    }

    const neighbors = adjacency.get(current.id) || [];
    for (const next of neighbors) {
      if (!visited.has(next)) {
        visited.add(next);
        queue.push({ id: next, d: current.d + 1 });
      }
    }
  }

  const filteredNodes = nodes.filter((node) => visited.has(node.id));
  const filteredEdges = edges.filter((edge) => visited.has(edge.source) && visited.has(edge.target));

  return {
    nodes: filteredNodes,
    edges: filteredEdges,
  };
}

export function isolateNodeNeighborhood<TNode extends TopologyNodeLike, TEdge extends TopologyEdgeLike>(
  nodes: TNode[],
  edges: TEdge[],
  centerNodeId: string | null
): GraphFilterResult<TNode, TEdge> {
  if (!centerNodeId) {
    return { nodes, edges };
  }
  return filterGraphByFocusDepth(nodes, edges, centerNodeId, 1);
}

export function sortEdgesByIssueScore<TEdge extends TopologyEdgeLike>(edges: TEdge[]): TEdge[] {
  return [...edges].sort((a, b) => computeEdgeIssueScore(b) - computeEdgeIssueScore(a));
}

export function computeEdgeIssueScore(edge: TopologyEdgeLike): number {
  const problemSummary = toRecord(edge?.problem_summary);
  const metricsProblemSummary = toRecord(edge?.metrics?.problem_summary);
  const backendIssueScore = toNumber(
    problemSummary?.issue_score ?? metricsProblemSummary?.issue_score,
    Number.NaN
  );
  if (Number.isFinite(backendIssueScore)) {
    return round2(backendIssueScore);
  }

  const metrics = edge.metrics || {};
  const errorRate = toNumber(metrics.error_rate, 0);
  const timeoutRate = toNumber(metrics.timeout_rate ?? edge.timeout_rate, 0);
  const p95 = toNumber(metrics.p95 ?? edge.p95, 0);
  const p99 = toNumber(metrics.p99 ?? edge.p99, 0);
  const qualityScore = toNumber(metrics.quality_score ?? edge.quality_score, 100);
  const evidencePenalty = resolveEdgeEvidence(edge) === 'inferred' ? 3 : 0;

  const latencyScore = Math.min((p95 + p99) / 2500, 1) * 30;
  const qualityPenalty = Math.max(0, (70 - qualityScore) / 70) * 30;
  const timeoutScore = Math.min(timeoutRate * 100, 1) * 25;
  const errorScore = Math.min(errorRate * 100, 1) * 50;

  return round2(errorScore + timeoutScore + latencyScore + qualityPenalty + evidencePenalty);
}

function buildAdjacency(edges: TopologyEdgeLike[]): Map<string, string[]> {
  const adjacency = new Map<string, string[]>();
  for (const edge of edges) {
    if (!adjacency.has(edge.source)) {
      adjacency.set(edge.source, []);
    }
    if (!adjacency.has(edge.target)) {
      adjacency.set(edge.target, []);
    }
    adjacency.get(edge.source)?.push(edge.target);
    adjacency.get(edge.target)?.push(edge.source);
  }
  return adjacency;
}

function isWeakEvidenceEdge(edge: TopologyEdgeLike): boolean {
  if (resolveEdgeEvidence(edge) !== 'inferred') {
    return false;
  }

  const metrics = edge.metrics || {};
  const issueScore = computeEdgeIssueScore(edge);
  const confidence = toNumber(metrics.confidence, toNumber(edge?.coverage, 0));
  const callCount = toNumber(metrics.call_count, 0);
  const requestRate = toNumber(metrics.rps, 0);
  const coverage = toNumber(metrics.coverage ?? edge.coverage, 0);
  const qualityScore = toNumber(metrics.quality_score ?? edge.quality_score, 100);

  if (issueScore >= 18) {
    return false;
  }
  if (confidence >= 0.55) {
    return false;
  }
  if (callCount >= 5 || requestRate >= 1) {
    return false;
  }
  if (coverage >= 0.2) {
    return false;
  }
  if (qualityScore < 70) {
    return false;
  }
  return true;
}

function toNumber(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function toRecord(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === 'object') {
    return value as Record<string, unknown>;
  }
  return null;
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}
