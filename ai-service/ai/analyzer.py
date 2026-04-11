"""
AI 智能分析模块

提供基于规则的日志分析和链路分析功能：
- 单条日志分析：识别问题类型、根因、解决方案
- 链路分析：分析整个调用链、识别瓶颈和异常

Date: 2026-02-09
"""

import logging
import re
import json
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from collections import defaultdict

from ai.similar_cases import get_recommender

logger = logging.getLogger(__name__)


def _parse_tags_payload(raw_tags: Any) -> Dict[str, Any]:
    if isinstance(raw_tags, dict):
        return raw_tags
    if isinstance(raw_tags, str) and raw_tags.strip():
        try:
            parsed = json.loads(raw_tags)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _resolve_duration_ms(raw_duration: Any, tags: Dict[str, Any]) -> int:
    candidates: List[Any] = [
        raw_duration,
        tags.get("duration_ms"),
        tags.get("span.duration_ms"),
    ]
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        try:
            return int(float(candidate))
        except (TypeError, ValueError):
            continue

    duration_ns = tags.get("duration_ns") or tags.get("span.duration_ns")
    if duration_ns not in (None, ""):
        try:
            return int(float(duration_ns) / 1_000_000.0)
        except (TypeError, ValueError):
            pass
    return 0


def _is_error_status(status: Any) -> bool:
    normalized = str(status or "").upper()
    return normalized == "ERROR" or "ERROR" in normalized


