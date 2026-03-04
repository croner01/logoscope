/**
 * 拓扑图计算工具
 * 用于 Focus/Depth、证据过滤、问题边排序等前端高频计算。
 */

export interface TopologyNodeLike {
  id: string;
  [key: string]: any;
}

export interface TopologyEdgeLike {
  id?: string;
  source: string;
  target: string;
  evidence_type?: string;
  metrics?: Record<string, any>;
  [key: string]: any;
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
  const backendIssueScore = toNumber(
    edge?.problem_summary?.issue_score ?? edge?.metrics?.problem_summary?.issue_score,
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

function toNumber(value: any, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}
