"""
增强服务名识别模块

降低 unknown 比例（从 7.5% 到 <1%）
支持更多数据源和启发式规则
"""
import re
from typing import Dict, Any


def extract_service_name_enhanced(log_data: Dict[str, Any]) -> str:
    """
    增强的服务名提取 - 多层回退策略

    Args:
        log_data: 原始日志数据

    Returns:
        str: 服务名称，优先返回最精确的值
    """
    # ========== 第一优先级：OTel 标准字段 ==========
    service_name = log_data.get("service.name") or \
                  log_data.get("resource", {}).get("service.name")
    if service_name and service_name != "unknown":
        return service_name

    # ========== 注入服务 ==========
    service_name = log_data.get("service_name")
    if service_name:
        return service_name

    # ========== 第二优先级：K8s 元数据 ==========
    k8s_context = log_data.get("kubernetes", {})

    # 如果 k8s_context 是空字典，尝试从其他位置获取
    if not k8s_context:
        # 尝试从 resource 获取
        resource = log_data.get("resource", {})
        if "kubernetes" in resource:
            k8s_context = resource.get("kubernetes", {})
        # 尝试从 attributes 获取
        attributes = log_data.get("attributes", {})
        if "kubernetes" in attributes:
            k8s_context = attributes.get("kubernetes", {})
        # 尝试从 _raw_attributes 获取
        raw_attrs = log_data.get("_raw_attributes", {})
        if isinstance(raw_attrs, dict):
            if "kubernetes" in raw_attrs:
                k8s_context = raw_attrs.get("kubernetes", {})
            # 也检查嵌套的 kubernetes
            for key, value in raw_attrs.items():
                if isinstance(value, dict) and "pod_name" in value:
                    k8s_context = value
                    break

    # 2.1 从 pod annotations 提取
    service_name = k8s_context.get("annotations", {}).get("app.kubernetes.io/name") or \
                  k8s_context.get("annotations", {}).get("app.kubernetes.io/instance")
    if service_name:
        return service_name

    # 2.2 从 pod labels 提取（按优先级）
    labels = k8s_context.get("labels", {})
    if isinstance(labels, dict):
        service_name = labels.get("app.kubernetes.io/name") or \
                      labels.get("app") or \
                      labels.get("app.kubernetes.io/instance") or \
                      labels.get("application") or \
                      labels.get("service")
        if service_name:
            return service_name

    # ========== 第三优先级：容器信息 ==========
    # 3.1 从 container 镜像名提取
    container_image = k8s_context.get("container_image", "")
    if container_image:
        # 提取镜像名（如 docker.io/library/nginx:1.21 -> nginx）
        if "/" in container_image:
            image_name = container_image.split("/")[-1]
            if ":" in image_name:
                image_name = image_name.split(":")[0]
            if image_name and image_name not in ["pause", "POD"]:
                return image_name

    # 3.2 从 container name 提取
    container_name = k8s_context.get("container_name", "")
    if container_name:
        # 清理 hash 后缀（如 log-generator-568b584664-fv422 -> log-generator）
        clean_name = re.sub(r'-[a-f0-9]{8,10}-(?:[a-f0-9]{8,10}-)?[a-f0-9]{4,12}$', '', container_name)
        if clean_name:
            return clean_name

    # ========== 第四优先级：Pod 名称启发式提取 ==========
    pod_name = k8s_context.get("pod", "") or k8s_context.get("pod_name", "")

    # 4.1 StatefulSet Pod 格式（name-0, name-1）
    if pod_name:
        match = re.match(r'^(.+)-\d+$', pod_name)
        if match:
            return match.group(1)

    # 4.2 Deployment Pod 格式（deployment-hash）
    if pod_name:
        match = re.match(r'^(.+)-[a-f0-9]{8,10}-[a-f0-9]{5}$', pod_name)
        if match:
            return match.group(1)

    # 4.3 DaemonSet Pod 格式
    if pod_name:
        match = re.match(r'^(.+)-[a-f0-9]{8,10}$', pod_name)
        if match:
            return match.group(1)

    # ========== 第五优先级：已知服务前缀映射 ==========
    # 常见的服务前缀，推断实际服务名
    if pod_name:
        # coredns -> coredns
        if pod_name.startswith("coredns"):
            return "coredns"

        # otel-collector -> otel-collector
        if pod_name.startswith("otel-collector"):
            return "otel-collector"

        # fluent-bit -> fluent-bit
        if pod_name.startswith("fluent-bit"):
            return "fluent-bit"

    # ========== 第六优先级：命名空间启发式 ==========
    namespace = k8s_context.get("namespace", "")

    # 如果 pod 在特定命名空间，使用命名空间作为服务名
    if namespace == "kube-system":
        # 系统组件，使用 pod 名前缀
        if pod_name:
            prefix = pod_name.split("-")[0]
            return prefix
    elif namespace == "islap":
        # 应用命名空间，使用 pod 名
        if pod_name and "-" in pod_name:
            prefix = pod_name.split("-")[0]
            if len(prefix) > 2:  # 至少 2 个字符
                return prefix

    # ========== 第七优先级：通用字段回退 ==========
    service_name = log_data.get("app") or \
                  log_data.get("service") or \
                  log_data.get("service_name") or \
                  log_data.get("application")
    if service_name:
        return service_name

    # ========== 第八优先级：从消息内容提取 ==========
    # 对于 Fluent Bit 采集的原始日志，尝试从消息中提取服务名
    message = log_data.get("message") or log_data.get("body") or ""
    if message:
        # 匹配标准容器日志格式：时间戳 + 流 + 标志 + 内容
        # 例如：log=2026-02-10T23:34:52.638588363+08:00 stdout F ...
        # 提取标准日志中的时间戳后的内容
        # 格式1: "log=2026-02-10T23:34:52.638588363+08:00 stdout F 内容"
        stdout_pattern = r'(?:log=)?\d{4}-\d{2}-\d{2}T[\d:.]+[+-]\d{2}:\d{2}\s+(stdout|stderr)\s+F\s+(.+)'
        match = re.search(stdout_pattern, message[:200])  # 只检查前 200 字符
        if match:
            content = match.group(2)
            # 从内容中提取可能的服务名
            # 常见格式：服务名 [进程] 日志内容
            service_pattern = r'^([a-zA-Z][a-zA-Z0-9_-]{2,30})\s+\['
            service_match = re.search(service_pattern, content)
            if service_match:
                return service_match.group(1)

            # 格式2: 时间戳 + 服务名 + 日志级别
            # 例如：2026-02-10 15:34:52 INFO service-name ...
            log_time_pattern = r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+(?:INFO|WARN|ERROR|DEBUG)\s+([a-zA-Z][a-zA-Z0-9_-]{2,30})\s'
            log_match = re.search(log_time_pattern, content[:100])
            if log_match:
                return log_match.group(1)

        # 如果消息本身包含服务标识（如 Python logging 格式）
        # 例如：[semantic-engine] INFO: ...
        bracket_pattern = r'\[([a-zA-Z][a-zA-Z0-9_-]{2,30})\]'
        bracket_match = re.search(bracket_pattern, message[:100])
        if bracket_match:
            potential_service = bracket_match.group(1)
            # 排除常见的非服务名
            if potential_service not in ['INFO', 'WARN', 'ERROR', 'DEBUG', 'INFO', 'main', 'root']:
                return potential_service

    # ========== 第九优先级：resource 字段 ==========
    resource = log_data.get("resource", {})
    if isinstance(resource, dict):
        service_name = resource.get("service.name") or \
                      resource.get("service") or \
                      resource.get("app")
        if service_name:
            return service_name

    # ========== 最后回退：使用 pod 名或实例 ID ==========
    return pod_name or log_data.get("instance", "unknown")