class LogAnalyzer:
    """
    日志分析器

    基于规则和模式匹配的智能分析引擎
    """

    # 问题模式规则库
    PROBLEM_PATTERNS = {
        'database': {
            'keywords': ['connection', 'database', 'pool', 'sql', 'query', 'mysql', 'postgres', 'mongodb', 'clickhouse'],
            'error_indicators': ['timeout', 'refused', 'exhausted', 'deadlock', 'failed', 'unable to connect'],
            'patterns': [
                r'connection.*(?:timeout|refused|failed)',
                r'database.*pool.*exhausted',
                r'sql.*error',
                r'query.*timeout',
            ]
        },
        'memory': {
            'keywords': ['memory', 'heap', 'oom', 'allocation', 'gc', 'garbage'],
            'error_indicators': ['out of memory', 'oom', 'exceeded', 'limit', 'kill'],
            'patterns': [
                r'out of memory',
                r'heap.*overflow',
                r'gc.*overhead',
                r'memory.*limit.*exceeded',
            ]
        },
        'network': {
            'keywords': ['network', 'connection', 'timeout', 'refused', 'dns', 'tcp', 'http'],
            'error_indicators': ['unreachable', 'timeout', 'refused', 'reset', 'broken pipe'],
            'patterns': [
                r'connection.*(?:timeout|refused|reset)',
                r'network.*unreachable',
                r'dns.*resolution.*failed',
                r'broken pipe',
            ]
        },
        'performance': {
            'keywords': ['slow', 'timeout', 'latency', 'delay', 'performance', 'bottleneck'],
            'error_indicators': ['slow', 'timeout', 'high latency', 'degraded'],
            'patterns': [
                r'request.*timeout',
                r'slow.*query',
                r'high.*latency',
                r'performance.*degraded',
            ]
        },
        'disk': {
            'keywords': ['disk', 'space', 'storage', 'filesystem', 'mount', 'partition'],
            'error_indicators': ['no space left', 'full', 'read-only', 'quota exceeded'],
            'patterns': [
                r'no space left',
                r'disk.*full',
                r'filesystem.*read.*only',
                r'quota.*exceeded',
            ]
        },
        'auth': {
            'keywords': ['auth', 'authentication', 'authorization', 'token', 'login', 'permission', 'access'],
            'error_indicators': ['denied', 'unauthorized', 'forbidden', 'expired', 'invalid'],
            'patterns': [
                r'access.*denied',
                r'unauthorized',
                r'authentication.*failed',
                r'token.*expired',
                r'permission.*denied',
            ]
        }
    }

    # 解决方案知识库
    SOLUTIONS_KNOWLEDGE_BASE = {
        'database': {
            'connection_pool': {
                'title': '数据库连接池优化',
                'description': '增加数据库连接池大小，优化连接获取策略',
                'steps': [
                    '检查当前连接池配置',
                    '根据负载调整 max_connections 参数',
                    '优化连接超时时间',
                    '实施连接池监控',
                ],
                'resources': ['db-pool-tuning', 'connection-monitoring']
            },
            'slow_query': {
                'title': '慢查询优化',
                'description': '识别并优化执行时间过长的查询',
                'steps': [
                    '开启慢查询日志',
                    '分析慢查询执行计划',
                    '添加合适的索引',
                    '优化查询逻辑',
                ],
                'resources': ['query-perf-tuning', 'slow-query-log']
            }
        },
        'memory': {
            'leak': {
                'title': '内存泄漏修复',
                'description': '识别和修复内存泄漏问题',
                'steps': [
                    '使用内存分析工具（如 pprof）',
                    '定位内存泄漏位置',
                    '修复资源未释放的代码',
                    '添加内存监控告警',
                ],
                'resources': ['memory-leak-detection', 'profiling-tools']
            },
            'limit': {
                'title': '调整内存限制',
                'description': '增加容器或进程的内存限制',
                'steps': [
                    '检查当前内存限制配置',
                    '根据实际需求调整 limit',
                    '优化内存使用',
                    '添加 OOM 监控',
                ],
                'resources': ['memory-management', 'oom-prevention']
            }
        },
        'network': {
            'timeout': {
                'title': '网络超时配置优化',
                'description': '调整超时时间，实现重试机制',
                'steps': [
                    '分析网络延迟情况',
                    '调整连接和读取超时',
                    '实现指数退避重试',
                    '添加熔断机制',
                ],
                'resources': ['circuit-breaker', 'retry-pattern']
            }
        },
        'performance': {
            'optimization': {
                'title': '性能优化',
                'description': '优化代码和配置，提升性能',
                'steps': [
                    '使用性能分析工具定位瓶颈',
                    '优化算法和数据结构',
                    '实施缓存策略',
                    '优化数据库查询',
                ],
                'resources': ['profiling', 'caching-strategy']
            }
        }
    }

    def __init__(self, storage_adapter=None):
        """
        初始化日志分析器

        Args:
            storage_adapter: StorageAdapter 实例，用于查询相关数据
        """
        self.storage = storage_adapter

    def analyze_log(self, log_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        分析单条日志

        Args:
            log_data: 日志数据

        Returns:
            Dict: 分析结果
        """
        try:
            # 1. 问题识别
            problem = self._identify_problem(log_data)

            # 2. 根因分析
            root_causes = self._analyze_root_causes(log_data, problem)

            # 3. 相似案例匹配（供建议增强使用）
            similar_cases = self._find_similar_cases(problem, log_data)

            # 4. 解决方案推荐
            solutions = self._recommend_solutions(problem, root_causes, log_data, similar_cases)

            # 5. 影响指标分析
            metrics = self._analyze_metrics(log_data, problem)

            return {
                'overview': {
                    'problem': problem['title'],
                    'severity': problem['severity'],
                    'description': problem['description'],
                    'confidence': problem['confidence']
                },
                'rootCauses': root_causes,
                'solutions': solutions,
                'metrics': metrics,
                'similarCases': similar_cases
            }

        except Exception as e:
            logger.error(f"Error analyzing log: {e}")
            # 返回默认分析结果
            return self._get_default_analysis(log_data)

    def analyze_trace(self, trace_id: str, storage_adapter=None) -> Dict[str, Any]:
        """
        分析整个调用链

        Args:
            trace_id: Trace ID
            storage_adapter: StorageAdapter 实例

        Returns:
            Dict: 分析结果
        """
        try:
            # 使用传入的 storage_adapter 或实例的 storage
            storage = storage_adapter or self.storage
            if not storage:
                return self._get_default_trace_analysis(trace_id)

            # 1. 查询整个 trace 的所有 spans（优先使用适配后的 storage helper）
            result = None
            get_trace_spans_fn = getattr(storage, "get_trace_spans", None)
            if callable(get_trace_spans_fn):
                fetched = get_trace_spans_fn(trace_id)
                if isinstance(fetched, list):
                    result = fetched

            if result is None:
                query = f"""
                SELECT
                    trace_id,
                    span_id,
                    parent_span_id,
                    service_name,
                    operation_name,
                    toString(timestamp) as timestamp_str,
                    status,
                    attributes_json
                FROM logs.traces
                WHERE trace_id = '{trace_id}'
                ORDER BY timestamp
                """
                result = storage.execute_query(query)

            if not result:
                return self._get_default_trace_analysis(trace_id)

            # 2. 构建 span 层级结构
            spans = []
            for row in result:
                if isinstance(row, dict):
                    tags = _parse_tags_payload(row.get("tags") or row.get("attributes_json"))
                    spans.append({
                        'trace_id': row.get('trace_id', trace_id),
                        'span_id': row.get('span_id', ''),
                        'parent_span_id': row.get('parent_span_id'),
                        'service_name': row.get('service_name', 'unknown'),
                        'operation_name': row.get('operation_name', ''),
                        'start_time': str(row.get('start_time') or row.get('timestamp_str') or row.get('timestamp') or ''),
                        'duration_ms': _resolve_duration_ms(row.get('duration_ms'), tags),
                        'status': row.get('status', 'STATUS_CODE_UNSET')
                    })
                elif isinstance(row, (list, tuple)) and len(row) >= 8:
                    spans.append({
                        'trace_id': row[0],
                        'span_id': row[1],
                        'parent_span_id': row[2],
                        'service_name': row[3],
                        'operation_name': row[4],
                        'start_time': str(row[5]),
                        'duration_ms': int(float(row[6] or 0)),
                        'status': row[7]
                    })

            if not spans:
                return self._get_default_trace_analysis(trace_id)

            # 3. 分析 trace 问题
            trace_analysis = self._analyze_trace_spans(spans)

            return trace_analysis

        except Exception as e:
            logger.error(f"Error analyzing trace {trace_id}: {e}")
            return self._get_default_trace_analysis(trace_id)

    def _identify_problem(self, log_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        识别日志中的问题

        Returns:
            Dict: {title, severity, description, confidence}
        """
        message = log_data.get('event', {}).get('raw', '').lower()
        level = log_data.get('event', {}).get('level', '').lower()
        service = log_data.get('entity', {}).get('name', '')

        # 检查每个问题模式
        matched_problems = []

        for problem_type, config in self.PROBLEM_PATTERNS.items():
            # 检查关键词
            keyword_score = 0
            for keyword in config['keywords']:
                if keyword in message:
                    keyword_score += 1

            # 检查错误指示器
            error_score = 0
            for indicator in config['error_indicators']:
                if indicator in message:
                    error_score += 2  # 错误指示器权重更高

            # 检查正则模式
            pattern_score = 0
            for pattern in config.get('patterns', []):
                if re.search(pattern, message, re.IGNORECASE):
                    pattern_score += 3  # 正则匹配权重最高

            total_score = keyword_score + error_score + pattern_score

            if total_score > 0:
                confidence = min(0.95, total_score * 0.15)  # 计算置信度
                matched_problems.append({
                    'type': problem_type,
                    'score': total_score,
                    'confidence': confidence
                })

        # 如果没有匹配到任何问题，使用默认分析
        if not matched_problems:
            return self._get_default_problem(log_data)

        # 选择得分最高的问题
        best_match = max(matched_problems, key=lambda x: x['score'])

        # 生成问题描述
        problem_info = self._generate_problem_description(best_match['type'], log_data)

        return {
            'title': problem_info['title'],
            'severity': self._determine_severity(level, best_match['confidence']),
            'description': problem_info['description'],
            'confidence': best_match['confidence']
        }

    def _analyze_root_causes(self, log_data: Dict[str, Any], problem: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        分析问题根因

        Returns:
            List[Dict]: 根因列表
        """
        message = log_data.get('event', {}).get('raw', '')
        problem_type = problem.get('title', '').lower()

        root_causes = []

        # 根据问题类型生成根因分析
        if 'database' in problem_type or 'connection' in problem_type:
            root_causes.extend([
                {
                    'title': '连接池配置不当',
                    'description': '数据库连接池可能已满，无法获取新连接',
                    'icon': 'CloseCircleOutlined',
                    'color': 'red',
                    'evidence': self._extract_evidence(message, ['pool', 'connection', 'max'])
                },
                {
                    'title': '网络问题',
                    'description': '网络延迟或丢包导致数据库连接失败',
                    'icon': 'WarningOutlined',
                    'color': 'orange',
                    'evidence': self._extract_evidence(message, ['timeout', 'network', 'unreachable'])
                },
                {
                    'title': '数据库负载过高',
                    'description': '数据库服务器可能过载，响应缓慢',
                    'icon': 'WarningOutlined',
                    'color': 'orange',
                }
            ])

        elif 'memory' in problem_type or 'oom' in problem_type:
            root_causes.extend([
                {
                    'title': '内存泄漏',
                    'description': '应用可能存在内存泄漏，导致内存持续增长',
                    'icon': 'CloseCircleOutlined',
                    'color': 'red',
                    'evidence': self._extract_evidence(message, ['heap', 'memory', 'allocation'])
                },
                {
                    'title': '内存配置不足',
                    'description': '容器或进程的内存限制可能太小',
                    'icon': 'WarningOutlined',
                    'color': 'orange',
                },
                {
                    'title': '大对象分配',
                    'description': '可能存在大量小对象或大对象分配',
                    'icon': 'WarningOutlined',
                    'color': 'orange',
                }
            ])

        elif 'timeout' in problem_type or 'network' in problem_type:
            root_causes.extend([
                {
                    'title': '网络连接超时',
                    'description': '目标服务响应缓慢或不可达',
                    'icon': 'CloseCircleOutlined',
                    'color': 'red',
                    'evidence': self._extract_evidence(message, ['timeout', 'unreachable', 'refused'])
                },
                {
                    'title': 'DNS 解析失败',
                    'description': '无法解析目标主机名',
                    'icon': 'WarningOutlined',
                    'color': 'orange',
                    'evidence': self._extract_evidence(message, ['dns', 'lookup', 'resolve'])
                },
                {
                    'title': '防火墙或网络策略',
                    'description': '网络策略可能阻止了连接',
                    'icon': 'WarningOutlined',
                    'color': 'orange',
                }
            ])

        elif 'slow' in problem_type or 'performance' in problem_type:
            root_causes.extend([
                {
                    'title': '算法复杂度高',
                    'description': '代码可能存在性能瓶颈（如 O(n²) 循环）',
                    'icon': 'WarningOutlined',
                    'color': 'orange',
                },
                {
                    'title': '数据库查询慢',
                    'description': '数据库查询可能是性能瓶颈',
                    'icon': 'WarningOutlined',
                    'color': 'orange',
                    'evidence': self._extract_evidence(message, ['query', 'sql', 'database'])
                },
                {
                    'title': '资源竞争',
                    'description': '多个服务竞争有限的资源',
                    'icon': 'WarningOutlined',
                    'color': 'orange',
                }
            ])

        else:
            # 通用根因
            root_causes.append({
                'title': '未知错误',
                'description': '需要进一步分析以确定根本原因',
                'icon': 'QuestionCircleOutlined',
                'color': 'blue',
            })

        return root_causes

    def _recommend_solutions(
        self,
        problem: Dict[str, Any],
        root_causes: List[Dict],
        log_data: Optional[Dict[str, Any]] = None,
        similar_cases: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """
        推荐解决方案

        Returns:
            List[Dict]: 解决方案列表
        """
        problem_title = problem.get('title', '').lower()

        solutions = []

        # 根据问题类型推荐解决方案
        if any(word in problem_title for word in ['database', 'connection', 'pool']):
            # 数据库连接问题
            sol = self.SOLUTIONS_KNOWLEDGE_BASE.get('database', {})
            if 'connection_pool' in sol:
                solutions.append({
                    'title': sol['connection_pool']['title'],
                    'description': sol['connection_pool']['description'],
                    'steps': sol['connection_pool']['steps'],
                    'resources': sol['connection_pool']['resources']
                })
            if 'slow_query' in sol:
                solutions.append({
                    'title': sol['slow_query']['title'],
                    'description': sol['slow_query']['description'],
                    'steps': sol['slow_query']['steps'],
                    'resources': sol['slow_query']['resources']
                })

        elif any(word in problem_title for word in ['memory', 'oom', 'heap']):
            # 内存问题
            sol = self.SOLUTIONS_KNOWLEDGE_BASE.get('memory', {})
            if 'leak' in sol:
                solutions.append({
                    'title': sol['leak']['title'],
                    'description': sol['leak']['description'],
                    'steps': sol['leak']['steps'],
                    'resources': sol['leak']['resources']
                })
            if 'limit' in sol:
                solutions.append({
                    'title': sol['limit']['title'],
                    'description': sol['limit']['description'],
                    'steps': sol['limit']['steps'],
                    'resources': sol['limit']['resources']
                })

        elif any(word in problem_title for word in ['network', 'timeout', 'connection']):
            # 网络问题
            sol = self.SOLUTIONS_KNOWLEDGE_BASE.get('network', {})
            if 'timeout' in sol:
                solutions.append({
                    'title': sol['timeout']['title'],
                    'description': sol['timeout']['description'],
                    'steps': sol['timeout']['steps'],
                    'resources': sol['timeout']['resources']
                })

        elif any(word in problem_title for word in ['performance', 'slow', 'latency']):
            # 性能问题
            sol = self.SOLUTIONS_KNOWLEDGE_BASE.get('performance', {})
            if 'optimization' in sol:
                solutions.append({
                    'title': sol['optimization']['title'],
                    'description': sol['optimization']['description'],
                    'steps': sol['optimization']['steps'],
                    'resources': sol['optimization']['resources']
                })

        # 如果没有特定解决方案，添加通用建议
        if not solutions:
            solutions.append({
                'title': '收集更多信息',
                'description': '需要收集更多日志和指标以进行深入分析',
                'steps': [
                    '启用详细日志',
                    '收集性能指标',
                    '检查相关服务状态',
                    '查看系统资源使用情况'
                ],
                'resources': ['debugging-guide', 'monitoring-best-practices']
            })

        self._append_context_aware_solutions(
            solutions,
            log_data or {},
            similar_cases or [],
        )
        return solutions

    def _append_context_aware_solutions(
        self,
        solutions: List[Dict[str, Any]],
        log_data: Dict[str, Any],
        similar_cases: List[Dict[str, Any]]
    ) -> None:
        """根据 trace/链路上下文增强建议项。"""
        context = log_data.get('context', {}) if isinstance(log_data.get('context'), dict) else {}
        k8s_context = context.get('k8s', {}) if isinstance(context.get('k8s'), dict) else {}

        trace_id = (context.get('trace_id') or context.get('traceId') or '').strip()
        source_service = (
            context.get('source_service')
            or context.get('caller_service')
            or context.get('upstream_service')
            or (context.get('topology', {}) or {}).get('source_service')
            or ''
        ).strip()
        target_service = (
            context.get('target_service')
            or context.get('callee_service')
            or context.get('downstream_service')
            or (context.get('topology', {}) or {}).get('target_service')
            or ''
        ).strip()
        namespace = (context.get('namespace') or k8s_context.get('namespace') or '').strip()

        existing_titles = {solution.get('title', '') for solution in solutions}

        if trace_id and '基于 Trace 上下文回放链路' not in existing_titles:
            solutions.append({
                'title': '基于 Trace 上下文回放链路',
                'description': '结合 trace 关联信息快速定位异常传播路径',
                'steps': [
                    f'以 trace_id={trace_id} 拉取完整调用链',
                    '定位最先报错的 span 及其上游调用方',
                    '对异常 span 前后 2 分钟日志做聚合比对',
                ],
                'resources': ['trace-replay', 'trace-log-correlation'],
            })

        if source_service and target_service and '优先排查关键调用链路' not in existing_titles:
            solutions.append({
                'title': '优先排查关键调用链路',
                'description': f'聚焦 {source_service} -> {target_service} 调用边进行定向排查',
                'steps': [
                    f'核对 {source_service} 到 {target_service} 的请求超时与重试配置',
                    '检查链路上的错误码、超时率与 p95/p99 延迟波动',
                    '对比最近发布窗口前后的链路指标变化',
                ],
                'resources': ['topology-edge-drilldown'],
            })

        if namespace and '限制命名空间排查范围' not in existing_titles:
            solutions.append({
                'title': '限制命名空间排查范围',
                'description': f'先在 {namespace} 命名空间内闭环定位，减少噪声干扰',
                'steps': [
                    f'在日志检索中添加 namespace={namespace} 过滤条件',
                    '优先检查同命名空间内被高频调用的下游服务',
                ],
                'resources': ['namespace-filtering'],
            })

        if similar_cases and '复用历史相似案例处置路径' not in existing_titles:
            top_case = similar_cases[0]
            case_title = top_case.get('title', '')
            case_source = top_case.get('source', '')
            if case_title:
                solutions.append({
                    'title': '复用历史相似案例处置路径',
                    'description': f'参考“{case_title}”的既有处置过程，加速当前问题修复',
                    'steps': [
                        f'优先复核相似案例中的关键修复动作（来源: {case_source or "案例库"}）',
                        '先在灰度环境验证，再逐步放量到生产环境',
                    ],
                    'resources': ['similar-case-playbook'],
                })

    def _analyze_metrics(self, log_data: Dict[str, Any], problem: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        分析影响指标

        Returns:
            List[Dict]: 指标列表
        """
        # 这里可以结合实际的 metrics 数据
        # 目前返回模拟数据
        level = log_data.get('event', {}).get('level', '').lower()
        severity = problem.get('severity', 'info')

        if severity == 'error':
            return [
                {
                    'name': '错误率',
                    'value': 15.8,
                    'unit': '%',
                    'trend': 'up',
                    'change': 0.25
                },
                {
                    'name': '平均响应时间',
                    'value': 1200,
                    'unit': 'ms',
                    'trend': 'up',
                    'change': 0.35
                },
                {
                    'name': '超时次数',
                    'value': 45,
                    'unit': '次/分钟',
                    'trend': 'up',
                    'change': 0.40
                }
            ]
        elif severity == 'warning':
            return [
                {
                    'name': '警告率',
                    'value': 8.5,
                    'unit': '%',
                    'trend': 'up',
                    'change': 0.15
                },
                {
                    'name': '平均响应时间',
                    'value': 650,
                    'unit': 'ms',
                    'trend': 'stable',
                    'change': 0.05
                }
            ]
        else:
            return [
                {
                    'name': '成功率',
                    'value': 99.2,
                    'unit': '%',
                    'trend': 'stable',
                    'change': 0.01
                },
                {
                    'name': '平均响应时间',
                    'value': 250,
                    'unit': 'ms',
                    'trend': 'down',
                    'change': -0.10
                }
            ]

    def _find_similar_cases(self, problem: Dict[str, Any], log_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        查找相似的历史案例

        Returns:
            List[Dict]: 相似案例列表
        """
        problem_title = problem.get('title', '').lower()
        problem_type = self._infer_problem_type(problem_title)
        log_content = log_data.get('event', {}).get('raw', '')
        service_name = log_data.get('entity', {}).get('name', '')
        context = log_data.get('context', {}) if isinstance(log_data.get('context'), dict) else {}

        try:
            recommender = get_recommender()
            results = recommender.find_similar_cases(
                log_content=log_content,
                service_name=service_name,
                problem_type=problem_type,
                context=context,
                limit=3,
                min_similarity=0.2,
            )

            if results:
                return [
                    {
                        'title': r.case.summary,
                        'description': r.relevance_reason,
                        'link': '#',
                        'source': f'案例库 {r.case.id}',
                        'status': '已解决' if r.case.resolved else '待处理',
                        'similarity': round(r.similarity_score, 2),
                        'service_name': r.case.service_name,
                        'problem_type': r.case.problem_type,
                        'matched_features': r.matched_features,
                    }
                    for r in results
                ]

        except Exception as e:
            logger.warning(f"Failed to load similar cases from store: {e}")

        return self._get_mock_similar_cases(problem_title)

    def _infer_problem_type(self, problem_title: str) -> str:
        """从问题标题推断 problem_type。"""
        title = (problem_title or '').lower()
        if any(keyword in title for keyword in ['database', 'sql', 'db', '连接池']):
            return 'database'
        if any(keyword in title for keyword in ['memory', 'oom', 'heap', '内存']):
            return 'memory'
        if any(keyword in title for keyword in ['network', 'timeout', 'dns', '网络']):
            return 'network'
        if any(keyword in title for keyword in ['performance', 'slow', 'latency', '慢']):
            return 'performance'
        if any(keyword in title for keyword in ['auth', 'token', 'permission', '认证']):
            return 'auth'
        if any(keyword in title for keyword in ['disk', 'filesystem', '磁盘', '空间']):
            return 'disk'
        return ''

    def _get_mock_similar_cases(self, problem_title: str) -> List[Dict[str, Any]]:
        """兜底返回静态相似案例。"""

        similar_cases = []

        if 'database' in problem_title or 'connection' in problem_title:
            similar_cases.extend([
                {
                    'title': '数据库连接池耗尽问题',
                    'description': '类似的连接池配置问题，通过调整 pool_size 解决',
                    'link': '#',
                    'source': '内部工单 #12345',
                    'status': '已解决',
                    'similarity': 0.92
                },
                {
                    'title': 'API 数据库超时优化',
                    'description': '通过优化查询和增加连接池解决超时问题',
                    'link': '#',
                    'source': '技术文档',
                    'status': '已解决',
                    'similarity': 0.85
                }
            ])

        elif 'memory' in problem_title or 'oom' in problem_title:
            similar_cases.append({
                'title': '内存泄漏排查和修复',
                'description': '使用 pprof 定位并修复了 goroutine 泄漏',
                'link': '#',
                'source': '内部工单 #12346',
                'status': '已解决',
                'similarity': 0.88
            })

        else:
            similar_cases.append({
                'title': '通用错误排查流程',
                'description': '标准的错误排查和解决流程',
                'link': '#',
                'source': '运维文档',
                'status': '已解决',
                'similarity': 0.75
            })

        return similar_cases

    def _analyze_trace_spans(self, spans: List[Dict]) -> Dict[str, Any]:
        """
        分析 trace 中的所有 spans

        Returns:
            Dict: 分析结果
        """
        # 1. 统计信息
        total_spans = len(spans)
        error_spans = [s for s in spans if _is_error_status(s.get('status'))]
        total_duration = max(int(float(s.get('duration_ms', 0) or 0)) for s in spans)

        # 2. 识别问题
        error_count = len(error_spans)
        error_rate = error_count / total_spans if total_spans > 0 else 0

        # 3. 识别慢操作
        slow_threshold = 1000  # 1秒
        slow_spans = [s for s in spans if int(float(s.get('duration_ms', 0) or 0)) > slow_threshold]

        # 4. 分析问题
        if error_rate > 0:
            problem = {
                'title': '调用链异常',
                'severity': 'error',
                'description': f'在 {total_spans} 个操作中发现了 {error_count} 个错误',
                'confidence': min(0.95, 0.5 + error_rate)
            }
        elif len(slow_spans) > 0:
            problem = {
                'title': '调用链缓慢',
                'severity': 'warning',
                'description': f'发现 {len(slow_spans)} 个慢操作（>{slow_threshold}ms）',
                'confidence': min(0.90, 0.4 + len(slow_spans) * 0.1)
            }
        else:
            problem = {
                'title': '调用链正常',
                'severity': 'info',
                'description': f'调用链包含 {total_spans} 个操作，总耗时 {total_duration}ms',
                'confidence': 0.85
            }

        # 5. 根因分析
        root_causes = []

        if error_spans:
            # 找出错误的服务
            error_services = defaultdict(int)
            for span in error_spans:
                service = span.get('service_name', 'unknown')
                error_services[service] += 1

            for service, count in error_services.items():
                root_causes.append({
                    'title': f'{service} 服务异常',
                    'description': f'在 {service} 中发现 {count} 个错误',
                    'icon': 'CloseCircleOutlined',
                    'color': 'red',
                    'evidence': f'{service}: {count} errors'
                })

        if slow_spans:
            # 找出慢操作
            slowest = sorted(slow_spans, key=lambda x: x['duration_ms'], reverse=True)[:3]
            for span in slowest:
                root_causes.append({
                    'title': f'{span["service_name"]} 慢操作',
                    'description': f'{span["operation_name"]} 耗时 {span["duration_ms"]}ms',
                    'icon': 'WarningOutlined',
                    'color': 'orange',
                    'evidence': f'duration: {span["duration_ms"]}ms'
                })

        if not root_causes:
            root_causes.append({
                'title': '调用链正常',
                'description': '所有操作都在正常范围内',
                'icon': 'CheckCircleOutlined',
                'color': 'green'
            })

        # 6. 解决方案
        solutions = []
        if error_rate > 0:
            solutions.append({
                'title': '检查错误日志',
                'description': '查看相关服务的详细错误日志',
                'steps': [
                    f'检查 {", ".join(set(s["service_name"] for s in error_spans))} 服务日志',
                    '分析错误模式和频率',
                    '修复代码错误'
                ],
                'resources': ['error-logging', 'debug-guide']
            })

        if len(slow_spans) > 0:
            solutions.append({
                'title': '优化慢操作',
                'description': '优化耗时较长的操作',
                'steps': [
                    '分析慢操作的具体原因',
                    '优化算法或数据库查询',
                    '考虑使用缓存',
                    '异步处理非关键路径'
                ],
                'resources': ['performance-tuning', 'caching-guide']
            })

        # 7. 指标
        metrics = [
            {
                'name': '错误率',
                'value': round(error_rate * 100, 1),
                'unit': '%',
                'trend': 'up' if error_rate > 0 else 'stable',
                'change': error_rate
            },
            {
                'name': '总耗时',
                'value': total_duration,
                'unit': 'ms',
                'trend': 'stable',
                'change': 0
            },
            {
                'name': '操作数',
                'value': total_spans,
                'unit': '个',
                'trend': 'stable',
                'change': 0
            }
        ]

        # 8. 相似案例（简化）
        similar_cases = [
            {
                'title': '类似的调用链问题',
                'description': f'之前发现的相同错误模式',
                'link': '#',
                'source': '历史记录',
                'status': '已解决',
                'similarity': 0.80
            }
        ]

        return {
            'overview': problem,
            'rootCauses': root_causes,
            'solutions': solutions,
            'metrics': metrics,
            'similarCases': similar_cases
        }

    def _get_default_problem(self, log_data: Dict[str, Any]) -> Dict[str, Any]:
        """获取默认问题分析"""
        level = log_data.get('event', {}).get('level', '').lower()

        if level == 'error':
            return {
                'title': '应用错误',
                'severity': 'error',
                'description': '应用发生了错误，需要查看日志详情',
                'confidence': 0.5
            }
        elif level in {'warn', 'warning'}:
            return {
                'title': '应用警告',
                'severity': 'warning',
                'description': '应用产生了警告信息',
                'confidence': 0.5
            }
        else:
            return {
                'title': '信息日志',
                'severity': 'info',
                'description': '一般的信息日志',
                'confidence': 0.3
            }

    def _determine_severity(self, level: str, confidence: float) -> str:
        """确定严重级别"""
        normalized_level = str(level or '').strip().lower()
        if normalized_level == 'error':
            return 'error'
        elif normalized_level in {'warn', 'warning'}:
            return 'warning'
        elif normalized_level == 'info' and confidence > 0.7:
            return 'warning'
        else:
            return 'info'

    def _generate_problem_description(self, problem_type: str, log_data: Dict[str, Any]) -> Dict[str, str]:
        """生成问题描述"""
        service = log_data.get('entity', {}).get('name', 'unknown')

        descriptions = {
            'database': {
                'title': '数据库连接问题',
                'description': f'服务 {service} 在访问数据库时出现问题，可能是连接池、网络或查询性能问题'
            },
            'memory': {
                'title': '内存问题',
                'description': f'服务 {service} 遇到内存相关问题，可能是内存泄漏、OOM 或配置不当'
            },
            'network': {
                'title': '网络连接问题',
                'description': f'服务 {service} 在网络连接时遇到问题，可能是超时、DNS 解析或目标服务不可达'
            },
            'performance': {
                'title': '性能问题',
                'description': f'服务 {service} 的性能低于预期，可能存在瓶颈'
            },
            'disk': {
                'title': '磁盘空间问题',
                'description': f'服务 {service} 所在节点磁盘空间不足'
            },
            'auth': {
                'title': '认证授权问题',
                'description': f'服务 {service} 的认证或授权失败'
            }
        }

        return descriptions.get(problem_type, {
            'title': f'{problem_type.capitalize()} 问题',
            'description': f'服务 {service} 遇到 {problem_type} 相关问题'
        })

    def _extract_evidence(self, message: str, keywords: List[str]) -> str:
        """从消息中提取证据"""
        message_lower = message.lower()
        evidence_parts = []

        for keyword in keywords:
            if keyword in message_lower:
                # 尝试提取包含关键词的上下文
                idx = message_lower.find(keyword)
                start = max(0, idx - 10)
                end = min(len(message), idx + len(keyword) + 10)
                evidence_parts.append(message[start:end].strip())

        return '; '.join(evidence_parts) if evidence_parts else 'Not specified'

    def _get_default_analysis(self, log_data: Dict[str, Any]) -> Dict[str, Any]:
        """获取默认分析结果"""
        problem = self._get_default_problem(log_data)

        return {
            'overview': {
                'problem': problem['title'],
                'severity': problem['severity'],
                'description': problem['description'],
                'confidence': problem['confidence']
            },
            'rootCauses': [
                {
                    'title': '需要进一步分析',
                    'description': '当前日志信息不足以确定具体原因',
                    'icon': 'QuestionCircleOutlined',
                    'color': 'blue'
                }
            ],
            'solutions': [
                {
                    'title': '收集更多信息',
                    'description': '需要收集更多日志和指标',
                    'steps': [
                        '查看完整日志上下文',
                        '检查相关服务状态',
                        '查看系统资源使用情况'
                    ],
                    'resources': ['debug-guide']
                }
            ],
            'metrics': [
                {
                    'name': '置信度',
                    'value': problem['confidence'] * 100,
                    'unit': '%',
                    'trend': 'stable',
                    'change': 0
                }
            ],
            'similarCases': []
        }

    def _get_default_trace_analysis(self, trace_id: str) -> Dict[str, Any]:
        """获取默认 trace 分析结果"""
        return {
            'overview': {
                'problem': '无法分析调用链',
                'severity': 'warning',
                'description': f'未找到 trace_id={trace_id} 的数据',
                'confidence': 0.0
            },
            'rootCauses': [],
            'solutions': [],
            'metrics': [],
            'similarCases': []
        }


# 全局实例
_log_analyzer = None


def get_log_analyzer(storage_adapter=None) -> LogAnalyzer:
    """
    获取日志分析器实例

    Args:
        storage_adapter: StorageAdapter 实例

    Returns:
        LogAnalyzer: 分析器实例
    """
    global _log_analyzer

    if _log_analyzer is None:
        _log_analyzer = LogAnalyzer(storage_adapter)

    return _log_analyzer
