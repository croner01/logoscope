"""
Topology Service - 拓扑分析服务
提供拓扑构建、拓扑查询、实时拓扑推送功能
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
from api import topology_routes, realtime_topology, monitor_topology, topology_adjustment
from platform_kernel.fastapi_kernel import install_common_fastapi_handlers, install_cors
from utils.logging_config import get_logger, setup_logging
from api.websocket import (
    topology_websocket_endpoint,
    topology_poller,
    topology_manager
)
from graph.hybrid_topology import get_hybrid_topology_builder
from graph.enhanced_topology import get_enhanced_topology_builder
from graph.service_sync import sync_services_from_logs

setup_logging(
    service_name="topology-service",
    level=getattr(logging, str(settings.log_level).upper(), logging.INFO),
    log_format=os.getenv("LOG_FORMAT", "text"),
)
logger = get_logger(__name__)

# 全局实例
_storage_adapter: StorageAdapter = None
_hybrid_builder = None
_enhanced_builder = None
_topology_poller_task = None
_service_sync_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global _storage_adapter, _hybrid_builder, _enhanced_builder, _topology_poller_task, _service_sync_task

    logger.info("Starting Topology Service...")

    # 初始化 Storage Adapter
    logger.info("Initializing Storage Adapter...")
    storage_config = settings.get_storage_config()
    logger.info(f"ClickHouse config: host={storage_config['clickhouse']['host']}, port={storage_config['clickhouse']['port']}")
    _storage_adapter = StorageAdapter(config=storage_config)

    # 初始化拓扑构建器
    logger.info("Initializing Topology Builders...")
    _hybrid_builder = get_hybrid_topology_builder(_storage_adapter)
    _enhanced_builder = get_enhanced_topology_builder(_storage_adapter)

    # 初始化 API 模块
    topology_routes.set_storage_and_builders(_storage_adapter, _hybrid_builder, _enhanced_builder)
    realtime_topology.set_storage_adapter(_storage_adapter)
    monitor_topology.set_storage_adapter(_storage_adapter)
    topology_adjustment.set_storage_adapter(_storage_adapter)

    # 启动拓扑轮询任务
    _topology_poller_task = asyncio.create_task(topology_poller(_hybrid_builder, interval=5.0))
    logger.info("Topology poller task started")

    async def service_node_sync_loop():
        """周期性将 ClickHouse 服务清单回填到 Neo4j Service 节点。"""
        interval_seconds = int(settings.SERVICE_NODE_SYNC_INTERVAL_SECONDS)
        while True:
            try:
                result = await sync_services_from_logs(_storage_adapter)
                status = str(result.get("status") or "unknown")
                if status == "completed":
                    logger.info(
                        "Service node sync completed: total=%s synced=%s failed=%s coverage=%.2f%%",
                        result.get("total_services", 0),
                        result.get("synced_count", 0),
                        result.get("failed_count", 0),
                        float((result.get("coverage_after") or {}).get("coverage_percent") or 0.0),
                    )
                else:
                    logger.warning("Service node sync skipped or failed: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Service node sync loop failed: %s", exc)
            await asyncio.sleep(interval_seconds)

    if settings.SERVICE_NODE_SYNC_ENABLED:
        _service_sync_task = asyncio.create_task(service_node_sync_loop())
        logger.info(
            "Service node sync loop started (interval=%ss)",
            settings.SERVICE_NODE_SYNC_INTERVAL_SECONDS,
        )

    logger.info("Topology Service started successfully!")
    yield

    # 清理
    logger.info("Shutting down Topology Service...")
    if _topology_poller_task:
        _topology_poller_task.cancel()
        try:
            await _topology_poller_task
        except asyncio.CancelledError:
            pass
    if _service_sync_task:
        _service_sync_task.cancel()
        try:
            await _service_sync_task
        except asyncio.CancelledError:
            pass
    if _storage_adapter:
        _storage_adapter.close()
    logger.info("Topology Service shutdown complete")


# 创建 FastAPI 应用
app = FastAPI(
    title="Logoscope Topology Service",
    description="拓扑分析服务 - 提供拓扑构建、拓扑查询、实时拓扑推送",
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
    init_otel(service_name="topology-service", service_version="1.0.0", app=app)
    logger.info("OpenTelemetry initialized")

# 根路径
@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "topology-service",
        "status": "ok",
        "version": "1.0.0"
    }

# 健康检查
@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "healthy", "service": "topology-service"}


# 包含路由
app.include_router(topology_routes.router)
# 兼容扩展路由（实时订阅/快照、监控拓扑、手动调整）
app.include_router(realtime_topology.router)
app.include_router(monitor_topology.router)
app.include_router(topology_adjustment.router)


# WebSocket 路由
@app.websocket("/ws/topology")
async def ws_topology(websocket: WebSocket):
    """WebSocket 实时拓扑更新端点"""
    await topology_websocket_endpoint(websocket, _hybrid_builder)


# WebSocket 状态查询
@app.get("/ws/status")
async def ws_status():
    """获取 WebSocket 连接状态"""
    return {
        "active_connections": len(topology_manager.active_connections),
        "status": "ok"
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.TOPOLOGY_SERVICE_PORT or 8003,
        reload=settings.DEBUG,
        access_log=False,
        log_config=None,
    )
