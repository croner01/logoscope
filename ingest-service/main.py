"""
Ingest Service 主应用 - 解耦优化版本
OpenTelemetry 数据摄入服务

优化特性：
- 启动时不强制连接 Redis（懒加载）
- 健康检查增强（区分服务健康和依赖健康）
- 支持降级模式运行
- 日志带时间戳
"""
import logging
import sys
import os
from fastapi import FastAPI
from contextlib import asynccontextmanager
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
from api.ingest import router as ingest_router
from platform_kernel.fastapi_kernel import install_common_fastapi_handlers
from utils.logging_config import get_logger, setup_logging

setup_logging(
    service_name=config.app_name,
    level=getattr(logging, str(config.log_level).upper(), logging.INFO),
    log_format=os.getenv("LOG_FORMAT", "text"),
)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info(f"Starting {config.app_name} v{config.app_version}...")

    # 初始化队列写入服务（不强制连接 Redis）
    from services.queue_writer import init_queue_writer
    await init_queue_writer()

    yield

    logger.info(f"Shutting down {config.app_name}...")


# 创建 FastAPI 应用
app = FastAPI(
    title="Ingest Service",
    description="Logoscope 数据摄入服务 - OTLP 协议接收",
    version=config.app_version,
    lifespan=lifespan
)

_common_handlers = install_common_fastapi_handlers(app, logger=logger)
request_id_middleware = _common_handlers["request_id_middleware"]
http_exception_handler = _common_handlers["http_exception_handler"]
validation_exception_handler = _common_handlers["validation_exception_handler"]
unhandled_exception_handler = _common_handlers["unhandled_exception_handler"]

# 注册路由
app.include_router(ingest_router, tags=["ingest"])


@app.get("/health")
async def health_check():
    """
    健康检查接口 - 增强版本

    区分服务本身健康和依赖服务健康：
    - status: 服务本身状态（始终 healthy 表示服务可接收请求）
    - redis_connected: Redis 连接状态
    - mode: normal（正常模式）或 degraded（降级模式）

    Returns:
        Dict[str, any]: 健康状态信息
    """
    from services.queue_writer import get_stats, is_redis_connected

    stats = get_stats()
    redis_connected = is_redis_connected()

    return {
        "status": "healthy",  # 服务本身始终健康（可接收请求）
        "service": config.app_name,
        "version": config.app_version,
        "mode": "normal" if redis_connected else "degraded",
        "redis_connected": redis_connected,
        "memory_queue": {
            "size": stats.get("memory_queue_size", 0),
            "max_size": stats.get("memory_queue_max_size", 1000),
            "dropped": stats.get("dropped", 0)
        },
        "stats": {
            "total_written": stats.get("total_written", 0),
            "redis_written": stats.get("redis_written", 0),
            "memory_queued": stats.get("memory_queued", 0),
            "reconnect_attempts": stats.get("reconnect_attempts", 0)
        },
        "timestamp": __import__("datetime").datetime.utcnow().isoformat()
    }


@app.get("/ready")
async def readiness_check():
    """
    就绪检查接口

    用于 Kubernetes readinessProbe，服务启动后即可就绪
    不依赖 Redis 连接状态

    Returns:
        Dict[str, any]: 就绪状态
    """
    return {
        "ready": True,
        "service": config.app_name,
        "timestamp": __import__("datetime").datetime.utcnow().isoformat()
    }


@app.get("/")
async def root():
    """
    根路径接口

    Returns:
        Dict[str, any]: 服务信息
    """
    from services.queue_writer import is_redis_connected

    return {
        "service": "Ingest Service",
        "version": config.app_version,
        "description": "Logoscope OTLP 数据摄入服务",
        "mode": "normal" if is_redis_connected() else "degraded",
        "features": {
            "lazy_redis_connection": True,
            "memory_queue_fallback": True,
            "auto_reconnect": True
        }
    }


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=config.log_level,
        access_log=False,  # 统一使用应用层请求摘要日志
        log_config=None,
    )
