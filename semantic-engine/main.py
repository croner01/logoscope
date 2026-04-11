"""
Semantic Engine 主应用 - 核心智能组件
提供告警管理、标签发现等核心智能功能
"""
import logging
import os
import sys

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
import asyncio
import requests

_SHARED_LIB_CANDIDATES = (
    os.getenv("LOGOSCOPE_SHARED_LIB", ""),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "shared_src")),
    "/app/shared_lib",
)
for _candidate in _SHARED_LIB_CANDIDATES:
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.append(_candidate)

from api.cache import cached, clear_cache, get_cache_stats
from api.alerts import (
    AlertRule, AlertEvent, CreateRuleFromTemplateRequest,
    create_alert_rule, update_alert_rule, delete_alert_rule,
    get_alert_rules, get_alert_rule, get_alert_events,
    evaluate_alert_rules, get_alert_stats,
    get_alert_rule_templates, create_alert_rule_from_template, get_alert_notifications,
    acknowledge_alert_event, silence_alert_event, resolve_alert_event,
    set_storage_adapter as set_alerts_storage_adapter
)
from storage.adapter import StorageAdapter
from labels.discovery import discover_labels_from_events, label_discoverer
from config import config
from platform_kernel.fastapi_kernel import install_common_fastapi_handlers
from utils.logging_config import get_logger, setup_logging

setup_logging(
    service_name=config.app_name,
    level=getattr(logging, str(config.log_level).upper(), logging.INFO),
    log_format=os.getenv("LOG_FORMAT", "text"),
)
logger = get_logger(__name__)