def test_service_name_extraction():
    """测试服务名提取函数"""
    test_cases = [
        # 测试用例：(输入, 期望输出, 描述)
        ({"kubernetes": {"pod": "log-generator-568b584664-fv422"}}, "log-generator", "StatefulSet pod"),
        ({"kubernetes": {"pod": "semantic-engine-7d6f8c9d-abc12"}}, "semantic-engine", "Deployment pod"),
        ({"kubernetes": {"pod": "coredns-5d798b6f7-xyz"}}, "coredns", "DaemonSet pod with prefix"),
        ({"kubernetes": {"container_image": "docker.io/library/redis:7-alpine"}}, "redis", "Container image"),
        ({"kubernetes": {"labels": {"app": "my-app"}}}, "my-app", "Label app"),
        ({"kubernetes": {"namespace": "kube-system", "pod": "etcd-ren"}}, "etcd", "System namespace"),
        ({"service.name": "frontend-api"}, "frontend-api", "OTel standard field"),
        ({}, "unknown", "Empty data"),
    ]

    print("服务名提取测试：")
    print(f"{'输入':<40} | {'期望':<20} | {'实际':<20} | {'结果':<10}")
    print("-" * 90)

    passed = 0
    failed = 0

    for input_data, expected, description in test_cases:
        result = extract_service_name_enhanced(input_data)
        status = "✅" if result == expected else "❌"
        if result == expected:
            passed += 1
        else:
            failed += 1

        input_str = str(input_data) if input_data else '空数据'
        print(f"{input_str:<40} | {expected:<20} | {result:<20} | {status}")

    print("-" * 90)
    print(f"通过: {passed}/{len(test_cases)} ({passed*100/len(test_cases):.1f}%)")

    return passed == len(test_cases)


if __name__ == "__main__":
    test_service_name_extraction()
