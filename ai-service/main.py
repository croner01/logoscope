"""
AI Service 主应用
负责 LLM、会话、案例库与 follow-up 能力。
"""
import asyncio
import logging
import os
import sys

from fastapi import FastAPI
import uvicorn

_SHARED_LIB_CANDIDATES = (
    os.getenv("LOGOSCOPE_SHARED_LIB", ""),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "shared_src")),
    "/app/shared_lib",
)
for _candidate in _SHARED_LIB_CANDIDATES:
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.append(_candidate)

from config import config
from storage.adapter import StorageAdapter
from api.ai import router as ai_router
from api.ai import set_storage_adapter as set_ai_storage
from api.ai import shutdown_background_tasks as shutdown_ai_background_tasks
from platform_kernel.fastapi_kernel import install_common_fastapi_handlers
from utils.logging_config import get_logger, setup_logging

setup_logging(
    service_name=config.app_name,
    level=getattr(logging, str(config.log_level).upper(), logging.INFO),
    log_format=os.getenv("LOG_FORMAT", "text"),
)
logger = get_logger(__name__)
storage = None


app = FastAPI(
    title="AI Service",
    description="Logoscope AI Service - LLM/会话/案例库/follow-up",
    version=config.app_version,
)

_common_handlers = install_common_fastapi_handlers(app, logger=logger)
request_id_middleware = _common_handlers["request_id_middleware"]
http_exception_handler = _common_handlers["http_exception_handler"]
validation_exception_handler = _common_handlers["validation_exception_handler"]
unhandled_exception_handler = _common_handlers["unhandled_exception_handler"]

try:
    from otel_init import init_otel

    init_otel(
        service_name=config.app_name,
        service_version=config.app_version,
        app=app,
    )
except Exception as exc:
    logger.warning("Failed to initialize OpenTelemetry: %s", exc)


@app.on_event("startup")
async def startup_event():
    global storage

    storage = StorageAdapter(config.get_storage_config())
    await asyncio.to_thread(set_ai_storage, storage)


@app.on_event("shutdown")
async def shutdown_event():
    try:
        shutdown_ai_background_tasks()
    except Exception as exc:
        logger.warning("Failed to shutdown AI background tasks cleanly: %s", exc)
    if storage:
        try:
            storage.close()
        except Exception as exc:
            logger.warning("Failed to close storage cleanly: %s", exc)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "ai-service",
        "version": config.app_version,
    }


@app.get("/")
async def root():
    return {
        "service": "AI Service",
        "version": config.app_version,
        "description": "Logoscope AI Service - LLM/会话/案例库/follow-up",
    }


app.include_router(ai_router)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=config.log_level,
        access_log=False,
        log_config=None,
    )
