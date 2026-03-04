import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import {
  AlertCircle,
  BrainCircuit,
  Download,
  ExternalLink,
  FileText,
  GripHorizontal,
  LayoutGrid,
  Maximize2,
  Minimize2,
  Network,
  RefreshCw,
  X,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';

import { useEvents, useHybridTopology, useRealtimeTopology, useTopologyEdgeLogPreview, useTopologyStats } from '../hooks/useApi';
import { useNavigation } from '../hooks/useNavigation';
import LoadingState from '../components/common/LoadingState';
import ErrorState from '../components/common/ErrorState';
import Tooltip from '../components/common/Tooltip';
import {
  computeEdgeIssueScore,
  filterByEvidenceMode,
  filterGraphByFocusDepth,
  isolateNodeNeighborhood,
  sortEdgesByIssueScore,
} from '../utils/topologyGraph';
import {
  resolveEdgeIssueScore,
  resolveEdgeProblemSummary,
  resolveIssueSummary,
  resolveNodeProblemSummary,
} from '../utils/topologyProblemSummary';

type LayoutMode = 'swimlane' | 'grid' | 'free';
type EvidenceMode = 'all' | 'observed' | 'inferred';
type MessageTargetPattern = 'url' | 'kv' | 'proxy' | 'rpc';
type InferenceMode = 'rule' | 'hybrid_score';

type EdgeSortMode = 'anomaly' | 'error_rate' | 'timeout_rate' | 'p99';
type PanelKey = 'control' | 'issues' | 'detail';
type PathDirection = 'upstream' | 'downstream';
type PathViewMode = 'all' | PathDirection;

interface NodePosition {
  x: number;
  y: number;
  laneKey?: string;
  laneLabel?: string;
}

interface DraggingNode {
  id: string;
  startClientX: number;
  startClientY: number;
  startNodeX: number;
  startNodeY: number;
}

interface DraggingPanel {
  panel: PanelKey;
  offsetX: number;
  offsetY: number;
}

interface PanelPos {
  x: number;
  y: number;
}

interface ServicePathSummary {
  id: string;
  direction: PathDirection;
  nodeIds: string[];
  edgeIds: string[];
  pathText: string;
  hopCount: number;
  requestRate: number;
  errorRate: number;
  timeoutRate: number;
  p95: number;
  p99: number;
  qualityScore: number;
  issueScore: number;
  riskLevel: '高风险' | '中风险' | '低风险';
  explanation: string;
}

interface EdgeLabelBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

interface EdgeBundleMeta {
  key: string;
  index: number;
  size: number;
  expanded: boolean;
  spacing: number;
}

interface EdgeRenderDatum {
  uid: string;
  edge: any;
  edgeIndex: number;
  path: string;
  labelX: number;
  labelY: number;
  labelVisible: boolean;
  labelTitle: string;
  labelStroke: string;
  labelTextColor: string;
  score: number;
  edgeOpacity: number;
  edgeWidth: number;
  flowDotCount: number;
  flowDuration: number;
  color: { stroke: string; marker: string; severity: 'danger' | 'warning' | 'normal'; meaning: string };
}

interface HoverCardState {
  kind: 'node' | 'edge';
  cursorX: number;
  cursorY: number;
  node?: any;
  edge?: any;
}

const TIME_WINDOWS = ['15 MINUTE', '30 MINUTE', '1 HOUR', '6 HOUR', '24 HOUR'];
const DEPTH_OPTIONS = [1, 2, 3];
const MAX_PATHS_PER_DIRECTION = 28;
const MESSAGE_TARGET_PATTERN_OPTIONS: Array<{ key: MessageTargetPattern; label: string }> = [
  { key: 'url', label: 'URL' },
  { key: 'kv', label: 'KV' },
  { key: 'proxy', label: 'Proxy' },
  { key: 'rpc', label: 'RPC' },
];
const LAYOUT = {
  nodeWidth: 186,
  nodeHeight: 86,
  gridCols: 5,
  gridGapX: 220,
  gridGapY: 140,
  laneStartX: 220,
  laneStartY: 120,
  laneColGapX: 230,
  laneRowGapY: 144,
  laneColsPerRow: 4,
  laneRowPaddingTop: 54,
  laneRowPaddingBottom: 42,
  laneBlockGapY: 58,
};

const PANEL_DEFAULTS: Record<PanelKey, PanelPos> = {
  control: { x: 20, y: 18 },
  issues: { x: 20, y: 290 },
  detail: { x: 0, y: 18 },
};

const LANE_COLORS = ['#22d3ee', '#a78bfa', '#34d399', '#fb923c', '#fb7185', '#60a5fa'];
const DENSE_TOPOLOGY_EDGE_THRESHOLD = 180;
const HEAVY_TOPOLOGY_EDGE_THRESHOLD = 320;
const PATH_SUMMARY_EDGE_THRESHOLD = 260;

interface TimeWindowNodeTheme {
  serviceFrom: string;
  serviceTo: string;
  serviceRing: string;
  serviceDot: string;
  databaseFrom: string;
  databaseTo: string;
  databaseRing: string;
  databaseDot: string;
  cacheFrom: string;
  cacheTo: string;
  cacheRing: string;
  cacheDot: string;
}

const TIME_WINDOW_NODE_THEMES: Record<string, TimeWindowNodeTheme> = {
  '15 MINUTE': {
    serviceFrom: '#0c4a6e',
    serviceTo: '#06b6d4',
    serviceRing: 'shadow-[0_0_0_1px_rgba(103,232,249,0.75),0_0_30px_rgba(6,182,212,0.42)]',
    serviceDot: 'bg-cyan-200',
    databaseFrom: '#134e4a',
    databaseTo: '#14b8a6',
    databaseRing: 'shadow-[0_0_0_1px_rgba(94,234,212,0.72),0_0_28px_rgba(20,184,166,0.36)]',
    databaseDot: 'bg-teal-200',
    cacheFrom: '#7c2d12',
    cacheTo: '#f97316',
    cacheRing: 'shadow-[0_0_0_1px_rgba(253,186,116,0.7),0_0_26px_rgba(249,115,22,0.34)]',
    cacheDot: 'bg-orange-200',
  },
  '30 MINUTE': {
    serviceFrom: '#164e63',
    serviceTo: '#0ea5e9',
    serviceRing: 'shadow-[0_0_0_1px_rgba(125,211,252,0.72),0_0_28px_rgba(14,165,233,0.4)]',
    serviceDot: 'bg-sky-200',
    databaseFrom: '#14532d',
    databaseTo: '#22c55e',
    databaseRing: 'shadow-[0_0_0_1px_rgba(134,239,172,0.68),0_0_24px_rgba(34,197,94,0.32)]',
    databaseDot: 'bg-lime-200',
    cacheFrom: '#7c2d12',
    cacheTo: '#fb923c',
    cacheRing: 'shadow-[0_0_0_1px_rgba(254,215,170,0.7),0_0_24px_rgba(251,146,60,0.34)]',
    cacheDot: 'bg-amber-200',
  },
  '1 HOUR': {
    serviceFrom: '#0f172a',
    serviceTo: '#2563eb',
    serviceRing: 'shadow-[0_0_0_1px_rgba(96,165,250,0.65),0_0_28px_rgba(37,99,235,0.35)]',
    serviceDot: 'bg-cyan-300',
    databaseFrom: '#064e3b',
    databaseTo: '#10b981',
    databaseRing: 'shadow-[0_0_0_1px_rgba(52,211,153,0.65),0_0_26px_rgba(16,185,129,0.35)]',
    databaseDot: 'bg-emerald-300',
    cacheFrom: '#422006',
    cacheTo: '#f97316',
    cacheRing: 'shadow-[0_0_0_1px_rgba(251,146,60,0.65),0_0_26px_rgba(249,115,22,0.35)]',
    cacheDot: 'bg-orange-300',
  },
  '6 HOUR': {
    serviceFrom: '#312e81',
    serviceTo: '#6366f1',
    serviceRing: 'shadow-[0_0_0_1px_rgba(165,180,252,0.72),0_0_30px_rgba(99,102,241,0.38)]',
    serviceDot: 'bg-indigo-200',
    databaseFrom: '#3f6212',
    databaseTo: '#84cc16',
    databaseRing: 'shadow-[0_0_0_1px_rgba(190,242,100,0.68),0_0_26px_rgba(132,204,22,0.32)]',
    databaseDot: 'bg-lime-200',
    cacheFrom: '#7f1d1d',
    cacheTo: '#f97316',
    cacheRing: 'shadow-[0_0_0_1px_rgba(254,205,211,0.7),0_0_26px_rgba(249,115,22,0.32)]',
    cacheDot: 'bg-rose-200',
  },
  '24 HOUR': {
    serviceFrom: '#1f2937',
    serviceTo: '#475569',
    serviceRing: 'shadow-[0_0_0_1px_rgba(148,163,184,0.68),0_0_28px_rgba(71,85,105,0.34)]',
    serviceDot: 'bg-slate-300',
    databaseFrom: '#334155',
    databaseTo: '#64748b',
    databaseRing: 'shadow-[0_0_0_1px_rgba(148,163,184,0.68),0_0_24px_rgba(100,116,139,0.3)]',
    databaseDot: 'bg-slate-200',
    cacheFrom: '#374151',
    cacheTo: '#6b7280',
    cacheRing: 'shadow-[0_0_0_1px_rgba(156,163,175,0.68),0_0_24px_rgba(107,114,128,0.3)]',
    cacheDot: 'bg-zinc-200',
  },
};

const DEFAULT_TIME_WINDOW_NODE_THEME = TIME_WINDOW_NODE_THEMES['1 HOUR'];

const REASON_LABELS: Record<string, { label: string; description: string }> = {
  dns_resolution: { label: 'DNS 解析', description: '服务通过域名发现目标服务。' },
  cache_access: { label: '缓存访问', description: '服务访问缓存组件（如 Redis）。' },
  image_pull: { label: '镜像拉取', description: '组件从镜像仓库拉取镜像。' },
  database_access: { label: '数据库访问', description: '服务与数据库之间存在访问关系。' },
  http_call: { label: 'HTTP 调用', description: '服务之间通过 HTTP 进行调用。' },
  rpc_call: { label: 'RPC 调用', description: '服务之间通过 RPC 进行调用。' },
  message_queue: { label: '消息队列', description: '通过队列/流实现异步传递。' },
  service_discovery: { label: '服务发现', description: '调用链路通过注册发现组件建立。' },
};

const toPct = (value: number | undefined | null): string => `${(((value ?? 0) as number) * 100).toFixed(2)}%`;
const toNum = (value: number | undefined | null, digits = 1): string => Number(value ?? 0).toFixed(digits);

const safeText = (value: unknown): string => String(value ?? '').trim();
const toMetric = (value: unknown, fallback = 0): number => {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
};

function getRelationshipLabel(reason: string): { label: string; description: string } {
  return REASON_LABELS[reason] || {
    label: reason || '调用关系',
    description: reason ? `链路由系统规则推断: ${reason}` : '链路由系统推断。',
  };
}

function resolveServiceName(node: any): string {
  return node?.service?.name || node?.metrics?.service_name || node?.label || node?.id || 'unknown';
}

function resolveNamespace(node: any): string {
  const normalizeNamespace = (value: unknown): string => safeText(value);
  const namespaceQuality = (value: string): number => {
    const text = safeText(value).toLowerCase();
    if (!text || ['unknown', 'none', 'null', 'n/a', '-'].includes(text)) {
      return 0;
    }
    if (text === 'default') {
      return 1;
    }
    return 2;
  };
  const isValidNamespace = (value: unknown): boolean => namespaceQuality(normalizeNamespace(value)) > 0;
  const candidates: string[] = [];
  const pushCandidate = (value: unknown): void => {
    const normalized = normalizeNamespace(value);
    if (isValidNamespace(normalized)) {
      candidates.push(normalized);
    }
  };

  const directCandidates = [
    node?.service?.namespace,
    node?.service_namespace,
    node?.metrics?.service_namespace,
    node?.metrics?.namespace,
    node?.kubernetes?.namespace_name,
    node?.namespace,
  ];
  directCandidates.forEach((candidate) => pushCandidate(candidate));

  const nodeKeyCandidates = [node?.node_key, node?.metrics?.node_key];
  for (const key of nodeKeyCandidates) {
    const rawKey = safeText(key);
    if (!rawKey) {
      continue;
    }
    const firstToken = safeText(rawKey.split(':')[0]);
    pushCandidate(firstToken);
  }

  const nodeId = safeText(node?.id);
  if (nodeId.includes('/')) {
    const tokens = nodeId.split('/').map((token) => safeText(token)).filter(Boolean);
    const serviceNameCandidates = new Set(
      [
        node?.service?.name,
        node?.metrics?.service_name,
        node?.name,
        node?.label,
      ]
        .map((item) => safeText(item).toLowerCase())
        .filter(Boolean),
    );

    const namespaceToken = tokens.find(
      (token) => isValidNamespace(token) && !serviceNameCandidates.has(token.toLowerCase()),
    );
    if (namespaceToken) {
      pushCandidate(namespaceToken);
    }

    const firstToken = safeText(tokens[0]);
    pushCandidate(firstToken);
  }

  let bestNamespace = 'unknown';
  let bestScore = -1;
  candidates.forEach((candidate, index) => {
    const score = namespaceQuality(candidate) * 10_000 - index;
    if (score > bestScore) {
      bestScore = score;
      bestNamespace = candidate;
    }
  });
  if (bestScore >= 0) {
    return bestNamespace;
  }
  return 'unknown';
}

function resolveLane(node: any): { key: string; label: string } {
  const namespace = resolveNamespace(node);
  if (namespace === 'unknown') {
    const type = safeText(node?.type || 'service').toLowerCase();
    if (type.includes('database') || type.includes('db')) {
      return { key: 'type:database', label: 'Data Plane · Database' };
    }
    if (type.includes('cache') || type.includes('redis')) {
      return { key: 'type:cache', label: 'Data Plane · Cache' };
    }
    if (type.includes('external') || type.includes('api')) {
      return { key: 'type:external', label: 'Edge Plane · External' };
    }

    return { key: 'type:service', label: 'Service Plane · Core Services' };
  }

  if (namespace) {
    return { key: `ns:${namespace.toLowerCase()}`, label: `Namespace · ${namespace}` };
  }
  return { key: 'type:service', label: 'Service Plane · Core Services' };
}

function getNodeStatus(node: any): 'error' | 'warning' | 'normal' {
  const summary = resolveNodeProblemSummary(node);
  if (summary.riskLevel === '高风险') {
    return 'error';
  }
  if (summary.riskLevel === '中风险') {
    return 'warning';
  }

  const errorCount = Number(node?.metrics?.error_count ?? 0);
  const timeoutRate = Number(node?.metrics?.timeout_rate ?? 0);
  if (errorCount > 0 || timeoutRate > 0.05) {
    return 'error';
  }
  if (Number(node?.metrics?.log_count ?? 0) > 1000) {
    return 'warning';
  }
  return 'normal';
}

function resolveNodeThemeByWindow(timeWindow: string): TimeWindowNodeTheme {
  const normalized = String(timeWindow || '').trim().toUpperCase();
  return TIME_WINDOW_NODE_THEMES[normalized] || DEFAULT_TIME_WINDOW_NODE_THEME;
}

function getNodePalette(node: any, timeWindow: string): { from: string; to: string; ring: string; statusDot: string } {
  const status = getNodeStatus(node);
  if (status === 'error') {
    return {
      from: '#7f1d1d',
      to: '#ef4444',
      ring: 'shadow-[0_0_0_1px_rgba(248,113,113,0.7),0_0_28px_rgba(239,68,68,0.45)]',
      statusDot: 'bg-red-400',
    };
  }
  if (status === 'warning') {
    return {
      from: '#78350f',
      to: '#f59e0b',
      ring: 'shadow-[0_0_0_1px_rgba(251,191,36,0.65),0_0_22px_rgba(245,158,11,0.35)]',
      statusDot: 'bg-amber-300',
    };
  }

  const theme = resolveNodeThemeByWindow(timeWindow);
  const type = safeText(node?.type || '').toLowerCase();
  if (type.includes('database') || type.includes('db')) {
    return {
      from: theme.databaseFrom,
      to: theme.databaseTo,
      ring: theme.databaseRing,
      statusDot: theme.databaseDot,
    };
  }
  if (type.includes('cache') || type.includes('redis')) {
    return {
      from: theme.cacheFrom,
      to: theme.cacheTo,
      ring: theme.cacheRing,
      statusDot: theme.cacheDot,
    };
  }

  return {
    from: theme.serviceFrom,
    to: theme.serviceTo,
    ring: theme.serviceRing,
    statusDot: theme.serviceDot,
  };
}

function getEdgeColor(edge: any): { stroke: string; marker: string; severity: 'danger' | 'warning' | 'normal'; meaning: string } {
  const summary = resolveEdgeProblemSummary(edge);
  if (summary.riskLevel === '高风险') {
    return { stroke: '#fb7185', marker: 'arrow-danger', severity: 'danger', meaning: '高风险链路' };
  }
  if (summary.riskLevel === '中风险') {
    return { stroke: '#fbbf24', marker: 'arrow-warning', severity: 'warning', meaning: '预警链路' };
  }

  const errorRate = Number(edge?.metrics?.error_rate ?? 0);
  const timeoutRate = Number(edge?.metrics?.timeout_rate ?? edge?.timeout_rate ?? 0);
  const evidence = safeText(edge?.metrics?.evidence_type || edge?.evidence_type || 'observed');

  if (errorRate > 0.08 || timeoutRate > 0.05) {
    return { stroke: '#fb7185', marker: 'arrow-danger', severity: 'danger', meaning: '高风险链路' };
  }
  if (errorRate > 0.03 || timeoutRate > 0.02) {
    return { stroke: '#fbbf24', marker: 'arrow-warning', severity: 'warning', meaning: '预警链路' };
  }
  if (evidence === 'inferred') {
    return { stroke: '#a78bfa', marker: 'arrow-inferred', severity: 'normal', meaning: '推断链路' };
  }
  return { stroke: '#38bdf8', marker: 'arrow-observed', severity: 'normal', meaning: '观测链路' };
}

function hashText(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

function edgePairKey(edge: any): string {
  return `${safeText(edge?.source)}=>${safeText(edge?.target)}`;
}

function resolveEdgeUid(edge: any, fallbackIndex = 0): string {
  const rawId = safeText(edge?.id);
  if (rawId) {
    return rawId;
  }
  return `${edgePairKey(edge)}#${fallbackIndex}`;
}

function getRiskLevel(errorRate: number, timeoutRate: number, p99: number, qualityScore: number): '高风险' | '中风险' | '低风险' {
  if (errorRate > 0.08 || timeoutRate > 0.05 || p99 > 1200 || qualityScore < 60) {
    return '高风险';
  }
  if (errorRate > 0.03 || timeoutRate > 0.02 || p99 > 650 || qualityScore < 80) {
    return '中风险';
  }
  return '低风险';
}

function resolveDirectionalContribution(edge: any): {
  hasMetric: boolean;
  value: number;
  confidenceContribution: number;
  evidenceContribution: number;
  inferenceMode: string;
} {
  const raw = edge?.metrics?.directional_consistency;
  const parsed = Number(raw);
  const hasMetric = raw !== undefined && raw !== null && raw !== '' && Number.isFinite(parsed);
  const value = Math.max(0, Math.min(1, hasMetric ? parsed : 1));
  const inferenceMode = safeText(edge?.metrics?.inference_mode || '');

  return {
    hasMetric,
    value,
    confidenceContribution: value * 0.24,
    evidenceContribution: value * 2.0,
    inferenceMode,
  };
}

function formatEdgeDescription(edge: any): string {
  const relation = getRelationshipLabel(safeText(edge?.metrics?.reason || ''));
  const protocol = safeText(edge?.metrics?.protocol || edge?.metrics?.transport || edge?.metrics?.operation || relation.label || 'unknown');
  const requestRate = toMetric(edge?.metrics?.rps ?? edge?.metrics?.call_count, 0);
  const errorRate = toMetric(edge?.metrics?.error_rate, 0);
  const timeoutRate = toMetric(edge?.metrics?.timeout_rate ?? edge?.timeout_rate, 0);
  const p95 = toMetric(edge?.metrics?.p95 ?? edge?.p95, 0);
  const p99 = toMetric(edge?.metrics?.p99 ?? edge?.p99, 0);
  const quality = toMetric(edge?.metrics?.quality_score ?? edge?.quality_score, 100);
  const evidence = safeText(edge?.metrics?.evidence_type || edge?.evidence_type || 'observed');
  const risk = getRiskLevel(errorRate, timeoutRate, p99, quality);

  return `${safeText(edge?.source || 'unknown')} -> ${safeText(edge?.target || 'unknown')} | ${protocol} | ${toNum(
    requestRate,
    1,
  )} rpm | 错误率 ${toPct(errorRate)} | 超时率 ${toPct(timeoutRate)} | P95 ${toNum(p95, 0)}ms / P99 ${toNum(
    p99,
    0,
  )}ms | 质量分 ${toNum(quality, 1)} | 证据 ${evidence} | ${risk}（${relation.description}）`;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function enumerateDirectionalPaths(
  startId: string,
  edges: any[],
  direction: PathDirection,
  maxDepth: number,
  maxPaths: number,
): Array<{ nodeIds: string[]; edgeChain: any[] }> {
  const adjacency = new Map<string, any[]>();
  for (const edge of edges) {
    const from = direction === 'downstream' ? edge.source : edge.target;
    if (!adjacency.has(from)) {
      adjacency.set(from, []);
    }
    adjacency.get(from)?.push(edge);
  }

  const results: Array<{ nodeIds: string[]; edgeChain: any[] }> = [];
  const stack: Array<{ nodeIds: string[]; edgeChain: any[] }> = [{ nodeIds: [startId], edgeChain: [] }];

  while (stack.length > 0 && results.length < maxPaths) {
    const current = stack.pop();
    if (!current) {
      continue;
    }
    const currentId = current.nodeIds[current.nodeIds.length - 1];
    const nextEdges = adjacency.get(currentId) || [];

    for (const edge of nextEdges) {
      const nextId = direction === 'downstream' ? edge.target : edge.source;
      if (current.nodeIds.includes(nextId)) {
        continue;
      }
      const nextNodeIds = [...current.nodeIds, nextId];
      const nextEdgeChain = [...current.edgeChain, edge];

      results.push({ nodeIds: nextNodeIds, edgeChain: nextEdgeChain });
      if (results.length >= maxPaths) {
        break;
      }
      if (nextEdgeChain.length < maxDepth) {
        stack.push({ nodeIds: nextNodeIds, edgeChain: nextEdgeChain });
      }
    }
  }

  return results;
}

const TopologyPage: React.FC = () => {
  const location = useLocation();
  const navigation = useNavigation();
  const queryParams = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const queryNamespace = useMemo(() => {
    const value = String(queryParams.get('namespace') || '').trim();
    return value || undefined;
  }, [queryParams]);

  const [timeWindow, setTimeWindow] = useState('1 HOUR');
  const [selectedNode, setSelectedNode] = useState<any | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<any | null>(null);
  const [layoutMode, setLayoutMode] = useState<LayoutMode>('swimlane');
  const [focusNodeId, setFocusNodeId] = useState('');
  const [focusDepth, setFocusDepth] = useState(2);
  const [evidenceMode, setEvidenceMode] = useState<EvidenceMode>('all');
  const [inferenceMode, setInferenceMode] = useState<InferenceMode>('rule');
  const [messageTargetEnabled, setMessageTargetEnabled] = useState(true);
  const [messageTargetPatterns, setMessageTargetPatterns] = useState<MessageTargetPattern[]>(['url', 'kv', 'proxy', 'rpc']);
  const [messageTargetMinSupport, setMessageTargetMinSupport] = useState(2);
  const [messageTargetMaxPerLog, setMessageTargetMaxPerLog] = useState(3);
  const [isolateMode, setIsolateMode] = useState(false);
  const [edgeSortMode, setEdgeSortMode] = useState<EdgeSortMode>('anomaly');
  const [showChangeOverlay, setShowChangeOverlay] = useState(true);
  const [pathViewMode, setPathViewMode] = useState<PathViewMode>('all');
  const [selectedPathId, setSelectedPathId] = useState('');

  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const [panStart, setPanStart] = useState({ x: 0, y: 0 });
  const [draggingNode, setDraggingNode] = useState<DraggingNode | null>(null);

  const [nodePositions, setNodePositions] = useState<Record<string, NodePosition>>({});

  const [isFullscreen, setIsFullscreen] = useState(false);
  const [panelPositions, setPanelPositions] = useState<Record<PanelKey, PanelPos>>(PANEL_DEFAULTS);
  const [draggingPanel, setDraggingPanel] = useState<DraggingPanel | null>(null);
  const [hoverCard, setHoverCard] = useState<HoverCardState | null>(null);
  const confidenceThreshold = 0.3;
  const messageTargetPatternsParam = useMemo(
    () => Array.from(new Set(messageTargetPatterns)).sort().join(','),
    [messageTargetPatterns],
  );

  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const hoverHideTimerRef = useRef<number | null>(null);

  const { data, loading, error, refetch } = useHybridTopology({
    time_window: timeWindow,
    namespace: queryNamespace,
    confidence_threshold: confidenceThreshold,
    inference_mode: inferenceMode,
    message_target_enabled: messageTargetEnabled,
    message_target_patterns: messageTargetPatternsParam,
    message_target_min_support: messageTargetMinSupport,
    message_target_max_per_log: messageTargetMaxPerLog,
  });
  const { topology: realtimeTopology, isConnected: realtimeConnected } = useRealtimeTopology({
    enabled: true,
    subscription: {
      time_window: timeWindow,
      namespace: queryNamespace,
      confidence_threshold: confidenceThreshold,
      inference_mode: inferenceMode,
      message_target_enabled: messageTargetEnabled,
      message_target_patterns: messageTargetPatternsParam,
      message_target_min_support: messageTargetMinSupport,
      message_target_max_per_log: messageTargetMaxPerLog,
    },
  });
  const { data: statsData } = useTopologyStats({ time_window: timeWindow });
  const { data: recentEventsData } = useEvents({ limit: 80, exclude_health_check: true });
  const edgePreviewParams = useMemo(() => {
    if (!selectedEdge?.source || !selectedEdge?.target) {
      return null;
    }
    return {
      source_service: safeText(selectedEdge.source),
      target_service: safeText(selectedEdge.target),
      time_window: timeWindow,
      limit: 8,
      exclude_health_check: true,
    };
  }, [selectedEdge, timeWindow]);
  const { data: edgeLogPreviewData, loading: edgeLogPreviewLoading } = useTopologyEdgeLogPreview(edgePreviewParams);

  const topologyData = useMemo(() => {
    const realtimeWindow = String(realtimeTopology?.metadata?.time_window || '').trim().toUpperCase();
    const expectedWindow = String(timeWindow || '').trim().toUpperCase();
    const realtimeInferenceQuality = realtimeTopology?.metadata?.inference_quality || {};
    const realtimeInferenceMode = String(
      realtimeInferenceQuality?.inference_mode || realtimeTopology?.metadata?.inference_mode || 'rule',
    )
      .trim()
      .toLowerCase();
    const realtimeMessageTargetEnabled = Boolean(realtimeInferenceQuality?.message_target_enabled ?? true);
    const realtimePatterns = Array.isArray(realtimeInferenceQuality?.message_target_patterns)
      ? realtimeInferenceQuality.message_target_patterns
      : String(realtimeInferenceQuality?.message_target_patterns || '')
          .split(',')
          .map((item: string) => item.trim().toLowerCase())
          .filter(Boolean);
    const normalizedRealtimePatterns = Array.from(new Set(realtimePatterns)).sort().join(',');
    const normalizedExpectedPatterns = messageTargetPatternsParam;
    const realtimeMinSupport = Number(realtimeInferenceQuality?.message_target_min_support ?? 2);
    const realtimeMaxPerLog = Number(realtimeInferenceQuality?.message_target_max_per_log ?? 3);
    const normalizedRealtimeNamespace = String(realtimeTopology?.metadata?.namespace || '').trim();
    const normalizedExpectedNamespace = String(queryNamespace || '').trim();
    if (
      realtimeTopology &&
      Array.isArray(realtimeTopology.nodes) &&
      realtimeWindow === expectedWindow &&
      normalizedRealtimeNamespace === normalizedExpectedNamespace &&
      realtimeInferenceMode === inferenceMode &&
      realtimeMessageTargetEnabled === messageTargetEnabled &&
      normalizedRealtimePatterns === normalizedExpectedPatterns &&
      realtimeMinSupport === messageTargetMinSupport &&
      realtimeMaxPerLog === messageTargetMaxPerLog
    ) {
      return realtimeTopology;
    }
    return data;
  }, [
    data,
    inferenceMode,
    messageTargetEnabled,
    messageTargetMaxPerLog,
    messageTargetMinSupport,
    messageTargetPatternsParam,
    queryNamespace,
    realtimeTopology,
    timeWindow,
  ]);

  const focusedService = queryParams.get('service');
  const highlightedService = queryParams.get('highlight') || focusedService;
  const queryTimeWindow = queryParams.get('timeWindow');
  const queryDepth = Number(queryParams.get('depth') || '');

  useEffect(() => {
    if (queryTimeWindow && TIME_WINDOWS.includes(queryTimeWindow)) {
      setTimeWindow(queryTimeWindow);
    }
  }, [queryTimeWindow]);

  useEffect(() => {
    if (DEPTH_OPTIONS.includes(queryDepth)) {
      setFocusDepth(queryDepth);
    }
  }, [queryDepth]);

  const filteredTopology = useMemo(() => {
    const start = typeof performance !== 'undefined' ? performance.now() : Date.now();
    const baseNodes = topologyData?.nodes || [];
    const baseEdges = topologyData?.edges || [];

    if (evidenceMode === 'all' && !focusNodeId && !isolateMode) {
      const cost = (typeof performance !== 'undefined' ? performance.now() : Date.now()) - start;
      return {
        nodes: baseNodes,
        edges: baseEdges,
        baseNodeCount: baseNodes.length,
        baseEdgeCount: baseEdges.length,
        costMs: Math.round(cost * 100) / 100,
      };
    }

    let nodes = baseNodes;
    let edges = baseEdges;

    const evidenceFiltered = filterByEvidenceMode(nodes, edges, evidenceMode);
    nodes = evidenceFiltered.nodes;
    edges = evidenceFiltered.edges;

    const activeFocus = focusNodeId;
    if (activeFocus) {
      const focused = filterGraphByFocusDepth(nodes, edges, activeFocus, focusDepth);
      nodes = focused.nodes;
      edges = focused.edges;
    }

    if (isolateMode) {
      const isolateTarget = activeFocus;
      if (isolateTarget) {
        const isolated = isolateNodeNeighborhood(nodes, edges, isolateTarget);
        nodes = isolated.nodes;
        edges = isolated.edges;
      }
    }

    const cost = (typeof performance !== 'undefined' ? performance.now() : Date.now()) - start;
    return {
      nodes,
      edges,
      baseNodeCount: baseNodes.length,
      baseEdgeCount: baseEdges.length,
      costMs: Math.round(cost * 100) / 100,
    };
  }, [topologyData, evidenceMode, focusNodeId, focusDepth, isolateMode]);

  const visibleNodes = filteredTopology.nodes || [];
  const visibleEdges = filteredTopology.edges || [];

  const nodeLabelById = useMemo(() => {
    const mapping = new Map<string, string>();
    (topologyData?.nodes || []).forEach((node: any) => {
      mapping.set(node.id, node.label || node.id);
    });
    visibleNodes.forEach((node: any) => {
      if (!mapping.has(node.id)) {
        mapping.set(node.id, node.label || node.id);
      }
    });
    return mapping;
  }, [topologyData, visibleNodes]);

  useEffect(() => {
    if (!focusedService || !topologyData?.nodes?.length) {
      return;
    }
    const targetNode = topologyData.nodes.find((node: any) => node.id === focusedService || node.label === focusedService);
    if (targetNode) {
      setSelectedNode(targetNode);
      setSelectedEdge(null);
      setFocusNodeId(targetNode.id || targetNode.label || '');
    }
  }, [focusedService, topologyData]);

  useEffect(() => {
    if (!selectedNode) {
      return;
    }
    const latestNode = visibleNodes.find((node: any) => node.id === selectedNode.id);
    if (!latestNode) {
      setSelectedNode(null);
      return;
    }
    if (latestNode !== selectedNode) {
      setSelectedNode(latestNode);
    }
  }, [visibleNodes, selectedNode]);

  useEffect(() => {
    if (!selectedEdge) {
      return;
    }
    const latestEdge = visibleEdges.find((edge: any) => edge.id === selectedEdge.id);
    if (!latestEdge) {
      setSelectedEdge(null);
      return;
    }
    if (latestEdge !== selectedEdge) {
      setSelectedEdge(latestEdge);
    }
  }, [visibleEdges, selectedEdge]);

  useEffect(() => {
    setSelectedPathId('');
  }, [selectedNode?.id, pathViewMode]);

  useEffect(() => {
    return () => {
      if (hoverHideTimerRef.current) {
        window.clearTimeout(hoverHideTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (visibleNodes.length === 0) {
      setNodePositions({});
      return;
    }

    if (layoutMode === 'free') {
      setNodePositions((prev) => {
        const next: Record<string, NodePosition> = {};
        visibleNodes.forEach((node: any, index: number) => {
          const existing = prev[node.id];
          if (existing) {
            next[node.id] = existing;
            return;
          }
          const col = index % LAYOUT.gridCols;
          const row = Math.floor(index / LAYOUT.gridCols);
          next[node.id] = {
            x: 120 + col * LAYOUT.gridGapX,
            y: 120 + row * LAYOUT.gridGapY,
            laneKey: 'free',
            laneLabel: '自由编排',
          };
        });
        return next;
      });
      return;
    }

    if (layoutMode === 'grid') {
      const next: Record<string, NodePosition> = {};
      visibleNodes.forEach((node: any, index: number) => {
        const col = index % LAYOUT.gridCols;
        const row = Math.floor(index / LAYOUT.gridCols);
        next[node.id] = {
          x: 120 + col * LAYOUT.gridGapX,
          y: 110 + row * LAYOUT.gridGapY,
          laneKey: 'grid',
          laneLabel: 'Grid',
        };
      });
      setNodePositions(next);
      return;
    }

    const groups = new Map<string, { label: string; nodes: any[] }>();
    visibleNodes.forEach((node: any) => {
      const lane = resolveLane(node);
      if (!groups.has(lane.key)) {
        groups.set(lane.key, { label: lane.label, nodes: [] });
      }
      groups.get(lane.key)?.nodes.push(node);
    });

    const laneEntries = Array.from(groups.entries()).sort((a, b) => a[0].localeCompare(b[0]));
    const next: Record<string, NodePosition> = {};

    let laneCursorY = LAYOUT.laneStartY;
    laneEntries.forEach(([laneKey, lane]) => {
      const ordered = [...lane.nodes].sort((a, b) => resolveServiceName(a).localeCompare(resolveServiceName(b)));
      const laneRows = Math.max(1, Math.ceil(ordered.length / LAYOUT.laneColsPerRow));
      ordered.forEach((node, index) => {
        const col = index % LAYOUT.laneColsPerRow;
        const row = Math.floor(index / LAYOUT.laneColsPerRow);
        next[node.id] = {
          x: LAYOUT.laneStartX + col * LAYOUT.laneColGapX,
          y: laneCursorY + LAYOUT.laneRowPaddingTop + row * LAYOUT.laneRowGapY,
          laneKey,
          laneLabel: lane.label,
        };
      });
      laneCursorY +=
        laneRows * LAYOUT.laneRowGapY + LAYOUT.laneRowPaddingTop + LAYOUT.laneRowPaddingBottom + LAYOUT.laneBlockGapY;
    });

    const missing = visibleNodes.filter((node: any) => !next[node.id]);
    missing.forEach((node: any, index: number) => {
      next[node.id] = {
        x: 120 + index * 180,
        y: 120,
        laneKey: 'fallback',
        laneLabel: 'Fallback',
      };
    });

    setNodePositions(next);
  }, [layoutMode, visibleNodes]);

  const laneBands = useMemo(() => {
    if (layoutMode !== 'swimlane') {
      return [] as Array<{ key: string; label: string; y: number; height: number; x: number; width: number; colorIndex: number }>;
    }

    const grouped = new Map<string, { label: string; positions: NodePosition[] }>();
    visibleNodes.forEach((node: any) => {
      const pos = nodePositions[node.id];
      if (!pos) {
        return;
      }
      const laneKey = pos.laneKey || resolveLane(node).key;
      const laneLabel = pos.laneLabel || resolveLane(node).label;
      if (!grouped.has(laneKey)) {
        grouped.set(laneKey, { label: laneLabel, positions: [] });
      }
      grouped.get(laneKey)?.positions.push(pos);
    });

    return Array.from(grouped.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([key, lane], index) => {
        const minX = Math.min(...lane.positions.map((p) => p.x));
        const maxX = Math.max(...lane.positions.map((p) => p.x));
        const minY = Math.min(...lane.positions.map((p) => p.y));
        const maxY = Math.max(...lane.positions.map((p) => p.y));
        const y = minY - 36;
        const height = maxY - minY + LAYOUT.nodeHeight + 72;
        return {
          key,
          label: lane.label,
          y,
          height,
          x: minX - 90,
          width: maxX - minX + LAYOUT.nodeWidth + 180,
          colorIndex: index,
        };
      });
  }, [layoutMode, nodePositions, visibleNodes]);

  const issueSummary = useMemo(() => {
    return resolveIssueSummary(visibleNodes, visibleEdges, topologyData?.metadata || null);
  }, [topologyData?.metadata, visibleEdges, visibleNodes]);

  const topProblemEdges = useMemo(() => {
    const edges = visibleEdges || [];
    let sorted = sortEdgesByIssueScore(edges);
    if (edgeSortMode === 'error_rate') {
      sorted = [...edges].sort((a: any, b: any) => Number(b?.metrics?.error_rate ?? 0) - Number(a?.metrics?.error_rate ?? 0));
    } else if (edgeSortMode === 'timeout_rate') {
      sorted = [...edges].sort((a: any, b: any) => Number(b?.metrics?.timeout_rate ?? b?.timeout_rate ?? 0) - Number(a?.metrics?.timeout_rate ?? a?.timeout_rate ?? 0));
    } else if (edgeSortMode === 'p99') {
      sorted = [...edges].sort((a: any, b: any) => Number(b?.metrics?.p99 ?? b?.p99 ?? 0) - Number(a?.metrics?.p99 ?? a?.p99 ?? 0));
    }
    return sorted.slice(0, 10).map((edge: any) => ({
      ...edge,
      issueScore: resolveEdgeIssueScore(edge),
    }));
  }, [edgeSortMode, visibleEdges]);

  const focusPathSummaries = useMemo(() => {
    if (!selectedNode?.id || !visibleEdges.length) {
      return [] as ServicePathSummary[];
    }

    const densePathMode = visibleEdges.length >= PATH_SUMMARY_EDGE_THRESHOLD;
    const depth = Math.max(1, Math.min(4, focusDepth + 1));
    const effectiveDepth = densePathMode ? Math.min(depth, 2) : depth;
    const pathLimit = densePathMode ? Math.max(8, Math.floor(MAX_PATHS_PER_DIRECTION / 3)) : MAX_PATHS_PER_DIRECTION;
    const selectedId = selectedNode.id;

    const upstream = enumerateDirectionalPaths(selectedId, visibleEdges, 'upstream', effectiveDepth, pathLimit).map((item, idx) => {
      const requestRate = item.edgeChain.reduce((sum, edge) => sum + toMetric(edge?.metrics?.rps ?? edge?.metrics?.call_count, 0), 0);
      const errorRate = Math.max(...item.edgeChain.map((edge) => toMetric(edge?.metrics?.error_rate, 0)));
      const timeoutRate = Math.max(...item.edgeChain.map((edge) => toMetric(edge?.metrics?.timeout_rate ?? edge?.timeout_rate, 0)));
      const p95 = Math.max(...item.edgeChain.map((edge) => toMetric(edge?.metrics?.p95 ?? edge?.p95, 0)));
      const p99 = Math.max(...item.edgeChain.map((edge) => toMetric(edge?.metrics?.p99 ?? edge?.p99, 0)));
      const qualityScore = Math.min(...item.edgeChain.map((edge) => toMetric(edge?.metrics?.quality_score ?? edge?.quality_score, 100)));
      const issueScore = toMetric(
        item.edgeChain.reduce((sum, edge) => sum + computeEdgeIssueScore(edge), 0) / Math.max(item.edgeChain.length, 1),
        0,
      );
      const riskLevel = getRiskLevel(errorRate, timeoutRate, p99, qualityScore);
      const pathText = item.nodeIds.map((id) => nodeLabelById.get(id) || id).join(' -> ');
      const explanation = `上游路径 ${pathText}，共 ${item.edgeChain.length} 跳。错误率峰值 ${toPct(errorRate)}，超时率峰值 ${toPct(
        timeoutRate,
      )}，P99 峰值 ${toNum(p99, 0)}ms，最低质量分 ${toNum(qualityScore, 1)}，综合判定 ${riskLevel}。`;

      return {
        id: `upstream-${idx}-${item.nodeIds.join('>')}`,
        direction: 'upstream' as const,
        nodeIds: item.nodeIds,
        edgeIds: item.edgeChain.map((edge) => edge.id),
        pathText,
        hopCount: item.edgeChain.length,
        requestRate,
        errorRate,
        timeoutRate,
        p95,
        p99,
        qualityScore,
        issueScore,
        riskLevel,
        explanation,
      };
    });

    const downstream = enumerateDirectionalPaths(selectedId, visibleEdges, 'downstream', effectiveDepth, pathLimit).map((item, idx) => {
      const requestRate = item.edgeChain.reduce((sum, edge) => sum + toMetric(edge?.metrics?.rps ?? edge?.metrics?.call_count, 0), 0);
      const errorRate = Math.max(...item.edgeChain.map((edge) => toMetric(edge?.metrics?.error_rate, 0)));
      const timeoutRate = Math.max(...item.edgeChain.map((edge) => toMetric(edge?.metrics?.timeout_rate ?? edge?.timeout_rate, 0)));
      const p95 = Math.max(...item.edgeChain.map((edge) => toMetric(edge?.metrics?.p95 ?? edge?.p95, 0)));
      const p99 = Math.max(...item.edgeChain.map((edge) => toMetric(edge?.metrics?.p99 ?? edge?.p99, 0)));
      const qualityScore = Math.min(...item.edgeChain.map((edge) => toMetric(edge?.metrics?.quality_score ?? edge?.quality_score, 100)));
      const issueScore = toMetric(
        item.edgeChain.reduce((sum, edge) => sum + computeEdgeIssueScore(edge), 0) / Math.max(item.edgeChain.length, 1),
        0,
      );
      const riskLevel = getRiskLevel(errorRate, timeoutRate, p99, qualityScore);
      const pathText = item.nodeIds.map((id) => nodeLabelById.get(id) || id).join(' -> ');
      const explanation = `下游路径 ${pathText}，共 ${item.edgeChain.length} 跳。错误率峰值 ${toPct(errorRate)}，超时率峰值 ${toPct(
        timeoutRate,
      )}，P99 峰值 ${toNum(p99, 0)}ms，最低质量分 ${toNum(qualityScore, 1)}，综合判定 ${riskLevel}。`;

      return {
        id: `downstream-${idx}-${item.nodeIds.join('>')}`,
        direction: 'downstream' as const,
        nodeIds: item.nodeIds,
        edgeIds: item.edgeChain.map((edge) => edge.id),
        pathText,
        hopCount: item.edgeChain.length,
        requestRate,
        errorRate,
        timeoutRate,
        p95,
        p99,
        qualityScore,
        issueScore,
        riskLevel,
        explanation,
      };
    });

    return [...upstream, ...downstream]
      .filter((path) => (pathViewMode === 'all' ? true : path.direction === pathViewMode))
      .sort((a, b) => b.issueScore - a.issueScore || b.p99 - a.p99)
      .slice(0, 20);
  }, [focusDepth, nodeLabelById, pathViewMode, selectedNode, visibleEdges]);

  const selectedPath = useMemo(() => {
    return focusPathSummaries.find((path) => path.id === selectedPathId) || null;
  }, [focusPathSummaries, selectedPathId]);

  const selectedPathEdgeIds = useMemo(() => new Set(selectedPath?.edgeIds || []), [selectedPath]);
  const selectedPathNodeIds = useMemo(() => new Set(selectedPath?.nodeIds || []), [selectedPath]);
  const pathDirectionCounts = useMemo(() => {
    if (!selectedNode?.id) {
      return { upstream: 0, downstream: 0 };
    }
    return visibleEdges.reduce(
      (acc: { upstream: number; downstream: number }, edge: any) => {
        if (edge?.target === selectedNode.id) {
          acc.upstream += 1;
        }
        if (edge?.source === selectedNode.id) {
          acc.downstream += 1;
        }
        return acc;
      },
      { upstream: 0, downstream: 0 },
    );
  }, [selectedNode?.id, visibleEdges]);
  const serviceNameById = useMemo(() => {
    const mapping = new Map<string, string>();
    (topologyData?.nodes || []).forEach((node: any) => {
      mapping.set(node.id, resolveServiceName(node));
    });
    visibleNodes.forEach((node: any) => {
      if (!mapping.has(node.id)) {
        mapping.set(node.id, resolveServiceName(node));
      }
    });
    return mapping;
  }, [topologyData, visibleNodes]);
  const selectedPathPeerService = useMemo(() => {
    if (!selectedPath || selectedPath.nodeIds.length < 2) {
      return '';
    }
    const firstHopId = selectedPath.nodeIds[1];
    return serviceNameById.get(firstHopId) || nodeLabelById.get(firstHopId) || firstHopId || '';
  }, [nodeLabelById, selectedPath, serviceNameById]);
  const selectedPathTerminalService = useMemo(() => {
    if (!selectedPath || !selectedPath.nodeIds.length) {
      return '';
    }
    const terminalId = selectedPath.nodeIds[selectedPath.nodeIds.length - 1];
    return serviceNameById.get(terminalId) || nodeLabelById.get(terminalId) || terminalId || '';
  }, [nodeLabelById, selectedPath, serviceNameById]);

  const changeOverlayEvents = useMemo(() => {
    const items = recentEventsData?.events || [];
    const keywords = ['deployment', 'deploy', 'rollout', 'release', 'version', 'helm', '镜像', '发布', '变更'];
    return items
      .filter((event: any) => {
        const text = String(event?.message || '').toLowerCase();
        return keywords.some((keyword) => text.includes(keyword));
      })
      .slice(0, 8)
      .map((event: any) => ({
        id: event.id,
        service_name: event.service_name || 'unknown',
        timestamp: event.timestamp,
        message: String(event.message || ''),
      }));
  }, [recentEventsData]);

  const getNodePosition = useCallback((nodeId: string): NodePosition => {
    return nodePositions[nodeId] || { x: 120, y: 120 };
  }, [nodePositions]);

  const canvasSize = useMemo(() => {
    const positions = Object.values(nodePositions);
    if (!positions.length) {
      return { width: 1800, height: 1180 };
    }
    const maxX = Math.max(...positions.map((p) => p.x));
    const maxY = Math.max(...positions.map((p) => p.y));
    return {
      width: Math.max(1800, maxX + LAYOUT.nodeWidth + 220),
      height: Math.max(1180, maxY + LAYOUT.nodeHeight + 260),
    };
  }, [nodePositions]);

  useEffect(() => {
    const handleFullscreenChange = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener('fullscreenchange', handleFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (!containerRef.current) {
      return;
    }
    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen().catch(() => {});
      return;
    }
    document.exitFullscreen().catch(() => {});
  }, []);

  const exportTopology = useCallback(() => {
    if (!topologyData) {
      return;
    }
    const blob = new Blob([JSON.stringify(topologyData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `topology-${new Date().toISOString()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [topologyData]);

  const handleCanvasMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest('[data-floating-panel]')) {
      return;
    }
    setHoverCard(null);
    setIsPanning(true);
    setPanStart({ x: e.clientX - pan.x, y: e.clientY - pan.y });
  };

  const handleCanvasMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (isPanning) {
      setPan({ x: e.clientX - panStart.x, y: e.clientY - panStart.y });
    }

    if (draggingNode) {
      const deltaX = (e.clientX - draggingNode.startClientX) / zoom;
      const deltaY = (e.clientY - draggingNode.startClientY) / zoom;
      setNodePositions((prev) => ({
        ...prev,
        [draggingNode.id]: {
          ...(prev[draggingNode.id] || { x: 0, y: 0 }),
          x: draggingNode.startNodeX + deltaX,
          y: draggingNode.startNodeY + deltaY,
        },
      }));
    }
  };

  const stopCanvasInteractions = () => {
    setIsPanning(false);
    setDraggingNode(null);
  };

  const handleWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? -0.12 : 0.12;
    setZoom((prev) => Math.max(0.35, Math.min(2.8, prev + delta)));
  };

  const handleNodeMouseDown = (e: React.MouseEvent, node: any) => {
    e.preventDefault();
    e.stopPropagation();
    const pos = getNodePosition(node.id);
    setLayoutMode('free');
    setDraggingNode({
      id: node.id,
      startClientX: e.clientX,
      startClientY: e.clientY,
      startNodeX: pos.x,
      startNodeY: pos.y,
    });
  };

  const startPanelDrag = (panel: PanelKey, e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const containerRect = containerRef.current?.getBoundingClientRect();
    if (!containerRect) {
      return;
    }
    const panelPos = panelPositions[panel];
    setDraggingPanel({
      panel,
      offsetX: e.clientX - (containerRect.left + panelPos.x),
      offsetY: e.clientY - (containerRect.top + panelPos.y),
    });
  };

  useEffect(() => {
    if (!draggingPanel) {
      return;
    }

    const onMove = (event: MouseEvent) => {
      const containerRect = containerRef.current?.getBoundingClientRect();
      if (!containerRect) {
        return;
      }
      const width = containerRect.width;
      const height = containerRect.height;
      const nextX = event.clientX - containerRect.left - draggingPanel.offsetX;
      const nextY = event.clientY - containerRect.top - draggingPanel.offsetY;

      const boundedX = Math.max(8, Math.min(nextX, width - 360));
      const boundedY = Math.max(8, Math.min(nextY, height - 120));

      setPanelPositions((prev) => ({
        ...prev,
        [draggingPanel.panel]: { x: boundedX, y: boundedY },
      }));
    };

    const onUp = () => setDraggingPanel(null);

    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);

    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [draggingPanel]);

  useEffect(() => {
    if (!containerRef.current) {
      return;
    }
    const width = containerRef.current.clientWidth;
    setPanelPositions((prev) => ({
      ...prev,
      detail: {
        x: Math.max(18, width - 390),
        y: prev.detail.y,
      },
    }));
  }, [isFullscreen]);

  const clearHoverHideTimer = useCallback(() => {
    if (hoverHideTimerRef.current) {
      window.clearTimeout(hoverHideTimerRef.current);
      hoverHideTimerRef.current = null;
    }
  }, []);

  const scheduleHoverCardHide = useCallback(() => {
    clearHoverHideTimer();
    hoverHideTimerRef.current = window.setTimeout(() => {
      setHoverCard(null);
      hoverHideTimerRef.current = null;
    }, 120);
  }, [clearHoverHideTimer]);

  const resolveRelativeCursor = useCallback((clientX: number, clientY: number): { x: number; y: number } => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) {
      return { x: clientX, y: clientY };
    }
    return {
      x: clientX - rect.left,
      y: clientY - rect.top,
    };
  }, []);

  const showNodeHoverCard = useCallback(
    (event: React.MouseEvent, node: any) => {
      clearHoverHideTimer();
      const cursor = resolveRelativeCursor(event.clientX, event.clientY);
      setHoverCard({
        kind: 'node',
        cursorX: cursor.x,
        cursorY: cursor.y,
        node,
      });
    },
    [clearHoverHideTimer, resolveRelativeCursor],
  );

  const showEdgeHoverCard = useCallback(
    (event: React.MouseEvent, edge: any) => {
      clearHoverHideTimer();
      const cursor = resolveRelativeCursor(event.clientX, event.clientY);
      setHoverCard({
        kind: 'edge',
        cursorX: cursor.x,
        cursorY: cursor.y,
        edge,
      });
    },
    [clearHoverHideTimer, resolveRelativeCursor],
  );

  const hoverCardPosition = useMemo(() => {
    if (!hoverCard) {
      return { left: 0, top: 0, width: 320 };
    }
    const rect = containerRef.current?.getBoundingClientRect();
    const containerWidth = rect?.width || 1280;
    const containerHeight = rect?.height || 780;
    const cardWidth = hoverCard.kind === 'edge' ? 338 : 308;
    const cardHeight = hoverCard.kind === 'edge' ? 246 : 228;

    let left = hoverCard.cursorX + 16;
    let top = hoverCard.cursorY + 14;

    if (left + cardWidth > containerWidth - 10) {
      left = Math.max(10, hoverCard.cursorX - cardWidth - 16);
    }
    if (top + cardHeight > containerHeight - 10) {
      top = Math.max(10, hoverCard.cursorY - cardHeight - 14);
    }

    return { left, top, width: cardWidth };
  }, [hoverCard]);

  const handleNodeClick = (node: any) => {
    setHoverCard(null);
    setSelectedEdge(null);
    setSelectedNode(node);
    setSelectedPathId('');
  };

  const handleEdgeClick = (edge: any) => {
    setHoverCard(null);
    setSelectedNode(null);
    setSelectedEdge(edge);
    setSelectedPathId('');
  };

  const handleToggleMessageTargetPattern = (pattern: MessageTargetPattern) => {
    setMessageTargetPatterns((prev) => {
      const exists = prev.includes(pattern);
      if (exists) {
        if (prev.length <= 1) {
          return prev;
        }
        return prev.filter((item) => item !== pattern);
      }
      return [...prev, pattern];
    });
  };

  const nodeCount = visibleNodes.length;
  const edgeCount = visibleEdges.length;
  const laneCount = laneBands.length;
  const selectedEdgeId = safeText(selectedEdge?.id);
  const activeFocusNodeId = selectedNode?.id || focusNodeId || '';
  const denseTopologyMode = edgeCount >= DENSE_TOPOLOGY_EDGE_THRESHOLD || nodeCount >= 120;
  const showCompactEdgeLabel = (!denseTopologyMode && visibleEdges.length <= 16) || !!selectedEdgeId || !!selectedPathId;

  const edgeRenderData = useMemo(() => {
    if (!visibleEdges.length) {
      return [] as EdgeRenderDatum[];
    }

    const denseMode = visibleEdges.length >= DENSE_TOPOLOGY_EDGE_THRESHOLD || visibleNodes.length >= 120;
    const heavyMode = visibleEdges.length >= HEAVY_TOPOLOGY_EDGE_THRESHOLD;
    const edgeUidByRef = new WeakMap<object, string>();
    const edgeByUid = new Map<string, any>();
    const groups = new Map<string, any[]>();

    visibleEdges.forEach((edge: any, index: number) => {
      const uid = resolveEdgeUid(edge, index);
      edgeUidByRef.set(edge as object, uid);
      edgeByUid.set(uid, edge);
      const rawId = safeText(edge.id);
      if (rawId) {
        edgeByUid.set(rawId, edge);
      }
      const key = edgePairKey(edge);
      if (!groups.has(key)) {
        groups.set(key, []);
      }
      groups.get(key)?.push(edge);
    });

    const expandedPairKeys = new Set<string>();
    const globalExpandBundles = !denseMode && zoom >= 1.15;
    if (globalExpandBundles) {
      groups.forEach((_, key) => expandedPairKeys.add(key));
    }
    if (selectedEdgeId) {
      const found = edgeByUid.get(selectedEdgeId);
      if (found) {
        expandedPairKeys.add(edgePairKey(found));
      }
    }
    selectedPathEdgeIds.forEach((edgeId) => {
      const found = edgeByUid.get(String(edgeId));
      if (found) {
        expandedPairKeys.add(edgePairKey(found));
      }
    });
    if (activeFocusNodeId) {
      visibleEdges.forEach((edge: any) => {
        if (edge.source === activeFocusNodeId || edge.target === activeFocusNodeId) {
          expandedPairKeys.add(edgePairKey(edge));
        }
      });
    }

    const bundleMetaByUid = new Map<string, EdgeBundleMeta>();
    groups.forEach((groupEdges, key) => {
      const sortedGroup = heavyMode
        ? groupEdges
        : [...groupEdges].sort(
            (a, b) => computeEdgeIssueScore(b) - computeEdgeIssueScore(a) || String(a.id || '').localeCompare(String(b.id || '')),
          );
      const expanded = expandedPairKeys.has(key);
      const spacing = expanded ? (sortedGroup.length >= 5 ? (denseMode ? 10 : 14) : denseMode ? 12 : 18) : denseMode ? 4 : 6;
      sortedGroup.forEach((edge, index) => {
        const uid = edgeUidByRef.get(edge as object) || resolveEdgeUid(edge, index);
        bundleMetaByUid.set(uid, {
          key,
          index,
          size: sortedGroup.length,
          expanded,
          spacing,
        });
      });
    });

    const edgePriority = (edge: any): number => {
      const uid = edgeUidByRef.get(edge as object) || safeText(edge.id);
      if (selectedEdgeId && (uid === selectedEdgeId || safeText(edge.id) === selectedEdgeId)) {
        return 0;
      }
      if (selectedPathEdgeIds.has(edge.id) || selectedPathEdgeIds.has(safeText(edge.id)) || selectedPathEdgeIds.has(uid)) {
        return 1;
      }
      if (activeFocusNodeId && (edge.source === activeFocusNodeId || edge.target === activeFocusNodeId)) {
        return 2;
      }
      return 3;
    };

    const shouldSortDetailed = !heavyMode || !!selectedEdgeId || !!selectedPathId || !!activeFocusNodeId;
    const orderedEdges = shouldSortDetailed
      ? [...visibleEdges].sort(
          (a, b) =>
            edgePriority(a) - edgePriority(b) ||
            computeEdgeIssueScore(b) - computeEdgeIssueScore(a) ||
            String(a.id || '').localeCompare(String(b.id || '')),
        )
      : visibleEdges;

    const labelBoxes: EdgeLabelBox[] = [];
    const rendered: EdgeRenderDatum[] = [];

    orderedEdges.forEach((edge: any, edgeIndex: number) => {
      const uid = edgeUidByRef.get(edge as object) || resolveEdgeUid(edge, edgeIndex);
      const meta = bundleMetaByUid.get(uid) || {
        key: edgePairKey(edge),
        index: 0,
        size: 1,
        expanded: true,
        spacing: 12,
      };

      const sourcePos = getNodePosition(edge.source);
      const targetPos = getNodePosition(edge.target);
      const x1 = sourcePos.x + LAYOUT.nodeWidth / 2;
      const y1 = sourcePos.y + LAYOUT.nodeHeight / 2;
      const x2 = targetPos.x + LAYOUT.nodeWidth / 2;
      const y2 = targetPos.y + LAYOUT.nodeHeight / 2;
      const distance = Math.hypot(x2 - x1, y2 - y1);
      const unitDistance = Math.max(distance, 1);
      const dx = x2 - x1;
      const dy = y2 - y1;
      const normalX = -dy / unitDistance;
      const normalY = dx / unitDistance;

      const bundleOffset = (meta.index - (meta.size - 1) / 2) * meta.spacing;
      const idShift = ((hashText(uid) % 5) - 2) * 2.5;
      const curveStrength = Math.min(150, Math.max(30, distance * 0.2)) + bundleOffset + idShift;

      const c1x = x1 + dx * 0.28 + normalX * curveStrength;
      const c1y = y1 + dy * 0.28 + normalY * curveStrength;
      const c2x = x1 + dx * 0.72 + normalX * curveStrength;
      const c2y = y1 + dy * 0.72 + normalY * curveStrength;
      const path = `M ${x1} ${y1} C ${c1x} ${c1y} ${c2x} ${c2y} ${x2} ${y2}`;

      const relation = getRelationshipLabel(edge?.metrics?.reason || '');
      const color = getEdgeColor(edge);
      const score = computeEdgeIssueScore(edge);
      const requestRate = toMetric(edge?.metrics?.rps ?? edge?.metrics?.call_count, 0);
      const baseWidth = Math.min(6.8, Math.max(2.2, Math.log10(requestRate + 10) * 1.75));
      const isPathEdge = selectedPathEdgeIds.has(edge.id) || selectedPathEdgeIds.has(safeText(edge.id)) || selectedPathEdgeIds.has(uid);
      const isSelectedEdge = selectedEdgeId === uid || selectedEdgeId === safeText(edge.id);
      const isFocusEdge = activeFocusNodeId ? edge.source === activeFocusNodeId || edge.target === activeFocusNodeId : false;
      const hasActiveHighlight = !!selectedPathId || !!selectedEdgeId || !!activeFocusNodeId;
      const bundleCollapsed = meta.size > 1 && !meta.expanded;

      let edgeOpacity = isPathEdge || isSelectedEdge ? 0.98 : isFocusEdge ? 0.86 : hasActiveHighlight ? 0.18 : 0.72;
      if (bundleCollapsed && !isPathEdge && !isSelectedEdge) {
        edgeOpacity = Math.min(edgeOpacity, meta.index === Math.floor((meta.size - 1) / 2) ? 0.6 : 0.48);
      }

      const edgeWidth = isPathEdge || isSelectedEdge ? baseWidth + 1.8 : isFocusEdge ? baseWidth + 0.9 : baseWidth;
      const labelTextColor = color.severity === 'danger' ? '#fecdd3' : color.severity === 'warning' ? '#fde68a' : '#bae6fd';
      const labelStroke = isPathEdge || isSelectedEdge ? '#e2e8f0' : color.stroke;
      const isLabelOwner = meta.size <= 1 || meta.expanded || meta.index === Math.floor((meta.size - 1) / 2);
      const labelEligible = !denseMode || isPathEdge || isSelectedEdge || isFocusEdge;
      const labelVisible = labelEligible && isLabelOwner && (showCompactEdgeLabel || isPathEdge || isSelectedEdge || bundleCollapsed);
      const labelTitle = bundleCollapsed ? `${relation.label} ×${meta.size}` : relation.label;

      let labelX = (x1 + 3 * c1x + 3 * c2x + x2) / 8 + normalX * bundleOffset * 0.2;
      let labelY = (y1 + 3 * c1y + 3 * c2y + y2) / 8 + normalY * bundleOffset * 0.2;

      if (labelVisible) {
        const candidates = denseMode ? [0] : [0, 28, -28, 54, -54, 82, -82];
        let placed = false;
        for (const offset of candidates) {
          const candidateX = (x1 + 3 * c1x + 3 * c2x + x2) / 8 + normalX * (bundleOffset * 0.2 + offset);
          const candidateY = (y1 + 3 * c1y + 3 * c2y + y2) / 8 + normalY * (bundleOffset * 0.2 + offset);
          const box: EdgeLabelBox = {
            x1: candidateX - 90,
            y1: candidateY - 24,
            x2: candidateX + 90,
            y2: candidateY + 10,
          };

          if (box.x1 < 8 || box.x2 > canvasSize.width - 8 || box.y1 < 8 || box.y2 > canvasSize.height - 8) {
            continue;
          }
          const overlapped = labelBoxes.some((existing) => {
            const overlapXPadding = denseMode ? 4 : 10;
            const overlapYPadding = denseMode ? 4 : 8;
            return !(
              box.x2 + overlapXPadding < existing.x1 ||
              box.x1 > existing.x2 + overlapXPadding ||
              box.y2 + overlapYPadding < existing.y1 ||
              box.y1 > existing.y2 + overlapYPadding
            );
          });
          if (!overlapped) {
            labelX = candidateX;
            labelY = candidateY;
            labelBoxes.push(box);
            placed = true;
            break;
          }
        }
        if (!placed) {
          labelBoxes.push({
            x1: labelX - 90,
            y1: labelY - 24,
            x2: labelX + 90,
            y2: labelY + 10,
          });
        }
      }

      const flowDotCount = denseMode
        ? (isPathEdge || isSelectedEdge ? 1 : 0)
        : (isPathEdge || isSelectedEdge ? 2 : edgeOpacity > 0.58 ? 1 : 0);
      const flowDuration = Math.max(1.7, Math.min(6.8, 6.3 - Math.log10(requestRate + 1) * 1.05));

      rendered.push({
        uid,
        edge,
        edgeIndex,
        path,
        labelX,
        labelY,
        labelVisible,
        labelTitle,
        labelStroke,
        labelTextColor,
        score,
        edgeOpacity,
        edgeWidth,
        flowDotCount,
        flowDuration,
        color,
      });
    });

    return rendered;
  }, [
    activeFocusNodeId,
    canvasSize.height,
    canvasSize.width,
    getNodePosition,
    selectedEdgeId,
    selectedPathEdgeIds,
    selectedPathId,
    showCompactEdgeLabel,
    visibleNodes.length,
    visibleEdges,
    zoom,
  ]);

  const edgeNarrative = useMemo(() => {
    if (!selectedEdge) {
      return '';
    }
    return formatEdgeDescription(selectedEdge);
  }, [selectedEdge]);
  const selectedEdgeDirectional = useMemo(() => resolveDirectionalContribution(selectedEdge), [selectedEdge]);

  const renderEdgePreviewMessage = useCallback((message: string, source: string, target: string) => {
    const text = String(message || '');
    const tokens = [safeText(source), safeText(target)].filter(Boolean);
    if (!tokens.length || !text) {
      return text;
    }

    const uniqueTokens = Array.from(new Set(tokens.map((item) => item.toLowerCase())));
    const pattern = new RegExp(`(${uniqueTokens.map(escapeRegExp).join('|')})`, 'ig');
    const segments = text.split(pattern);

    return segments.map((segment, idx) => {
      const normalized = segment.toLowerCase();
      const highlighted = uniqueTokens.includes(normalized);
      if (!highlighted) {
        return <React.Fragment key={`seg-${idx}`}>{segment}</React.Fragment>;
      }
      return (
        <mark key={`seg-${idx}`} className="rounded bg-cyan-400/30 px-0.5 text-cyan-100">
          {segment}
        </mark>
      );
    });
  }, []);

  const buildNodeAiPayload = useCallback((node: any) => {
    const service = resolveServiceName(node);
    const status = getNodeStatus(node);
    const level = status === 'error' ? 'ERROR' : status === 'warning' ? 'WARN' : 'INFO';

    return {
      id: `topology-node-${service}-${Date.now()}`,
      timestamp: new Date().toISOString(),
      service_name: service,
      level,
      message: [
        `Topology node health analysis for service=${service}`,
        `error_count=${toMetric(node?.metrics?.error_count, 0)}`,
        `log_count=${toMetric(node?.metrics?.log_count, 0)}`,
        `coverage=${toMetric(node?.coverage ?? node?.metrics?.coverage, 0).toFixed(3)}`,
        `quality_score=${toMetric(node?.quality_score ?? node?.metrics?.quality_score, 0).toFixed(2)}`,
      ].join(' | '),
      namespace: resolveNamespace(node),
      attributes: {
        source: 'topology-node',
        source_service: service,
        time_window: timeWindow,
        lane: resolveLane(node).label,
        node_metrics: node?.metrics || {},
      },
    };
  }, [timeWindow]);

  const buildEdgeAiPayload = useCallback((edge: any) => {
    const errorRate = toMetric(edge?.metrics?.error_rate, 0);
    const timeoutRate = toMetric(edge?.metrics?.timeout_rate ?? edge?.timeout_rate, 0);
    const p99 = toMetric(edge?.metrics?.p99 ?? edge?.p99, 0);
    const quality = toMetric(edge?.metrics?.quality_score ?? edge?.quality_score, 100);
    const risk = getRiskLevel(errorRate, timeoutRate, p99, quality);
    const level = risk === '高风险' ? 'ERROR' : risk === '中风险' ? 'WARN' : 'INFO';

    return {
      id: `topology-edge-${edge?.source}-${edge?.target}-${Date.now()}`,
      timestamp: new Date().toISOString(),
      service_name: safeText(edge?.source || 'unknown'),
      level,
      message: [
        `Topology edge anomaly ${safeText(edge?.source)} -> ${safeText(edge?.target)}`,
        `error_rate=${errorRate.toFixed(4)}`,
        `timeout_rate=${timeoutRate.toFixed(4)}`,
        `p95=${toMetric(edge?.metrics?.p95 ?? edge?.p95, 0).toFixed(1)}ms`,
        `p99=${p99.toFixed(1)}ms`,
        `quality_score=${quality.toFixed(2)}`,
        `risk=${risk}`,
      ].join(' | '),
      attributes: {
        source: 'topology-edge',
        edge_narrative: formatEdgeDescription(edge),
        edge_metrics: edge?.metrics || {},
        target_service: safeText(edge?.target || 'unknown'),
        time_window: timeWindow,
      },
    };
  }, [timeWindow]);

  if (loading) {
    return <LoadingState message="加载拓扑数据..." />;
  }

  if (error) {
    return <ErrorState message={error.message} onRetry={refetch} />;
  }

  return (
    <div ref={containerRef} className="flex h-full flex-col overflow-hidden bg-slate-950 text-slate-100">
      <div className="relative z-[220] border-b border-slate-700/60 bg-slate-950/95 px-4 py-3 backdrop-blur">
        <div className="flex flex-wrap items-center gap-2">
          <div className="mr-3">
            <h1 className="text-lg font-semibold tracking-wide text-cyan-200">服务拓扑作战视图</h1>
            <p className="text-xs text-slate-400">泳道编排 + 链路情报 + 可拖拽面板</p>
          </div>

          <select
            value={timeWindow}
            onChange={(e) => setTimeWindow(e.target.value)}
            className="rounded-lg border border-slate-600 bg-slate-900 px-3 py-1.5 text-xs text-slate-100"
          >
            {TIME_WINDOWS.map((window) => (
              <option key={window} value={window}>
                {window}
              </option>
            ))}
          </select>

          <select
            value={focusNodeId}
            onChange={(e) => setFocusNodeId(e.target.value)}
            className="max-w-[240px] rounded-lg border border-slate-600 bg-slate-900 px-3 py-1.5 text-xs text-slate-100"
          >
            <option value="">Focus: 全图</option>
            {(topologyData?.nodes || []).map((node: any) => (
              <option key={`focus-${node.id}`} value={node.id}>
                {node.label}
              </option>
            ))}
          </select>

          <div className="inline-flex items-center gap-1">
            <select
              value={focusDepth}
              onChange={(e) => setFocusDepth(Number(e.target.value) || 1)}
              className="rounded-lg border border-slate-600 bg-slate-900 px-3 py-1.5 text-xs text-slate-100"
            >
              {DEPTH_OPTIONS.map((depth) => (
                <option key={depth} value={depth}>
                  深度 {depth} 跳
                </option>
              ))}
            </select>
            <Tooltip
              title="Depth 深度"
              lines={[
                '控制焦点服务向上游/下游展开的跳数。',
                '仅在“服务下拉框”选中焦点服务后生效。',
                '深度 1 = 直接关联，深度 2/3 = 扩展到更远链路。',
              ]}
            />
          </div>

          <div className="inline-flex items-center gap-1">
            <select
              value={evidenceMode}
              onChange={(e) => setEvidenceMode((e.target.value || 'all') as EvidenceMode)}
              className="rounded-lg border border-slate-600 bg-slate-900 px-3 py-1.5 text-xs text-slate-100"
            >
              <option value="all">证据：全部</option>
              <option value="observed">证据：仅观测</option>
              <option value="inferred">证据：仅推断</option>
            </select>
            <Tooltip
              title="Evidence 证据类型"
              lines={[
                '全部：同时展示观测链路与推断链路。',
                '仅观测：只显示 traces/logs 等采集到的真实关系。',
                '仅推断：只显示系统根据规则/上下文推断出的关系。',
              ]}
            />
          </div>

          <div className="inline-flex items-center gap-1">
            <select
              value={inferenceMode}
              onChange={(e) => setInferenceMode((e.target.value || 'rule') as InferenceMode)}
              className="rounded-lg border border-slate-600 bg-slate-900 px-3 py-1.5 text-xs text-slate-100"
            >
              <option value="rule">推断：规则模式</option>
              <option value="hybrid_score">推断：混合打分</option>
            </select>
            <Tooltip
              title="Inference 推断模式"
              lines={[
                '规则模式（rule）：使用稳定规则链路推断，便于快速回滚。',
                '混合打分（hybrid_score）：启用 P0 打分优化（动态阈值+时间窗候选打分）。',
                '建议先灰度观察质量指标，再决定是否全量切换。',
              ]}
            />
          </div>

          <button
            onClick={() => setMessageTargetEnabled((prev) => !prev)}
            className={`rounded-lg border px-3 py-1.5 text-xs ${
              messageTargetEnabled
                ? 'border-cyan-400 bg-cyan-500/20 text-cyan-100'
                : 'border-slate-600 bg-slate-900 text-slate-200 hover:bg-slate-800'
            }`}
            title="开启后会基于日志中的 host/upstream/proxy/rpc 信息推断链路"
          >
            MsgTarget {messageTargetEnabled ? 'ON' : 'OFF'}
          </button>

          <select
            value={messageTargetMinSupport}
            onChange={(e) => setMessageTargetMinSupport(Math.max(1, Math.min(20, Number(e.target.value) || 2)))}
            className="rounded-lg border border-slate-600 bg-slate-900 px-3 py-1.5 text-xs text-slate-100"
            title="message_target 最小支持数"
          >
            {[1, 2, 3, 4, 5, 6].map((count) => (
              <option key={`msg-min-${count}`} value={count}>
                MsgMin {count}
              </option>
            ))}
          </select>

          <select
            value={messageTargetMaxPerLog}
            onChange={(e) => setMessageTargetMaxPerLog(Math.max(1, Math.min(12, Number(e.target.value) || 3)))}
            className="rounded-lg border border-slate-600 bg-slate-900 px-3 py-1.5 text-xs text-slate-100"
            title="单条日志最多提取目标服务数"
          >
            {[1, 2, 3, 4, 5, 6].map((count) => (
              <option key={`msg-max-${count}`} value={count}>
                MsgMax {count}
              </option>
            ))}
          </select>

          <div className="flex items-center gap-1 rounded-lg border border-slate-600 bg-slate-900 px-2 py-1.5">
            {MESSAGE_TARGET_PATTERN_OPTIONS.map((option) => {
              const active = messageTargetPatterns.includes(option.key);
              return (
                <button
                  key={`msg-pattern-${option.key}`}
                  onClick={() => handleToggleMessageTargetPattern(option.key)}
                  className={`rounded px-2 py-0.5 text-[10px] ${
                    active
                      ? 'bg-cyan-500/25 text-cyan-100'
                      : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
                  }`}
                  title={`启用 ${option.label} 模式`}
                >
                  {option.label}
                </button>
              );
            })}
          </div>

          <div className="inline-flex items-center gap-1">
            <button
              onClick={() => setIsolateMode((prev) => !prev)}
              className={`rounded-lg border px-3 py-1.5 text-xs ${
                isolateMode
                  ? 'border-emerald-400 bg-emerald-500/20 text-emerald-200'
                  : 'border-slate-600 bg-slate-900 text-slate-200 hover:bg-slate-800'
              }`}
            >
              邻域隔离 {isolateMode ? 'ON' : 'OFF'}
            </button>
            <Tooltip
              title="Isolate 邻域隔离"
              lines={[
                'ON：只保留焦点服务及其直接关联链路，便于局部排障。',
                'OFF：显示完整拓扑，不主动隐藏无关服务卡片。',
                '是否隔离仅对“服务下拉框”选中的焦点服务生效，点击卡片不会触发隐藏。',
              ]}
            />
          </div>

          <div className="ml-auto flex items-center gap-2">
            <div className="flex items-center gap-1 rounded-lg border border-slate-700 bg-slate-900 px-1 py-1">
              <button
                onClick={() => setLayoutMode('swimlane')}
                className={`rounded p-1 ${layoutMode === 'swimlane' ? 'bg-cyan-500 text-slate-900' : 'text-slate-300 hover:bg-slate-800'}`}
                title="泳道布局"
              >
                <Network className="h-4 w-4" />
              </button>
              <button
                onClick={() => setLayoutMode('grid')}
                className={`rounded p-1 ${layoutMode === 'grid' ? 'bg-cyan-500 text-slate-900' : 'text-slate-300 hover:bg-slate-800'}`}
                title="网格布局"
              >
                <LayoutGrid className="h-4 w-4" />
              </button>
              <button
                onClick={() => setLayoutMode('free')}
                className={`rounded p-1 ${layoutMode === 'free' ? 'bg-cyan-500 text-slate-900' : 'text-slate-300 hover:bg-slate-800'}`}
                title="自由布局"
              >
                <GripHorizontal className="h-4 w-4" />
              </button>
            </div>

            <div className="flex items-center gap-1 rounded-lg border border-slate-700 bg-slate-900 px-2 py-1">
              <button onClick={() => setZoom((z) => Math.max(0.35, z - 0.15))} className="rounded p-1 text-slate-300 hover:bg-slate-800">
                <ZoomOut className="h-4 w-4" />
              </button>
              <span className="w-14 text-center text-xs text-slate-300">{Math.round(zoom * 100)}%</span>
              <button onClick={() => setZoom((z) => Math.min(2.8, z + 0.15))} className="rounded p-1 text-slate-300 hover:bg-slate-800">
                <ZoomIn className="h-4 w-4" />
              </button>
            </div>

            <button onClick={exportTopology} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-800">
              <Download className="mr-1 inline h-3.5 w-3.5" />导出
            </button>
            <button onClick={refetch} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-800">
              <RefreshCw className="mr-1 inline h-3.5 w-3.5" />刷新
            </button>
            <button onClick={toggleFullscreen} className="rounded-lg border border-slate-700 bg-slate-900 px-2 py-1.5 text-slate-200 hover:bg-slate-800">
              {isFullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
            </button>
          </div>
        </div>
      </div>

      <div className="relative flex-1 overflow-hidden">
        <div
          ref={canvasRef}
          className={`h-full w-full overflow-hidden ${isPanning || draggingNode ? 'cursor-grabbing' : 'cursor-grab'}`}
          onMouseDown={handleCanvasMouseDown}
          onMouseMove={handleCanvasMouseMove}
          onMouseUp={stopCanvasInteractions}
          onMouseLeave={stopCanvasInteractions}
          onWheel={handleWheel}
          style={{
            backgroundColor: '#020617',
            backgroundImage:
              'radial-gradient(circle at 20% 15%, rgba(8,47,73,0.35) 0, rgba(8,47,73,0) 32%), radial-gradient(circle at 80% 0%, rgba(76,29,149,0.24) 0, rgba(76,29,149,0) 30%), linear-gradient(rgba(15,23,42,0.65) 1px, transparent 1px), linear-gradient(90deg, rgba(15,23,42,0.65) 1px, transparent 1px)',
            backgroundSize: 'auto, auto, 24px 24px, 24px 24px',
          }}
        >
          {visibleNodes.length > 0 ? (
            <div
              className="relative"
              style={{
                width: canvasSize.width,
                height: canvasSize.height,
                transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
                transformOrigin: '0 0',
              }}
            >
              <svg className="pointer-events-none absolute inset-0" style={{ width: canvasSize.width, height: canvasSize.height }}>
                <defs>
                  <marker id="arrow-observed" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                    <path d="M0,0 L8,4 L0,8 L2,4 Z" fill="#38bdf8" />
                  </marker>
                  <marker id="arrow-inferred" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                    <path d="M0,0 L8,4 L0,8 L2,4 Z" fill="#a78bfa" />
                  </marker>
                  <marker id="arrow-warning" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                    <path d="M0,0 L8,4 L0,8 L2,4 Z" fill="#fbbf24" />
                  </marker>
                  <marker id="arrow-danger" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                    <path d="M0,0 L8,4 L0,8 L2,4 Z" fill="#fb7185" />
                  </marker>
                </defs>

                {laneBands.map((lane, index) => (
                  <g key={`lane-${lane.key}`}>
                    <rect
                      x={lane.x}
                      y={lane.y}
                      width={lane.width}
                      height={lane.height}
                      rx={20}
                      fill="rgba(15,23,42,0.55)"
                      stroke="rgba(148,163,184,0.22)"
                      strokeDasharray="8 6"
                    />
                    <text x={lane.x + 18} y={lane.y + 24} fill="#cbd5e1" fontSize="12" fontWeight="600">
                      {lane.label}
                    </text>
                    <rect
                      x={lane.x + 12}
                      y={lane.y + 10}
                      width={6}
                      height={lane.height - 20}
                      rx={3}
                      fill={LANE_COLORS[index % LANE_COLORS.length]}
                      opacity={0.7}
                    />
                  </g>
                ))}

                {edgeRenderData.map((item) => {
                  const { uid, edge, path, color, edgeWidth, edgeOpacity, flowDotCount, labelVisible, labelX, labelY, labelStroke, labelTextColor, score, flowDuration } =
                    item;
                  return (
                    <g key={`edge-vis-${uid}`}>
                      <path d={path} stroke={color.stroke} strokeWidth={edgeWidth + 4} fill="none" opacity={Math.max(0.1, edgeOpacity * 0.22)} />
                      <path d={path} stroke={color.stroke} strokeWidth={edgeWidth} fill="none" opacity={edgeOpacity} markerEnd={`url(#${color.marker})`} />
                      {flowDotCount > 0 &&
                        Array.from({ length: flowDotCount }).map((_, dotIndex) => (
                          <circle
                            key={`flow-dot-${uid}-${dotIndex}`}
                            r={dotIndex === 0 ? 2.7 : 2.2}
                            fill={selectedEdgeId === safeText(edge.id) || selectedEdgeId === uid ? '#ffffff' : color.stroke}
                            opacity={dotIndex === 0 ? 0.95 : 0.76}
                          >
                            <animateMotion
                              path={path}
                              dur={`${(flowDuration + dotIndex * 0.42).toFixed(2)}s`}
                              repeatCount="indefinite"
                              rotate="auto"
                              begin={`${(item.edgeIndex * 0.17 + dotIndex * 0.23).toFixed(2)}s`}
                            />
                          </circle>
                        ))}
                      {labelVisible && (
                        <g>
                          <rect
                            x={labelX - 90}
                            y={labelY - 24}
                            width={180}
                            height={34}
                            rx={8}
                            fill="rgba(2,6,23,0.92)"
                            stroke={labelStroke}
                            strokeWidth={1.2}
                          />
                          <text x={labelX} y={labelY - 10} textAnchor="middle" fill={labelTextColor} fontSize="10" fontWeight="700">
                            {item.labelTitle}
                          </text>
                          <text x={labelX} y={labelY + 3} textAnchor="middle" fill="#cbd5e1" fontSize="9" fontWeight="500">
                            err {toPct(edge?.metrics?.error_rate)} | p99 {toNum(edge?.metrics?.p99 ?? edge?.p99, 0)}ms | qos {toNum(
                              edge?.metrics?.quality_score ?? edge?.quality_score,
                              0,
                            )}
                            {' '}| score {score}
                          </text>
                        </g>
                      )}
                    </g>
                  );
                })}
              </svg>

              {edgeRenderData.map((item) => {
                const { uid, edge, path } = item;
                return (
                  <svg key={`edge-hit-${uid}`} className="absolute inset-0" style={{ width: canvasSize.width, height: canvasSize.height }}>
                    <path
                      d={path}
                      stroke="transparent"
                      strokeWidth={16}
                      fill="none"
                      className="cursor-pointer"
                      onMouseEnter={(e) => {
                        e.stopPropagation();
                        showEdgeHoverCard(e, edge);
                      }}
                      onMouseMove={(e) => {
                        e.stopPropagation();
                        showEdgeHoverCard(e, edge);
                      }}
                      onMouseLeave={scheduleHoverCardHide}
                      onClick={(e) => {
                        e.stopPropagation();
                        handleEdgeClick(edge);
                      }}
                    />
                  </svg>
                );
              })}

              {visibleNodes.map((node: any) => {
                const pos = getNodePosition(node.id);
                const palette = getNodePalette(node, timeWindow);
                const isSelected = selectedNode?.id === node.id;
                const isHighlighted = !!highlightedService && (node.id === highlightedService || node.label === highlightedService);
                const isPathNode = selectedPathNodeIds.has(node.id);
                const status = getNodeStatus(node);
                const dimByPath = !!selectedPathId && !isPathNode;

                return (
                  <div
                    key={node.id}
                    onMouseDown={(e) => handleNodeMouseDown(e, node)}
                    onMouseEnter={(e) => showNodeHoverCard(e, node)}
                    onMouseMove={(e) => showNodeHoverCard(e, node)}
                    onMouseLeave={scheduleHoverCardHide}
                    onClick={(e) => {
                      e.stopPropagation();
                      handleNodeClick(node);
                    }}
                    className={`absolute rounded-2xl px-3 py-2 text-slate-100 transition ${palette.ring} ${
                      isSelected ? 'scale-[1.04] ring-2 ring-cyan-300' : 'hover:scale-[1.02]'
                    } ${isHighlighted && !isSelected ? 'ring-2 ring-emerald-300' : ''} ${isPathNode && !isSelected ? 'ring-2 ring-violet-300' : ''}`}
                    style={{
                      left: pos.x,
                      top: pos.y,
                      width: LAYOUT.nodeWidth,
                      height: LAYOUT.nodeHeight,
                      background: `linear-gradient(135deg, ${palette.from} 0%, ${palette.to} 100%)`,
                      opacity: dimByPath ? 0.36 : 1,
                    }}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-semibold tracking-wide">{node.label}</div>
                        <div className="truncate text-[10px] text-slate-200/80">{resolveLane(node).label}</div>
                      </div>
                      <span className={`mt-0.5 inline-block h-2.5 w-2.5 rounded-full ${palette.statusDot} ${status !== 'normal' ? 'animate-pulse' : ''}`} />
                    </div>
                    <div className="mt-2 grid grid-cols-3 gap-1 text-[10px]">
                      <div className="rounded bg-slate-950/35 px-1.5 py-1 text-center">log {node?.metrics?.log_count ?? 0}</div>
                      <div className="rounded bg-slate-950/35 px-1.5 py-1 text-center">err {node?.metrics?.error_count ?? 0}</div>
                      <div className="rounded bg-slate-950/35 px-1.5 py-1 text-center">cov {Math.round(Number(node?.coverage ?? node?.metrics?.coverage ?? 0) * 100)}%</div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="flex h-full flex-col items-center justify-center">
              <AlertCircle className="mb-4 h-12 w-12 text-slate-500" />
              <p className="text-sm text-slate-300">当前时间窗口暂无可展示拓扑数据。</p>
              <p className="mt-1 text-xs text-slate-500">可尝试扩大时间窗口或检查采集链路。</p>
              <button onClick={refetch} className="mt-4 rounded-lg border border-slate-600 bg-slate-900 px-3 py-2 text-xs text-slate-100 hover:bg-slate-800">
                <RefreshCw className="mr-1 inline h-3.5 w-3.5" />重试
              </button>
            </div>
          )}
        </div>

        {hoverCard && (
          <div className="pointer-events-none absolute inset-0 z-20">
            <div
              className="pointer-events-auto absolute rounded-xl border border-cyan-400/40 bg-slate-950/96 p-3 text-[11px] text-slate-100 shadow-[0_0_28px_rgba(56,189,248,0.24)] backdrop-blur"
              style={{ left: hoverCardPosition.left, top: hoverCardPosition.top, width: hoverCardPosition.width }}
              onMouseEnter={clearHoverHideTimer}
              onMouseLeave={scheduleHoverCardHide}
            >
              {hoverCard.kind === 'node' && hoverCard.node && (
                <>
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-semibold text-cyan-200">{resolveServiceName(hoverCard.node)}</div>
                      <div className="mt-0.5 truncate text-[10px] text-slate-400">
                        {resolveNamespace(hoverCard.node)} | {resolveLane(hoverCard.node).label}
                      </div>
                    </div>
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] ${
                        getNodeStatus(hoverCard.node) === 'error'
                          ? 'bg-rose-500/20 text-rose-200'
                          : getNodeStatus(hoverCard.node) === 'warning'
                            ? 'bg-amber-500/20 text-amber-200'
                            : 'bg-emerald-500/20 text-emerald-200'
                      }`}
                    >
                      {getNodeStatus(hoverCard.node) === 'error' ? '异常' : getNodeStatus(hoverCard.node) === 'warning' ? '预警' : '正常'}
                    </span>
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-1.5 text-[10px]">
                    <div className="rounded border border-slate-700 bg-slate-900/70 px-1.5 py-1">日志 {hoverCard.node?.metrics?.log_count ?? 0}</div>
                    <div className="rounded border border-slate-700 bg-slate-900/70 px-1.5 py-1 text-rose-300">错误 {hoverCard.node?.metrics?.error_count ?? 0}</div>
                    <div className="rounded border border-slate-700 bg-slate-900/70 px-1.5 py-1">
                      覆盖率 {Math.round(Number(hoverCard.node?.coverage ?? hoverCard.node?.metrics?.coverage ?? 0) * 100)}%
                    </div>
                    <div className="rounded border border-slate-700 bg-slate-900/70 px-1.5 py-1">
                      质量分 {toNum(hoverCard.node?.quality_score ?? hoverCard.node?.metrics?.quality_score, 1)}
                    </div>
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-1.5">
                    <button
                      onClick={() => navigation.goToLogs({ serviceName: resolveServiceName(hoverCard.node) })}
                      className="rounded border border-slate-700 bg-slate-900/65 px-2 py-1 text-left text-[10px] hover:bg-slate-800"
                    >
                      <FileText className="mr-1 inline h-3.5 w-3.5 text-sky-300" />
                      查看日志
                    </button>
                    <button
                      onClick={() => navigation.goToAIAnalysis({ logData: buildNodeAiPayload(hoverCard.node) })}
                      className="rounded border border-slate-700 bg-slate-900/65 px-2 py-1 text-left text-[10px] hover:bg-slate-800"
                    >
                      <BrainCircuit className="mr-1 inline h-3.5 w-3.5 text-fuchsia-300" />
                      AI 分析
                    </button>
                  </div>
                </>
              )}

              {hoverCard.kind === 'edge' && hoverCard.edge && (
                <>
                  <div className="flex items-center justify-between gap-2">
                    <div className="truncate text-sm font-semibold text-cyan-200">
                      {safeText(hoverCard.edge.source)} → {safeText(hoverCard.edge.target)}
                    </div>
                    {(() => {
                      const risk = getRiskLevel(
                        toMetric(hoverCard.edge?.metrics?.error_rate, 0),
                        toMetric(hoverCard.edge?.metrics?.timeout_rate ?? hoverCard.edge?.timeout_rate, 0),
                        toMetric(hoverCard.edge?.metrics?.p99 ?? hoverCard.edge?.p99, 0),
                        toMetric(hoverCard.edge?.metrics?.quality_score ?? hoverCard.edge?.quality_score, 100),
                      );
                      return (
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] ${
                            risk === '高风险' ? 'bg-rose-500/20 text-rose-200' : risk === '中风险' ? 'bg-amber-500/20 text-amber-200' : 'bg-emerald-500/20 text-emerald-200'
                          }`}
                        >
                          {risk}
                        </span>
                      );
                    })()}
                  </div>
                  <div className="mt-1 text-[10px] text-slate-400">
                    {getRelationshipLabel(hoverCard.edge?.metrics?.reason || '').label} ·{' '}
                    {safeText(hoverCard.edge?.metrics?.evidence_type || hoverCard.edge?.evidence_type || 'observed')}
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-1.5 text-[10px]">
                    <div className="rounded border border-slate-700 bg-slate-900/70 px-1.5 py-1 text-rose-300">
                      错误率 {toPct(hoverCard.edge?.metrics?.error_rate)}
                    </div>
                    <div className="rounded border border-slate-700 bg-slate-900/70 px-1.5 py-1 text-amber-300">
                      超时率 {toPct(hoverCard.edge?.metrics?.timeout_rate ?? hoverCard.edge?.timeout_rate)}
                    </div>
                    <div className="rounded border border-slate-700 bg-slate-900/70 px-1.5 py-1">
                      P95/P99 {toNum(hoverCard.edge?.metrics?.p95 ?? hoverCard.edge?.p95, 0)}/{toNum(hoverCard.edge?.metrics?.p99 ?? hoverCard.edge?.p99, 0)}ms
                    </div>
                    <div className="rounded border border-slate-700 bg-slate-900/70 px-1.5 py-1">
                      质量分 {toNum(hoverCard.edge?.metrics?.quality_score ?? hoverCard.edge?.quality_score, 1)}
                    </div>
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-1.5">
                    <button
                      onClick={() =>
                        navigation.goToLogs({
                          serviceName: safeText(hoverCard.edge?.source),
                          search: safeText(hoverCard.edge?.target),
                          sourceService: safeText(hoverCard.edge?.source),
                          targetService: safeText(hoverCard.edge?.target),
                          timeWindow,
                        })
                      }
                      className="rounded border border-slate-700 bg-slate-900/65 px-2 py-1 text-left text-[10px] hover:bg-slate-800"
                    >
                      <FileText className="mr-1 inline h-3.5 w-3.5 text-sky-300" />
                      查看日志
                    </button>
                    <button
                      onClick={() => navigation.goToAIAnalysis({ logData: buildEdgeAiPayload(hoverCard.edge) })}
                      className="rounded border border-slate-700 bg-slate-900/65 px-2 py-1 text-left text-[10px] hover:bg-slate-800"
                    >
                      <BrainCircuit className="mr-1 inline h-3.5 w-3.5 text-fuchsia-300" />
                      AI 分析
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        )}

        <div className="pointer-events-none absolute inset-0">
          <div
            data-floating-panel
            className="pointer-events-auto absolute w-[340px] rounded-2xl border border-cyan-500/30 bg-slate-900/85 shadow-[0_0_38px_rgba(56,189,248,0.18)] backdrop-blur"
            style={{ left: panelPositions.control.x, top: panelPositions.control.y }}
          >
            <div
              className="flex cursor-move items-center justify-between border-b border-slate-700 px-3 py-2"
              onMouseDown={(e) => startPanelDrag('control', e)}
            >
              <div className="flex items-center gap-2 text-xs font-semibold text-cyan-200">
                <GripHorizontal className="h-3.5 w-3.5" /> 拓扑态势面板
              </div>
              <button
                onClick={() => setShowChangeOverlay((prev) => !prev)}
                className={`rounded border px-2 py-0.5 text-[10px] ${
                  showChangeOverlay ? 'border-emerald-400 text-emerald-200' : 'border-slate-600 text-slate-300'
                }`}
              >
                变更叠加 {showChangeOverlay ? 'ON' : 'OFF'}
              </button>
            </div>

            <div className="space-y-3 p-3 text-xs text-slate-200">
              <div className="grid grid-cols-4 gap-2">
                <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2 text-center">
                  <div className="text-[10px] text-slate-400">节点</div>
                  <div className="mt-1 text-lg font-semibold text-cyan-200">{nodeCount}</div>
                </div>
                <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2 text-center">
                  <div className="text-[10px] text-slate-400">边</div>
                  <div className="mt-1 text-lg font-semibold text-cyan-200">{edgeCount}</div>
                </div>
                <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2 text-center">
                  <div className="text-[10px] text-slate-400">泳道</div>
                  <div className="mt-1 text-lg font-semibold text-cyan-200">{laneCount}</div>
                </div>
                <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2 text-center">
                  <div className="text-[10px] text-slate-400">状态</div>
                  <div className={`mt-1 text-sm font-semibold ${realtimeConnected ? 'text-emerald-300' : 'text-amber-300'}`}>
                    {realtimeConnected ? '在线' : '离线'}
                  </div>
                </div>
              </div>

              <div className="rounded-lg border border-slate-700 bg-slate-950/55 p-2 text-[11px]">
                <div className="mb-1 text-slate-400">渲染性能</div>
                <div>过滤耗时: <span className="text-cyan-200">{filteredTopology.costMs}ms</span></div>
                <div>问题节点/链路: <span className="text-rose-300">{issueSummary.unhealthyNodes}/{issueSummary.unhealthyEdges}</span></div>
                <div className="text-slate-400">
                  高风险: 节点 {issueSummary.highRiskNodes} / 链路 {issueSummary.highRiskEdges}
                </div>
                <div className="text-slate-400">
                  中风险: 节点 {issueSummary.mediumRiskNodes} / 链路 {issueSummary.mediumRiskEdges}
                </div>
                <div className="mt-1">全量视图: {filteredTopology.baseNodeCount} 节点 / {filteredTopology.baseEdgeCount} 边</div>
                {statsData && (
                  <div className="mt-1 text-slate-400">统计接口: 已接入</div>
                )}
              </div>

              <div className="rounded-lg border border-slate-700 bg-slate-950/55 p-2 text-[10px] text-slate-300">
                <div className="mb-1 text-slate-400">连线图例</div>
                <div className="flex items-center gap-2">
                  <span className="h-0.5 w-7 bg-cyan-400" /> 正常观测链路
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <span className="h-0.5 w-7 bg-violet-400" /> 推断链路（规则推断）
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <span className="h-0.5 w-7 bg-amber-300" /> 预警链路（错误率/超时率偏高）
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <span className="h-0.5 w-7 bg-rose-400" /> 高风险链路
                </div>
                <div className="mt-1 text-slate-500">全部采用实线；颜色表示语义类型。线越粗代表流量越大；流动小圆点表示实时数据流向。</div>
                <div className="mt-1 text-slate-500">同源同目标链路会自动捆绑；选中链路/路径或放大后自动展开。</div>
              </div>

              {showChangeOverlay && (
                <div className="rounded-lg border border-slate-700 bg-slate-950/55 p-2">
                  <div className="mb-1 text-[11px] font-medium text-slate-300">变更事件叠加</div>
                  {changeOverlayEvents.length ? (
                    <div className="space-y-1">
                      {changeOverlayEvents.map((event) => (
                        <button
                          key={`evt-${event.id}`}
                          onClick={() => navigation.goToLogs({ serviceName: event.service_name })}
                          className="block w-full truncate rounded border border-slate-700 px-2 py-1 text-left text-[10px] text-slate-300 hover:bg-slate-800"
                          title={event.message}
                        >
                          <span className="mr-1 text-cyan-300">{event.service_name}</span>
                          <span>{event.message}</span>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <div className="text-[10px] text-slate-500">当前窗口无 deployment/release 事件。</div>
                  )}
                </div>
              )}
            </div>
          </div>

          <div
            data-floating-panel
            className="pointer-events-auto absolute w-[380px] rounded-2xl border border-violet-500/30 bg-slate-900/85 shadow-[0_0_38px_rgba(139,92,246,0.18)] backdrop-blur"
            style={{ left: panelPositions.issues.x, top: panelPositions.issues.y }}
          >
            <div className="flex cursor-move items-center justify-between border-b border-slate-700 px-3 py-2" onMouseDown={(e) => startPanelDrag('issues', e)}>
              <div className="flex items-center gap-2 text-xs font-semibold text-violet-200">
                <GripHorizontal className="h-3.5 w-3.5" /> 链路情报看板
              </div>
              <select
                value={edgeSortMode}
                onChange={(e) => setEdgeSortMode((e.target.value || 'anomaly') as EdgeSortMode)}
                className="rounded border border-slate-600 bg-slate-900 px-2 py-1 text-[10px] text-slate-200"
              >
                <option value="anomaly">综合</option>
                <option value="error_rate">错误率</option>
                <option value="timeout_rate">超时率</option>
                <option value="p99">P99</option>
              </select>
            </div>

            <div className="max-h-[340px] space-y-1 overflow-auto p-3">
              {topProblemEdges.length ? (
                topProblemEdges.slice(0, 7).map((edge: any) => (
                  <button
                    key={`problem-${edge.id}`}
                    onClick={() => handleEdgeClick(edge)}
                    className="block w-full rounded-lg border border-slate-700 bg-slate-950/50 px-2 py-2 text-left text-xs text-slate-200 hover:bg-slate-800"
                  >
                    <div className="truncate font-medium text-cyan-200">
                      {edge.source} → {edge.target}
                    </div>
                    <div className="mt-1 grid grid-cols-4 gap-1 text-[10px] text-slate-400">
                      <span>err {toPct(edge?.metrics?.error_rate)}</span>
                      <span>p99 {toNum(edge?.metrics?.p99 ?? edge?.p99, 0)}ms</span>
                      <span>to {toPct(edge?.metrics?.timeout_rate ?? edge?.timeout_rate)}</span>
                      <span className="text-rose-300">score {edge.issueScore}</span>
                    </div>
                  </button>
                ))
              ) : (
                <div className="rounded-lg border border-slate-700 bg-slate-950/60 px-3 py-4 text-center text-xs text-slate-500">
                  当前视图无问题链路。
                </div>
              )}
            </div>
          </div>

          {(selectedNode || selectedEdge) && (
            <div
              data-floating-panel
              className="pointer-events-auto absolute w-[370px] rounded-2xl border border-emerald-500/30 bg-slate-900/90 shadow-[0_0_40px_rgba(16,185,129,0.18)] backdrop-blur"
              style={{ left: panelPositions.detail.x, top: panelPositions.detail.y }}
            >
              <div className="flex cursor-move items-center justify-between border-b border-slate-700 px-3 py-2" onMouseDown={(e) => startPanelDrag('detail', e)}>
                <div className="flex items-center gap-2 text-xs font-semibold text-emerald-200">
                  <GripHorizontal className="h-3.5 w-3.5" />
                  {selectedNode ? '服务节点详情' : '链路详情'}
                </div>
                <button
                  onClick={() => {
                    setSelectedNode(null);
                    setSelectedEdge(null);
                  }}
                  className="rounded p-1 text-slate-300 hover:bg-slate-800"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>

              <div className="max-h-[72vh] space-y-3 overflow-auto p-3 text-xs text-slate-200">
                {selectedNode && (
                  <>
                    <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-3">
                      <div className="text-[10px] text-slate-400">服务名称</div>
                      <div className="mt-1 text-sm font-semibold text-cyan-200">{selectedNode.label}</div>
                      <div className="mt-1 flex items-center gap-2 text-[11px]">
                        <span className="rounded border border-slate-700 bg-slate-900/70 px-1.5 py-0.5 text-slate-300">
                          Namespace: {resolveNamespace(selectedNode)}
                        </span>
                        <span className="text-slate-400">{resolveLane(selectedNode).label}</span>
                      </div>
                    </div>

                    {(() => {
                      const nodeProblemSummary = resolveNodeProblemSummary(selectedNode);
                      return (
                        <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-3">
                          <div className="flex items-center justify-between">
                            <div className="text-[10px] text-slate-400">节点问题摘要（TS-02）</div>
                            <span
                              className={`rounded px-1.5 py-0.5 text-[10px] ${
                                nodeProblemSummary.riskLevel === '高风险'
                                  ? 'bg-rose-500/20 text-rose-200'
                                  : nodeProblemSummary.riskLevel === '中风险'
                                    ? 'bg-amber-500/20 text-amber-200'
                                    : 'bg-emerald-500/20 text-emerald-200'
                              }`}
                            >
                              {nodeProblemSummary.riskLevel}
                            </span>
                          </div>
                          <div className="mt-1 text-[11px] text-slate-300">
                            score {toNum(nodeProblemSummary.issueScore, 1)} · {nodeProblemSummary.headline || '当前节点无显著异常摘要。'}
                          </div>
                          {nodeProblemSummary.suggestion ? (
                            <div className="mt-1 text-[10px] text-slate-500">建议: {nodeProblemSummary.suggestion}</div>
                          ) : null}
                        </div>
                      );
                    })()}

                    <div className="grid grid-cols-2 gap-2">
                      <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2">
                        <div className="text-[10px] text-slate-400">日志数</div>
                        <div className="mt-1 text-sm font-semibold">{selectedNode?.metrics?.log_count ?? 0}</div>
                      </div>
                      <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2">
                        <div className="text-[10px] text-slate-400">错误数</div>
                        <div className="mt-1 text-sm font-semibold text-rose-300">{selectedNode?.metrics?.error_count ?? 0}</div>
                      </div>
                      <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2">
                        <div className="text-[10px] text-slate-400">覆盖率</div>
                        <div className="mt-1 text-sm font-semibold">{Math.round(Number(selectedNode?.coverage ?? selectedNode?.metrics?.coverage ?? 0) * 100)}%</div>
                      </div>
                      <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2">
                        <div className="text-[10px] text-slate-400">质量分</div>
                        <div className="mt-1 text-sm font-semibold">{toNum(selectedNode?.quality_score ?? selectedNode?.metrics?.quality_score, 1)}</div>
                      </div>
                    </div>

                    <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-3">
                      <div className="flex items-center justify-between">
                        <div className="text-[11px] font-medium text-slate-200">关联服务路径（焦点模式）</div>
                        <button
                          onClick={() => setSelectedPathId('')}
                          className="rounded border border-slate-700 px-2 py-0.5 text-[10px] text-slate-300 hover:bg-slate-800"
                        >
                          清除高亮路径
                        </button>
                      </div>
                      <div className="mt-2 flex gap-1">
                        <button
                          onClick={() => setPathViewMode('all')}
                          className={`rounded px-2 py-1 text-[10px] ${
                            pathViewMode === 'all' ? 'bg-cyan-500 text-slate-900' : 'border border-slate-700 text-slate-300 hover:bg-slate-800'
                          }`}
                        >
                          全路径
                        </button>
                        <button
                          onClick={() => setPathViewMode('upstream')}
                          className={`rounded px-2 py-1 text-[10px] ${
                            pathViewMode === 'upstream' ? 'bg-cyan-500 text-slate-900' : 'border border-slate-700 text-slate-300 hover:bg-slate-800'
                          }`}
                        >
                          上游
                        </button>
                        <button
                          onClick={() => setPathViewMode('downstream')}
                          className={`rounded px-2 py-1 text-[10px] ${
                            pathViewMode === 'downstream' ? 'bg-cyan-500 text-slate-900' : 'border border-slate-700 text-slate-300 hover:bg-slate-800'
                          }`}
                        >
                          下游
                        </button>
                      </div>
                      <div className="mt-2 grid grid-cols-2 gap-2 text-[10px]">
                        <div className="rounded border border-slate-700 bg-slate-950/60 px-2 py-1 text-slate-400">
                          上游相邻链路: <span className="font-semibold text-violet-200">{pathDirectionCounts.upstream}</span>
                        </div>
                        <div className="rounded border border-slate-700 bg-slate-950/60 px-2 py-1 text-slate-400">
                          下游相邻链路: <span className="font-semibold text-emerald-200">{pathDirectionCounts.downstream}</span>
                        </div>
                      </div>

                      <div className="mt-2 max-h-[240px] space-y-2 overflow-auto pr-1">
                        {focusPathSummaries.length ? (
                          focusPathSummaries.map((path) => (
                            <button
                              key={path.id}
                              onClick={() => {
                                setSelectedPathId(path.id);
                                setSelectedEdge(null);
                              }}
                              className={`block w-full rounded-lg border px-2 py-2 text-left text-[11px] ${
                                selectedPathId === path.id
                                  ? 'border-cyan-400 bg-cyan-500/10 text-cyan-100'
                                  : 'border-slate-700 bg-slate-950/50 text-slate-200 hover:bg-slate-800'
                              }`}
                            >
                              <div className="truncate font-medium">
                                <span className={`mr-1 inline-block rounded px-1 py-0.5 text-[10px] ${
                                  path.direction === 'upstream' ? 'bg-violet-500/25 text-violet-200' : 'bg-emerald-500/25 text-emerald-200'
                                }`}>
                                  {path.direction === 'upstream' ? '上游' : '下游'}
                                </span>
                                {path.pathText}
                              </div>
                              <div className="mt-1 grid grid-cols-3 gap-1 text-[10px] text-slate-400">
                                <span>err {toPct(path.errorRate)}</span>
                                <span>p99 {toNum(path.p99, 0)}ms</span>
                                <span>qos {toNum(path.qualityScore, 0)}</span>
                                <span>hop {path.hopCount}</span>
                                <span>req {toNum(path.requestRate, 1)}</span>
                                <span className={path.riskLevel === '高风险' ? 'text-rose-300' : path.riskLevel === '中风险' ? 'text-amber-300' : 'text-emerald-300'}>
                                  {path.riskLevel}
                                </span>
                              </div>
                              <div className="mt-1 truncate text-[10px] text-slate-500">{path.explanation}</div>
                            </button>
                          ))
                        ) : (
                          <div className="rounded border border-slate-700 bg-slate-950/60 px-2 py-3 text-center text-[11px] text-slate-500">
                            当前焦点服务暂无可枚举路径，或请提升 Depth。
                          </div>
                        )}
                      </div>
                      {selectedPath && (
                        <div className="mt-2 rounded border border-slate-700 bg-slate-950/60 p-2 text-[10px] text-slate-300">
                          <div className="text-slate-400">当前高亮路径说明</div>
                          <div className="mt-1 leading-5">{selectedPath.explanation}</div>
                          <div className="mt-2 grid grid-cols-2 gap-2">
                            <button
                              onClick={() =>
                                navigation.goToLogs({
                                  serviceName: selectedPathPeerService || resolveServiceName(selectedNode),
                                  search: resolveServiceName(selectedNode),
                                  sourceService:
                                    selectedPath.direction === 'upstream'
                                      ? selectedPathPeerService || resolveServiceName(selectedNode)
                                      : resolveServiceName(selectedNode),
                                  targetService:
                                    selectedPath.direction === 'upstream'
                                      ? resolveServiceName(selectedNode)
                                      : selectedPathPeerService || resolveServiceName(selectedNode),
                                  timeWindow,
                                })
                              }
                              className="rounded border border-slate-700 bg-slate-900/60 px-2 py-1 text-left text-[10px] hover:bg-slate-800"
                            >
                              <FileText className="mr-1 inline h-3.5 w-3.5 text-sky-300" />
                              {selectedPath.direction === 'upstream' ? '查看上游首跳日志' : '查看下游首跳日志'}
                            </button>
                            <button
                              onClick={() =>
                                navigation.goToLogs({
                                  serviceName: selectedPathTerminalService || resolveServiceName(selectedNode),
                                  search: resolveServiceName(selectedNode),
                                  timeWindow,
                                })
                              }
                              className="rounded border border-slate-700 bg-slate-900/60 px-2 py-1 text-left text-[10px] hover:bg-slate-800"
                            >
                              <FileText className="mr-1 inline h-3.5 w-3.5 text-sky-300" />
                              查看路径末端日志
                            </button>
                          </div>
                        </div>
                      )}
                    </div>

                    <div className="space-y-2 pt-1">
                      <button
                        onClick={() => navigation.goToLogs({ serviceName: resolveServiceName(selectedNode) })}
                        className="flex w-full items-center justify-between rounded-lg border border-slate-700 bg-slate-950/55 px-3 py-2 text-left hover:bg-slate-800"
                      >
                        <span className="flex items-center gap-2">
                          <FileText className="h-4 w-4 text-sky-300" /> 查看服务日志
                        </span>
                        <ExternalLink className="h-3.5 w-3.5 text-slate-400" />
                      </button>
                      <button
                        onClick={() => navigation.goToTraces({ serviceName: resolveServiceName(selectedNode), mode: 'observed' })}
                        className="flex w-full items-center justify-between rounded-lg border border-slate-700 bg-slate-950/55 px-3 py-2 text-left hover:bg-slate-800"
                      >
                        <span className="flex items-center gap-2">
                          <Network className="h-4 w-4 text-emerald-300" /> 查看服务 Traces
                        </span>
                        <ExternalLink className="h-3.5 w-3.5 text-slate-400" />
                      </button>
                      <button
                        onClick={() =>
                          navigation.goToAIAnalysis({
                            logData: buildNodeAiPayload(selectedNode),
                          })
                        }
                        className="flex w-full items-center justify-between rounded-lg border border-slate-700 bg-slate-950/55 px-3 py-2 text-left hover:bg-slate-800"
                      >
                        <span className="flex items-center gap-2">
                          <BrainCircuit className="h-4 w-4 text-fuchsia-300" /> AI 分析节点
                        </span>
                        <ExternalLink className="h-3.5 w-3.5 text-slate-400" />
                      </button>
                    </div>
                  </>
                )}

                {selectedEdge && (
                  <>
                    {(() => {
                      const edgeProblemSummary = resolveEdgeProblemSummary(selectedEdge);
                      const edgeErrorRate = toMetric(selectedEdge?.metrics?.error_rate, 0);
                      const edgeTimeoutRate = toMetric(selectedEdge?.metrics?.timeout_rate ?? selectedEdge?.timeout_rate, 0);
                      const edgeP99 = toMetric(selectedEdge?.metrics?.p99 ?? selectedEdge?.p99, 0);
                      const edgeQuality = toMetric(selectedEdge?.metrics?.quality_score ?? selectedEdge?.quality_score, 100);
                      const edgeRisk = edgeProblemSummary.riskLevel || getRiskLevel(edgeErrorRate, edgeTimeoutRate, edgeP99, edgeQuality);

                      return (
                        <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-3">
                          <div className="text-[10px] text-slate-400">链路</div>
                          <div className="mt-1 text-sm font-semibold text-cyan-200">
                            {selectedEdge.source} → {selectedEdge.target}
                          </div>
                          <div className="mt-1 flex items-center gap-2 text-[11px] text-slate-400">
                            <span>
                              {getRelationshipLabel(selectedEdge?.metrics?.reason || '').label} /{' '}
                              {safeText(selectedEdge?.metrics?.evidence_type || selectedEdge?.evidence_type || 'observed')}
                            </span>
                            <span
                              className={`rounded px-1.5 py-0.5 ${
                                edgeRisk === '高风险' ? 'bg-rose-500/20 text-rose-200' : edgeRisk === '中风险' ? 'bg-amber-500/20 text-amber-200' : 'bg-emerald-500/20 text-emerald-200'
                              }`}
                            >
                              {edgeRisk}
                            </span>
                          </div>
                          <div className="mt-1 text-[11px] text-slate-300">
                            score {toNum(edgeProblemSummary.issueScore, 1)} · {edgeProblemSummary.headline || '暂无链路问题摘要。'}
                          </div>
                          {edgeProblemSummary.suggestion ? (
                            <div className="mt-1 text-[10px] text-slate-500">建议: {edgeProblemSummary.suggestion}</div>
                          ) : null}
                        </div>
                      );
                    })()}

                    <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-3">
                      <div className="text-[10px] text-slate-400">标准化链路描述（可读模板）</div>
                      <div className="mt-2 rounded border border-slate-700 bg-slate-950/65 p-2 text-[11px] leading-5 text-slate-100">
                        {edgeNarrative}
                      </div>
                    </div>

                    <div className="grid grid-cols-2 gap-2">
                      <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2">
                        <div className="text-[10px] text-slate-400">RPS(近似)</div>
                        <div className="mt-1 text-sm font-semibold">{toNum(selectedEdge?.metrics?.rps ?? selectedEdge?.metrics?.call_count, 1)}</div>
                      </div>
                      <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2">
                        <div className="text-[10px] text-slate-400">错误率</div>
                        <div className="mt-1 text-sm font-semibold text-rose-300">{toPct(selectedEdge?.metrics?.error_rate)}</div>
                      </div>
                      <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2">
                        <div className="text-[10px] text-slate-400">P95 / P99</div>
                        <div className="mt-1 text-sm font-semibold">{toNum(selectedEdge?.metrics?.p95 ?? selectedEdge?.p95, 0)} / {toNum(selectedEdge?.metrics?.p99 ?? selectedEdge?.p99, 0)} ms</div>
                      </div>
                      <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2">
                        <div className="text-[10px] text-slate-400">超时率</div>
                        <div className="mt-1 text-sm font-semibold text-amber-300">{toPct(selectedEdge?.metrics?.timeout_rate ?? selectedEdge?.timeout_rate)}</div>
                      </div>
                      <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2">
                        <div className="text-[10px] text-slate-400">覆盖率</div>
                        <div className="mt-1 text-sm font-semibold">{Math.round(Number(selectedEdge?.metrics?.coverage ?? selectedEdge?.coverage ?? 0) * 100)}%</div>
                      </div>
                      <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-2">
                        <div className="text-[10px] text-slate-400">质量分</div>
                        <div className="mt-1 text-sm font-semibold">{toNum(selectedEdge?.metrics?.quality_score ?? selectedEdge?.quality_score, 1)}</div>
                      </div>
                    </div>

                    <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-3">
                      <div className="flex items-center justify-between text-[10px] text-slate-400">
                        <span>Direction 一致性贡献</span>
                        <span>{selectedEdgeDirectional.inferenceMode || 'rule'} mode</span>
                      </div>
                      {selectedEdgeDirectional.hasMetric ? (
                        <>
                          <div className="mt-2 grid grid-cols-2 gap-2 text-[11px] text-slate-300">
                            <div className="text-slate-400">directional_consistency</div>
                            <div className="text-right font-semibold text-cyan-200">{toPct(selectedEdgeDirectional.value)}</div>
                            <div className="text-slate-400">对 confidence 的贡献</div>
                            <div className="text-right font-semibold text-violet-200">
                              +{toNum(selectedEdgeDirectional.confidenceContribution, 3)} / +0.240
                            </div>
                            <div className="text-slate-400">对 evidence_score 的贡献</div>
                            <div className="text-right font-semibold text-emerald-200">
                              +{toNum(selectedEdgeDirectional.evidenceContribution, 2)} / +2.00
                            </div>
                          </div>
                          <div className="mt-2 space-y-2">
                            <div>
                              <div className="mb-1 text-[10px] text-slate-500">directional_consistency</div>
                              <div className="h-1.5 overflow-hidden rounded bg-slate-800">
                                <div
                                  className="h-full rounded bg-cyan-400/90 transition-all"
                                  style={{ width: `${Math.max(2, Math.min(100, selectedEdgeDirectional.value * 100))}%` }}
                                />
                              </div>
                            </div>
                            <div>
                              <div className="mb-1 text-[10px] text-slate-500">confidence 权重占比（0.24）</div>
                              <div className="h-1.5 overflow-hidden rounded bg-slate-800">
                                <div
                                  className="h-full rounded bg-violet-400/90 transition-all"
                                  style={{ width: `${Math.max(2, Math.min(100, selectedEdgeDirectional.value * 100))}%` }}
                                />
                              </div>
                            </div>
                            <div>
                              <div className="mb-1 text-[10px] text-slate-500">evidence_score 权重占比（2.0）</div>
                              <div className="h-1.5 overflow-hidden rounded bg-slate-800">
                                <div
                                  className="h-full rounded bg-emerald-400/90 transition-all"
                                  style={{ width: `${Math.max(2, Math.min(100, selectedEdgeDirectional.value * 100))}%` }}
                                />
                              </div>
                            </div>
                          </div>
                          <div className="mt-2 text-[10px] text-slate-500">
                            {selectedEdgeDirectional.inferenceMode === 'hybrid_score'
                              ? '当前边为 hybrid_score 推断链路，可直接用于灰度对比方向一致性收益。'
                              : '当前边非 hybrid_score，贡献分按同权重公式估算，仅用于横向观察。'}
                          </div>
                        </>
                      ) : (
                        <div className="mt-2 rounded border border-dashed border-slate-700 bg-slate-950/70 px-2 py-2 text-[11px] text-slate-500">
                          当前边未返回 directional_consistency（通常仅 inferred/hybrid 链路提供），暂无法展示贡献分。
                        </div>
                      )}
                    </div>

                    <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-3">
                      <div className="text-[10px] text-slate-400">链路解读</div>
                      <p className="mt-2 text-[12px] leading-5 text-slate-200">
                        这条链路表示 <span className="text-cyan-200">{selectedEdge.source}</span> 调用{' '}
                        <span className="text-cyan-200">{selectedEdge.target}</span>。当错误率和超时率抬升时，优先从源服务日志和 Trace
                        片段定位失败点，再对比目标服务近期发布变更。
                      </p>
                      <div className="mt-2 text-[10px] text-slate-500">
                        原始 reason: {safeText(selectedEdge?.metrics?.reason || 'unknown')} | data source: {safeText(selectedEdge?.metrics?.data_source || 'unknown')}
                      </div>
                    </div>

                    <div className="rounded-lg border border-slate-700 bg-slate-950/60 p-3">
                      <div className="mb-2 flex items-center justify-between">
                        <div className="text-[10px] text-slate-400">链路问题日志预览（QS-01）</div>
                        <button
                          onClick={() =>
                            navigation.goToLogs({
                              serviceName: selectedEdge.source,
                              search: safeText(selectedEdge.target),
                              sourceService: selectedEdge.source,
                              targetService: selectedEdge.target,
                              timeWindow,
                            })
                          }
                          className="rounded border border-slate-700 px-2 py-0.5 text-[10px] text-slate-300 hover:bg-slate-800"
                        >
                          查看全部
                        </button>
                      </div>
                      {edgeLogPreviewLoading ? (
                        <div className="rounded border border-slate-700 bg-slate-950/65 px-2 py-3 text-center text-[11px] text-slate-500">
                          正在加载链路关联日志...
                        </div>
                      ) : (edgeLogPreviewData?.data?.length || 0) > 0 ? (
                        <div className="max-h-[220px] space-y-1 overflow-auto pr-1">
                          {(edgeLogPreviewData?.data || []).slice(0, 6).map((log) => (
                            <button
                              key={`edge-preview-${log.id}`}
                              onClick={() =>
                                navigation.goToLogs({
                                  serviceName: log.service_name,
                                  search: safeText(selectedEdge.target),
                                  sourceService: selectedEdge.source,
                                  targetService: selectedEdge.target,
                                  timeWindow,
                                })
                              }
                              className="block w-full rounded border border-slate-700 bg-slate-950/65 px-2 py-2 text-left hover:bg-slate-800"
                            >
                              <div className="flex items-center justify-between gap-2 text-[10px]">
                                <span className="truncate text-cyan-200">{log.service_name}</span>
                                <span className={`rounded px-1.5 py-0.5 ${
                                  log.level === 'ERROR' || log.level === 'FATAL'
                                    ? 'bg-rose-500/20 text-rose-200'
                                    : log.level === 'WARN'
                                      ? 'bg-amber-500/20 text-amber-200'
                                      : 'bg-slate-700 text-slate-200'
                                }`}>
                                  {log.level}
                                </span>
                              </div>
                              <div className="mt-1 truncate text-[10px] text-slate-400">{new Date(log.timestamp).toLocaleString()}</div>
                              <div className="mt-1 line-clamp-2 text-[11px] text-slate-200">
                                {renderEdgePreviewMessage(log.message, safeText(selectedEdge.source), safeText(selectedEdge.target))}
                              </div>
                            </button>
                          ))}
                        </div>
                      ) : (
                        <div className="rounded border border-slate-700 bg-slate-950/65 px-2 py-3 text-center text-[11px] text-slate-500">
                          当前窗口暂无链路关联日志，建议扩大时间窗口后重试。
                        </div>
                      )}
                    </div>

                    <div className="space-y-2 pt-1">
                      <button
                        onClick={() =>
                          navigation.goToTraces({
                            mode: (safeText(selectedEdge?.metrics?.evidence_type || selectedEdge?.evidence_type) === 'inferred' ? 'inferred' : 'observed') as
                              | 'observed'
                              | 'inferred',
                            sourceService: selectedEdge.source,
                            targetService: selectedEdge.target,
                            serviceName: selectedEdge.source,
                          })
                        }
                        className="flex w-full items-center justify-between rounded-lg border border-slate-700 bg-slate-950/55 px-3 py-2 text-left hover:bg-slate-800"
                      >
                        <span className="flex items-center gap-2">
                          <Network className="h-4 w-4 text-emerald-300" /> 查看 Trace-Lite 片段
                        </span>
                        <ExternalLink className="h-3.5 w-3.5 text-slate-400" />
                      </button>
                      <button
                        onClick={() =>
                          navigation.goToLogs({
                            serviceName: selectedEdge.source,
                            search: safeText(selectedEdge.target),
                            sourceService: selectedEdge.source,
                            targetService: selectedEdge.target,
                            timeWindow,
                          })
                        }
                        className="flex w-full items-center justify-between rounded-lg border border-slate-700 bg-slate-950/55 px-3 py-2 text-left hover:bg-slate-800"
                      >
                        <span className="flex items-center gap-2">
                          <FileText className="h-4 w-4 text-sky-300" /> 查看源服务日志
                        </span>
                        <ExternalLink className="h-3.5 w-3.5 text-slate-400" />
                      </button>
                      <button
                        onClick={() =>
                          navigation.goToAIAnalysis({
                            logData: buildEdgeAiPayload(selectedEdge),
                          })
                        }
                        className="flex w-full items-center justify-between rounded-lg border border-slate-700 bg-slate-950/55 px-3 py-2 text-left hover:bg-slate-800"
                      >
                        <span className="flex items-center gap-2">
                          <BrainCircuit className="h-4 w-4 text-fuchsia-300" /> AI 分析链路
                        </span>
                        <ExternalLink className="h-3.5 w-3.5 text-slate-400" />
                      </button>
                    </div>
                  </>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {isFullscreen && (
        <div className="pointer-events-none absolute bottom-4 left-1/2 -translate-x-1/2 rounded-lg border border-slate-700 bg-black/60 px-4 py-2 text-xs text-slate-200">
          滚轮缩放 | 拖拽空白处平移 | 拖拽节点重排 | 拖拽面板移动
        </div>
      )}
    </div>
  );
};

export default TopologyPage;
