"""
OpenTelemetry initialization helpers shared by backend services.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)


_DEFAULT_FASTAPI_EXCLUDED_URLS = ",".join(
    (
        "^/health$",
        "^/healthz$",
        "^/ready$",
        "^/readiness$",
        "^/live$",
        "^/liveness$",
    )
)


def _parse_resource_attributes(raw_attrs: str) -> Dict[str, str]:
    """Parse OTEL_RESOURCE_ATTRIBUTES (k=v,k2=v2) into a dict."""
    attrs: Dict[str, str] = {}
    if not raw_attrs:
        return attrs

    for attr in raw_attrs.split(","):
        if "=" not in attr:
            continue
        key, value = attr.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            attrs[key] = value
    return attrs


def _resolve_otlp_http_traces_endpoint() -> str:
    """
    Resolve OTLP HTTP traces endpoint and normalize to /v1/traces path.
    """
    endpoint = (
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    )
    if not endpoint:
        endpoint = "http://otel-collector.islap.svc.cluster.local:4318"
        logger.info("Using default OTLP endpoint: %s", endpoint)

    if "://" not in endpoint:
        endpoint = f"http://{endpoint}"

    parsed = urlsplit(endpoint)
    netloc = parsed.netloc
    if netloc.endswith(":4317"):
        netloc = f"{netloc[:-5]}:4318"

    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/v1/traces"):
        path = base_path
    elif not base_path:
        path = "/v1/traces"
    else:
        path = f"{base_path}/v1/traces"

    return urlunsplit((parsed.scheme or "http", netloc, path, "", ""))


def _is_otel_enabled() -> bool:
    """
    Backward-compatible OTEL switch.

    Priority:
    1) OTEL_ENABLED
    2) OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED
    """
    if "OTEL_ENABLED" in os.environ:
        return os.getenv("OTEL_ENABLED", "false").lower() == "true"
    return os.getenv("OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED", "false").lower() == "true"


def _resolve_fastapi_excluded_urls() -> str:
    """Resolve excluded URLs for FastAPI auto instrumentation."""
    return (
        os.getenv("OTEL_FASTAPI_EXCLUDED_URLS", "").strip()
        or os.getenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "").strip()
        or _DEFAULT_FASTAPI_EXCLUDED_URLS
    )


def init_otel(
    service_name: str = "service",
    service_version: str = "1.0.0",
    app: Any = None,
) -> None:
    """
    Initialize OpenTelemetry tracing for a service.

    When `app` is provided, instrument this FastAPI app instance directly.
    """
    enabled = _is_otel_enabled()
    if not enabled:
        logger.info("OpenTelemetry is disabled")
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "").strip() or service_name
    service_version = os.getenv("APP_VERSION", "").strip() or service_version

    logger.info("Initializing OpenTelemetry for %s...", service_name)
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        attrs = {SERVICE_NAME: service_name}
        if service_version:
            attrs["service.version"] = service_version
        attrs.update(_parse_resource_attributes(os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")))

        # Remove empty keys to avoid invalid resource attributes.
        attrs = {k: v for k, v in attrs.items() if str(k).strip()}
        resource = Resource.create(attrs)

        tracer_provider = TracerProvider(resource=resource)
        otlp_endpoint = _resolve_otlp_http_traces_endpoint()
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=2048,
                schedule_delay_millis=5000,
                max_export_batch_size=512,
            )
        )
        trace.set_tracer_provider(tracer_provider)

        excluded_urls = _resolve_fastapi_excluded_urls()
        try:
            if app is not None:
                if not getattr(getattr(app, "state", object()), "_logoscope_otel_instrumented", False):
                    FastAPIInstrumentor.instrument_app(
                        app,
                        tracer_provider=tracer_provider,
                        excluded_urls=excluded_urls,
                    )
                    if hasattr(app, "state"):
                        setattr(app.state, "_logoscope_otel_instrumented", True)
                    logger.info("Instrumented FastAPI app (excluded_urls=%s)", excluded_urls)
                else:
                    logger.info("FastAPI app already instrumented, skip duplicate")
            else:
                FastAPIInstrumentor().instrument(
                    tracer_provider=tracer_provider,
                    excluded_urls=excluded_urls,
                )
                logger.info("Instrumented FastAPI class (excluded_urls=%s)", excluded_urls)
        except Exception as exc:
            logger.warning("Failed to instrument FastAPI: %s", exc)

        try:
            RequestsInstrumentor().instrument(tracer_provider=tracer_provider)
            logger.info("Instrumented Requests")
        except Exception as exc:
            logger.warning("Failed to instrument Requests: %s", exc)

        logger.info(
            "OpenTelemetry initialized successfully (service: %s, endpoint: %s)",
            service_name,
            otlp_endpoint,
        )

    except Exception as exc:
        logger.error("Failed to initialize OpenTelemetry: %s", exc)


def init_opentelemetry(app: Any = None) -> None:
    """Backward-compatible alias."""
    init_otel(
        service_name=os.getenv("OTEL_SERVICE_NAME", "service"),
        service_version=os.getenv("APP_VERSION", "1.0.0"),
        app=app,
    )


__all__ = ["init_otel", "init_opentelemetry"]
