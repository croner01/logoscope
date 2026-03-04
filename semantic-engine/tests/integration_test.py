#!/usr/bin/env python3
"""
后端集成测试脚本
测试不同数据类型的写入、查询验证、字段完整性检查
"""
import asyncio
import aiohttp
import json
from datetime import datetime, timezone
import sys
import os

# 测试配置
BASE_URL = "http://localhost:8000"
TEST_TIMEOUT = 30  # seconds


class Colors:
    """终端颜色输出"""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    RESET = "\3[0m"
    BOLD = "\033[1m"


def log(message, level="INFO"):
    """格式化日志输出"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = {
        "INFO": Colors.BLUE,
        "SUCCESS": Colors.GREEN,
        "WARNING": Colors.YELLOW,
        "ERROR": Colors.RED
    }.get(level, Colors.RESET)
    print(f"{color}[{timestamp}] [{level}] {message}{Colors.RESET}")


def print_section(title):
    """打印章节标题"""
    print(f"\n{Colors.BOLD}{'='*60}")
    print(f" {title}")
    print(f"{'='*60}{Colors.RESET}\n")


def print_test(test_name):
    """打印测试名称"""
    print(f"\n{Colors.YELLOW}▶ {test_name}{Colors.RESET}")


async def test_health_check():
    """测试健康检查接口"""
    print_test("健康检查")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/health", timeout=TEST_TIMEOUT) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    log("健康检查通过", "SUCCESS")
                    log(f"  状态: {data.get('status')}")
                    log(f"  服务: {data.get('service')}")
                    log(f"  版本: {data.get('version')}")
                    return True
                else:
                    log(f"健康检查失败: HTTP {resp.status}", "ERROR")
                    return False
    except Exception as e:
        log(f"健康检查异常: {e}", "ERROR")
        return False


async def test_ingest_log():
    """测试日志写入"""
    print_test("日志写入测试")

    test_event = {
        "id": "test-log-001",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entity": {
            "type": "service",
            "name": "test-api-service",
            "instance": "test-instance-1"
        },
        "event": {
            "type": "log",
            "level": "info",
            "raw": "Test log message from integration test"
        },
        "context": {
            "k8s": {
                "namespace": "default",
                "pod": "test-pod-123",
                "node": "node-1",
                "pod_id": "pod-id-abc",
                "container_name": "container-1",
                "container_id": "cont-id-xyz",
                "container_image": "nginx:1.21",
                "resources": {
                    "cpu_limit": "500m",
                    "cpu_request": "250m",
                    "memory_limit": "1Gi",
                    "memory_request": "512Mi"
                }
            },
            "trace_id": "trace-abc-123",
            "span_id": "span-def-456"
        },
        "severity_number": 9,
        "flags": 1,
        "labels": {
            "env": "test",
            "version": "1.0.0"
        },
        "relations": [
            {
                "type": "calls",
                "target": "database-service",
                "metadata": {"protocol": "http"}
            }
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            # 使用 /process 端点进行完整处理流程
            async with session.post(
                f"{BASE_URL}/process",
                json=test_event,
                timeout=TEST_TIMEOUT,
                headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    log("日志写入成功", "SUCCESS")
                    log(f"  状态: {result.get('status')}")
                    log(f"  事件ID: {result.get('event_id')}")
                    log(f"  事件类型: {result.get('event_type')}")
                    log(f"  关系数: {result.get('relations_count')}")

                    # 验证返回结果
                    assert result.get("status") == "success", "处理状态应为success"
                    assert result.get("event_id") == "test-log-001", "事件ID应匹配"
                    return True
                else:
                    text = await resp.text()
                    log(f"日志写入失败: HTTP {resp.status}", "ERROR")
                    log(f"  响应: {text[:200]}", "ERROR")
                    return False
    except Exception as e:
        log(f"日志写入异常: {e}", "ERROR")
        return False


async def test_ingest_metrics():
    """测试指标写入"""
    print_test("指标写入测试")

    test_metrics = {
        "metrics_data": [
            {
                "metric_name": "cpu_usage",
                "metric_value": 45.5,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "service_name": "test-api-service",
                "labels": {
                    "pod": "test-pod-123",
                    "namespace": "default",
                    "node": "node-1"
                }
            },
            {
                "metric_name": "memory_usage",
                "metric_value": 75.2,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "service_name": "test-api-service",
                "labels": {
                    "pod": "test-pod-123",
                    "namespace": "default"
                }
            }
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BASE_URL}/v1/metrics",
                json=test_metrics,
                timeout=TEST_TIMEOUT,
                headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    log("指标写入成功", "SUCCESS")
                    log(f"  状态: {result.get('status', 'unknown')}")
                    return True
                else:
                    text = await resp.text()
                    log(f"指标写入失败: HTTP {resp.status}", "ERROR")
                    log(f"  响应: {text[:200]}", "ERROR")
                    return False
    except Exception as e:
        log(f"指标写入异常: {e}", "ERROR")
        return False


async def test_ingest_traces():
    """测试追踪写入"""
    print_test("追踪写入测试")

    test_traces = {
        "traces_data": [
            {
                "trace_id": "trace-123-456",
                "span_id": "span-789",
                "parent_span_id": "",
                "operation_name": "GET /api/test",
                "duration_ns": 1500000,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "service_name": "test-api-service",
                "status_code": 200,
                "attributes": {
                    "http.method": "GET",
                    "http.url": "/api/test",
                    "http.status_code": "200"
                }
            },
            {
                "trace_id": "trace-123-456",
                "span_id": "span-790",
                "parent_span_id": "span-789",
                "operation_name": "db.query",
                "duration_ns": 500000,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "service_name": "test-api-service",
                "status_code": 200,
                "attributes": {}
            }
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BASE_URL}/v1/traces",
                json=test_traces,
                timeout=TEST_TIMEOUT,
                headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    log("追踪写入成功", "SUCCESS")
                    log(f"  状态: {result.get('status', 'unknown')}")
                    return True
                else:
                    text = await resp.text()
                    log(f"追踪写入失败: HTTP {resp.status}", "ERROR")
                    log(f"  响应: {text[:200]}", "ERROR")
                    return False
    except Exception as e:
        log(f"追踪写入异常: {e}", "ERROR")
        return False


async def test_query_events():
    """测试事件查询"""
    print_test("事件查询测试")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BASE_URL}/api/v1/events?limit=10",
                timeout=TEST_TIMEOUT
            ) as resp:
                if resp.status == 200:
                    events = await resp.json()
                    log(f"事件查询成功: 找到 {len(events)} 条事件", "SUCCESS")

                    # 验证数据结构
                    for event in events:
                        validate_event_structure(event)

                    # 检查unknown字段
                    check_unknown_fields(events)

                    return len(events) > 0
                else:
                    log(f"事件查询失败: HTTP {resp.status}", "ERROR")
                    return False
    except Exception as e:
        log(f"事件查询异常: {e}", "ERROR")
        return False


async def test_query_metrics():
    """测试指标查询"""
    print_test("指标查询测试")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BASE_URL}/api/v1/metrics?limit=10&service_name=test-api-service",
                timeout=TEST_TIMEOUT
            ) as resp:
                if resp.status == 200:
                    metrics = await resp.json()
                    log(f"指标查询成功: 找到 {len(metrics)} 条指标", "SUCCESS")

                    # 验证数据结构
                    for metric in metrics:
                        validate_metric_structure(metric)

                    return len(metrics) >= 0
                else:
                    log(f"指标查询失败: HTTP {resp.status}", "ERROR")
                    return False
    except Exception as e:
        log(f"指标查询异常: {e}", "ERROR")
        return False


async def test_query_traces():
    """测试追踪查询"""
    print_test("追踪查询测试")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BASE_URL}/api/v1/traces?limit=10&service_name=test-api-service",
                timeout=TEST_TIMEOUT
            ) as resp:
                if resp.status == 200:
                    traces = await resp.json()
                    log(f"追踪查询成功: 找到 {len(traces)} 条追踪", "SUCCESS")

                    # 验证数据结构
                    for trace in traces:
                        validate_trace_structure(trace)

                    return len(traces) >= 0
                else:
                    log(f"追踪查询失败: HTTP {resp.status}", "ERROR")
                    return False
    except Exception as e:
        log(f"追踪查询异常: {e}", "ERROR")
        return False


async def test_topology():
    """测试拓扑查询"""
    print_test("拓扑查询测试")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BASE_URL}/api/v1/graph/topology?limit=100&source=auto",
                timeout=TEST_TIMEOUT
            ) as resp:
                if resp.status == 200:
                    topology = await resp.json()
                    log("拓扑查询成功", "SUCCESS")

                    nodes = topology.get("nodes", [])
                    edges = topology.get("edges", [])

                    log(f"  节点数: {len(nodes)}")
                    log(f"  边数: {len(edges)}")

                    # 验证数据结构
                    for node in nodes:
                        validate_node_structure(node)

                    for edge in edges:
                        validate_edge_structure(edge)

                    return True
                else:
                    log(f"拓扑查询失败: HTTP {resp.status}", "ERROR")
                    return False
    except Exception as e:
        log(f"拓扑查询异常: {e}", "ERROR")
        return False


def validate_event_structure(event):
    """验证事件数据结构"""
    # API 返回的是嵌套结构：entity.name, event.level, event.raw
    service_name = event.get("entity", {}).get("name", "unknown")
    level = event.get("event", {}).get("level", "unknown")
    message = event.get("event", {}).get("raw", "")

    # 检查核心字段
    required_fields = ["id", "timestamp"]
    for field in required_fields:
        if field not in event:
            log(f"  ❌ 缺少必需字段: {field}", "ERROR")

    # 检查嵌套字段
    if service_name == "unknown":
        log(f"  ❌ 服务名为unknown", "ERROR")
    if level == "unknown":
        log(f"  ❌ 日志级别为unknown", "ERROR")
    if not message:
        log(f"  ⚠️  消息为空", "WARNING")


def validate_metric_structure(metric):
    """验证指标数据结构"""
    required_fields = [
        "timestamp", "service_name", "metric_name", "value"
    ]

    for field in required_fields:
        if field not in metric:
            log(f"  ❌ 指标缺少必需字段: {field}", "ERROR")


def validate_trace_structure(trace):
    """验证追踪数据结构"""
    required_fields = [
        "trace_id", "span_id", "operation_name", "timestamp", "service_name"
    ]

    for field in required_fields:
        if field not in trace:
            log(f"  ❌ 追踪缺少必需字段: {field}", "ERROR")


def validate_node_structure(node):
    """验证节点数据结构"""
    required_fields = ["id"]

    for field in required_fields:
        if field not in node:
            log(f"  ❌ 节点缺少必需字段: {field}", "ERROR")

    if node.get("name") == "unknown":
        log(f"  ⚠️  节点名称为unknown: {node.get('id')}", "WARNING")


def validate_edge_structure(edge):
    """验证边数据结构"""
    required_fields = ["source", "target", "type"]

    for field in required_fields:
        if field not in edge:
            log(f"  ❌ 边缺少必需字段: {field}", "ERROR")


def check_unknown_fields(items, item_type="item"):
    """检查是否存在unknown字段"""
    unknown_count = 0

    for item in items:
        # 检查嵌套结构中的服务名
        service_name = item.get("entity", {}).get("name", "")
        if service_name == "unknown":
            log(f"  ⚠️  {item_type}的服务名为unknown", "WARNING")
            unknown_count += 1

        # 检查其他嵌套字段中的unknown
        namespace = item.get("context", {}).get("k8s", {}).get("namespace", "")
        if namespace == "unknown":
            log(f"  ⚠️  {item_type}的namespace为unknown", "WARNING")
            unknown_count += 1

        pod = item.get("context", {}).get("k8s", {}).get("pod", "")
        if pod == "unknown":
            log(f"  ⚠️  {item_type}的pod为unknown", "WARNING")
            unknown_count += 1

    if unknown_count == 0:
        log(f"  ✓ 未发现unknown字段", "SUCCESS")


async def main():
    """主测试流程"""
    print_section("后端集成测试")

    results = {
        "health_check": False,
        "ingest_log": False,
        "ingest_metrics": False,
        "ingest_traces": False,
        "query_events": False,
        "query_metrics": False,
        "query_traces": False,
        "topology": False
    }

    # 1. 健康检查
    results["health_check"] = await test_health_check()

    if not results["health_check"]:
        log("健康检查失败，终止测试", "ERROR")
        return

    # 2. 测试日志写入
    results["ingest_log"] = await test_ingest_log()

    # 等待一秒确保数据写入
    await asyncio.sleep(1)

    # 3. 测试指标写入
    results["ingest_metrics"] = await test_ingest_metrics()

    # 4. 测试追踪写入
    results["ingest_traces"] = await test_ingest_traces()

    # 等待一秒确保数据写入
    await asyncio.sleep(1)

    # 5. 测试查询接口
    results["query_events"] = await test_query_events()
    results["query_metrics"] = await test_query_metrics()
    results["query_traces"] = await test_query_traces()

    # 6. 测试拓扑
    results["topology"] = await test_topology()

    # 打印测试结果摘要
    print_section("测试结果摘要")

    total_tests = len(results)
    passed_tests = sum(1 for v in results.values() if v)

    for test_name, result in results.items():
        status = "✓ 通过" if result else "✗ 失败"
        status_color = Colors.GREEN if result else Colors.RED
        log(f"{test_name}: {status}", status_color)

    log(f"\n测试通过率: {passed_tests}/{total_tests} ({passed_tests*100//total_tests}%)",
          "SUCCESS" if passed_tests == total_tests else "WARNING")

    # 检查是否有错误需要修复
    if passed_tests < total_tests:
        print_section("需要修复的问题")
        log("请查看上方日志中的ERROR和WARNING信息", "WARNING")

    return passed_tests == total_tests


if __name__ == "__main__":
    try:
        success = asyncio.run(main())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        log("\n测试被用户中断", "WARNING")
        sys.exit(1)
    except Exception as e:
        log(f"测试过程中出现异常: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        sys.exit(1)
