"""FastAPI router for dynamic host registry management."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.host_registry import (
    ensure_schema,
    get_host,
    list_hosts,
    register_host,
    unregister_host,
)

router = APIRouter(prefix="/hosts", tags=["hosts"])


class RegisterHostRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Unique host name")
    host: str = Field(..., min_length=1, description="IP address or hostname")
    port: int = Field(22, ge=1, le=65535)
    user: str = Field("root", min_length=1)
    key_file: Optional[str] = Field(None, description="SSH key file path. Leave empty when using private_key.")
    private_key: Optional[str] = Field(None, description="SSH private key content (pasted). Stored base64-encoded in ClickHouse.")
    labels: Optional[Dict[str, str]] = None


class HostResponse(BaseModel):
    name: str
    host: str
    port: int
    user: str
    key_file: str
    labels: Dict[str, str]
    created_at: str
    updated_at: str


@router.get("", response_model=List[HostResponse])
async def list_all_hosts():
    """List all registered hosts."""
    try:
        ensure_schema()
        hosts = list_hosts()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return hosts


@router.get("/{name}", response_model=HostResponse)
async def get_host_by_name(name: str):
    """Get a single host by name."""
    if not name.strip():
        raise HTTPException(status_code=400, detail="Host name is required")
    try:
        ensure_schema()
        host = get_host(name)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host '{name}' not found")
    return host


@router.post("", response_model=HostResponse, status_code=201)
async def register_new_host(req: RegisterHostRequest):
    """Register a new host or update an existing one."""
    try:
        ensure_schema()
        record = register_host(
            name=req.name,
            host=req.host,
            port=req.port,
            user=req.user,
            key_file=req.key_file or "",
            labels=req.labels,
            private_key=req.private_key,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return _record_to_response(record)


@router.delete("/{name}", status_code=204)
async def unregister_host_by_name(name: str):
    """Soft-delete a host by name."""
    if not name.strip():
        raise HTTPException(status_code=400, detail="Host name is required")
    try:
        ensure_schema()
        ok = unregister_host(name)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to unregister host '{name}'")
    return None


def _record_to_response(record: Dict[str, Any]) -> HostResponse:
    labels_raw = record.get("labels") if isinstance(record.get("labels"), dict) else {}
    return HostResponse(
        name=record.get("name", ""),
        host=record.get("host", ""),
        port=int(record.get("port", 22)),
        user=record.get("user", "root"),
        key_file=record.get("key_file", "") or "-",
        labels=labels_raw,
        created_at=record.get("created_at", ""),
        updated_at=record.get("updated_at", ""),
    )
