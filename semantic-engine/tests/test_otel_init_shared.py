"""
Tests for shared OTEL endpoint resolution compatibility.
"""

import os
import sys
import types
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.append(str(_PROJECT_ROOT))

from shared_src.utils import otel_init


def _install_fake_otel_modules(monkeypatch, calls):
    """Install fake OpenTelemetry modules for init_otel unit tests."""
    module_names = [
        "opentelemetry",
        "opentelemetry.trace",
        "opentelemetry.exporter",
        "opentelemetry.exporter.otlp",
        "opentelemetry.exporter.otlp.proto",
        "opentelemetry.exporter.otlp.proto.http",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.instrumentation",
        "opentelemetry.instrumentation.fastapi",
        "opentelemetry.instrumentation.requests",
        "opentelemetry.sdk",
        "opentelemetry.sdk.resources",
        "opentelemetry.sdk.trace",
        "opentelemetry.sdk.trace.export",
    ]
    for name in module_names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    trace_module = sys.modules["opentelemetry.trace"]

    def _set_tracer_provider(provider):
        calls["trace_set_provider"] = provider

    trace_module.set_tracer_provider = _set_tracer_provider
    sys.modules["opentelemetry"].trace = trace_module

    trace_exporter_module = sys.modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"]

    class OTLPSpanExporter:
        def __init__(self, endpoint):
            calls["otlp_endpoint"] = endpoint

    trace_exporter_module.OTLPSpanExporter = OTLPSpanExporter

    fastapi_module = sys.modules["opentelemetry.instrumentation.fastapi"]

    class FastAPIInstrumentor:
        @staticmethod
        def instrument_app(app, tracer_provider=None, excluded_urls=""):
            calls["instrument_app"] += 1
            calls["instrument_app_target"] = app
            calls["instrument_app_excluded_urls"] = excluded_urls
            calls["instrument_app_provider"] = tracer_provider

        def instrument(self, tracer_provider=None, excluded_urls=""):
            calls["instrument_class"] += 1
            calls["instrument_class_excluded_urls"] = excluded_urls
            calls["instrument_class_provider"] = tracer_provider

    fastapi_module.FastAPIInstrumentor = FastAPIInstrumentor

    requests_module = sys.modules["opentelemetry.instrumentation.requests"]

    class RequestsInstrumentor:
        def instrument(self, tracer_provider=None):
            calls["instrument_requests"] += 1
            calls["instrument_requests_provider"] = tracer_provider

    requests_module.RequestsInstrumentor = RequestsInstrumentor

    resources_module = sys.modules["opentelemetry.sdk.resources"]
    resources_module.SERVICE_NAME = "service.name"

    class Resource:
        @staticmethod
        def create(attrs):
            calls["resource_attrs"] = attrs
            return {"attrs": attrs}

    resources_module.Resource = Resource

    sdk_trace_module = sys.modules["opentelemetry.sdk.trace"]

    class TracerProvider:
        def __init__(self, resource=None):
            calls["tracer_provider_resource"] = resource

        def add_span_processor(self, processor):
            calls["span_processor"] = processor

    sdk_trace_module.TracerProvider = TracerProvider

    sdk_trace_export_module = sys.modules["opentelemetry.sdk.trace.export"]

    class BatchSpanProcessor:
        def __init__(self, exporter, **kwargs):
            calls["batch_exporter"] = exporter
            calls["batch_kwargs"] = kwargs

    sdk_trace_export_module.BatchSpanProcessor = BatchSpanProcessor


@pytest.mark.unit
class TestResolveOtlpHttpTracesEndpoint:
    """OTLP endpoint normalization tests."""

    def test_default_endpoint_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        result = otel_init._resolve_otlp_http_traces_endpoint()

        assert result == "http://otel-collector.islap.svc.cluster.local:4318/v1/traces"

    def test_convert_grpc_port_to_http_port(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

        result = otel_init._resolve_otlp_http_traces_endpoint()

        assert result == "http://otel-collector:4318/v1/traces"

    def test_keep_traces_path_without_duplication(self, monkeypatch):
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "http://otel-collector:4318/v1/traces",
        )
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://ignored:4318")

        result = otel_init._resolve_otlp_http_traces_endpoint()

        assert result == "http://otel-collector:4318/v1/traces"


@pytest.mark.unit
class TestOtelEnabledFlag:
    """Compatibility tests for OTEL enable flags."""

    def test_otel_enabled_has_higher_priority(self, monkeypatch):
        monkeypatch.setenv("OTEL_ENABLED", "false")
        monkeypatch.setenv("OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED", "true")

        assert otel_init._is_otel_enabled() is False

    def test_fallback_to_legacy_flag(self, monkeypatch):
        monkeypatch.delenv("OTEL_ENABLED", raising=False)
        monkeypatch.setenv("OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED", "true")

        assert otel_init._is_otel_enabled() is True


@pytest.mark.unit
class TestFastapiInstrumentation:
    """FastAPI instrumentation behavior tests."""

    def test_resolve_fastapi_excluded_urls_default(self, monkeypatch):
        monkeypatch.delenv("OTEL_FASTAPI_EXCLUDED_URLS", raising=False)
        monkeypatch.delenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", raising=False)

        result = otel_init._resolve_fastapi_excluded_urls()

        assert "^/health$" in result
        assert "^/ready$" in result

    def test_init_otel_instruments_given_app(self, monkeypatch):
        calls = {
            "instrument_app": 0,
            "instrument_class": 0,
            "instrument_requests": 0,
        }
        _install_fake_otel_modules(monkeypatch, calls)
        monkeypatch.setenv("OTEL_ENABLED", "true")
        monkeypatch.delenv("OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED", raising=False)
        monkeypatch.delenv("OTEL_FASTAPI_EXCLUDED_URLS", raising=False)
        monkeypatch.delenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", raising=False)

        class _State:
            pass

        class _App:
            state = _State()

        app = _App()

        otel_init.init_otel(service_name="test-svc", service_version="1.0.0", app=app)

        assert calls["instrument_app"] == 1
        assert calls["instrument_class"] == 0
        assert calls["instrument_requests"] == 1
        assert calls["resource_attrs"]["service.name"] == "test-svc"
        assert calls["otlp_endpoint"].endswith("/v1/traces")
        assert getattr(app.state, "_logoscope_otel_instrumented") is True

    def test_init_otel_skips_duplicate_app_instrumentation(self, monkeypatch):
        calls = {
            "instrument_app": 0,
            "instrument_class": 0,
            "instrument_requests": 0,
        }
        _install_fake_otel_modules(monkeypatch, calls)
        monkeypatch.setenv("OTEL_ENABLED", "true")
        monkeypatch.delenv("OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED", raising=False)

        class _State:
            _logoscope_otel_instrumented = True

        class _App:
            state = _State()

        app = _App()
        otel_init.init_otel(service_name="test-svc", service_version="1.0.0", app=app)

        assert calls["instrument_app"] == 0
        assert calls["instrument_class"] == 0
        assert calls["instrument_requests"] == 1