def _utc_now_iso() -> str:
    """Return timezone-aware UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()

# 全局 storage 实例
storage = None
alert_evaluation_task: Optional[asyncio.Task] = None
AI_SERVICE_BASE_URL = os.getenv("AI_SERVICE_BASE_URL", "http://ai-service:8090").rstrip("/")
EXEC_SERVICE_BASE_URL = os.getenv("EXEC_SERVICE_BASE_URL", "http://exec-service:8095").rstrip("/")


def _alert_evaluation_interval_seconds() -> int:
    """获取告警评估间隔（秒），设置最小值避免误配导致空转。"""
    raw = os.getenv("ALERT_EVALUATION_INTERVAL_SECONDS", "30")
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = 30
    return max(parsed, 5)


async def _run_alert_evaluation_loop():
    """后台周期评估告警规则。"""
    interval_seconds = _alert_evaluation_interval_seconds()
    while True:
        try:
            result = await evaluate_alert_rules()
            triggered = int(result.get("triggered_alerts", 0) or 0)
            resolved = int(result.get("resolved_alerts", 0) or 0)
            if triggered > 0 or resolved > 0:
                log_format(
                    "INFO",
                    "alerts",
                    "Periodic alert evaluation finished",
                    triggered_alerts=triggered,
                    resolved_alerts=resolved,
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            log_format("ERROR", "alerts", f"Periodic alert evaluation failed: {e}")

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


def log_format(level: str, step: str, message: str, **kwargs):
    """
    格式化日志输出

    Args:
        level: 日志级别
        step: 步骤名称
        message: 日志消息
        **kwargs: 额外的键值对
    """
    level_name = str(level or "INFO").upper()
    level_value = getattr(logging, level_name, logging.INFO)
    extra = {"action": step}
    for key, value in kwargs.items():
        if value is not None and value != "":
            extra[key] = value
    logger.log(level_value, message, extra=extra)


def _filtered_proxy_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """过滤 hop-by-hop 头，避免代理返回异常。"""
    blocked = {
        "connection",
        "transfer-encoding",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
        "content-encoding",
        "content-length",
    }
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


app = FastAPI(
        title="Semantic Engine",
        description="Logoscope Semantic Engine - 告警与标签核心组件",
        version=config.app_version
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
except Exception as e:
    logger.warning("Failed to initialize OpenTelemetry: %s", e)


@app.on_event("startup")
async def startup_event():
    """
    应用启动时的初始化逻辑
    """
    logger.info("Starting Semantic Engine...")

    # 初始化 Storage Adapter
    global storage
    storage = StorageAdapter(config.get_storage_config())

    # ⭐ 设置 Alerts 模块的 storage adapter（避免启动阶段阻塞事件循环）
    await asyncio.to_thread(set_alerts_storage_adapter, storage)

    # 启动后台告警评估任务（阶段 A：打通告警闭环）
    global alert_evaluation_task
    if alert_evaluation_task is None or alert_evaluation_task.done():
        alert_evaluation_task = asyncio.create_task(_run_alert_evaluation_loop())
        log_format(
            "INFO",
            "alerts",
            "Started periodic alert evaluation task",
            interval_seconds=_alert_evaluation_interval_seconds(),
        )

    logger.info("Semantic Engine started successfully")


@app.on_event("shutdown")
async def shutdown_event():
    """
    应用关闭时的清理逻辑
    """
    global alert_evaluation_task
    if alert_evaluation_task:
        alert_evaluation_task.cancel()
        try:
            await alert_evaluation_task
        except asyncio.CancelledError:
            pass
        finally:
            alert_evaluation_task = None

    logger.info("Shutting down Semantic Engine...")


@app.get("/health")
async def health_check():
    """
    健康检查接口

    ⚠️ 重要：此端点不创建 OpenTelemetry span，避免 OTLP 导出阻塞
    健康检查必须快速响应，不应依赖外部服务（如 otel-collector）

    Returns:
        Dict[str, Any]: 健康状态信息
    """
    # ⚠️ 移除 OpenTelemetry span 创建，避免因 OTLP 导出超时阻塞健康检查
    # 如果需要追踪健康检查，应在反向代理层（如 ingress）实现

    return {
            "status": "healthy",
            "service": "semantic-engine",
            "version": config.app_version,
            "opentelemetry": "enabled" if os.getenv("OTEL_PYTHON_AUTO_INSTRUMENTATION_ENABLED") == "true" else "disabled",
            "timestamp": _utc_now_iso()
    }


@app.get("/")
async def root():
    """
    根路径接口
    
    Returns:
        Dict[str, Any]: 服务信息
    """
    return {
            "service": "Semantic Engine",
            "version": config.app_version,
            "description": "Logoscope Semantic Engine - 核心智能组件（告警管理、标签发现）"
    }


@app.api_route("/api/v1/ai", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/api/v1/ai/{subpath:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_ai_api(request: Request, subpath: str = ""):
    """兼容路径：将 AI 请求转发到独立 ai-service。"""
    target = f"{AI_SERVICE_BASE_URL}/api/v1/ai"
    if subpath:
        target = f"{target}/{subpath}"
    query = request.url.query
    if query:
        target = f"{target}?{query}"

    outbound_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }
    body = await request.body()

    if subpath.endswith("/stream"):
        try:
            upstream = await asyncio.to_thread(
                requests.request,
                method=request.method,
                url=target,
                headers=outbound_headers,
                data=body if body else None,
                timeout=(5, 300),
                stream=True,
            )
        except requests.RequestException as exc:
            logger.warning("AI stream proxy upstream request failed target=%s error=%s", target, exc)
            raise HTTPException(status_code=503, detail="ai-service unavailable")
        response_headers = _filtered_proxy_headers(dict(upstream.headers))
        media_type = upstream.headers.get("content-type", "text/event-stream")
        if int(upstream.status_code) >= 400:
            try:
                payload = upstream.json()
            except ValueError:
                payload = {"detail": upstream.text}
            return JSONResponse(
                status_code=upstream.status_code,
                content=payload,
                headers=response_headers,
            )

        def _iter_stream():
            try:
                for chunk in upstream.iter_content(chunk_size=1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return StreamingResponse(
            _iter_stream(),
            status_code=upstream.status_code,
            media_type=media_type,
            headers=response_headers,
        )

    try:
        upstream = await asyncio.to_thread(
            requests.request,
            method=request.method,
            url=target,
            headers=outbound_headers,
            data=body if body else None,
            timeout=(5, 90),
        )
    except requests.RequestException as exc:
        logger.warning("AI proxy upstream request failed target=%s error=%s", target, exc)
        raise HTTPException(status_code=503, detail="ai-service unavailable")

    response_headers = _filtered_proxy_headers(dict(upstream.headers))
    try:
        payload = upstream.json()
    except ValueError:
        payload = {"detail": upstream.text}

    return JSONResponse(
        status_code=upstream.status_code,
        content=payload,
        headers=response_headers,
    )


@app.api_route("/api/v1/exec", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/api/v1/exec/{subpath:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_exec_api(request: Request, subpath: str = ""):
    """兼容路径：转发命令执行请求到 exec-service。"""
    target = f"{EXEC_SERVICE_BASE_URL}/api/v1/exec"
    if subpath:
        target = f"{target}/{subpath}"
    query = request.url.query
    if query:
        target = f"{target}?{query}"

    outbound_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }
    body = await request.body()
    is_stream = subpath.endswith("/stream")

    try:
        upstream = await asyncio.to_thread(
            requests.request,
            method=request.method,
            url=target,
            headers=outbound_headers,
            data=body if body else None,
            timeout=(5, 300 if is_stream else 90),
            stream=is_stream,
        )
    except requests.RequestException as exc:
        logger.warning("Exec proxy upstream request failed target=%s error=%s", target, exc)
        raise HTTPException(status_code=503, detail="exec-service unavailable")

    response_headers = _filtered_proxy_headers(dict(upstream.headers))
    if is_stream:
        media_type = upstream.headers.get("content-type", "text/event-stream")
        if int(upstream.status_code) >= 400:
            try:
                payload = upstream.json()
            except ValueError:
                payload = {"detail": upstream.text}
            return JSONResponse(
                status_code=upstream.status_code,
                content=payload,
                headers=response_headers,
            )

        def _iter_stream():
            try:
                for chunk in upstream.iter_content(chunk_size=1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return StreamingResponse(
            _iter_stream(),
            status_code=upstream.status_code,
            media_type=media_type,
            headers=response_headers,
        )

    try:
        payload = upstream.json()
    except ValueError:
        payload = {"detail": upstream.text}
    return JSONResponse(
        status_code=upstream.status_code,
        content=payload,
        headers=response_headers,
    )


@app.get("/api/v1/cache/stats")
async def get_cache_stats_api():
    """
    获取缓存统计信息

    Returns:
        Dict[str, Any]: 缓存统计数据
    """
    return get_cache_stats()


@app.delete("/api/v1/cache")
async def clear_cache_delete_api(pattern: str = None):
    """
    清除 API 缓存（REST 风格）

    Args:
        pattern: 可选的模式匹配，如果为 None 则清除所有

    Returns:
        Dict[str, Any]: 清理结果
    """
    cleared = clear_cache(pattern)
    return {
        "status": "ok",
        "cleared": cleared,
        "pattern": pattern,
    }


@app.post("/api/v1/cache/clear")
async def clear_cache_api(pattern: str = None):
    """
    清除 API 缓存

    Args:
        pattern: 可选的模式匹配，如果为 None 则清除所有

    Returns:
        Dict[str, Any]: 操作结果
    """
    cleared = clear_cache(pattern)
    return {
        "status": "ok",
        "message": "Cache cleared",
        "pattern": pattern,
        "cleared": cleared,
    }


@app.get("/api/v1/deduplication/stats")
async def get_deduplication_stats_api():
    """
    获取去重统计信息

    Returns:
        Dict[str, Any]: 去重统计数据
    """
    deduplicator = getattr(storage, "deduplicator", None) if storage else None
    if not deduplicator:
        return {
            "total_processed": 0,
            "duplicates_found": 0,
            "duplicates_by_id": 0,
            "duplicates_by_semantic": 0,
            "duplicate_rate": 0.0,
            "id_cache_size": 0,
            "semantic_cache_size": 0,
            "cache_age_seconds": 0.0,
        }
    return deduplicator.get_stats()


@app.post("/api/v1/deduplication/clear-cache")
async def clear_deduplication_cache_api():
    """
    清除去重缓存

    Returns:
        Dict[str, Any]: 操作结果
    """
    deduplicator = getattr(storage, "deduplicator", None) if storage else None
    if deduplicator:
        deduplicator.clear_cache()
    return {"status": "ok", "message": "Deduplication cache cleared"}


# ==================== 告警管理 API ====================

@app.post("/api/v1/alerts/rules")
async def create_alert_rule_api(rule: AlertRule):
    """创建告警规则"""
    return await create_alert_rule(rule)


@app.get("/api/v1/alerts/rules")
async def get_alert_rules_api():
    """获取所有告警规则"""
    return await get_alert_rules()


@app.get("/api/v1/alerts/rules/{rule_id}")
async def get_alert_rule_api(rule_id: str):
    """获取单个告警规则"""
    return await get_alert_rule(rule_id)


@app.get("/api/v1/alerts/rule-templates")
async def get_alert_rule_templates_api():
    """获取告警规则模板"""
    return await get_alert_rule_templates()


@app.post("/api/v1/alerts/rules/from-template")
async def create_alert_rule_from_template_api(payload: CreateRuleFromTemplateRequest):
    """基于模板创建告警规则"""
    return await create_alert_rule_from_template(payload)


@app.put("/api/v1/alerts/rules/{rule_id}")
async def update_alert_rule_api(rule_id: str, rule: Dict[str, Any]):
    """更新告警规则"""
    return await update_alert_rule(rule_id, rule)


@app.patch("/api/v1/alerts/rules/{rule_id}")
async def patch_alert_rule_api(rule_id: str, rule: Dict[str, Any]):
    """部分更新告警规则"""
    return await update_alert_rule(rule_id, rule)


@app.delete("/api/v1/alerts/rules/{rule_id}")
async def delete_alert_rule_api(rule_id: str):
    """删除告警规则"""
    return await delete_alert_rule(rule_id)


@app.get("/api/v1/alerts/events")
async def get_alert_events_api(
    limit: int = 100,
    status: str = None,
    severity: str = None,
    cursor: str = None,
    service_name: str = None,
    source_service: str = None,
    target_service: str = None,
    namespace: str = None,
    search: str = None,
    scope: str = None,
):
    """获取告警事件列表"""
    return await get_alert_events(
        limit=limit,
        status=status,
        severity=severity,
        cursor=cursor,
        service_name=service_name,
        source_service=source_service,
        target_service=target_service,
        namespace=namespace,
        search=search,
        scope=scope,
    )


@app.get("/api/v1/alerts/notifications")
async def get_alert_notifications_api(
    limit: int = 100,
    channel: str = None,
    delivery_status: str = None,
    event_id: str = None,
):
    """获取告警通知记录"""
    return await get_alert_notifications(
        limit=limit,
        channel=channel,
        delivery_status=delivery_status,
        event_id=event_id,
    )


@app.post("/api/v1/alerts/events/{event_id}/ack")
async def acknowledge_alert_event_api(event_id: str):
    """确认告警事件"""
    return await acknowledge_alert_event(event_id)


@app.post("/api/v1/alerts/events/{event_id}/silence")
async def silence_alert_event_api(event_id: str, duration_seconds: int = 3600):
    """静默告警事件"""
    return await silence_alert_event(event_id, duration_seconds=duration_seconds)


@app.post("/api/v1/alerts/events/{event_id}/resolve")
async def resolve_alert_event_api(event_id: str, reason: str = None):
    """手工关闭告警事件"""
    return await resolve_alert_event(event_id, reason=reason)


@app.post("/api/v1/alerts/evaluate")
async def evaluate_alert_rules_api():
    """评估告警规则"""
    return await evaluate_alert_rules()


@app.get("/api/v1/alerts/stats")
async def get_alert_stats_api():
    """获取告警统计信息"""
    return await get_alert_stats()


# ==================== 标签发现 API ====================

@app.get("/api/v1/labels/discover")
async def discover_labels_api(limit: int = 1000):
    """
    标签自动发现 (API v1)

    ⭐ P1优化：自动识别、分类和推荐 Kubernetes Labels

    Args:
        limit: 分析的事件数量限制

    Returns:
        Dict[str, Any]: 标签发现结果
    """
    try:
        if not storage:
            return {"error": "Storage not available"}

        # 获取最近的事件
        events = await asyncio.to_thread(storage.get_events, limit)

        if not events:
            return {
                "total_labels": 0,
                "unique_labels": 0,
                "common_labels": [],
                "recommended_labels": [],
                "label_categories": {}
            }

        # 发现标签
        result = discover_labels_from_events(events)

        log_format("INFO", "labels", f"Discovered {result['unique_labels']} unique labels")

        return result

    except Exception as e:
        log_format("ERROR", "labels", f"Failed to discover labels: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/v1/labels/suggestions")
async def get_label_suggestions_api(service_name: str = None):
    """
    获取标签建议 (API v1)

    ⭐ P1优化：根据服务名生成 Kubernetes 标准标签建议

    Args:
        service_name: 服务名称（可选）

    Returns:
        Dict[str, Any]: 标签建议
    """
    try:
        if not service_name:
            # 如果没有提供服务名，从最近的日志中提取
            if storage:
                events = await asyncio.to_thread(storage.get_events, 10)
                if events:
                    service_name = events[0].get('service_name', 'unknown')

        if not service_name:
            service_name = "my-app"

        suggestions = label_discoverer.get_label_suggestions(service_name)

        return {
            "service_name": service_name,
            "suggestions": suggestions
        }

    except Exception as e:
        log_format("ERROR", "labels", f"Failed to get suggestions: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/v1/labels/categories")
async def get_label_categories_api():
    """
    获取标签分类信息 (API v1)

    ⭐ P1优化：返回支持的标签分类

    Returns:
        Dict[str, Any]: 标签分类信息
    """
    from labels.discovery import LABEL_CATEGORIES

    return {
        "categories": LABEL_CATEGORIES,
        "description": "标签按功能和用途分类，便于管理和过滤"
    }


if __name__ == "__main__":
    uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_level=config.log_level,
            access_log=False,
            log_config=None,
    )
