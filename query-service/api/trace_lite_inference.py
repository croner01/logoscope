"""Trace-Lite inference helpers extracted from query routes."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Tuple


def parse_json_dict(raw: Any) -> Dict[str, Any]:
    """Safely parse raw JSON payload to dictionary."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}

    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def extract_request_id(attrs: Dict[str, Any], message: str = "") -> str:
    """Extract request_id from attrs and message text."""
    keys = (
        "request_id",
        "request.id",
        "requestId",
        "req_id",
        "x-request-id",
        "x_request_id",
        "http.request_id",
        "trace.request_id",
    )
    for key in keys:
        value = attrs.get(key)
        if value:
            return str(value).strip()

    text = str(message or "")
    patterns = [
        r"(?:request[_-]?id|x-request-id)\s*[:=]\s*([a-zA-Z0-9\-_.]{6,})",
        r"\b([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ""


def to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "")
    if not text:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return datetime.utcnow()


def infer_trace_lite_fragments_from_logs(
    log_rows: List[Dict[str, Any]],
    fallback_window_sec: float = 2.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Infer Trace-Lite fragments from logs using request_id-first strategy.
    Returns (fragments, stats).
    """
    prepared: List[Dict[str, Any]] = []
    for row in log_rows:
        attrs = parse_json_dict(row.get("attributes_json"))
        prepared.append(
            {
                "id": row.get("id"),
                "ts": to_datetime(row.get("timestamp")),
                "service_name": row.get("service_name") or "unknown",
                "namespace": row.get("namespace") or attrs.get("namespace") or "default",
                "message": row.get("message") or "",
                "trace_id": row.get("trace_id") or "",
                "attrs": attrs,
                "request_id": extract_request_id(attrs, row.get("message") or ""),
            }
        )

    request_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    fallback_records: List[Dict[str, Any]] = []
    for item in prepared:
        if item["request_id"]:
            request_groups[item["request_id"]].append(item)
        else:
            fallback_records.append(item)

    fragments_acc: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def add_fragment(
        source_service: str,
        target_service: str,
        method: str,
        evidence: Dict[str, Any],
    ) -> None:
        if not source_service or not target_service or source_service == target_service:
            return
        key = (source_service, target_service)
        if key not in fragments_acc:
            fragments_acc[key] = {
                "source_service": source_service,
                "target_service": target_service,
                "inference_method": method,
                "sample_size": 0,
                "evidence_chain": [],
                "request_ids": set(),
                "trace_ids": set(),
                "first_seen": None,
                "last_seen": None,
            }
        item = fragments_acc[key]
        item["sample_size"] += 1
        if len(item["evidence_chain"]) < 10:
            item["evidence_chain"].append(evidence)
        rid = evidence.get("request_id")
        if rid:
            item["request_ids"].add(rid)
        tid = evidence.get("trace_id")
        if tid:
            item["trace_ids"].add(tid)

        ts_text = evidence.get("source_ts") or evidence.get("target_ts")
        ts_val = to_datetime(ts_text)
        if not item["first_seen"] or ts_val < item["first_seen"]:
            item["first_seen"] = ts_val
        if not item["last_seen"] or ts_val > item["last_seen"]:
            item["last_seen"] = ts_val

    request_id_edges = 0
    for request_id, records in request_groups.items():
        if len(records) < 2:
            continue
        records = sorted(records, key=lambda x: x["ts"])
        compacted = [records[0]]
        for record in records[1:]:
            if record["service_name"] != compacted[-1]["service_name"]:
                compacted.append(record)
        for idx in range(len(compacted) - 1):
            src = compacted[idx]
            dst = compacted[idx + 1]
            add_fragment(
                source_service=src["service_name"],
                target_service=dst["service_name"],
                method="request_id",
                evidence={
                    "request_id": request_id,
                    "trace_id": src["trace_id"] or dst["trace_id"],
                    "source_log_id": src["id"],
                    "target_log_id": dst["id"],
                    "source_ts": src["ts"].isoformat(),
                    "target_ts": dst["ts"].isoformat(),
                },
            )
            request_id_edges += 1

    time_window_edges = 0
    fallback_records = sorted(fallback_records, key=lambda x: x["ts"])
    for idx in range(len(fallback_records) - 1):
        src = fallback_records[idx]
        dst = fallback_records[idx + 1]
        if src["service_name"] == dst["service_name"]:
            continue
        if src["namespace"] != dst["namespace"]:
            continue
        delta = (dst["ts"] - src["ts"]).total_seconds()
        if delta < 0 or delta > fallback_window_sec:
            continue
        add_fragment(
            source_service=src["service_name"],
            target_service=dst["service_name"],
            method="time_window",
            evidence={
                "request_id": "",
                "trace_id": src["trace_id"] or dst["trace_id"],
                "source_log_id": src["id"],
                "target_log_id": dst["id"],
                "source_ts": src["ts"].isoformat(),
                "target_ts": dst["ts"].isoformat(),
                "time_window_sec": round(delta, 3),
            },
        )
        time_window_edges += 1

    fragments: List[Dict[str, Any]] = []
    for key, item in fragments_acc.items():
        method = item["inference_method"]
        base = 0.72 if method == "request_id" else 0.46
        confidence = min(0.92, base + min(0.2, item["sample_size"] * 0.02))
        fragments.append(
            {
                "fragment_id": f"{key[0]}->{key[1]}",
                "source_service": item["source_service"],
                "target_service": item["target_service"],
                "inference_method": method,
                "confidence": round(confidence, 3),
                "confidence_explain": (
                    "request_id matched"
                    if method == "request_id"
                    else "request_id missing, fallback to time-window strategy"
                ),
                "sample_size": item["sample_size"],
                "request_ids": sorted(item["request_ids"]),
                "trace_ids": sorted(item["trace_ids"]),
                "evidence_chain": item["evidence_chain"],
                "first_seen": item["first_seen"].isoformat() if item["first_seen"] else None,
                "last_seen": item["last_seen"].isoformat() if item["last_seen"] else None,
            }
        )

    stats = {
        "total_logs": len(prepared),
        "request_id_groups": len(request_groups),
        "request_id_edges": request_id_edges,
        "time_window_edges": time_window_edges,
        "inferred_edges": len(fragments),
        "strategy": "request_id_first_then_time_window",
    }
    return fragments, stats
