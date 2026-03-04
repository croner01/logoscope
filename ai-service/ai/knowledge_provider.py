"""
Knowledge provider abstraction for local/remote KB integration.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ai.similar_cases import CaseStore, SimilarCaseRecommender, get_case_store, get_recommender

try:
    from prometheus_client import Counter, Histogram
except Exception:  # pragma: no cover
    Counter = None
    Histogram = None

logger = logging.getLogger(__name__)


def _build_counter(name: str, description: str):
    if Counter is None:
        return None
    try:
        return Counter(name, description)
    except Exception:
        return None


def _build_histogram(name: str, description: str, buckets: Tuple[float, ...]):
    if Histogram is None:
        return None
    try:
        return Histogram(name, description, buckets=buckets)
    except Exception:
        return None


KB_REMOTE_FALLBACK_TOTAL = _build_counter(
    "kb_remote_fallback_total",
    "Total fallback count from remote/hybrid mode to local mode.",
)
KB_SYNC_FAILED_TOTAL = _build_counter(
    "kb_sync_failed_total",
    "Total failed remote knowledge sync count.",
)
KB_HYBRID_SEARCH_LATENCY_MS = _build_histogram(
    "kb_hybrid_search_latency_ms",
    "Hybrid knowledge search latency in milliseconds.",
    buckets=(20, 50, 100, 200, 500, 1000, 3000, 5000),
)
KB_LOCAL_SEARCH_LATENCY_MS = _build_histogram(
    "kb_local_search_latency_ms",
    "Local knowledge search latency in milliseconds.",
    buckets=(10, 20, 50, 100, 200, 500, 1000, 3000),
)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _metric_inc(counter_obj: Any, amount: float = 1.0) -> None:
    if counter_obj is None:
        return
    try:
        counter_obj.inc(amount)
    except Exception:
        return


def _metric_observe(histogram_obj: Any, value: float) -> None:
    if histogram_obj is None:
        return
    try:
        histogram_obj.observe(value)
    except Exception:
        return


def _remote_error_code_by_status(status_code: int) -> str:
    if status_code == 429:
        return "KBR-009"
    return "KBR-008"


class RemoteProviderError(RuntimeError):
    """Remote provider error with status and normalized error code."""

    def __init__(self, message: str, status_code: int = 0, error_code: str = "KBR-008") -> None:
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.error_code = _as_str(error_code, "KBR-008")


def _case_status(case_obj: Any) -> str:
    llm_meta = case_obj.llm_metadata if isinstance(getattr(case_obj, "llm_metadata", None), dict) else {}
    explicit = _as_str(llm_meta.get("case_status"))
    if explicit:
        return explicit
    return "resolved" if bool(getattr(case_obj, "resolved", False)) else "archived"


class BaseKnowledgeProvider:
    """External KB provider interface."""

    name = "base"

    def health(self) -> Dict[str, Any]:
        raise NotImplementedError

    def search_cases(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def upsert_case(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class GenericRESTKnowledgeProvider(BaseKnowledgeProvider):
    """Generic REST adapter for remote KB service."""

    name = "generic_rest"

    def __init__(self, provider_name: str = "generic_rest") -> None:
        self.name = _as_str(provider_name, "generic_rest").lower()
        self.base_url = _as_str(
            os.getenv("KB_REMOTE_BASE_URL")
            or (os.getenv("KB_RAGFLOW_BASE_URL") if self.name == "ragflow" else "")
        ).rstrip("/")
        self.api_key = _as_str(
            os.getenv("KB_REMOTE_API_KEY")
            or (os.getenv("KB_RAGFLOW_API_KEY") if self.name == "ragflow" else "")
        )
        self.timeout_seconds = max(1, _as_int(os.getenv("KB_REMOTE_TIMEOUT_SECONDS"), 5))
        default_health_path = "/health"
        default_search_path = "/search"
        default_upsert_path = "/upsert"
        if self.name == "ragflow":
            # RAGFlow 优先使用官方常见路径；若企业侧有网关适配，可在运行时改写这三个 path。
            default_health_path = "/api/v1/system/health"
            default_search_path = "/api/v1/retrieval"
            default_upsert_path = "/api/v1/kb/upsert"
        self.health_path = _as_str(os.getenv("KB_REMOTE_HEALTH_PATH"), default_health_path)
        self.search_path = _as_str(os.getenv("KB_REMOTE_SEARCH_PATH"), default_search_path)
        self.upsert_path = _as_str(os.getenv("KB_REMOTE_UPSERT_PATH"), default_upsert_path)

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        if not self.base_url:
            raise RemoteProviderError("KB_REMOTE_BASE_URL not configured", status_code=0, error_code="KBR-006")
        url = f"{self.base_url}{path}"
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            url=url,
            data=body,
            headers=self._build_headers(),
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
                if not raw:
                    return int(resp.status), {}
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return int(resp.status), parsed
                return int(resp.status), {"data": parsed}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="ignore")
            except Exception:
                detail = ""
            code = int(getattr(e, "code", 0) or 0)
            error_code = _remote_error_code_by_status(code)
            raise RemoteProviderError(
                f"remote HTTP error {code}: {detail[:240]}",
                status_code=code,
                error_code=error_code,
            )
        except urllib.error.URLError as e:
            raise RemoteProviderError(
                f"remote URL error: {e.reason}",
                status_code=502,
                error_code="KBR-008",
            )
        except TimeoutError:
            raise RemoteProviderError("remote request timeout", status_code=504, error_code="KBR-008")

    def health(self) -> Dict[str, Any]:
        if not self.base_url:
            return {
                "provider": self.name,
                "configured": False,
                "available": False,
                "message": "KB_REMOTE_BASE_URL not configured",
            }
        try:
            status_code, payload = self._request_json("GET", self.health_path, None)
            available = status_code < 500
            return {
                "provider": self.name,
                "configured": True,
                "available": available,
                "status_code": status_code,
                "payload": payload,
                "message": "ok" if available else "remote unavailable",
            }
        except Exception as e:
            return {
                "provider": self.name,
                "configured": True,
                "available": False,
                "message": str(e),
                "error_code": _as_str(getattr(e, "error_code", "")),
                "status_code": _as_int(getattr(e, "status_code", 0), 0),
            }

    def search_cases(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        status_code, response = self._request_json("POST", self.search_path, payload)
        if status_code >= 500:
            raise RemoteProviderError(
                f"remote search failed with status {status_code}",
                status_code=status_code,
                error_code=_remote_error_code_by_status(status_code),
            )
        cases = response.get("cases")
        return cases if isinstance(cases, list) else []

    def upsert_case(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        status_code, response = self._request_json("POST", self.upsert_path, payload)
        if status_code >= 500:
            raise RemoteProviderError(
                f"remote upsert failed with status {status_code}",
                status_code=status_code,
                error_code=_remote_error_code_by_status(status_code),
            )
        return response if isinstance(response, dict) else {}


class KnowledgeGateway:
    """Gateway for local and remote KB access."""

    def __init__(self, storage_adapter: Any = None) -> None:
        self.case_store: CaseStore = get_case_store(storage_adapter)
        self.recommender: SimilarCaseRecommender = get_recommender(storage_adapter)
        self.provider: Optional[BaseKnowledgeProvider] = self._build_provider()
        self._status_cache_seconds = max(5, _as_int(os.getenv("KB_PROVIDER_STATUS_CACHE_SECONDS"), 45))
        self._last_status_ts = 0.0
        self._last_status_payload: Dict[str, Any] = {}
        self._outbox_enabled = _as_str(os.getenv("KB_REMOTE_OUTBOX_ENABLED"), "true").lower() == "true"
        self._outbox_poll_seconds = max(1, _as_int(os.getenv("KB_REMOTE_OUTBOX_POLL_SECONDS"), 5))
        self._outbox_max_attempts = max(1, _as_int(os.getenv("KB_REMOTE_OUTBOX_MAX_ATTEMPTS"), 5))
        self._outbox_backoff_base_seconds = max(1, _as_int(os.getenv("KB_REMOTE_OUTBOX_BACKOFF_BASE_SECONDS"), 8))
        self._outbox_backoff_max_seconds = max(
            self._outbox_backoff_base_seconds,
            _as_int(os.getenv("KB_REMOTE_OUTBOX_BACKOFF_MAX_SECONDS"), 600),
        )
        self._outbox_path = _as_str(os.getenv("KB_REMOTE_OUTBOX_PATH"), "/tmp/logoscope-kb-outbox.json")
        self._outbox_lock = threading.Lock()
        self._outbox_items: List[Dict[str, Any]] = []
        self._outbox_worker_thread: Optional[threading.Thread] = None
        self._outbox_stop_event = threading.Event()
        self._case_sync_lock = threading.Lock()
        self._load_outbox_items()

    def _build_provider(self) -> Optional[BaseKnowledgeProvider]:
        provider_name = _as_str(os.getenv("KB_REMOTE_PROVIDER"), "generic_rest").lower()
        if provider_name in {"", "none", "disabled"}:
            return None
        if provider_name in {"generic_rest", "ragflow"}:
            return GenericRESTKnowledgeProvider(provider_name=provider_name)
        logger.warning(f"Unsupported KB_REMOTE_PROVIDER={provider_name}, fallback to disabled")
        return None

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _safe_json_loads(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        text = str(value).strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except Exception:
            return default

    @staticmethod
    def _extract_sync_error(result_or_error: Any) -> Tuple[str, str]:
        """Extract error text and KBR code from provider response/exception."""
        if isinstance(result_or_error, dict):
            sync_error = _as_str(result_or_error.get("sync_error"))
            sync_error_code = _as_str(result_or_error.get("sync_error_code"))
            if sync_error:
                if not sync_error_code and "429" in sync_error:
                    sync_error_code = "KBR-009"
                if not sync_error_code:
                    sync_error_code = "KBR-008"
            return sync_error, sync_error_code

        error_text = _as_str(result_or_error)
        error_code = _as_str(getattr(result_or_error, "error_code", ""))
        status_code = _as_int(getattr(result_or_error, "status_code", 0), 0)
        if not error_code:
            if status_code == 429 or "429" in error_text:
                error_code = "KBR-009"
            elif error_text:
                error_code = "KBR-008"
        return error_text, error_code

    def _load_outbox_items(self) -> None:
        if not self._outbox_enabled or not self._outbox_path:
            return
        if not os.path.exists(self._outbox_path):
            return
        try:
            with open(self._outbox_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            items = payload.get("items") if isinstance(payload, dict) else payload
            parsed_items = items if isinstance(items, list) else []
            normalized: List[Dict[str, Any]] = []
            now_ts = time.time()
            for raw in parsed_items:
                if not isinstance(raw, dict):
                    continue
                outbox_id = _as_str(raw.get("outbox_id"))
                case_id = _as_str(raw.get("case_id"))
                payload_obj = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
                if not outbox_id or not case_id or not payload_obj:
                    continue
                status = _as_str(raw.get("status"), "pending").lower()
                if status not in {"pending", "processing", "failed"}:
                    status = "pending"
                normalized.append(
                    {
                        "outbox_id": outbox_id,
                        "case_id": case_id,
                        "payload": payload_obj,
                        "status": "pending" if status == "processing" else status,
                        "attempts": max(0, _as_int(raw.get("attempts"), 0)),
                        "max_attempts": max(1, _as_int(raw.get("max_attempts"), self._outbox_max_attempts)),
                        "next_retry_at": _as_float(raw.get("next_retry_at"), now_ts),
                        "created_at": _as_str(raw.get("created_at"), self._now_iso()),
                        "updated_at": _as_str(raw.get("updated_at"), self._now_iso()),
                        "last_error": _as_str(raw.get("last_error")),
                        "last_error_code": _as_str(raw.get("last_error_code")),
                        "last_result": raw.get("last_result") if isinstance(raw.get("last_result"), dict) else {},
                    }
                )
            with self._outbox_lock:
                self._outbox_items = normalized
            if normalized:
                logger.info("KB outbox loaded %s pending records from %s", len(normalized), self._outbox_path)
        except Exception as e:
            logger.warning("Failed to load KB outbox from %s: %s", self._outbox_path, e)

    def _persist_outbox_items(self) -> None:
        if not self._outbox_enabled or not self._outbox_path:
            return
        try:
            parent = os.path.dirname(self._outbox_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with self._outbox_lock:
                snapshot = list(self._outbox_items)
            payload = {
                "version": 1,
                "generated_at": self._now_iso(),
                "items": snapshot,
            }
            temp_path = f"{self._outbox_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self._outbox_path)
        except Exception as e:
            logger.warning("Failed to persist KB outbox to %s: %s", self._outbox_path, e)

    def _compute_backoff_seconds(self, attempts: int) -> int:
        exponent = max(0, attempts - 1)
        delay = int(self._outbox_backoff_base_seconds * (2 ** exponent))
        return min(delay, self._outbox_backoff_max_seconds)

    def enqueue_remote_sync(self, case_payload: Dict[str, Any]) -> str:
        case_id = _as_str(case_payload.get("id"))
        if not case_id:
            raise RuntimeError("case_payload.id is required for KB outbox")
        now_iso = self._now_iso()
        item = {
            "outbox_id": f"kb-outbox-{uuid.uuid4().hex[:12]}",
            "case_id": case_id,
            "payload": case_payload,
            "status": "pending",
            "attempts": 0,
            "max_attempts": self._outbox_max_attempts,
            "next_retry_at": time.time(),
            "created_at": now_iso,
            "updated_at": now_iso,
            "last_error": "",
            "last_error_code": "",
            "last_result": {},
        }
        with self._outbox_lock:
            self._outbox_items.append(item)
        self._persist_outbox_items()
        return _as_str(item.get("outbox_id"))

    def _update_history_sync_status(self, llm_meta: Dict[str, Any], payload_version: int, sync_status: str) -> None:
        history = llm_meta.get("remediation_history")
        if not isinstance(history, list):
            return
        updated = False
        for index in range(len(history) - 1, -1, -1):
            entry = history[index]
            if not isinstance(entry, dict):
                continue
            version = _as_int(entry.get("version"), 0)
            if version == payload_version:
                entry["sync_status"] = sync_status
                updated = True
                break
        if not updated and history and isinstance(history[-1], dict):
            history[-1]["sync_status"] = sync_status

    def _apply_case_sync_result(
        self,
        case_payload: Dict[str, Any],
        sync_status: str,
        external_doc_id: str = "",
        sync_error: str = "",
        sync_error_code: str = "",
    ) -> None:
        case_id = _as_str(case_payload.get("id"))
        if not case_id:
            return
        payload_version = max(1, _as_int(case_payload.get("knowledge_version"), 1))
        try:
            with self._case_sync_lock:
                case_obj = self.case_store.get_case(case_id)
                if not case_obj:
                    return
                updated = case_obj.__class__(**case_obj.to_dict())
                llm_meta = updated.llm_metadata if isinstance(updated.llm_metadata, dict) else {}
                llm_meta = dict(llm_meta)
                current_version = max(1, _as_int(llm_meta.get("knowledge_version"), 1))
                if payload_version < current_version:
                    return

                llm_meta["sync_status"] = sync_status
                llm_meta["external_doc_id"] = external_doc_id if sync_status == "synced" else _as_str(
                    llm_meta.get("external_doc_id")
                )
                llm_meta["sync_error"] = sync_error
                llm_meta["sync_error_code"] = sync_error_code
                self._update_history_sync_status(llm_meta, payload_version, sync_status)
                updated.llm_metadata = llm_meta
                updated.updated_at = self._now_iso()
                self.case_store.update_case(updated)
        except Exception as e:
            logger.warning("Failed to apply KB outbox sync result for case %s: %s", case_id, e)

    def process_outbox_once(self) -> Dict[str, Any]:
        if not self._outbox_enabled:
            return {"enabled": False, "processed": 0}
        now_ts = time.time()
        due_ids: List[str] = []
        with self._outbox_lock:
            for item in self._outbox_items:
                if _as_str(item.get("status"), "pending") != "pending":
                    continue
                if _as_float(item.get("next_retry_at"), now_ts) <= now_ts:
                    due_ids.append(_as_str(item.get("outbox_id")))
        processed = 0
        for outbox_id in due_ids:
            if self._process_outbox_item(outbox_id):
                processed += 1
        return {"enabled": True, "processed": processed, "due": len(due_ids)}

    def _process_outbox_item(self, outbox_id: str) -> bool:
        item_snapshot: Optional[Dict[str, Any]] = None
        item_index = -1
        with self._outbox_lock:
            for index, candidate in enumerate(self._outbox_items):
                if _as_str(candidate.get("outbox_id")) == outbox_id:
                    item_snapshot = dict(candidate)
                    item_index = index
                    self._outbox_items[index]["status"] = "processing"
                    self._outbox_items[index]["updated_at"] = self._now_iso()
                    break
        if item_snapshot is None or item_index < 0:
            return False

        case_payload = item_snapshot.get("payload") if isinstance(item_snapshot.get("payload"), dict) else {}
        result = self.upsert_remote_if_needed(case_payload, save_mode="local_and_remote")
        sync_status = _as_str(result.get("sync_status"), "failed")
        external_doc_id = _as_str(result.get("external_doc_id"))
        sync_error = _as_str(result.get("sync_error"))
        sync_error_code = _as_str(result.get("sync_error_code"))

        if sync_status == "synced":
            self._apply_case_sync_result(
                case_payload=case_payload,
                sync_status="synced",
                external_doc_id=external_doc_id,
                sync_error="",
                sync_error_code="",
            )
            with self._outbox_lock:
                self._outbox_items = [
                    item for item in self._outbox_items if _as_str(item.get("outbox_id")) != outbox_id
                ]
            self._persist_outbox_items()
            return True

        attempts = max(0, _as_int(item_snapshot.get("attempts"), 0)) + 1
        max_attempts = max(1, _as_int(item_snapshot.get("max_attempts"), self._outbox_max_attempts))
        retryable = attempts < max_attempts
        next_retry_at = time.time() + self._compute_backoff_seconds(attempts)

        with self._outbox_lock:
            if item_index >= len(self._outbox_items):
                return False
            target = self._outbox_items[item_index]
            target["attempts"] = attempts
            target["updated_at"] = self._now_iso()
            target["last_error"] = sync_error
            target["last_error_code"] = sync_error_code
            target["last_result"] = result if isinstance(result, dict) else {}
            target["next_retry_at"] = next_retry_at
            target["status"] = "pending" if retryable else "failed"
        self._persist_outbox_items()

        if retryable:
            self._apply_case_sync_result(
                case_payload=case_payload,
                sync_status="pending",
                external_doc_id="",
                sync_error=sync_error,
                sync_error_code=sync_error_code,
            )
        else:
            _metric_inc(KB_SYNC_FAILED_TOTAL)
            self._apply_case_sync_result(
                case_payload=case_payload,
                sync_status="failed",
                external_doc_id="",
                sync_error=sync_error or "outbox max retries exhausted",
                sync_error_code=sync_error_code or "KBR-008",
            )
        return True

    def _outbox_worker_loop(self) -> None:
        logger.info(
            "KB outbox worker started: poll=%ss max_attempts=%s",
            self._outbox_poll_seconds,
            self._outbox_max_attempts,
        )
        while not self._outbox_stop_event.is_set():
            try:
                self.process_outbox_once()
            except Exception as e:
                logger.warning("KB outbox worker tick failed: %s", e)
            self._outbox_stop_event.wait(self._outbox_poll_seconds)
        logger.info("KB outbox worker stopped")

    def start_outbox_worker(self) -> bool:
        if not self._outbox_enabled:
            return False
        if self._outbox_worker_thread and self._outbox_worker_thread.is_alive():
            return True
        self._outbox_stop_event.clear()
        self._outbox_worker_thread = threading.Thread(
            target=self._outbox_worker_loop,
            name="kb-outbox-worker",
            daemon=True,
        )
        self._outbox_worker_thread.start()
        return True

    def stop_outbox_worker(self) -> None:
        if not self._outbox_enabled:
            return
        self._outbox_stop_event.set()
        if self._outbox_worker_thread and self._outbox_worker_thread.is_alive():
            self._outbox_worker_thread.join(timeout=2.0)

    def get_outbox_status(self) -> Dict[str, Any]:
        with self._outbox_lock:
            queue_total = len(self._outbox_items)
            pending = len([item for item in self._outbox_items if _as_str(item.get("status")) == "pending"])
            failed = len([item for item in self._outbox_items if _as_str(item.get("status")) == "failed"])
            processing = len([item for item in self._outbox_items if _as_str(item.get("status")) == "processing"])
            failed_retry_attempts = sum(
                _as_int(item.get("attempts"), 0)
                for item in self._outbox_items
                if _as_str(item.get("status")) == "failed"
            )
            failed_by_code: Dict[str, int] = {}
            for item in self._outbox_items:
                if _as_str(item.get("status")) != "failed":
                    continue
                code = _as_str(item.get("last_error_code"))
                if not code:
                    continue
                failed_by_code[code] = failed_by_code.get(code, 0) + 1
            sample = [
                {
                    "outbox_id": _as_str(item.get("outbox_id")),
                    "case_id": _as_str(item.get("case_id")),
                    "status": _as_str(item.get("status")),
                    "attempts": _as_int(item.get("attempts"), 0),
                    "max_attempts": _as_int(item.get("max_attempts"), self._outbox_max_attempts),
                    "next_retry_at": _as_float(item.get("next_retry_at"), 0.0),
                    "last_error": _as_str(item.get("last_error")),
                    "last_error_code": _as_str(item.get("last_error_code")),
                }
                for item in self._outbox_items[:20]
            ]
        return {
            "enabled": self._outbox_enabled,
            "worker_running": bool(self._outbox_worker_thread and self._outbox_worker_thread.is_alive()),
            "queue_total": queue_total,
            "pending": pending,
            "failed": failed,
            "processing": processing,
            "failed_retry_attempts": failed_retry_attempts,
            "failed_by_code": failed_by_code,
            "poll_seconds": self._outbox_poll_seconds,
            "max_attempts": self._outbox_max_attempts,
            "items": sample,
        }

    def get_provider_status(self, force_refresh: bool = False) -> Dict[str, Any]:
        now_ts = time.time()
        if (
            not force_refresh
            and self._last_status_payload
            and (now_ts - self._last_status_ts) < self._status_cache_seconds
        ):
            cached = dict(self._last_status_payload)
            cached["cached"] = True
            return cached

        if self.provider is None:
            payload = {
                "mode": "local_only",
                "provider": "",
                "remote_configured": False,
                "remote_available": False,
                "message": "remote provider disabled",
                "cached": False,
            }
        else:
            health = self.provider.health()
            payload = {
                "mode": "hybrid" if health.get("available") else "local_only",
                "provider": _as_str(health.get("provider")),
                "remote_configured": bool(health.get("configured", False)),
                "remote_available": bool(health.get("available", False)),
                "message": _as_str(health.get("message")),
                "detail": health,
                "cached": False,
            }

        outbox = self.get_outbox_status()
        payload["outbox_queue_total"] = _as_int(outbox.get("queue_total"), 0)
        payload["outbox_failed"] = _as_int(outbox.get("failed"), 0)
        payload["outbox_failed_retry_attempts"] = _as_int(outbox.get("failed_retry_attempts"), 0)
        payload["outbox_worker_running"] = bool(outbox.get("worker_running", False))

        self._last_status_ts = now_ts
        self._last_status_payload = dict(payload)
        return payload

    def resolve_runtime_options(
        self,
        remote_enabled: bool,
        retrieval_mode: str,
        save_mode: str,
    ) -> Dict[str, Any]:
        normalized_retrieval = retrieval_mode if retrieval_mode in {"local", "hybrid"} else "local"
        normalized_save = save_mode if save_mode in {"local_only", "local_and_remote"} else "local_only"
        status = self.get_provider_status()
        remote_available = bool(status.get("remote_available"))
        remote_configured = bool(status.get("remote_configured"))

        if not remote_enabled:
            return {
                "effective_retrieval_mode": "local",
                "effective_save_mode": "local_only",
                "remote_available": remote_available,
                "provider_name": _as_str(status.get("provider")),
                "message": "remote disabled by user",
            }

        if not remote_configured:
            _metric_inc(KB_REMOTE_FALLBACK_TOTAL)
            return {
                "effective_retrieval_mode": "local",
                "effective_save_mode": "local_only",
                "remote_available": False,
                "remote_configured": False,
                "provider_name": _as_str(status.get("provider")),
                "message": "未检测到远端知识库接入，已切换为本地知识库模式。",
                "warning_code": "KBR-006",
            }

        if not remote_available:
            _metric_inc(KB_REMOTE_FALLBACK_TOTAL)
            return {
                "effective_retrieval_mode": "local",
                "effective_save_mode": "local_only",
                "remote_available": False,
                "remote_configured": remote_configured,
                "provider_name": _as_str(status.get("provider")),
                "message": "远端知识库未接入，已自动回退本地模式",
                "warning_code": "KBR-007",
            }

        return {
            "effective_retrieval_mode": normalized_retrieval,
            "effective_save_mode": normalized_save,
            "remote_available": True,
            "remote_configured": remote_configured,
            "provider_name": _as_str(status.get("provider")),
            "message": "ok",
        }

    @staticmethod
    def _normalize_remote_case(item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        case_id = _as_str(item.get("id") or item.get("doc_id"))
        summary = _as_str(item.get("summary") or item.get("title"))
        if not case_id and not summary:
            return None
        return {
            "id": case_id or f"ext-{abs(hash(summary))}",
            "summary": summary or "remote-case",
            "problem_type": _as_str(item.get("problem_type"), "unknown"),
            "service_name": _as_str(item.get("service_name")),
            "similarity_score": float(item.get("score") or item.get("similarity_score") or 0.0),
            "source_backend": "external",
            "resolution": _as_str(item.get("resolution")),
            "verification_result": _as_str(item.get("verification_result")),
            "raw": item,
        }

    def _is_case_visible(self, case_obj: Any, include_draft: bool = False) -> bool:
        status = _case_status(case_obj)
        if include_draft:
            return status in {"draft", "archived", "resolved"}
        return status in {"archived", "resolved"}

    def _local_search(
        self,
        query: str,
        service_name: str,
        problem_type: str,
        top_k: int,
        include_draft: bool,
    ) -> List[Dict[str, Any]]:
        local_results = self.recommender.find_similar_cases(
            log_content=query,
            service_name=service_name,
            problem_type=problem_type,
            context={},
            limit=top_k,
            min_similarity=0.2,
        )

        if not local_results:
            searched = self.case_store.search(query, limit=top_k)
            payload = []
            for case in searched:
                if not self._is_case_visible(case, include_draft=include_draft):
                    continue
                payload.append(
                    {
                        "id": case.id,
                        "summary": case.summary,
                        "problem_type": case.problem_type,
                        "service_name": case.service_name,
                        "similarity_score": 0.2,
                        "source_backend": "local",
                        "resolution": case.resolution,
                        "verification_result": _as_str((case.llm_metadata or {}).get("verification_result")),
                    }
                )
            return payload

        payload = []
        for item in local_results:
            case = item.case
            if not self._is_case_visible(case, include_draft=include_draft):
                continue
            payload.append(
                {
                    "id": case.id,
                    "summary": case.summary,
                    "problem_type": case.problem_type,
                    "service_name": case.service_name,
                    "similarity_score": float(item.similarity_score),
                    "source_backend": "local",
                    "resolution": case.resolution,
                    "verification_result": _as_str((case.llm_metadata or {}).get("verification_result")),
                }
            )
        return payload

    def search(
        self,
        query: str,
        service_name: str = "",
        problem_type: str = "",
        top_k: int = 5,
        retrieval_mode: str = "local",
        include_draft: bool = False,
    ) -> Dict[str, Any]:
        safe_top_k = min(max(1, int(top_k)), 20)
        search_start = time.perf_counter()
        local_start = time.perf_counter()
        local = self._local_search(
            query=query,
            service_name=service_name,
            problem_type=problem_type,
            top_k=safe_top_k,
            include_draft=include_draft,
        )
        _metric_observe(KB_LOCAL_SEARCH_LATENCY_MS, (time.perf_counter() - local_start) * 1000.0)

        merged = list(local)
        source_counter = {"local": len(local), "external": 0}
        warning_code = ""
        warning_message = ""

        if retrieval_mode == "hybrid" and self.provider is not None and self.get_provider_status().get("remote_available"):
            try:
                remote_items = self.provider.search_cases(
                    {
                        "query": query,
                        "service_name": service_name,
                        "problem_type": problem_type,
                        "top_k": safe_top_k,
                    }
                )
                normalized_remote = [
                    self._normalize_remote_case(item)
                    for item in remote_items
                ]
                normalized_remote = [item for item in normalized_remote if item]
                source_counter["external"] = len(normalized_remote)
                merged.extend(normalized_remote)
            except Exception as e:
                logger.warning(f"Remote KB search failed, fallback to local: {e}")
                warning_message, warning_code = self._extract_sync_error(e)
                if not warning_code:
                    warning_code = "KBR-008"
                if not warning_message:
                    warning_message = "remote search failed, fallback to local"
                _metric_inc(KB_REMOTE_FALLBACK_TOTAL)
        elif retrieval_mode == "hybrid":
            _metric_inc(KB_REMOTE_FALLBACK_TOTAL)
            warning_code = "KBR-007"
            warning_message = "remote unavailable, fallback to local"

        dedup: Dict[str, Dict[str, Any]] = {}
        for item in merged:
            key = _as_str(item.get("id")) or _as_str(item.get("summary"))
            if not key:
                continue
            prev = dedup.get(key)
            if prev is None or float(item.get("similarity_score", 0.0)) > float(prev.get("similarity_score", 0.0)):
                dedup[key] = item

        final_cases = sorted(dedup.values(), key=lambda x: float(x.get("similarity_score", 0.0)), reverse=True)[:safe_top_k]
        if retrieval_mode == "hybrid":
            _metric_observe(KB_HYBRID_SEARCH_LATENCY_MS, (time.perf_counter() - search_start) * 1000.0)
        return {
            "cases": final_cases,
            "total": len(final_cases),
            "sources": source_counter,
            "warning_code": warning_code,
            "warning_message": warning_message,
        }

    def upsert_remote_if_needed(self, case_payload: Dict[str, Any], save_mode: str) -> Dict[str, Any]:
        if save_mode != "local_and_remote":
            return {"sync_status": "not_requested", "external_doc_id": "", "sync_error": "", "sync_error_code": ""}
        if self.provider is None:
            _metric_inc(KB_SYNC_FAILED_TOTAL)
            return {
                "sync_status": "failed",
                "external_doc_id": "",
                "sync_error": "remote provider disabled",
                "sync_error_code": "KBR-006",
            }
        if not self.get_provider_status().get("remote_available"):
            _metric_inc(KB_SYNC_FAILED_TOTAL)
            return {
                "sync_status": "failed",
                "external_doc_id": "",
                "sync_error": "remote provider unavailable",
                "sync_error_code": "KBR-007",
            }
        try:
            response = self.provider.upsert_case(case_payload)
            external_id = _as_str(response.get("doc_id") or response.get("id"))
            return {"sync_status": "synced", "external_doc_id": external_id, "sync_error": "", "sync_error_code": ""}
        except Exception as e:
            sync_error, sync_error_code = self._extract_sync_error(e)
            _metric_inc(KB_SYNC_FAILED_TOTAL)
            return {
                "sync_status": "failed",
                "external_doc_id": "",
                "sync_error": sync_error or str(e),
                "sync_error_code": sync_error_code or "KBR-008",
            }

    def upsert_remote_with_outbox(self, case_payload: Dict[str, Any], save_mode: str) -> Dict[str, Any]:
        """本地主链路优先：远端写入通过 Outbox 异步重试。"""
        if save_mode != "local_and_remote":
            return {"sync_status": "not_requested", "external_doc_id": "", "sync_error": "", "sync_error_code": ""}
        if not self._outbox_enabled:
            return self.upsert_remote_if_needed(case_payload, save_mode)
        if not (self._outbox_worker_thread and self._outbox_worker_thread.is_alive()):
            self.start_outbox_worker()
        try:
            outbox_id = self.enqueue_remote_sync(case_payload)
            return {
                "sync_status": "pending",
                "external_doc_id": "",
                "sync_error": "",
                "sync_error_code": "",
                "outbox_id": outbox_id,
            }
        except Exception as e:
            _metric_inc(KB_SYNC_FAILED_TOTAL)
            return {
                "sync_status": "failed",
                "external_doc_id": "",
                "sync_error": str(e),
                "sync_error_code": "KBR-008",
            }


_knowledge_gateway: Optional[KnowledgeGateway] = None


def get_knowledge_gateway(storage_adapter: Any = None) -> KnowledgeGateway:
    global _knowledge_gateway
    if _knowledge_gateway is None:
        _knowledge_gateway = KnowledgeGateway(storage_adapter=storage_adapter)
    elif storage_adapter is not None and not _knowledge_gateway.case_store.storage:
        _knowledge_gateway = KnowledgeGateway(storage_adapter=storage_adapter)
    return _knowledge_gateway


def shutdown_knowledge_gateway() -> None:
    global _knowledge_gateway
    if _knowledge_gateway is None:
        return
    try:
        _knowledge_gateway.stop_outbox_worker()
    except Exception:
        pass
    _knowledge_gateway = None


def reload_knowledge_gateway(storage_adapter: Any = None) -> KnowledgeGateway:
    """重建全局网关，使运行时配置更新后即时生效。"""
    shutdown_knowledge_gateway()
    return get_knowledge_gateway(storage_adapter=storage_adapter)
