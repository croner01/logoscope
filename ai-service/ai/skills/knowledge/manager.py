"""Knowledge base management for case library and history."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class CaseStatus(str):
    """Case status enumeration."""
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


@dataclass
class AnalysisCase:
    """A case in the knowledge base."""
    case_id: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Core identifiers
    trace_id: Optional[str] = None
    request_id: Optional[str] = None
    service_name: Optional[str] = None
    log_timestamp: Optional[str] = None

    # Analysis results
    flow_summary: str = ""
    fault_cause: str = ""
    root_cause: str = ""
    severity: str = ""
    remediation_steps: List[Dict[str, Any]] = field(default_factory=list)

    # Keywords for similarity matching
    keywords: List[str] = field(default_factory=list)

    # Status
    status: str = CaseStatus.ACTIVE

    # Metadata
    analyst: str = ""
    tags: List[str] = field(default_factory=list)

    # Related cases
    related_cases: List[str] = field(default_factory=list)


class SimilarCase(BaseModel):
    """A similar case found in knowledge base."""
    case_id: str
    similarity_score: float
    trace_id: Optional[str] = None
    service_name: Optional[str] = None
    fault_cause: str
    root_cause: str
    severity: str
    remediation_summary: str


class KnowledgeBaseManager:
    """
    Manages the local knowledge base for analysis cases.

    Features:
    - Store analysis cases with full context
    - Search by keywords, service, trace_id, request_id
    - Find similar cases for reuse
    - Integrate with remote RAGFlow knowledge base
    """

    def __init__(
        self,
        storage_path: str = "/tmp/logoscope_knowledge",
        ragflow_config: Optional[Dict[str, Any]] = None,
    ):
        self._storage_path = storage_path
        self._ragflow_config = ragflow_config
        self._cases: Dict[str, AnalysisCase] = {}
        self._index: Dict[str, List[str]] = {}  # keyword -> case_ids
        self._load_cases()

    def _load_cases(self) -> None:
        """Load cases from storage."""
        os.makedirs(self._storage_path, exist_ok=True)
        cases_file = os.path.join(self._storage_path, "cases.json")

        if os.path.exists(cases_file):
            try:
                with open(cases_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for case_id, case_data in data.items():
                        self._cases[case_id] = AnalysisCase(**case_data)
                        self._reindex_case(case_id)
            except Exception:
                pass

    def _save_cases(self) -> None:
        """Save cases to storage."""
        os.makedirs(self._storage_path, exist_ok=True)
        cases_file = os.path.join(self._storage_path, "cases.json")

        data = {
            case_id: case.__dict__
            for case_id, case in self._cases.items()
            if case.status != CaseStatus.DELETED
        }

        with open(cases_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _reindex_case(self, case_id: str) -> None:
        """Reindex a case for search."""
        case = self._cases.get(case_id)
        if not case:
            return

        # Remove from old index
        for keyword, case_ids in self._index.items():
            if case_id in case_ids:
                case_ids.remove(case_id)

        # Add to new index
        for keyword in case.keywords:
            if keyword not in self._index:
                self._index[keyword] = []
            if case_id not in self._index[keyword]:
                self._index[keyword].append(case_id)

    def add_case(self, case: AnalysisCase) -> str:
        """Add a new case to the knowledge base."""
        self._cases[case.case_id] = case
        self._reindex_case(case.case_id)
        self._save_cases()

        # Sync to remote if configured
        if self._ragflow_config:
            self._sync_to_remote(case)

        return case.case_id

    def update_case(self, case_id: str, updates: Dict[str, Any]) -> bool:
        """Update an existing case."""
        case = self._cases.get(case_id)
        if not case:
            return False

        for key, value in updates.items():
            if hasattr(case, key):
                setattr(case, key, value)

        case.updated_at = time.time()
        self._reindex_case(case_id)
        self._save_cases()
        return True

    def get_case(self, case_id: str) -> Optional[AnalysisCase]:
        """Get a case by ID."""
        return self._cases.get(case_id)

    def search_cases(
        self,
        query: Optional[str] = None,
        service_name: Optional[str] = None,
        severity: Optional[str] = None,
        time_range: Optional[tuple[float, float]] = None,
        limit: int = 10,
    ) -> List[AnalysisCase]:
        """Search cases by various criteria."""
        results = []

        for case in self._cases.values():
            if case.status == CaseStatus.DELETED:
                continue

            # Filter by service
            if service_name and case.service_name != service_name:
                continue

            # Filter by severity
            if severity and case.severity != severity:
                continue

            # Filter by time range
            if time_range:
                start, end = time_range
                if not (start <= case.created_at <= end):
                    continue

            # Filter by query
            if query:
                query_lower = query.lower()
                match = (
                    query_lower in case.fault_cause.lower() or
                    query_lower in case.root_cause.lower() or
                    query_lower in case.flow_summary.lower() or
                    any(query_lower in kw.lower() for kw in case.keywords)
                )
                if not match:
                    continue

            results.append(case)

        # Sort by updated_at descending
        results.sort(key=lambda c: c.updated_at, reverse=True)
        return results[:limit]

    def find_similar_cases(
        self,
        keywords: List[str],
        service_name: Optional[str] = None,
        limit: int = 3,
    ) -> List[SimilarCase]:
        """Find similar cases based on keywords."""
        case_scores: Dict[str, float] = {}

        for keyword in keywords:
            keyword_lower = keyword.lower()
            for case_id in self._index.get(keyword, []):
                case = self._cases.get(case_id)
                if not case or case.status == CaseStatus.DELETED:
                    continue

                # Bonus for same service
                service_bonus = 0.2 if service_name and case.service_name == service_name else 0.0

                case_scores[case_id] = case_scores.get(case_id, 0) + 1.0 + service_bonus

        # Normalize scores
        max_score = max(case_scores.values()) if case_scores else 1
        similar_cases = []
        for case_id, score in sorted(case_scores.items(), key=lambda x: x[1], reverse=True)[:limit]:
            case = self._cases[case_id]
            similar_cases.append(SimilarCase(
                case_id=case_id,
                similarity_score=score / max_score,
                trace_id=case.trace_id,
                service_name=case.service_name,
                fault_cause=case.fault_cause,
                root_cause=case.root_cause,
                severity=case.severity,
                remediation_summary=", ".join(
                    s.get("title", "") for s in case.remediation_steps[:3]
                ),
            ))

        return similar_cases

    def _sync_to_remote(self, case: AnalysisCase) -> bool:
        """Sync a case to remote RAGFlow knowledge base."""
        if not self._ragflow_config:
            return False

        # TODO: Implement RAGFlow sync
        # This would involve:
        # 1. Format case data for RAGFlow
        # 2. Call RAGFlow API to add document
        # 3. Handle errors and retry
        return True

    def search_remote(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Search remote RAGFlow knowledge base."""
        if not self._ragflow_config:
            return []

        # TODO: Implement RAGFlow search
        # This would involve:
        # 1. Call RAGFlow search API
        # 2. Parse results
        # 3. Return formatted results
        return []

    def get_statistics(self) -> Dict[str, Any]:
        """Get knowledge base statistics."""
        active_cases = [c for c in self._cases.values() if c.status == CaseStatus.ACTIVE]
        return {
            "total_cases": len(self._cases),
            "active_cases": len(active_cases),
            "indexed_keywords": len(self._index),
            "by_service": self._count_by_field("service_name"),
            "by_severity": self._count_by_field("severity"),
        }

    def _count_by_field(self, field_name: str) -> Dict[str, int]:
        """Count cases by a field value."""
        counts: Dict[str, int] = {}
        for case in self._cases.values():
            if case.status == CaseStatus.DELETED:
                continue
            value = getattr(case, field_name, None) or "unknown"
            counts[value] = counts.get(value, 0) + 1
        return counts


# Global knowledge base instance
_global_kb: Optional[KnowledgeBaseManager] = None


def get_knowledge_base() -> KnowledgeBaseManager:
    """Get the global knowledge base instance."""
    global _global_kb
    if _global_kb is None:
        _global_kb = KnowledgeBaseManager()
    return _global_kb


def init_knowledge_base(
    storage_path: str,
    ragflow_config: Optional[Dict[str, Any]] = None,
) -> KnowledgeBaseManager:
    """Initialize the global knowledge base with configuration."""
    global _global_kb
    _global_kb = KnowledgeBaseManager(
        storage_path=storage_path,
        ragflow_config=ragflow_config,
    )
    return _global_kb
