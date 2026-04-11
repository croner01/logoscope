"""
Labels Discovery 模块单元测试

测试 labels/discovery.py 的核心功能：
- 标签发现
- 标签分类
- 推荐标签识别
- 应用标签提取
"""
import pytest
from labels.discovery import (
    LabelDiscoverer,
    discover_labels_from_events,
    LABEL_CATEGORIES
)


class TestLabelDiscovererInit:
    """测试 LabelDiscoverer 初始化"""

    def test_init(self):
        """测试初始化"""
        discoverer = LabelDiscoverer()
        assert discoverer.discovered_labels == set()
        assert isinstance(discoverer.label_stats, type(discoverer.label_stats))
        assert isinstance(discoverer.label_categories, dict)


class TestDiscoverLabels:
    """测试标签发现功能"""

    @pytest.fixture
    def discoverer(self):
        return LabelDiscoverer()

    @pytest.fixture
    def sample_events(self):
        """示例事件列表"""
        return [
            {
                "context": {
                    "k8s": {
                        "labels": {
                            "app": "test-app",
                            "version": "v1.0",
                            "app.kubernetes.io/name": "myapp"
                        }
                    }
                }
            },
            {
                "context": {
                    "k8s": {
                        "labels": {
                            "app": "test-app",
                            "env": "production",
                            "monitoring": "enabled"
                        }
                    }
                }
            },
            {
                "context": {
                    "k8s": {
                        "labels": {
                            "app": "another-app",
                            "version": "v1.0"
                        }
                    }
                }
            }
        ]

    def test_discover_labels_basic(self, discoverer, sample_events):
        """测试基本标签发现"""
        result = discoverer.discover_labels(sample_events)

        assert "total_labels" in result
        assert result["total_labels"] == 8  # 3 + 3 + 2

        assert "unique_labels" in result
        assert result["unique_labels"] == 5  # app, version, app.kubernetes.io/name, env, monitoring

    def test_discover_labels_common_labels(self, discoverer, sample_events):
        """测试常用标签统计"""
        result = discoverer.discover_labels(sample_events)

        assert "common_labels" in result
        common_labels = result["common_labels"]

        # app 标签出现最频繁（3次）
        assert common_labels[0]["name"] == "app"
        assert common_labels[0]["count"] == 3

        # version 标签出现2次
        version_label = next(l for l in common_labels if l["name"] == "version")
        assert version_label["count"] == 2

    def test_discover_labels_with_empty_events(self, discoverer):
        """测试空事件列表"""
        result = discoverer.discover_labels([])

        assert result["total_labels"] == 0
        assert result["unique_labels"] == 0
        assert result["common_labels"] == []

    def test_discover_labels_with_no_labels(self, discoverer):
        """测试没有标签的事件"""
        events = [
            {"context": {"k8s": {}}},
            {"context": {}},
            {}
        ]

        result = discoverer.discover_labels(events)

        assert result["total_labels"] == 0
        assert result["unique_labels"] == 0


class TestCategorizeLabels:
    """测试标签分类"""

    @pytest.fixture
    def discoverer(self):
        return LabelDiscoverer()

    def test_categorize_kubernetes_labels(self, discoverer):
        """测试 Kubernetes 标签分类"""
        labels = [
            "kubernetes.io/hostname",
            "kubernetes.io/pod-id",
            "topology.kubernetes.io/zone"
        ]

        categorized = discoverer._categorize_labels(labels)

        # infrastructure 分类包含 topology.kubernetes.io
        assert "infrastructure" in categorized or "kubernetes" in categorized

        # kubernetes.io 标签应该在某个分类中
        all_categorized = []
        for cat_labels in categorized.values():
            all_categorized.extend(cat_labels)

        assert "kubernetes.io/hostname" in all_categorized

    def test_categorize_application_labels(self, discoverer):
        """测试应用标签分类"""
        labels = ["app", "application", "name", "version", "component"]

        categorized = discoverer._categorize_labels(labels)

        assert "application" in categorized
        assert len(categorized["application"]) == 5

    def test_categorize_monitoring_labels(self, discoverer):
        """测试监控标签分类"""
        labels = ["prometheus.io/scrape", "prometheus.io/port", "monitoring"]

        categorized = discoverer._categorize_labels(labels)

        assert "monitoring" in categorized
        assert len(categorized["monitoring"]) == 3

    def test_categorize_custom_labels(self, discoverer):
        """测试自定义标签分类"""
        labels = ["custom-label", "my-tag", "random-key"]

        categorized = discoverer._categorize_labels(labels)

        assert "custom" in categorized
        assert len(categorized["custom"]) == 3

    def test_categorize_mixed_labels(self, discoverer):
        """测试混合标签分类"""
        labels = [
            "app",
            "component",  # application 关键词
            "prometheus.io/scrape",
            "custom-tag"
        ]

        categorized = discoverer._categorize_labels(labels)

        # 验证主要分类都存在
        assert "application" in categorized
        assert "monitoring" in categorized
        assert "custom" in categorized

        # 验证标签数量
        total_categorized = sum(len(v) for v in categorized.values())
        assert total_categorized == 4

    def test_categorize_empty_list(self, discoverer):
        """测试空列表分类"""
        categorized = discoverer._categorize_labels([])

        # 应该只返回非空分类
        assert len(categorized) == 0


