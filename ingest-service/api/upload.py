"""Upload API — offline log file ingestion endpoint."""
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from config import config
from services.queue_writer import write_to_queue

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 500 * 1024 * 1024
BATCH_SIZE = 100

# Priority-ordered timestamp+level extraction patterns for text logs
TIMESTAMP_PATTERNS = [
    re.compile(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))"
    ),
    re.compile(
        r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+\d+\s+"
        r"(ERROR|CRITICAL|WARN|INFO|DEBUG)"
    ),
    re.compile(
        r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+"
        r"(?:\[)?(ERROR|CRITICAL|WARN|INFO|DEBUG)"
    ),
    re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"),
]

SERVICE_NAME_PATTERN = re.compile(
    r"\d+\s+(?:ERROR|CRITICAL|WARN|INFO|DEBUG)\s+([\w-]+)"
)

_GENERIC_LOG_NAMES = {"output", "log", "console", "messages", "stdout", "stderr", "syslog"}
_SERVICE_SCAN_MAX_LINES = 50


def _parse_text_line(line: str) -> Dict[str, Any]:
    """Parse a single text log line, best-effort timestamp/level extraction."""
    record: Dict[str, Any] = {"message": line, "level": "INFO"}
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(line)
        if match:
            record["timestamp"] = match.group(1)
            if pattern.groups >= 2 and match.lastindex and match.lastindex >= 2:
                record["level"] = match.group(2).upper()
            break
    return record


def _detect_json_format(content: str) -> Optional[str]:
    """Detect whether content is JSON array or NDJSON."""
    stripped = content.strip()
    if stripped.startswith("["):
        return "array"
    for line in stripped.split("\n"):
        line = line.strip()
        if line:
            if line.startswith("{"):
                return "ndjson"
            break
    return None


def _parse_json_array(content: str) -> List[Dict[str, Any]]:
    records = json.loads(content)
    if not isinstance(records, list):
        raise ValueError("JSON content is not an array")
    return records


def _parse_ndjson(content: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def _resolve_service_name(
    filename: str,
    first_lines: List[str],
    user_input: Optional[str],
) -> str:
    """Resolve service name: user input > content auto-detect > filename > default."""
    if user_input and user_input.strip():
        return user_input.strip()
    for line in first_lines:
        match = SERVICE_NAME_PATTERN.search(line)
        if match:
            return match.group(1)
    stem = Path(filename).stem
    if stem.lower() not in _GENERIC_LOG_NAMES:
        return stem
    return "offline-upload"


async def _stream_parse_file(
    file: UploadFile,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Read entire file, yield individual parsed log records."""
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large, max 500MB")
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    body = content.decode("utf-8", errors="replace")

    ext = Path(file.filename or "").suffix.lower()
    if ext == ".json":
        json_fmt = _detect_json_format(body)
        if json_fmt == "array":
            records = _parse_json_array(body)
        elif json_fmt == "ndjson":
            records = _parse_ndjson(body)
        else:
            raise HTTPException(400, "Unrecognized JSON format (must be array or NDJSON)")
        for rec in records:
            yield rec
    else:
        for raw_line in body.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            yield _parse_text_line(line)


@router.post("/v1/logs/upload")
async def upload_logs(
    file: UploadFile = File(...),
    service_name: Optional[str] = Form(None),
    namespace: Optional[str] = Form("default"),
):
    """
    Upload a log file for ingestion into the platform.

    Supports .json (JSON array or NDJSON), .log, .txt.
    Service name resolution: 1) user override, 2) content detect, 3) filename, 4) default.
    """
    logger.info("Upload started: filename=%s", file.filename)
    upload_id = f"upl_{uuid.uuid4().hex[:12]}"
    _namespace = (namespace or "default").strip() or "default"

    total = 0
    batches = 0
    resolved_service: Optional[str] = None
    first_lines: List[str] = []
    batch_buffer: List[Dict[str, Any]] = []

    async for record in _stream_parse_file(file):
        if len(first_lines) < _SERVICE_SCAN_MAX_LINES:
            msg = record.get("message", "") or record.get("log", "") or ""
            if msg:
                first_lines.append(msg)

        if resolved_service is None and first_lines:
            resolved_service = _resolve_service_name(
                filename=file.filename or "unknown.log",
                first_lines=first_lines,
                user_input=service_name,
            )
            logger.info(
                "Resolved service_name=%s namespace=%s upload_id=%s",
                resolved_service, _namespace, upload_id,
            )

        batch_buffer.append(record)
        if len(batch_buffer) >= BATCH_SIZE:
            await _flush_batch(batch_buffer, upload_id, resolved_service or "offline-upload", _namespace, batches)
            total += len(batch_buffer)
            batches += 1
            batch_buffer = []

    if batch_buffer:
        if resolved_service is None:
            resolved_service = _resolve_service_name(
                filename=file.filename or "unknown.log",
                first_lines=first_lines,
                user_input=service_name,
            )
        await _flush_batch(batch_buffer, upload_id, resolved_service or "offline-upload", _namespace, batches)
        total += len(batch_buffer)
        batches += 1

    logger.info(
        "Upload complete: upload_id=%s filename=%s service=%s total=%d batches=%d",
        upload_id, file.filename, resolved_service, total, batches,
    )
    return {
        "status": "accepted",
        "upload_id": upload_id,
        "total": total,
        "batches": batches,
    }


async def _flush_batch(
    records: List[Dict[str, Any]],
    upload_id: str,
    service_name: str,
    namespace: str,
    batch_index: int,
) -> None:
    """Write a batch of records to the upload-type envelope in Redis Stream."""
    stream_records = []
    for i, rec in enumerate(records):
        ts = rec.get("timestamp") or ""
        level = str(rec.get("level", "INFO")).upper()[:8]
        msg: str = rec.get("message", "") or rec.get("log", "") or ""

        stream_records.append({
            "message": msg,
            "timestamp": ts,
            "level": level,
            "service_name": service_name,
            "_raw_attributes": {
                "upload_id": upload_id,
                "batch_index": batch_index,
                "record_index": i,
                "source": "upload",
            },
        })

    envelope = json.dumps({
        "type": "upload",
        "upload_id": upload_id,
        "service_name": service_name,
        "namespace": namespace,
        "records": stream_records,
    })

    await write_to_queue(
        stream=config.redis_stream_logs,
        data_type="logs",
        payload=envelope,
        metadata={
            "upload_id": upload_id,
            "batch_index": batch_index,
            "source": "upload",
            "service_name": service_name,
        },
    )
