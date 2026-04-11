"""
Runtime v4 target/capability registry models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List
import uuid


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _build_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class TargetRecord:
    target_id: str
    target_kind: str
    target_identity: str
    display_name: str = ""
    description: str = ""
    capabilities: List[str] = field(default_factory=list)
    credential_scope: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    version: int = 1
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    updated_by: str = "system"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "target_identity": self.target_identity,
            "display_name": self.display_name,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "credential_scope": dict(self.credential_scope),
            "metadata": dict(self.metadata),
            "status": self.status,
            "version": int(self.version),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


@dataclass
class TargetChangeRecord:
    seq: int
    change_id: str
    change_type: str
    target_id: str
    target_kind: str
    run_id: str = ""
    action_id: str = ""
    reason: str = ""
    before: Dict[str, Any] = field(default_factory=dict)
    after: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now_iso)
    updated_by: str = "system"

    @staticmethod
    def build_change_id() -> str:
        return _build_id("tchg")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": int(self.seq),
            "change_id": self.change_id,
            "change_type": self.change_type,
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "reason": self.reason,
            "before": dict(self.before),
            "after": dict(self.after),
            "created_at": self.created_at,
            "updated_by": self.updated_by,
        }
