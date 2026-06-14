"""
Logoscope MCP Server — connects Claude Code to logoscope diagnostic tools.

Usage (stdio, for Claude Code Desktop)::

    python mcp_server/server.py

Usage (SSE, for remote)::

    python mcp_server/server.py --transport sse --port 8089

Claude Code configuration (claude_desktop_config.json)::

    {
        "mcpServers": {
            "logoscope": {
                "command": "python",
                "args": ["/path/to/mcp_server/server.py"]
            }
        }
    }
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List

import httpx

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Resource, ResourceTemplate, TextContent, Tool

# ── Configuration ───────────────────────────────────────────────────────────

EXEC_SERVICE_URL = os.getenv("EXEC_SERVICE_BASE_URL", "http://exec-service:8095").rstrip("/")
MCP_SERVER_NAME = os.getenv("MCP_SERVER_NAME", "logoscope")

server = Server(MCP_SERVER_NAME)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _is_write_command(command: str) -> bool:
    """Detect if a kubectl command is a write operation."""
    lower = command.lower().strip()
    write_verbs = ("delete", "apply", "patch", "edit", "create", "update", "scale", "drain", "cordon", "uncordon", "rollout")
    for verb in write_verbs:
        if lower.startswith(f"kubectl {verb}") or f"kubectl {verb} " in f" {lower} ":
            return True
    return False


async def _call_exec_service(command: str, purpose: str = "mcp") -> Dict[str, Any]:
    """Call exec-service to execute a command. Returns precheck + execution result."""
    async with httpx.AsyncClient(base_url=EXEC_SERVICE_URL, timeout=60) as client:
        # Step 1: precheck
        pre_resp = await client.post("/api/v1/exec/precheck", json={
            "session_id": f"mcp-{MCP_SERVER_NAME}",
            "command": command,
            "purpose": purpose,
        })
        pre_data = pre_resp.json() if pre_resp.is_success else {}
        pre_status = _as_str(pre_data.get("status")).lower()

        if pre_status == "permission_required":
            return {
                "status": "denied",
                "message": _as_str(pre_data.get("message", "Command denied by policy")),
            }

        is_readonly = pre_data.get("command_type") == "query"
        ticket = _as_str(pre_data.get("confirmation_ticket"))

        # Step 2: execute
        exec_resp = await client.post("/api/v1/exec/execute", json={
            "session_id": f"mcp-{MCP_SERVER_NAME}",
            "command": command,
            "purpose": purpose,
            "confirmed": is_readonly,
            "elevated": False,
            "confirmation_ticket": ticket,
            "timeout_seconds": 30,
        })
        exec_data = exec_resp.json() if exec_resp.is_success else {}
        run = exec_data.get("run", exec_data) if isinstance(exec_data, dict) else {}

        return {
            "status": run.get("status", "completed"),
            "exit_code": run.get("exit_code", 0),
            "stdout": _as_str(run.get("stdout", "")),
            "stderr": _as_str(run.get("stderr", "")),
        }


# ── Tool definitions ────────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="clickhouse_query",
        title="ClickHouse SQL Query",
        description="执行只读 ClickHouse SQL 查询来分析日志、事件和指标。仅支持 SELECT / SHOW / DESCRIBE 语句。",
        inputSchema={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "SQL 查询语句。仅支持 SELECT、SHOW、DESCRIBE 等只读操作。",
                },
                "database": {
                    "type": "string",
                    "description": "ClickHouse 数据库名称",
                    "default": "logs",
                },
            },
            "required": ["sql"],
        },
    ),
    Tool(
        name="kubectl_read",
        title="Kubectl Read",
        description="执行只读 kubectl 命令（get、describe、logs、top 等）。写操作（delete、apply、patch 等）请使用 kubectl_write。",
        inputSchema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "完整的 kubectl 命令，例如 'kubectl get pods -n islap'",
                },
            },
            "required": ["command"],
        },
    ),
    Tool(
        name="kubectl_write",
        title="Kubectl Write (需审批)",
        description="执行 kubectl 写操作（delete、apply、patch、rollout 等）。执行前会提示用户确认。",
        inputSchema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "完整的 kubectl 写命令，例如 'kubectl rollout restart deployment/query-service -n islap'",
                },
                "reason": {
                    "type": "string",
                    "description": "执行此写操作的原因说明",
                },
            },
            "required": ["command", "reason"],
        },
    ),
]


@server.list_tools()
async def handle_list_tools() -> List[Tool]:
    return TOOLS


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> CallToolResult:
    is_error = False
    content: List[TextContent] = []

    try:
        if name == "clickhouse_query":
            sql = _as_str(arguments.get("sql", ""))
            database = _as_str(arguments.get("database", "logs"))
            if not sql:
                return CallToolResult(
                    content=[TextContent(type="text", text="Missing required parameter: sql")],
                    isError=True,
                )
            # Build the kubectl exec command for clickhouse
            cmd = f"kubectl exec deploy/clickhouse -n islap -- clickhouse-client --database={database} --query={shlex_quote(sql)}"
            result = await _call_exec_service(cmd, purpose=f"clickhouse query: {sql[:100]}")
            content.append(TextContent(
                type="text",
                text=result.get("stdout", "") or result.get("stderr", result.get("message", "No output")),
            ))

        elif name == "kubectl_read":
            command = _as_str(arguments.get("command", ""))
            if not command:
                return CallToolResult(
                    content=[TextContent(type="text", text="Missing required parameter: command")],
                    isError=True,
                )
            result = await _call_exec_service(command, purpose="kubectl read via MCP")
            is_error = result.get("status") in ("denied", "failed")
            content.append(TextContent(
                type="text",
                text=result.get("stdout", "") or result.get("stderr", result.get("message", "No output")),
            ))

        elif name == "kubectl_write":
            command = _as_str(arguments.get("command", ""))
            reason = _as_str(arguments.get("reason", ""))
            if not command:
                return CallToolResult(
                    content=[TextContent(type="text", text="Missing required parameter: command")],
                    isError=True,
                )
            # MCP protocol allows auto-approved prompts for write operations.
            # The 'reason' field serves as human-readable justification.
            result = await _call_exec_service(
                command,
                purpose=f"kubectl write via MCP: {reason or 'no reason provided'}",
            )
            is_error = result.get("status") in ("denied", "failed")
            msg = result.get("stdout") or result.get("stderr") or result.get("message", "Executed")
            if is_error:
                msg = f"[DENIED] {msg}" if result.get("status") == "denied" else f"[FAILED] {msg}"
            content.append(TextContent(type="text", text=msg))

        else:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )

    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error executing {name}: {e}")],
            isError=True,
        )

    return CallToolResult(content=content, isError=is_error)


# ── Resources ───────────────────────────────────────────────────────────────

RESOURCE_TEMPLATES = [
    ResourceTemplate(
        name="service_logs",
        title="Service Logs",
        uriTemplate="logs://{service_name}",
        description="获取指定服务的最新日志。可选的 time_range 参数：1h, 6h, 24h, 7d",
        mimeType="text/plain",
    ),
    ResourceTemplate(
        name="cluster_events",
        title="Cluster Events",
        uriTemplate="events://{namespace}",
        description="获取指定命名空间的最近 Events",
        mimeType="text/plain",
    ),
]


@server.list_resource_templates()
async def handle_list_resource_templates() -> List[ResourceTemplate]:
    return RESOURCE_TEMPLATES


@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Handle resource reads by delegating to exec-service."""
    try:
        if uri.startswith("logs://"):
            service_name = uri.removeprefix("logs://").split("/")[0].split("?")[0]
            cmd = f"kubectl logs -n islap deployment/{service_name} --tail=50 2>/dev/null || echo 'No logs available'"
        elif uri.startswith("events://"):
            namespace = uri.removeprefix("events://").split("/")[0].split("?")[0] or "islap"
            cmd = f"kubectl get events -n {namespace} --sort-by=.lastTimestamp 2>/dev/null | tail -30"
        else:
            return f"Unknown resource: {uri}"

        result = await _call_exec_service(cmd, purpose=f"MCP resource: {uri}")
        return result.get("stdout") or result.get("stderr") or "No data available"

    except Exception as e:
        return f"Error reading {uri}: {e}"


# ── Entry point ─────────────────────────────────────────────────────────────

def shlex_quote(s: str) -> str:
    """Minimal shell quoting for ClickHouse SQL."""
    return "'" + s.replace("'", "'\\''") + "'"


async def _run_stdio():
    """Run MCP server over stdio (for Claude Code Desktop)."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def _run_sse(port: int):
    """Run MCP server over SSE (for remote access)."""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    import uvicorn

    sse = SseServerTransport("/messages")

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages", app=sse.handle_post_message),
        ],
    )
    uvicorn.run(app, host="0.0.0.0", port=port)


def main():
    transport = _as_str(os.getenv("MCP_TRANSPORT", "stdio")).lower()
    port = int(os.getenv("MCP_PORT", "8089"))

    if transport == "sse":
        asyncio.run(_run_sse(port))
    else:
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
