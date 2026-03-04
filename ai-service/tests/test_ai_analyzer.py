"""
AI 分析模块单元测试

测试 ai/analyzer.py 的核心功能：
- 问题模式识别
- 置信度计算
- 根因分析
- 解决方案推荐
- Trace 分析
"""
import pytest
from unittest.mock import Mock, MagicMock
from ai.analyzer import LogAnalyzer


class TestLogAnalyzerInit:
    """测试 LogAnalyzer 初始化"""

    def test_init_without_storage(self):
        """无 storage adapter 初始化"""
        analyzer = LogAnalyzer()
        assert analyzer.storage is None

    def test_init_with_storage(self):
        """带 storage adapter 初始化"""
        mock_storage = Mock()
        analyzer = LogAnalyzer(mock_storage)
        assert analyzer.storage == mock_storage


class TestProblemPatternMatching:
    """测试问题模式匹配"""

    @pytest.fixture
    def analyzer(self):
        return LogAnalyzer()

    def test_database_problem_detection(self, analyzer):
        """测试数据库问题识别"""
        log_data = {
            'event': {'raw': 'Database connection timeout: pool exhausted', 'level': 'error'},
            'entity': {'name': 'api-server'}
        }
        result = analyzer.analyze_log(log_data)

        assert result is not None
        assert 'overview' in result
        # AI 分析器返回中文问题名称
        problem = result['overview']['problem']
        assert len(problem) > 0  # 应该有问题描述
        assert result['overview']['severity'] in ['error', 'warning']

    def test_memory_problem_detection(self, analyzer):
        """测试内存问题识别"""
        log_data = {
            'event': {'raw': 'OutOfMemoryError: Java heap space. Cannot allocate 512MB', 'level': 'error'},
            'entity': {'name': 'worker-service'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'overview' in result
        problem = result['overview']['problem']
        assert len(problem) > 0  # 应该有问题描述

    def test_network_problem_detection(self, analyzer):
        """测试网络问题识别"""
        log_data = {
            'event': {'raw': 'Connection timeout: Failed to connect to backend-api:8080 after 30s', 'level': 'warn'},
            'entity': {'name': 'frontend'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'overview' in result
        problem = result['overview']['problem']
        assert len(problem) > 0  # 应该有问题描述

    def test_performance_problem_detection(self, analyzer):
        """测试性能问题识别"""
        log_data = {
            'event': {'raw': 'Slow query: SELECT * FROM logs took 5.2 seconds', 'level': 'warning'},
            'entity': {'name': 'database'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'overview' in result
        # 应该有问题描述

    def test_disk_problem_detection(self, analyzer):
        """测试磁盘问题识别"""
        log_data = {
            'event': {'raw': 'ERROR: No space left on device', 'level': 'error'},
            'entity': {'name': 'storage-service'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'overview' in result
        problem = result['overview']['problem']
        assert len(problem) > 0  # 应该有问题描述

    def test_auth_problem_detection(self, analyzer):
        """测试认证问题识别"""
        log_data = {
            'event': {'raw': 'Access denied: Invalid token', 'level': 'error'},
            'entity': {'name': 'auth-service'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'overview' in result
        problem = result['overview']['problem']
        assert len(problem) > 0  # 应该有问题描述

    def test_no_problem_matched(self, analyzer):
        """测试没有匹配到任何问题的情况"""
        log_data = {
            'event': {'raw': 'Service started successfully', 'level': 'info'},
            'entity': {'name': 'my-service'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'overview' in result
        # 应该返回默认分析


class TestConfidenceCalculation:
    """测试置信度计算"""

    @pytest.fixture
    def analyzer(self):
        return LogAnalyzer()

    def test_high_confidence_database_error(self, analyzer):
        """测试高置信度的数据库错误"""
        log_data = {
            'event': {'raw': 'Database connection pool exhausted after 30s timeout', 'level': 'error'},
            'entity': {'name': 'api-server'}
        }
        result = analyzer.analyze_log(log_data)

        # 多个关键词 + 错误指示器 + 正则匹配 = 高置信度
        confidence = result['overview']['confidence']
        assert confidence >= 0.7  # 至少 70% 置信度

    def test_low_confidence_single_keyword(self, analyzer):
        """测试低置信度的单关键词匹配"""
        log_data = {
            'event': {'raw': 'Query executed successfully', 'level': 'info'},
            'entity': {'name': 'database'}
        }
        result = analyzer.analyze_log(log_data)

        # 只有单个关键词，应该是低置信度或默认分析
        assert 'overview' in result

    def test_confidence_with_multiple_matches(self, analyzer):
        """测试多个匹配项的置信度"""
        log_data = {
            'event': {
                'raw': 'Database connection timeout: pool exhausted. SQL query failed after 30s',
                'level': 'error'
            },
            'entity': {'name': 'api-server'}
        }
        result = analyzer.analyze_log(log_data)

        # 多个关键词和错误指示器 = 高置信度
        confidence = result['overview']['confidence']
        assert confidence > 0.8  # 应该是高置信度


class TestRootCauseAnalysis:
    """测试根因分析"""

    @pytest.fixture
    def analyzer(self):
        return LogAnalyzer()

    def test_database_root_causes(self, analyzer):
        """测试数据库问题的根因分析"""
        log_data = {
            'event': {'raw': 'Database connection timeout: pool exhausted', 'level': 'error'},
            'entity': {'name': 'api-server'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'rootCauses' in result
        assert len(result['rootCauses']) > 0

        # 验证根因包含必要字段
        for cause in result['rootCauses']:
            assert 'title' in cause
            assert 'description' in cause
            assert 'icon' in cause or 'color' in cause

    def test_memory_root_causes(self, analyzer):
        """测试内存问题的根因分析"""
        log_data = {
            'event': {'raw': 'OutOfMemoryError: Java heap space', 'level': 'error'},
            'entity': {'name': 'worker'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'rootCauses' in result
        assert len(result['rootCauses']) > 0

        # 应该有根因分析
        assert len(result['rootCauses']) >= 1


class TestSolutionRecommendation:
    """测试解决方案推荐"""

    @pytest.fixture
    def analyzer(self):
        return LogAnalyzer()

    def test_database_solutions(self, analyzer):
        """测试数据库问题的解决方案"""
        log_data = {
            'event': {'raw': 'Database connection pool exhausted', 'level': 'error'},
            'entity': {'name': 'api-server'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'solutions' in result
        assert len(result['solutions']) > 0

        # 验证解决方案包含必要字段
        for solution in result['solutions']:
            assert 'title' in solution
            assert 'description' in solution

    def test_memory_solutions(self, analyzer):
        """测试内存问题的解决方案"""
        log_data = {
            'event': {'raw': 'OutOfMemoryError: Java heap space', 'level': 'error'},
            'entity': {'name': 'worker'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'solutions' in result
        assert len(result['solutions']) > 0

    def test_context_aware_solutions(self, analyzer):
        """测试 trace/链路上下文会增强解决方案"""
        log_data = {
            'event': {'raw': 'Database connection timeout: pool exhausted', 'level': 'error'},
            'entity': {'name': 'api-gateway'},
            'context': {
                'trace_id': 'trace-context-001',
                'source_service': 'api-gateway',
                'target_service': 'payment-service',
                'k8s': {'namespace': 'islap'},
            }
        }
        result = analyzer.analyze_log(log_data)

        solution_titles = {solution.get('title') for solution in result['solutions']}
        assert '基于 Trace 上下文回放链路' in solution_titles
        assert '优先排查关键调用链路' in solution_titles
        assert '限制命名空间排查范围' in solution_titles
        assert '复用历史相似案例处置路径' in solution_titles


class TestMetricsAnalysis:
    """测试影响指标分析"""

    @pytest.fixture
    def analyzer(self):
        return LogAnalyzer()

    def test_metrics_structure(self, analyzer):
        """测试指标结构"""
        log_data = {
            'event': {'raw': 'Database connection timeout', 'level': 'error'},
            'entity': {'name': 'api-server'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'metrics' in result
        assert isinstance(result['metrics'], list)

        # 验证指标包含必要字段
        for metric in result['metrics']:
            assert 'name' in metric
            assert 'value' in metric


class TestSimilarCases:
    """测试相似案例匹配"""

    @pytest.fixture
    def analyzer(self):
        return LogAnalyzer()

    def test_similar_cases_structure(self, analyzer):
        """测试相似案例结构"""
        log_data = {
            'event': {'raw': 'Database connection timeout', 'level': 'error'},
            'entity': {'name': 'api-server'}
        }
        result = analyzer.analyze_log(log_data)

        assert 'similarCases' in result
        assert isinstance(result['similarCases'], list)


class TestTraceAnalysis:
    """测试 Trace 分析"""

    def test_analyze_trace_without_storage(self):
        """测试没有 storage adapter 的 trace 分析"""
        analyzer = LogAnalyzer()
        result = analyzer.analyze_trace('trace-id-123')

        assert result is not None
        assert 'overview' in result

    def test_analyze_trace_with_empty_result(self):
        """测试查询结果为空的情况"""
        mock_storage = Mock()
        mock_storage.execute_query = Mock(return_value=[])

        analyzer = LogAnalyzer(mock_storage)
        result = analyzer.analyze_trace('trace-id-123')

        assert result is not None
        assert 'overview' in result

    def test_analyze_trace_with_data(self):
        """测试有数据的 trace 分析"""
        # Mock trace 数据
        mock_trace_data = [
            ('trace-id-123', 'span-1', '', 'api-server', 'GET /api/users', 1000, 50, 'ok'),
            ('trace-id-123', 'span-2', 'span-1', 'database', 'SELECT * FROM users', 1010, 30, 'ok'),
        ]

        mock_storage = Mock()
        mock_storage.execute_query = Mock(return_value=mock_trace_data)

        analyzer = LogAnalyzer(mock_storage)
        result = analyzer.analyze_trace('trace-id-123')

        assert result is not None


class TestEdgeCases:
    """测试边界情况"""

    @pytest.fixture
    def analyzer(self):
        return LogAnalyzer()

    def test_empty_log_data(self, analyzer):
        """测试空日志数据"""
        log_data = {}
        result = analyzer.analyze_log(log_data)

        assert result is not None
        assert 'overview' in result

    def test_missing_event_field(self, analyzer):
        """测试缺少 event 字段"""
        log_data = {
            'entity': {'name': 'test-service'}
        }
        result = analyzer.analyze_log(log_data)

        assert result is not None

    def test_missing_entity_field(self, analyzer):
        """测试缺少 entity 字段"""
        log_data = {
            'event': {'raw': 'Error occurred', 'level': 'error'}
        }
        result = analyzer.analyze_log(log_data)

        assert result is not None

    def test_unicode_characters(self, analyzer):
        """测试 Unicode 字符"""
        log_data = {
            'event': {'raw': '数据库连接超时 🔥 Connection timeout', 'level': 'error'},
            'entity': {'name': '测试服务'}
        }
        result = analyzer.analyze_log(log_data)

        assert result is not None
        assert 'overview' in result

    def test_very_long_message(self, analyzer):
        """测试超长日志消息"""
        long_message = 'Error: ' + 'A' * 10000
        log_data = {
            'event': {'raw': long_message, 'level': 'error'},
            'entity': {'name': 'test-service'}
        }
        result = analyzer.analyze_log(log_data)

        assert result is not None


class TestAnalyzeLogStructure:
    """测试 analyze_log 返回结构"""

    @pytest.fixture
    def analyzer(self):
        return LogAnalyzer()

    def test_return_structure(self, analyzer):
        """测试返回数据结构"""
        log_data = {
            'event': {'raw': 'Database connection timeout', 'level': 'error'},
            'entity': {'name': 'api-server'}
        }
        result = analyzer.analyze_log(log_data)

        # 验证顶层结构
        assert 'overview' in result
        assert 'rootCauses' in result
        assert 'solutions' in result
        assert 'metrics' in result
        assert 'similarCases' in result

        # 验证 overview 结构
        overview = result['overview']
        assert 'problem' in overview
        assert 'severity' in overview
        assert 'description' in overview
        assert 'confidence' in overview
        assert isinstance(overview['confidence'], float)
        assert 0 <= overview['confidence'] <= 1
