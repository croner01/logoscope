"""
Semantic Engine Labels Discovery 模块
负责自动识别和分类 Kubernetes Labels
"""
from typing import Dict, Any, List, Set
from collections import Counter
import logging

logger = logging.getLogger(__name__)


# Kubernetes 推荐标签前缀
RECOMMENDED_LABEL_PREFIXES = [
    "app.kubernetes.io",
    "apps.kubernetes.io"
]

# 常见标签分类
LABEL_CATEGORIES = {
    "application": ["app", "application", "name", "version", "component"],
    "kubernetes": ["kubernetes.io", "app.kubernetes.io", "apps.kubernetes.io"],
    "infrastructure": ["topology.kubernetes.io", "featuregates.kubernetes.io"],
    "monitoring": ["prometheus.io", "grafana.io", "monitoring"],
    "networking": ["network", "ingress", "service"],
    "storage": ["volume", "storage", "pvc"],
    "security": ["security", "authorization", "authentication"]
}


class LabelDiscoverer:
    """
    标签自动发现器

    自动识别、分类和推荐 Kubernetes Labels
    """

    def __init__(self):
        """初始化标签发现器"""
        # 已发现的标签集合
        self.discovered_labels: Set[str] = set()
        # 标签使用统计
        self.label_stats: Counter = Counter()
        # 标签到分类的映射
        self.label_categories: Dict[str, str] = {}

    def discover_labels(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        从事件列表中发现标签

        Args:
            events: 事件列表

        Returns:
            Dict[str, Any]: 发现结果，包含：
                - total_labels: 总标签数
                - unique_labels: 唯一标签数
                - common_labels: 常用标签（Top 10）
                - recommended_labels: 推荐添加的标签
                - label_categories: 标签分类统计
        """
        # 提取所有标签
        all_labels = []
        for event in events:
            labels = event.get('context', {}).get('k8s', {}).get('labels', {})
            if labels:
                all_labels.extend(labels.keys())

        # 统计标签使用频率
        label_counter = Counter(all_labels)

        # 分类标签
        categorized = self._categorize_labels(label_counter.keys())

        # 识别推荐的标签（基于 Kubernetes 标准）
        recommended = self._identify_recommended_labels(label_counter.keys())

        return {
            "total_labels": len(all_labels),
            "unique_labels": len(label_counter),
            "common_labels": [
                {"name": name, "count": count}
                for name, count in label_counter.most_common(10)
            ],
            "recommended_labels": recommended,
            "label_categories": categorized
        }

    def _categorize_labels(self, labels: List[str]) -> Dict[str, List[str]]:
        """
        对标签进行分类

        Args:
            labels: 标签列表

        Returns:
            Dict[str, List[str]]: 分类后的标签
        """
        categorized = {
            "application": [],
            "kubernetes": [],
            "infrastructure": [],
            "monitoring": [],
            "networking": [],
            "storage": [],
            "security": [],
            "custom": []
        }

        for label in labels:
            matched = False
            for category, keywords in LABEL_CATEGORIES.items():
                if any(keyword in label.lower() for keyword in keywords):
                    categorized[category].append(label)
                    matched = True
                    break

            if not matched:
                categorized["custom"].append(label)

        return {k: v for k, v in categorized.items() if v}

    def _identify_recommended_labels(self, current_labels: List[str]) -> List[Dict[str, str]]:
        """
        识别推荐添加的 Kubernetes 标准标签

        Args:
            current_labels: 当前已有的标签

        Returns:
            List[Dict[str, str]]: 推荐的标签列表
        """
        # Kubernetes 推荐的标准标签
        k8s_standard_labels = {
            "app.kubernetes.io/name": "应用名称",
            "app.kubernetes.io/instance": "实例唯一标识",
            "app.kubernetes.io/version": "应用版本",
            "app.kubernetes.io/component": "组件架构",
            "app.kubernetes.io/part-of": "高层级应用",
            "app.kubernetes.io/managed-by": "被哪个工具管理",
            "app.kubernetes.io/created-by": "创建资源的控制器"
        }

        recommended = []
        current_label_set = set(current_labels)

        for label, description in k8s_standard_labels.items():
            if label not in current_label_set:
                recommended.append({
                    "name": label,
                    "description": description,
                    "priority": "high" if "name" in label else "medium"
                })

        return recommended

    def extract_app_labels(self, labels: Dict[str, str]) -> Dict[str, str]:
        """
        提取应用相关标签

        Args:
            labels: 原始标签字典

        Returns:
            Dict[str, str]: 应用标签（包含 app、version 等）
        """
        app_labels = {}

        # 直接提取 app 标签
        if "app" in labels:
            app_labels["app"] = labels["app"]

        # 提取 Kubernetes 推荐标签
        for key in labels:
            if key.startswith("app.kubernetes.io/"):
                app_labels[key] = labels[key]

        # 提取 version 相关标签
        for key in labels:
            if "version" in key.lower():
                app_labels[key] = labels[key]

        return app_labels

    def get_label_suggestions(self, service_name: str) -> List[Dict[str, str]]:
        """
        根据服务名生成标签建议

        Args:
            service_name: 服务名称

        Returns:
            List[Dict[str, str]]: 建议的标签列表
        """
        suggestions = [
            {
                "key": "app.kubernetes.io/name",
                "value": service_name,
                "reason": "Kubernetes 标准应用名标签"
            },
            {
                "key": "app",
                "value": service_name.split("-")[0],
                "reason": "简短的应用标识"
            }
        ]

        # 如果服务名包含版本信息
        if "v" in service_name or any(c.isdigit() for c in service_name):
            suggestions.append({
                "key": "app.kubernetes.io/version",
                "value": service_name,
                "reason": "应用版本标签"
            })

        return suggestions


# 全局实例
label_discoverer = LabelDiscoverer()


def discover_labels_from_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    从事件列表中发现标签（便捷函数）

    Args:
        events: 事件列表

    Returns:
        Dict[str, Any]: 发现结果
    """
    return label_discoverer.discover_labels(events)
