// ── 轻量类型（来自拓扑图，避免依赖 TopologyPage 的内部类型）────
// TopologyPage 中 TopologyNodeEntity/TopologyEdgeEntity 是 TopologyEntity 的扩展，
// 本模块只使用 id/service_name/source/target/edge_key 字段。

export interface TopoNode {
  id: string;
  service_name?: string;
}

export interface TopoEdge {
  id?: string;
  source: string;
  target: string;
  edge_key?: string;
}

// ── 类型定义 ──────────────────────────────────────────────

export interface StepInfo {
  index: number;
  serviceName: string;
  nodeId: string | null;
  action: string;
  startedAt: string;
  durationMs: number;
  status: string;
  level: string;
}

export interface TempEdge {
  source: string;
  target: string;
  stepIndex: number;
}

export interface WorkflowHighlightResult {
  nodeIds: string[];
  edgeIds: string[];
  stepSequence: StepInfo[];
  tempEdges: TempEdge[];
}

export interface WorkflowDetail {
  execution_id: string;
  operation_type: string;
  resource_id: string;
  global_request_id: string;
  status: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  error_message: string;
  source_cluster: string;
  step_count: number;
  'steps.service_name': string[];
  'steps.action': string[];
  'steps.started_at': string[];
  'steps.duration_ms': number[];
  'steps.status': string[];
  'steps.level': string[];
}

// ── 辅助函数 ──────────────────────────────────────────────

/** 模糊匹配服务名到拓扑节点（精确→前缀→包含→反向包含） */
export function findBestMatch(
  serviceName: string,
  nodes: TopoNode[],
): TopoNode | null {
  const name = serviceName.toLowerCase().trim();
  if (!name) return null;

  // 精确匹配
  let match = nodes.find(n => n.service_name?.toLowerCase() === name);
  if (match) return match;

  // 前缀匹配
  match = nodes.find(n => n.service_name?.toLowerCase().startsWith(name));
  if (match) return match;

  // 包含匹配
  match = nodes.find(n => n.service_name?.toLowerCase().includes(name));
  if (match) return match;

  // 反向包含匹配（拓扑名较短但包含在步骤服务名中）
  match = nodes.find(n => n.service_name && name.includes(n.service_name.toLowerCase()));
  return match ?? null;
}

// ── 主函数 ────────────────────────────────────────────────

export function mapWorkflowToTopology(
  detail: WorkflowDetail,
  nodes: TopoNode[],
  edges: TopoEdge[],
): WorkflowHighlightResult {
  const rawSteps = detail['steps.service_name'].map((svc, i) => ({
    serviceName: svc,
    action: detail['steps.action']?.[i] ?? '',
    startedAt: detail['steps.started_at']?.[i] ?? '',
    durationMs: detail['steps.duration_ms']?.[i] ?? 0,
    status: detail['steps.status']?.[i] ?? 'success',
    level: detail['steps.level']?.[i] ?? 'INFO',
  }));

  const stepSequence: StepInfo[] = rawSteps.map((step, idx) => {
    const matchedNode = findBestMatch(step.serviceName, nodes);
    return {
      index: idx + 1,
      serviceName: step.serviceName,
      nodeId: matchedNode?.id ?? null,
      action: step.action,
      startedAt: step.startedAt,
      durationMs: step.durationMs,
      status: step.status,
      level: step.level,
    };
  });

  const highlightNodeIds = new Set<string>();
  const highlightEdgeIds = new Set<string>();
  const tempEdges: TempEdge[] = [];

  for (let i = 0; i < stepSequence.length - 1; i++) {
    const current = stepSequence[i];
    const next = stepSequence[i + 1];
    if (current.nodeId) highlightNodeIds.add(current.nodeId);
    if (next.nodeId) highlightNodeIds.add(next.nodeId);
    if (!current.nodeId || !next.nodeId) continue;

    const matchedEdge = edges.find(edge =>
      edge.source === current.nodeId && edge.target === next.nodeId
    );
    if (matchedEdge) {
      const eid = matchedEdge.id ?? matchedEdge.edge_key ?? '';
      if (eid) highlightEdgeIds.add(eid);
    } else {
      tempEdges.push({ source: current.nodeId, target: next.nodeId, stepIndex: i });
    }
  }

  return {
    nodeIds: [...highlightNodeIds],
    edgeIds: [...highlightEdgeIds],
    stepSequence,
    tempEdges,
  };
}
