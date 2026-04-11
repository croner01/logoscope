"""Shared FastAPI middleware/exception/CORS utilities."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Tuple
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:
    from shared_src.utils.logging_config import (
        clear_request_context,
        is_health_check_path,
        set_request_context,
    )
except ImportError:
    from utils.logging_config import (
        clear_request_context,
        is_health_check_path,
        set_request_context,
    )


def error_payload(code: str, message: str, request_id: str, detail: Any = None) -> Dict[str, Any]:
    """Build a standard API error payload."""
    payload: Dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
    }
    payload["detail"] = message if detail is None else detail
    return payload


def _extract_trace_span_from_headers(request: Request) -> Tuple[str, str]:
    """Extract trace_id/span_id from standard/custom headers."""
    trace_id = (request.headers.get("X-Trace-ID") or "").strip()
    span_id = (request.headers.get("X-Span-ID") or "").strip()
    trace_parent = (request.headers.get("traceparent") or "").strip()

    if trace_parent:
        parts = trace_parent.split("-")
        if len(parts) >= 4:
            trace_id = trace_id or parts[1].strip()
            span_id = span_id or parts[2].strip()
    return trace_id, span_id


def _extract_trace_span_from_otel_context() -> Tuple[str, str]:
    """Extract active OpenTelemetry trace/span ids, if available."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore

        span = otel_trace.get_current_span()
        if span is None:
            return "", ""
        span_context = span.get_span_context()
        if not span_context or not getattr(span_context, "is_valid", False):
            return "", ""

        trace_id_value = int(getattr(span_context, "trace_id", 0) or 0)
        span_id_value = int(getattr(span_context, "span_id", 0) or 0)
        trace_id = f"{trace_id_value:032x}" if trace_id_value > 0 else ""
        span_id = f"{span_id_value:016x}" if span_id_value > 0 else ""
        return trace_id, span_id
    except Exception:
        return "", ""


