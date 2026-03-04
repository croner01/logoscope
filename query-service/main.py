"""
Query Service - 数据查询服务
提供日志、指标、追踪数据的查询接口和实时 WebSocket 推送
"""
import logging
import sys
import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket

_SHARED_LIB_CANDIDATES = (
    os.getenv("LOGOSCOPE_SHARED_LIB", ""),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "shared_src")),
    "/app/shared_lib",
)
for _candidate in _SHARED_LIB_CANDIDATES:
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.append(_candidate)

from config import settings
from otel_init import init_otel
from storage.adapter import StorageAdapter
from api import query_routes, data_quality
from platform_kernel.fastapi_kernel import install_common_fastapi_handlers, install_cors
try:
    from shared_src.utils.logging_config import get_logger, setup_logging
except ImportError:
    from utils.logging_config import get_logger, setup_logging
from api.websocket import (
    websocket_logs_endpoint,
    websocket_topology_endpoint,
    log_poller,
    manager as ws_manager
)

setup_logging(
    service_name="query-service",
    level=settings.log_level_int,
    log_format=settings.log_format,
)
logger = get_logger(__name__)

# 全局 storage 实例
_storage_adapter: StorageAdapter = None
_health_preagg_last_error: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global _storage_adapter

    logger.info("Starting Query Service...")

    # 先设置一个空的 storage adapter，让服务可以启动
    # 实际初始化在后台进行
    _storage_adapter = None
    query_routes.set_storage_adapter(_storage_adapter)
    data_quality.set_storage_adapter(_storage_adapter)
    logger.info("API modules initialized (storage adapter lazy loading)")

    # 后台初始化 Storage Adapter
    async def init_storage_async():
        global _storage_adapter
        try:
            logger.info("Initializing Storage Adapter in background...")
            _storage_adapter = StorageAdapter({
                "clickhouse": {
                    "host": settings.clickhouse_host,
                    "port": settings.clickhouse_port,
                    "database": settings.clickhouse_database,
                    "user": settings.clickhouse_user,
                    "password": settings.clickhouse_password
                },
                "neo4j": {
                    "host": settings.neo4j_host,
                    "port": settings.neo4j_port,
                    "user": settings.neo4j_user,
                    "password": settings.neo4j_password,
                    "database": settings.neo4j_database
                }
            })
            query_routes.set_storage_adapter(_storage_adapter)
            data_quality.set_storage_adapter(_storage_adapter)
            logger.info("Storage Adapter initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Storage Adapter: {e}")
            # 即使初始化失败，服务也继续运行（降级模式）

    # 启动后台初始化任务
    init_task = asyncio.create_task(init_storage_async())

    # 启动日志轮询任务（备用实时推送方案）- 但不立即启动，等 storage adapter 就绪
    poller_task = None

    async def start_poller_when_ready():
        nonlocal poller_task
        while _storage_adapter is None:
            await asyncio.sleep(1)
        poller_task = asyncio.create_task(log_poller(_storage_adapter, interval=2.0))
        logger.info("Log poller task started")

    poller_starter = asyncio.create_task(start_poller_when_ready())

    logger.info("Query Service started successfully!")
    yield

    # 清理
    logger.info("Shutting down Query Service...")
    init_task.cancel()
    poller_starter.cancel()
    if poller_task:
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass
    if _storage_adapter:
        _storage_adapter.close()
    logger.info("Query Service shutdown complete")


# 创建 FastAPI 应用
app = FastAPI(
    title="Logoscope Query Service",
    description="数据查询服务 - 提供日志、指标、追踪数据的查询接口",
    version="1.0.0",
    lifespan=lifespan,
)

install_cors(
    app,
    logger=logger,
    default_origins="http://localhost:3000,http://localhost:5173",
)

_common_handlers = install_common_fastapi_handlers(app, logger=logger)
request_id_middleware = _common_handlers["request_id_middleware"]
http_exception_handler = _common_handlers["http_exception_handler"]
validation_exception_handler = _common_handlers["validation_exception_handler"]
unhandled_exception_handler = _common_handlers["unhandled_exception_handler"]

if settings.OTEL_ENABLED:
    init_otel(service_name="query-service", service_version="1.0.0", app=app)
    logger.info("OpenTelemetry initialized")

# 根路径
@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "query-service",
        "status": "ok",
        "version": "1.0.0"
    }

# 健康检查
@app.get("/health")
async def health_check():
    """健康检查端点 - 增强版本
    
    区分服务本身健康和依赖服务健康：
    - status: 服务本身状态（始终 healthy 表示服务可接收请求）
    - storage_connected: 存储适配器连接状态
    - mode: normal（正常模式）或 degraded（降级模式）
    - preagg: 预聚合表就绪状态（缺失时会回退基表查询并给出告警）
    """
    global _health_preagg_last_error

    storage_connected = _storage_adapter is not None
    try:
        preagg_status = query_routes.refresh_preagg_runtime_status(force_reload=False)
        if _health_preagg_last_error:
            logger.info("Preagg status refresh recovered in /health")
            _health_preagg_last_error = ""
    except Exception as exc:
        current_error = str(exc)
        if current_error != _health_preagg_last_error:
            logger.warning("Failed to refresh preagg status in /health: %s", exc)
            _health_preagg_last_error = current_error
        preagg_status = query_routes.get_preagg_runtime_status()
    preagg_ready = bool(preagg_status.get("ready")) if storage_connected else False
    warnings = []
    if storage_connected and not preagg_ready:
        warnings.append("preagg_tables_missing")
    
    return {
        "status": "healthy",
        "service": "query-service",
        "version": "1.0.0",
        "mode": "normal" if storage_connected else "degraded",
        "storage_connected": storage_connected,
        "preagg_ready": preagg_ready,
        "preagg": preagg_status,
        "warnings": warnings,
    }


# 就绪检查
@app.get("/ready")
async def readiness_check():
    """就绪检查接口
    
    用于 Kubernetes readinessProbe，服务启动后即可就绪
    不依赖存储连接状态
    """
    return {
        "ready": True,
        "service": "query-service",
        "timestamp": __import__("datetime").datetime.utcnow().isoformat()
    }


# 包含路由
app.include_router(query_routes.router)
app.include_router(data_quality.router)


# WebSocket 路由
@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    """WebSocket 实时日志流端点"""
    await websocket_logs_endpoint(websocket)


@app.websocket("/ws/topology")
async def ws_topology(websocket: WebSocket):
    """WebSocket 实时拓扑更新端点"""
    await websocket_topology_endpoint(websocket)


# WebSocket 状态查询
@app.get("/ws/status")
async def ws_status():
    """获取 WebSocket 连接状态"""
    return {
        "active_connections": len(ws_manager.active_connections),
        "status": "ok"
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.QUERY_SERVICE_PORT or 8002,
        reload=settings.DEBUG,
        access_log=False,
        log_config=None,
    )
