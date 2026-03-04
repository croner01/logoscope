# OpenTelemetry 快速集成模板

## Python 服务 OTel 集成

### 1. 准备 otel_init.py 模块

将此文件复制到你的项目根目录：

```python
"""
OpenTelemetry 初始化模块
为 Python 服务添加自动追踪
"""
import logging
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
import os

logger = logging.getLogger(__name__)

def init_opentelemetry():
    """
    初始化 OpenTelemetry tracing
    """
    enabled = os.getenv("OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED", "false").lower() == "true"

    if not enabled:
        logger.info("OpenTelemetry disabled")
        return

    logger.info("Initializing OpenTelemetry...")

    try:
        # 1. 创建 Resource
        service_name = os.getenv("OTEL_SERVICE_NAME", "unknown-service")
        resource_attributes = os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")

        attrs = {SERVICE_NAME: service_name}
        if resource_attributes:
            for attr in resource_attributes.split(','):
                if '=' in attr:
                    key, value = attr.split('=', 1)
                    attrs[key.strip()] = value.strip()

        resource = Resource.create(attrs)

        # 2. 创建 TracerProvider
        tracer_provider = TracerProvider(resource=resource)

        # 3. 配置 Exporter
        otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector.islap.svc.cluster.local:4318")

        # 确保 endpoint 使用 HTTP 端口
        if otlp_endpoint.endswith(":4317"):
            otlp_endpoint = otlp_endpoint.replace(":4317", ":4318")

        exporter = OTLPSpanExporter(endpoint=otlp_endpoint + "/v1/traces")

        # 4. 添加 Processor
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=2048,
                schedule_delay_millis=5000,
                max_export_batch_size=512
            )
        )

        # 5. 设置全局 TracerProvider
        trace.set_tracer_provider(tracer_provider)

        # 6. 自动插桩
        try:
            FastAPIInstrumentor().instrument(tracer_provider=tracer_provider)
            logger.info("✓ FastAPI auto-instrumented")
        except Exception as e:
            logger.warning(f"FastAPI instrumentation failed: {e}")

        try:
            RequestsInstrumentor().instrument(tracer_provider=tracer_provider)
            logger.info("✓ Requests auto-instrumented")
        except Exception as e:
            logger.warning(f"Requests instrumentation failed: {e}")

        # 7. 测试创建一个 span
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("initialization") as span:
            span.set_attribute("init.success", True)

        logger.info(f"✓ OpenTelemetry initialized (service: {service_name}, endpoint: {otlp_endpoint})")

    except Exception as e:
        logger.error(f"Failed to initialize OpenTelemetry: {e}")
        # 不中断应用启动
