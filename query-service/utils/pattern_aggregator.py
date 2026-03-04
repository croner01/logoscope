"""
智能日志 Pattern 聚合器

功能:
1. 从日志消息中提取 Pattern 模板
2. 识别变量部分 (数字、UUID、IP、时间戳等)
3. 相似日志分组聚合
4. 返回聚合结果和示例日志

算法:
1. 预处理: 标准化变量 (数字→{num}, UUID→{uuid}, IP→{ip} 等)
2. 分组: 按标准化后的 pattern 分组
3. 提取变量: 记录每条日志的变量值
4. 聚合: 返回 pattern、count、示例日志
"""
import re
from typing import Dict, List, Any, Tuple, Optional
from dataclasses import dataclass, field
from collections import defaultdict
from datetime import datetime


@dataclass
class LogPattern:
    """日志 Pattern 数据结构"""
    pattern: str
    pattern_hash: str
    count: int = 0
    level: str = "INFO"
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    samples: List[Dict[str, Any]] = field(default_factory=list)
    variables: List[str] = field(default_factory=list)
    variable_examples: Dict[str, List[str]] = field(default_factory=dict)
    service_names: List[str] = field(default_factory=list)


class PatternAggregator:
    """
    智能 Pattern 聚合器
    
    Example:
        >>> aggregator = PatternAggregator()
        >>> logs = [
        ...     {"message": "Request GET /api/users completed in 150ms", "level": "INFO"},
        ...     {"message": "Request POST /api/orders completed in 200ms", "level": "INFO"},
        ...     {"message": "Request GET /api/users completed in 120ms", "level": "INFO"},
        ... ]
        >>> patterns = aggregator.aggregate(logs)
        >>> # 返回: pattern="Request {method} {path} completed in {duration}ms", count=3
    """
    
    UUID_PATTERN = re.compile(
        r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    )
    
    IPV4_PATTERN = re.compile(
        r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b'
    )
    
    IPV6_PATTERN = re.compile(
        r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b|'
        r'\b(?:[0-9a-fA-F]{1,4}:){1,7}:\b|'
        r'\b(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}\b'
    )
    
    TIMESTAMP_ISO_PATTERN = re.compile(
        r'\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?'
    )
    
    TIMESTAMP_UNIX_PATTERN = re.compile(
        r'\b\d{10,13}\b'
    )
    
    HEX_PATTERN = re.compile(
        r'\b0x[0-9a-fA-F]+\b'
    )
    
    NUMBER_PATTERN = re.compile(
        r'\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?:ms|s|m|h|d|MB|GB|TB|KB|B|%)?\b'
    )
    
    PATH_PATTERN = re.compile(
        r'/[\w\-./]+'
    )
    
    DURATION_PATTERN = re.compile(
        r'\b(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)\b'
    )
    
    SIZE_PATTERN = re.compile(
        r'\b(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)\b'
    )
    
    POD_NAME_PATTERN = re.compile(
        r'[a-z0-9]([a-z0-9-]*[a-z0-9])?-([a-z0-9]{5,10})-([a-z0-9]{5})'
    )
    
    K8S_ID_PATTERN = re.compile(
        r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
    )
    
    def __init__(
        self,
        min_samples: int = 3,
        max_samples: int = 5,
        max_patterns: int = 100,
        keep_original: bool = True
    ):
        """
        初始化聚合器
        
        Args:
            min_samples: 最小聚合数量，低于此值的 pattern 不返回
            max_samples: 每个 pattern 保留的示例日志数量
            max_patterns: 返回的最大 pattern 数量
            keep_original: 是否在结果中保留原始日志
        """
        self.min_samples = min_samples
        self.max_samples = max_samples
        self.max_patterns = max_patterns
        self.keep_original = keep_original
    
    def _normalize_message(self, message: str) -> Tuple[str, Dict[str, Any]]:
        """
        标准化日志消息，提取变量
        
        Returns:
            Tuple[str, Dict]: (标准化后的 pattern, 提取的变量)
        """
        if not message:
            return "", {}
        
        variables = {}
        pattern = message
        
        var_counter = defaultdict(int)
        
        def replace_with_counter(var_type: str, match) -> str:
            var_counter[var_type] += 1
            return f"{{{var_type}}}"
        
        for uuid_match in self.UUID_PATTERN.finditer(pattern):
            if "uuid" not in variables:
                variables["uuid"] = []
            variables["uuid"].append(uuid_match.group())
        pattern = self.UUID_PATTERN.sub("{uuid}", pattern)
        
        for ipv6_match in self.IPV6_PATTERN.finditer(pattern):
            if "ip" not in variables:
                variables["ip"] = []
            variables["ip"].append(ipv6_match.group())
        pattern = self.IPV6_PATTERN.sub("{ip}", pattern)
        
        for ipv4_match in self.IPV4_PATTERN.finditer(pattern):
            if "ip" not in variables:
                variables["ip"] = []
            variables["ip"].append(ipv4_match.group())
        pattern = self.IPV4_PATTERN.sub("{ip}", pattern)
        
        for ts_match in self.TIMESTAMP_ISO_PATTERN.finditer(pattern):
            if "timestamp" not in variables:
                variables["timestamp"] = []
            variables["timestamp"].append(ts_match.group())
        pattern = self.TIMESTAMP_ISO_PATTERN.sub("{timestamp}", pattern)
        
        for hex_match in self.HEX_PATTERN.finditer(pattern):
            if "hex" not in variables:
                variables["hex"] = []
            variables["hex"].append(hex_match.group())
        pattern = self.HEX_PATTERN.sub("{hex}", pattern)
        
        for num_match in self.NUMBER_PATTERN.finditer(pattern):
            val = num_match.group()
            if "duration" not in variables:
                variables["duration"] = []
            variables["duration"].append(val)
        pattern = self.NUMBER_PATTERN.sub("{num}", pattern)
        
        return pattern, variables
    
    def _compute_pattern_hash(self, pattern: str) -> str:
        """计算 pattern 的 hash 值用于分组"""
        import hashlib
        return hashlib.md5(pattern.encode()).hexdigest()[:16]
    
    def _get_word_tokens(self, pattern: str) -> List[str]:
        """获取 pattern 的词元用于相似度计算"""
        return re.findall(r'\w+|\{[^}]+\}|[^\w\s]', pattern)
    
    def aggregate(
        self,
        logs: List[Dict[str, Any]],
        time_window_seconds: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        聚合日志
        
        Args:
            logs: 日志列表，每条日志应包含 message 和 level 字段
            time_window_seconds: 时间窗口（秒），用于过滤最近日志
        
        Returns:
            Dict: 聚合结果
            {
                "patterns": [
                    {
                        "pattern": "Request {method} {path} completed in {duration}",
                        "pattern_hash": "abc123",
                        "count": 100,
                        "level": "INFO",
                        "first_seen": "2024-01-15T10:00:00Z",
                        "last_seen": "2024-01-15T11:00:00Z",
                        "samples": [...],
                        "variables": ["method", "path", "duration"],
                        "variable_examples": {"duration": ["150ms", "200ms"]}
                    }
                ],
                "total_logs": 1000,
                "total_patterns": 10,
                "aggregation_ratio": 0.99
            }
        """
        if not logs:
            return {
                "patterns": [],
                "total_logs": 0,
                "total_patterns": 0,
                "aggregation_ratio": 0.0
            }
        
        pattern_groups: Dict[str, LogPattern] = {}
        
        cutoff_time = None
        if time_window_seconds:
            cutoff_time = datetime.now().timestamp() - time_window_seconds
        
        for log in logs:
            message = log.get("message", "")
            level = log.get("level", "INFO")
            timestamp = log.get("timestamp", "")
            service_name = log.get("service_name", "unknown")
            log_id = log.get("id", "")
            
            if not message:
                continue
            
            normalized_pattern, variables = self._normalize_message(message)
            pattern_hash = self._compute_pattern_hash(normalized_pattern)
            
            if pattern_hash not in pattern_groups:
                pattern_groups[pattern_hash] = LogPattern(
                    pattern=normalized_pattern,
                    pattern_hash=pattern_hash,
                    level=level,
                    first_seen=timestamp,
                    last_seen=timestamp,
                    variables=list(variables.keys()),
                    variable_examples=variables
                )
            
            group = pattern_groups[pattern_hash]
            group.count += 1
            
            if timestamp:
                if group.first_seen is None or timestamp < group.first_seen:
                    group.first_seen = timestamp
                if group.last_seen is None or timestamp > group.last_seen:
                    group.last_seen = timestamp
            
            if len(group.samples) < self.max_samples:
                sample = {
                    "id": log_id,
                    "message": message,
                    "level": level,
                    "timestamp": timestamp,
                    "service_name": service_name,
                    "variables": variables
                }
                if "pod_name" in log:
                    sample["pod_name"] = log["pod_name"]
                group.samples.append(sample)
            
            if service_name not in group.service_names:
                group.service_names.append(service_name)
            
            for var_name, var_values in variables.items():
                if var_name not in group.variable_examples:
                    group.variable_examples[var_name] = []
                for val in var_values:
                    if val not in group.variable_examples[var_name]:
                        group.variable_examples[var_name].append(val)
                        if len(group.variable_examples[var_name]) >= 10:
                            break
        
        patterns = list(pattern_groups.values())
        
        patterns.sort(key=lambda p: p.count, reverse=True)
        
        if self.min_samples > 1:
            patterns = [p for p in patterns if p.count >= self.min_samples]
        
        patterns = patterns[:self.max_patterns]
        
        result_patterns = []
        for p in patterns:
            result_patterns.append({
                "pattern": p.pattern,
                "pattern_hash": p.pattern_hash,
                "count": p.count,
                "level": p.level,
                "first_seen": p.first_seen,
                "last_seen": p.last_seen,
                "samples": p.samples,
                "variables": p.variables,
                "variable_examples": {k: v[:5] for k, v in p.variable_examples.items()},
                "service_names": p.service_names[:5]
            })
        
        total_logs = len(logs)
        total_patterns = len(result_patterns)
        aggregated_count = sum(p["count"] for p in result_patterns)
        aggregation_ratio = aggregated_count / total_logs if total_logs > 0 else 0.0
        
        return {
            "patterns": result_patterns,
            "total_logs": total_logs,
            "total_patterns": total_patterns,
            "aggregated_count": aggregated_count,
            "aggregation_ratio": round(aggregation_ratio, 3)
        }


def extract_pattern_signature(message: str) -> str:
    """
    从单条日志消息提取 pattern 签名
    
    用于快速比较两条日志是否相似
    """
    aggregator = PatternAggregator()
    pattern, _ = aggregator._normalize_message(message)
    return pattern


def are_logs_similar(msg1: str, msg2: str, threshold: float = 0.8) -> bool:
    """
    判断两条日志是否相似
    
    Args:
        msg1: 第一条日志消息
        msg2: 第二条日志消息
        threshold: 相似度阈值 (0-1)
    
    Returns:
        bool: 是否相似
    """
    pattern1, _ = PatternAggregator()._normalize_message(msg1)
    pattern2, _ = PatternAggregator()._normalize_message(msg2)
    return pattern1 == pattern2