def create_request_id_middleware(
    logger: logging.Logger | None = None,
) -> Callable[[Request, Callable[[Request], Awaitable[Any]]], Awaitable[Any]]:
    """Create request-id middleware shared by services."""

    async def request_id_middleware(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        request_started = time.perf_counter()
        header_request_id = (request.headers.get("X-Request-ID") or "").strip()
        request_id = header_request_id if header_request_id else uuid4().hex
        trace_id, span_id = _extract_trace_span_from_headers(request)
        if not trace_id or not span_id:
            otel_trace_id, otel_span_id = _extract_trace_span_from_otel_context()
            trace_id = trace_id or otel_trace_id
            span_id = span_id or otel_span_id
        client_ip = str((request.client.host if request.client else "") or "")
        method = str(request.method or "")
        path = str(request.url.path or "")

        request.state.request_id = request_id
        set_request_context(
            request_id=request_id,
            trace_id=trace_id,
            span_id=span_id,
            method=method,
            path=path,
            client_ip=client_ip,
        )
        response: Any = None
        status_code = 500
        outcome = "error"
        try:
            response = await call_next(request)
            status_code = int(getattr(response, "status_code", 500) or 500)
            outcome = "success" if status_code < 500 else "error"
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            if not trace_id or not span_id:
                otel_trace_id, otel_span_id = _extract_trace_span_from_otel_context()
                trace_id = trace_id or otel_trace_id
                span_id = span_id or otel_span_id
                if trace_id or span_id:
                    set_request_context(trace_id=trace_id, span_id=span_id)

            duration_ms = round((time.perf_counter() - request_started) * 1000.0, 2)
            set_request_context(
                status_code=status_code,
                duration_ms=duration_ms,
                outcome=outcome,
            )
            if logger and not is_health_check_path(path):
                logger.info(
                    "HTTP request completed",
                    extra={
                        "event_id": "http.request",
                        "action": "http.request",
                        "request_id": request_id,
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "method": method,
                        "path": path,
                        "status_code": status_code,
                        "duration_ms": duration_ms,
                        "outcome": outcome,
                        "client_ip": client_ip,
                    },
                )
            clear_request_context()

    return request_id_middleware


def create_http_exception_handler() -> Callable[[Request, HTTPException], Awaitable[JSONResponse]]:
    """Create the standard HTTPException handler."""

    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = getattr(request.state, "request_id", uuid4().hex)
        status_code = int(getattr(exc, "status_code", 500) or 500)

        if status_code >= 500:
            code = "INTERNAL_SERVER_ERROR"
            message = "Internal server error"
            detail = "Internal server error"
        else:
            detail = getattr(exc, "detail", "")
            if isinstance(detail, dict):
                code = str(detail.get("code") or f"HTTP_{status_code}")
                message = str(detail.get("message") or detail.get("detail") or "Request failed")
            else:
                code = f"HTTP_{status_code}"
                message = str(detail or "Request failed")

        return JSONResponse(
            status_code=status_code,
            content=error_payload(code=code, message=message, request_id=request_id, detail=detail),
            headers={"X-Request-ID": request_id},
        )

    return http_exception_handler


def create_validation_exception_handler(
    logger: logging.Logger,
) -> Callable[[Request, RequestValidationError], Awaitable[JSONResponse]]:
    """Create the standard request validation handler."""

    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", uuid4().hex)
        logger.warning("Request validation failed: %s", exc.errors())
        return JSONResponse(
            status_code=422,
            content=error_payload(
                code="VALIDATION_ERROR",
                message="Request validation failed",
                request_id=request_id,
                detail=exc.errors(),
            ),
            headers={"X-Request-ID": request_id},
        )

    return validation_exception_handler


def create_unhandled_exception_handler(
    logger: logging.Logger,
) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    """Create the standard unhandled exception handler."""

    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", uuid4().hex)
        logger.exception("Unhandled exception (request_id=%s): %s", request_id, exc)
        return JSONResponse(
            status_code=500,
            content=error_payload(
                code="INTERNAL_SERVER_ERROR",
                message="Internal server error",
                request_id=request_id,
                detail="Internal server error",
            ),
            headers={"X-Request-ID": request_id},
        )

    return unhandled_exception_handler


def install_common_fastapi_handlers(app: FastAPI, logger: logging.Logger) -> Dict[str, Callable[..., Any]]:
    """Install shared middleware and exception handlers on a FastAPI app."""
    request_id_middleware = create_request_id_middleware(logger=logger)
    http_exception_handler = create_http_exception_handler()
    validation_exception_handler = create_validation_exception_handler(logger)
    unhandled_exception_handler = create_unhandled_exception_handler(logger)

    app.middleware("http")(request_id_middleware)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    return {
        "request_id_middleware": request_id_middleware,
        "http_exception_handler": http_exception_handler,
        "validation_exception_handler": validation_exception_handler,
        "unhandled_exception_handler": unhandled_exception_handler,
    }


def resolve_cors_config(default_origins: str) -> Tuple[list[str], bool]:
    """Resolve CORS origins/credentials from env with safe defaults."""
    origins_raw = os.getenv("CORS_ORIGINS", default_origins)
    origins = [item.strip() for item in origins_raw.split(",") if item.strip()]
    if not origins:
        origins = ["http://localhost:3000", "http://localhost:5173"]
    allow_credentials = os.getenv("CORS_ALLOW_CREDENTIALS", "true").lower() == "true"
    return origins, allow_credentials


def install_cors(app: FastAPI, logger: logging.Logger, default_origins: str) -> None:
    """Install CORS middleware with safe wildcard+credentials guard."""
    cors_origins, cors_allow_credentials = resolve_cors_config(default_origins)
    if cors_origins == ["*"] and cors_allow_credentials:
        logger.warning(
            "SECURITY WARNING: CORS configured with allow_origins=['*'] and allow_credentials=True. "
            "This is insecure. Please set CORS_ORIGINS environment variable."
        )
        cors_allow_credentials = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=cors_allow_credentials,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
