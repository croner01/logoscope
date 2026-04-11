#!/usr/bin/env node
/**
 * M3-06: 拓扑页面性能回归脚本
 * 目标：在 200 节点 / 400 边条件下，Focus + Depth 过滤与问题边排序保持流畅。
 */

const NODE_COUNT = 200;
const EDGE_COUNT = 400;
const ITERATIONS = 80;
const THRESHOLD_MS = 200;

function createGraph(nodeCount, edgeCount) {
  const nodes = Array.from({ length: nodeCount }).map((_, index) => ({
    id: `svc-${index}`,
    label: `service-${index}`,
  }));

  const edges = [];
  for (let i = 0; i < edgeCount; i += 1) {
    const sourceIndex = i % nodeCount;
    const targetIndex = (i * 7 + 13) % nodeCount;
    if (sourceIndex === targetIndex) {
      continue;
    }
    edges.push({
      id: `e-${i}`,
      source: `svc-${sourceIndex}`,
      target: `svc-${targetIndex}`,
      metrics: {
        error_rate: ((i * 17) % 100) / 1000,
        timeout_rate: ((i * 11) % 100) / 1200,
        p95: 10 + ((i * 19) % 1800),
        p99: 20 + ((i * 23) % 2400),
        quality_score: 40 + ((i * 29) % 60),
        evidence_type: i % 3 === 0 ? 'inferred' : 'observed',
      },
    });
  }

  return { nodes, edges };
}

function buildAdjacency(edges) {
  const adjacency = new Map();
  for (const edge of edges) {
    if (!adjacency.has(edge.source)) adjacency.set(edge.source, []);
    if (!adjacency.has(edge.target)) adjacency.set(edge.target, []);
    adjacency.get(edge.source).push(edge.target);
    adjacency.get(edge.target).push(edge.source);
  }
  return adjacency;
}

function filterGraphByFocusDepth(nodes, edges, focusNodeId, depth) {
  const adjacency = buildAdjacency(edges);
  const visited = new Set([focusNodeId]);
  const queue = [{ id: focusNodeId, d: 0 }];

  while (queue.length > 0) {
    const current = queue.shift();
    if (!current || current.d >= depth) {
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

  return {
    nodes: nodes.filter((node) => visited.has(node.id)),
    edges: edges.filter((edge) => visited.has(edge.source) && visited.has(edge.target)),
  };
}

function computeIssueScore(edge) {
  const metrics = edge.metrics || {};
  const errorRate = Number(metrics.error_rate || 0);
  const timeoutRate = Number(metrics.timeout_rate || 0);
  const p95 = Number(metrics.p95 || 0);
  const p99 = Number(metrics.p99 || 0);
  const qualityScore = Number(metrics.quality_score || 100);
  const inferredPenalty = metrics.evidence_type === 'inferred' ? 3 : 0;

  const latencyScore = Math.min((p95 + p99) / 2500, 1) * 30;
  const qualityPenalty = Math.max(0, (70 - qualityScore) / 70) * 30;
  const timeoutScore = Math.min(timeoutRate * 100, 1) * 25;
  const errorScore = Math.min(errorRate * 100, 1) * 50;

  return errorScore + timeoutScore + latencyScore + qualityPenalty + inferredPenalty;
}

function percentile(values, p) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[index];
}

function run() {
  const { nodes, edges } = createGraph(NODE_COUNT, EDGE_COUNT);
  const costs = [];

  for (let i = 0; i < ITERATIONS; i += 1) {
    const focusId = nodes[i % nodes.length].id;
    const depth = 1 + (i % 3);
    const startedAt = performance.now();
    const filtered = filterGraphByFocusDepth(nodes, edges, focusId, depth);
    filtered.edges.sort((a, b) => computeIssueScore(b) - computeIssueScore(a));
    const costMs = performance.now() - startedAt;
    costs.push(costMs);
  }

  const avg = costs.reduce((sum, value) => sum + value, 0) / costs.length;
  const p95 = percentile(costs, 95);
  const max = Math.max(...costs);

  console.log(`Topology perf regression: nodes=${NODE_COUNT}, edges=${EDGE_COUNT}, iterations=${ITERATIONS}`);
  console.log(`avg=${avg.toFixed(2)}ms, p95=${p95.toFixed(2)}ms, max=${max.toFixed(2)}ms, threshold=${THRESHOLD_MS}ms`);

  if (p95 > THRESHOLD_MS) {
    console.error(`FAILED: p95=${p95.toFixed(2)}ms > ${THRESHOLD_MS}ms`);
    process.exit(1);
  }
  console.log('PASSED');
}

run();