class TestIdentifyRecommendedLabels:
    """测试推荐标签识别"""

    @pytest.fixture
    def discoverer(self):
        return LabelDiscoverer()

    def test_identify_missing_k8s_labels(self, discoverer):
        """测试识别缺失的 Kubernetes 标准标签"""
        current_labels = ["app", "version", "env"]

        recommended = discoverer._identify_recommended_labels(current_labels)

        # 应该推荐 app.kubernetes.io/name
        assert any(r["name"] == "app.kubernetes.io/name" for r in recommended)

        # 应该推荐 app.kubernetes.io/version
        assert any(r["name"] == "app.kubernetes.io/version" for r in recommended)

    def test_with_existing_k8s_labels(self, discoverer):
        """测试已有部分标准标签的情况"""
        current_labels = [
            "app.kubernetes.io/name",
            "app.kubernetes.io/version"
        ]

        recommended = discoverer._identify_recommended_labels(current_labels)

        # 已有的标签不应再推荐
        assert not any(r["name"] == "app.kubernetes.io/name" for r in recommended)

        # 应该推荐其他标签
        assert len(recommended) > 0

    def test_priority_levels(self, discoverer):
        """测试推荐优先级"""
        current_labels = []

        recommended = discoverer._identify_recommended_labels(current_labels)

        # name 标签应该是高优先级
        name_label = next(r for r in recommended if "name" in r["name"])
        assert name_label["priority"] == "high"

        # 其他标签应该是中等优先级
        other_labels = [r for r in recommended if "name" not in r["name"]]
        if other_labels:
            assert other_labels[0]["priority"] == "medium"


class TestExtractAppLabels:
    """测试应用标签提取"""

    @pytest.fixture
    def discoverer(self):
        return LabelDiscoverer()

    def test_extract_app_label(self, discoverer):
        """测试提取 app 标签"""
        labels = {
            "app": "myapp",
            "env": "production",
            "team": "backend"
        }

        app_labels = discoverer.extract_app_labels(labels)

        assert "app" in app_labels
        assert app_labels["app"] == "myapp"
        assert "env" not in app_labels

    def test_extract_k8s_app_labels(self, discoverer):
        """测试提取 Kubernetes 应用标签"""
        labels = {
            "app.kubernetes.io/name": "myapp",
            "app.kubernetes.io/version": "v1.0",
            "app.kubernetes.io/component": "api",
            "env": "production"
        }

        app_labels = discoverer.extract_app_labels(labels)

        assert len(app_labels) == 3
        assert app_labels["app.kubernetes.io/name"] == "myapp"
        assert app_labels["app.kubernetes.io/version"] == "v1.0"
        assert app_labels["app.kubernetes.io/component"] == "api"

    def test_extract_version_labels(self, discoverer):
        """测试提取版本标签"""
        labels = {
            "app": "myapp",
            "version": "v1.0",
            "app-version": "1.0.0"
        }

        app_labels = discoverer.extract_app_labels(labels)

        # 应该包含所有 version 相关的标签
        assert "version" in app_labels
        assert "app-version" in app_labels

    def test_extract_empty_labels(self, discoverer):
        """测试空标签字典"""
        app_labels = discoverer.extract_app_labels({})

        assert app_labels == {}

    def test_extract_no_app_labels(self, discoverer):
        """测试没有应用标签的情况"""
        labels = {
            "env": "production",
            "team": "backend",
            "region": "us-west"
        }

        app_labels = discoverer.extract_app_labels(labels)

        assert app_labels == {}


class TestGetLabelSuggestions:
    """测试标签建议生成"""

    @pytest.fixture
    def discoverer(self):
        return LabelDiscoverer()

    def test_suggestions_for_simple_service(self, discoverer):
        """测试简单服务名的建议"""
        suggestions = discoverer.get_label_suggestions("myapp")

        assert len(suggestions) >= 2

        # 应该有 app.kubernetes.io/name 建议
        name_suggestion = next(s for s in suggestions if s["key"] == "app.kubernetes.io/name")
        assert name_suggestion["value"] == "myapp"

        # 应该有 app 建议
        app_suggestion = next(s for s in suggestions if s["key"] == "app")
        assert app_suggestion["value"] == "myapp"

    def test_suggestions_for_hyphenated_service(self, discoverer):
        """测试带连字符的服务名"""
        suggestions = discoverer.get_label_suggestions("myapp-api")

        # app 标签应该取第一部分
        app_suggestion = next(s for s in suggestions if s["key"] == "app")
        assert app_suggestion["value"] == "myapp"

    def test_suggestions_for_versioned_service(self, discoverer):
        """测试带版本信息的服务名"""
        suggestions = discoverer.get_label_suggestions("myapp-v1.0")

        # 应该包含版本建议
        version_suggestions = [s for s in suggestions if "version" in s["key"].lower()]
        assert len(version_suggestions) > 0

    def test_suggestions_include_reason(self, discoverer):
        """测试建议包含理由"""
        suggestions = discoverer.get_label_suggestions("myapp")

        for suggestion in suggestions:
            assert "reason" in suggestion
            assert len(suggestion["reason"]) > 0


class TestConvenienceFunction:
    """测试便捷函数"""

    def test_discover_labels_from_events(self):
        """测试全局便捷函数"""
        events = [
            {
                "context": {
                    "k8s": {
                        "labels": {"app": "test"}
                    }
                }
            }
        ]

        result = discover_labels_from_events(events)

        assert "total_labels" in result
        assert result["total_labels"] == 1


class TestLabelCategories:
    """测试标签分类常量"""

    def test_label_categories_exist(self):
        """测试标签分类定义存在"""
        assert isinstance(LABEL_CATEGORIES, dict)
        assert "application" in LABEL_CATEGORIES
        assert "kubernetes" in LABEL_CATEGORIES
        assert "monitoring" in LABEL_CATEGORIES

    def test_label_categories_have_keywords(self):
        """测试分类包含关键词"""
        for category, keywords in LABEL_CATEGORIES.items():
            assert isinstance(keywords, list)
            assert len(keywords) > 0
            assert all(isinstance(kw, str) for kw in keywords)
