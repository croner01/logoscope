"""
normalizer 模块单元测试
"""

import unittest
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent / "semantic-engine"))

from normalize import normalizer
from normalize import service_name_enhanced


class TestServiceNameExtraction(unittest.TestCase):
    """服务名提取测试"""
    
    def test_standard_otel_service_name(self):
        """测试标准 OTel 服务名"""
        log_data = {
            "service.name": "test-service",
            "timestamp": "2026-02-11T10:00:00Z"
        }
        result = normalizer.extract_service_name(log_data)
        self.assertEqual(result, "test-service")
    
    def test_unknown_service_with_pod_name(self):
        """测试未知服务名，从 Pod 名称提取"""
        log_data = {
            "service.name": "unknown",
            "kubernetes": {
                "pod": "frontend-5d9f8c5f9-a1b2c"  # 标准Deployment格式：name-hash-5chars (纯十六进制)
            },
            "timestamp": "2026-02-11T10:00:00Z"
        }
        result = service_name_enhanced.extract_service_name_enhanced(log_data)
        self.assertEqual(result, "frontend")
    
    def test_coredns_service_name(self):
        """测试 coredns 特殊处理"""
        log_data = {
            "kubernetes": {
                "namespace": "kube-system",
                "pod": "coredns-5d7c9df6b9-abc12"
            },
            "timestamp": "2026-02-11T10:00:00Z"
        }
        result = service_name_enhanced.extract_service_name_enhanced(log_data)
        self.assertEqual(result, "coredns")
    
    def test_trace_id_generation(self):
        """测试 trace_id 自动生成"""
        log_data = {
            "service_name": "test-service",
            "timestamp": "2026-02-11T10:00:00Z"
        }
        result = normalizer.extract_trace_id(log_data)
        self.assertIsNotNone(result)
        self.assertGreater(len(result), 10)  # trace_id 应该有合理长度


class TestLogNormalization(unittest.TestCase):
    """日志标准化测试"""
    
    def test_normalize_minimal_log(self):
        """测试最小日志标准化"""
        log_data = {
            "message": "Test log message",
            "timestamp": "2026-02-11T10:00:00Z"
        }
        result = normalizer.normalize_log(log_data)
        
        self.assertIn("id", result)
        self.assertIn("timestamp", result)
        self.assertIn("entity", result)
        self.assertIn("event", result)
        self.assertIn("context", result)
        
    def test_log_levels(self):
        """测试日志级别提取"""
        levels = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
        for level in levels:
            log_data = {"level": level, "message": "test"}
            result = normalizer.extract_log_level(log_data)
            self.assertEqual(result, level.lower())


class TestK8sContextExtraction(unittest.TestCase):
    """K8s 上下文提取测试"""
    
    def test_full_k8s_context(self):
        """测试完整 K8s 上下文提取"""
        log_data = {
            "kubernetes": {
                "namespace_name": "islap",
                "pod_name": "test-pod",
                "node_name": "ren",
                "container_name": "test-container",
                "labels": {
                    "app": "test-app",
                    "version": "v1.0"
                }
            }
        }
        result = normalizer.extract_k8s_context(log_data)
        
        self.assertEqual(result["namespace"], "islap")
        self.assertEqual(result["pod"], "test-pod")
        self.assertEqual(result["node"], "ren")
        self.assertIn("labels", result)


if __name__ == "__main__":
    unittest.main()
