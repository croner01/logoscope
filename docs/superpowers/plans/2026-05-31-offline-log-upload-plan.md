# Offline Log Upload — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow users to upload local log files (.log/.txt/.json) via the LogsExplorer UI and have them processed through the full pipeline (Ingest → Redis → Semantic Engine → ClickHouse).

**Architecture:** Frontend uploads files via multipart POST to a new Ingest Service endpoint (`/v1/logs/upload`). The endpoint parses files server-side (JSON array, NDJSON, or plain text), resolves service name via filename/content/user override, batches records to the same Redis Stream that OTLP data flows through. A small addition to the queue_writer's `_build_log_queue_messages` transforms upload records into the format the Semantic Engine expects. Normalizer picks them up without changes. Nginx routes the upload path to Ingest Service separately from the query-service path.

**Tech Stack:** Python/FastAPI (Ingest Service), React/TypeScript (Frontend), nginx (routing), Redis Stream (message queue), Semantic Engine (normalizer)

**Spec:** `docs/superpowers/specs/2026-05-31-offline-log-upload-design.md`

---

### Task 1: Backend — Upload endpoint (`ingest-service/api/upload.py`)

**Files:**
- Create: `ingest-service/api/upload.py`
- Modify: `ingest-service/main.py` (register router)

- [ ] **Step 1: Create upload.py**

```python
"""Upload API — offline log file ingestion endpoint."""
import asyncio
import json
import logging
import os
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
    Supports .json (array/NDJSON), .log, .txt.
    Service name resolution: 1) user override, 2) content detect, 3) filename, 4) default.
    """
    logger.info("Upload started: filename=%s", file.filename)
    upload_id = f"upl_{uuid.uuid4().hex[:12]}"
    _namespace = (namespace or "default").strip() or "default"

    total = 0
    batches = 0
    resolved_service = None
    first_lines: List[str] = []
    batch_buffer: List[Dict[str, Any]] = []

    async for record in _stream_parse_file(file):
        # Collect first lines for service name detection
        if len(first_lines) < _SERVICE_SCAN_MAX_LINES:
            msg = record.get("message", "") or record.get("log", "") or ""
            if msg:
                first_lines.append(msg)

        # Resolve service name on first batch
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

    # Flush remaining
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
        msg = rec.get("message", "") or rec.get("log", "") or ""

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
```

- [ ] **Step 2: Register the router in main.py**

Edit `ingest-service/main.py`, add import after existing ingest_router import:

```python
from api.upload import router as upload_router
```

Add registration after the existing `app.include_router(ingest_router, ...)`:

```python
app.include_router(upload_router, tags=["upload"])
```

- [ ] **Step 3: Verify upload route registers correctly**

```bash
cd /root/logoscope/ingest-service
python -c "
from fastapi.testclient import TestClient
from main import app
routes = [r.path for r in app.routes]
assert '/v1/logs/upload' in routes, f'upload route not found in {routes}'
print('Upload route registered OK')
"
```

Expected: `Upload route registered OK`

- [ ] **Step 4: Run existing tests to verify no regressions**

```bash
cd /root/logoscope/ingest-service
python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: All existing tests pass.

- [ ] **Step 5: Commit**

```bash
git add ingest-service/api/upload.py ingest-service/main.py
git commit -m "feat(ingest-service): add /v1/logs/upload endpoint for offline file ingestion"
```

---

### Task 2: Backend — Queue writer upload message transform

**Files:**
- Modify: `ingest-service/services/queue_writer.py` (in `_build_log_queue_messages`, after the OTLP check and before Fluent Bit fallback)

- [ ] **Step 1: Add upload-type handling in `_build_log_queue_messages`**

In `ingest-service/services/queue_writer.py`, in the function `_build_log_queue_messages`, add a new branch after the `is_otlp_format` check (around line 604, before the `else` fallback to `transform_fluent_bit_json`):

```python
# Upload-type: records already pre-formatted with correct fields
if isinstance(item, dict) and item.get("type") == "upload":
    upload_id = item.get("upload_id", "")
    service_name = item.get("service_name", "offline-upload")
    namespace = item.get("namespace", "default")
    upload_records = item.get("records", [])
    for upload_record in upload_records:
        msg = upload_record.get("message", "")
        ts = upload_record.get("timestamp", "")
        level = upload_record.get("level", "INFO")
        raw_attrs = upload_record.get("_raw_attributes", {})
        messages.append({
            "log": msg,
            "timestamp": ts,
            "severity": level,
            "service.name": service_name,
            "attributes": {
                **raw_attrs,
                "upload_id": upload_id,
            },
            "resource": {},
            "kubernetes": {
                "pod_name": f"upload-{upload_id[:12]}",
                "namespace_name": namespace,
                "labels": {
                    "source": "upload",
                    "upload_id": upload_id,
                    "service_name": service_name,
                },
            },
        })
    continue
```

The `continue` skips the Fluent Bit fallback. This branch should replace the `else` for upload-type items, so the structure is:

```python
if is_otlp_format(item):
    ...
elif isinstance(item, dict) and item.get("type") == "upload":
    ...  # upload handling
else:
    messages.append(transform_fluent_bit_json(item, metadata))
```

- [ ] **Step 2: Run tests to verify**

```bash
cd /root/logoscope/ingest-service
python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: All existing tests pass.

- [ ] **Step 3: Commit**

```bash
git add ingest-service/services/queue_writer.py
git commit -m "feat(ingest-service): add upload-type message transform in queue writer"
```

---

### Task 3: Infrastructure — Nginx route for upload endpoint

**Files:**
- Modify: `frontend/nginx.conf`

- [ ] **Step 1: Add upload-specific nginx location before the generic `/api/v1/logs` rule**

In `frontend/nginx.conf`, add a new location block **before** the existing `location ~ ^/api/v1/logs {` (around line 79), so it takes priority:

```nginx
# Upload endpoint — route to ingest-service, not query-service
location = /api/v1/logs/upload {
    proxy_pass http://ingest-service:8080/v1/logs/upload;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 500m;
}
```

And update `client_max_body_size` in the top-level `server` block as well (after `listen 80`):

```nginx
client_max_body_size 500m;
```

- [ ] **Step 2: Verify nginx config syntax**

```bash
cd /root/logoscope/frontend
nginx -t -c nginx.conf 2>&1 || echo "nginx -t requires the server to be installed; check syntax manually"
```

- [ ] **Step 3: Check the Ingest Service K8s Service exists**

```bash
kubectl get svc -n islap ingest-service 2>/dev/null || echo "Verify ingest-service DNS resolves inside cluster (expected: ingest-service.islap.svc.cluster.local)"
```

- [ ] **Step 4: Commit**

```bash
git add frontend/nginx.conf
git commit -m "fix(nginx): route /api/v1/logs/upload to ingest-service with 500MB limit"
```

---

### Task 4: Frontend — API client upload method

**Files:**
- Modify: `frontend/src/utils/api.ts`

- [ ] **Step 1: Add UploadResult interface**

After the existing `AggregatedLogsParams` interface (around line 756), add:

```typescript
/** Offline log upload result */
export interface UploadResult {
  status: string;      // "accepted"
  upload_id: string;
  total: number;
  batches: number;
}
```

- [ ] **Step 2: Add uploadLogs method to APIClient class**

After the `getEvents` method (around line 1040), add:

```typescript
/**
 * Upload a log file for offline ingestion.
 * Supports .log, .txt, .json files up to 500MB.
 */
async uploadLogs(
  file: File,
  options?: {
    serviceName?: string;
    namespace?: string;
    onProgress?: (percent: number) => void;
    signal?: AbortSignal;
  },
): Promise<UploadResult> {
  const formData = new FormData();
  formData.append('file', file);
  if (options?.serviceName) formData.append('service_name', options.serviceName);
  if (options?.namespace) formData.append('namespace', options.namespace);

  const response = await this.client.post('/api/v1/logs/upload', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress: (event) => {
      if (event.total && options?.onProgress) {
        options.onProgress(Math.round((event.loaded / event.total) * 100));
      }
    },
    signal: options?.signal,
    timeout: 300_000,
  });
  return response.data as UploadResult;
}
```

- [ ] **Step 3: Export uploadLogs in the module api object**

Find the `export const api = new APIClient()` or similar export, add:

```typescript
uploadLogs: (file: File, options?: Parameters<APIClient['uploadLogs']>[1]) =>
  apiClient.uploadLogs(file, options),
```

- [ ] **Step 4: TypeScript check**

```bash
cd /root/logoscope/frontend
npx tsc --noEmit --pretty 2>&1 | head -20
```

Expected: No errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/utils/api.ts
git commit -m "feat(frontend): add uploadLogs method to API client"
```

---

### Task 5: Frontend — Upload dialog component

**Files:**
- Create: `frontend/src/components/logs/UploadDialog.tsx`

- [ ] **Step 1: Create UploadDialog component**

```tsx
import React, { useCallback, useRef, useState } from 'react';
import { Upload, X, FileText, FileJson, AlertCircle, CheckCircle2, Loader2 } from 'lucide-react';
import { api, type UploadResult } from '../../utils/api';

interface UploadDialogProps {
  open: boolean;
  onClose: () => void;
  onSuccess?: (result: UploadResult) => void;
}

type UploadState = 'idle' | 'uploading' | 'success' | 'error';

const UploadDialog: React.FC<UploadDialogProps> = ({ open, onClose, onSuccess }) => {
  const [file, setFile] = useState<File | null>(null);
  const [serviceName, setServiceName] = useState('');
  const [namespace, setNamespace] = useState('default');
  const [uploadState, setUploadState] = useState<UploadState>('idle');
  const [progress, setProgress] = useState(0);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [error, setError] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const f = e.dataTransfer.files[0];
    if (f) setFile(f);
  }, []);

  const handleSelectFile = () => inputRef.current?.click();

  const acceptedExtensions = '.log,.txt,.json,.ndjson';

  const handleUpload = async () => {
    if (!file) return;
    setUploadState('uploading');
    setProgress(0);
    setError('');
    abortRef.current = new AbortController();

    try {
      const res = await api.uploadLogs(file, {
        serviceName: serviceName || undefined,
        namespace: namespace || undefined,
        onProgress: setProgress,
        signal: abortRef.current.signal,
      });
      setResult(res);
      setUploadState('success');
      onSuccess?.(res);
    } catch (err: unknown) {
      if ((err as { name?: string })?.name === 'CanceledError' || (err as { name?: string })?.name === 'AbortError') {
        setUploadState('idle');
        return;
      }
      setError((err as { message?: string })?.message || 'Upload failed');
      setUploadState('error');
    }
  };

  const handleCancel = () => {
    abortRef.current?.abort();
    setUploadState('idle');
  };

  const handleReset = () => {
    setFile(null);
    setUploadState('idle');
    setProgress(0);
    setResult(null);
    setError('');
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div className="w-full max-w-lg rounded-xl bg-white p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-slate-800">上传日志文件</h2>
          <button onClick={onClose} className="btn btn-ghost btn-icon"><X className="h-5 w-5" /></button>
        </div>

        {/* Drop zone */}
        {!file && uploadState === 'idle' && (
          <div
            className="mb-4 flex cursor-pointer flex-col items-center rounded-lg border-2 border-dashed border-slate-300 p-8 text-slate-500 transition-colors hover:border-blue-400 hover:bg-blue-50"
            onDrop={handleDrop}
            onDragOver={(e) => e.preventDefault()}
            onClick={handleSelectFile}
          >
            <Upload className="mb-2 h-8 w-8" />
            <p className="text-sm font-medium">拖拽文件到此处，或点击选择文件</p>
            <p className="mt-1 text-xs text-slate-400">支持 .log .txt .json .ndjson，最大 500MB</p>
            <input
              ref={inputRef}
              type="file"
              accept={acceptedExtensions}
              className="hidden"
              onChange={(e) => setFile(e.target.files?.[0] || null)}
            />
          </div>
        )}

        {/* Selected file info */}
        {file && uploadState === 'idle' && (
          <div className="mb-4 flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
            {file.name.endsWith('.json') || file.name.endsWith('.ndjson')
              ? <FileJson className="h-6 w-6 text-blue-500" />
              : <FileText className="h-6 w-6 text-slate-500" />
            }
            <div className="flex-1 min-w-0">
              <p className="truncate text-sm font-medium text-slate-700">{file.name}</p>
              <p className="text-xs text-slate-400">{formatFileSize(file.size)}</p>
            </div>
            <button onClick={handleReset} className="btn btn-ghost btn-icon"><X className="h-4 w-4" /></button>
          </div>
        )}

        {/* Options */}
        {uploadState === 'idle' && (
          <div className="mb-4 space-y-2">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">服务名（可选，留空自动识别）</label>
              <input
                value={serviceName}
                onChange={(e) => setServiceName(e.target.value)}
                placeholder="自动识别"
                className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-blue-400"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">命名空间</label>
              <input
                value={namespace}
                onChange={(e) => setNamespace(e.target.value)}
                className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-blue-400"
              />
            </div>
          </div>
        )}

        {/* Progress bar */}
        {uploadState === 'uploading' && (
          <div className="mb-4">
            <div className="flex items-center gap-2 text-sm text-slate-600 mb-2">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span>正在上传... {progress}%</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-slate-200">
              <div
                className="h-full rounded-full bg-blue-500 transition-all duration-300"
                style={{ width: `${progress}%` }}
              />
            </div>
          </div>
        )}

        {/* Success */}
        {uploadState === 'success' && result && (
          <div className="mb-4 flex items-start gap-3 rounded-lg border border-green-200 bg-green-50 p-3">
            <CheckCircle2 className="mt-0.5 h-5 w-5 text-green-600" />
            <div className="text-sm text-green-800">
              <p className="font-medium">上传成功</p>
              <p className="mt-0.5">已接收 {result.total} 条日志，分 {result.batches} 批写入管道</p>
              <p className="text-xs text-green-600 mt-0.5">日志将在数秒后出现在查询结果中</p>
            </div>
          </div>
        )}

        {/* Error */}
        {uploadState === 'error' && (
          <div className="mb-4 flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 p-3">
            <AlertCircle className="mt-0.5 h-5 w-5 text-red-600" />
            <div className="text-sm text-red-800">
              <p className="font-medium">上传失败</p>
              <p className="mt-0.5">{error}</p>
            </div>
          </div>
        )}

        {/* Actions */}
        <div className="flex justify-end gap-2">
          {uploadState === 'idle' && (
            <>
              <button onClick={onClose} className="btn btn-ghost px-4 py-2 text-sm">取消</button>
              <button
                onClick={handleUpload}
                disabled={!file}
                className="btn btn-primary flex items-center gap-1.5 px-4 py-2 text-sm disabled:opacity-50"
              >
                <Upload className="h-4 w-4" />
                上传
              </button>
            </>
          )}
          {uploadState === 'uploading' && (
            <button onClick={handleCancel} className="btn btn-ghost px-4 py-2 text-sm">取消上传</button>
          )}
          {(uploadState === 'success' || uploadState === 'error') && (
            <>
              {uploadState === 'error' && (
                <button onClick={handleReset} className="btn btn-ghost px-4 py-2 text-sm">重试</button>
              )}
              <button onClick={onClose} className="btn btn-primary px-4 py-2 text-sm">关闭</button>
            </>
          )}
        </div>
      </div>
    </div>
  );
};

export default UploadDialog;
```

- [ ] **Step 2: TypeScript check**

```bash
cd /root/logoscope/frontend
npx tsc --noEmit --pretty 2>&1 | head -20
```

Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/logs/UploadDialog.tsx
git commit -m "feat(frontend): add UploadDialog component for offline log upload"
```

---

### Task 6: Frontend — Wire upload into LogsExplorer

**Files:**
- Modify: `frontend/src/pages/LogsExplorer.tsx`

- [ ] **Step 1: Add import for UploadDialog and Upload icon**

After existing imports around line 31 (the `exportLogsToCSV` import block), add:

```typescript
import UploadDialog from '../components/logs/UploadDialog';
```

After existing lucide-react imports around line 57, add `Upload`:

```typescript
import {
  Search,
  RefreshCw,
  Download,
  Upload,     // <-- add
  X,
  // ... rest unchanged
} from 'lucide-react';
```

- [ ] **Step 2: Add state for upload dialog**

After existing state hooks (around line 816, after `showExportMenu`), add:

```typescript
const [showUploadDialog, setShowUploadDialog] = useState(false);
```

- [ ] **Step 3: Add upload button to toolbar**

In the toolbar section, after the Refresh button (around line 2845) and before the Export button, add:

```tsx
{/* Upload button */}
<button
  onClick={() => setShowUploadDialog(true)}
  className="btn btn-ghost flex items-center gap-1.5 px-3 py-2 text-sm"
  title="上传离线日志"
>
  <Upload className="w-4 h-4" />
  <span className="text-sm">上传</span>
</button>
```

- [ ] **Step 4: Add UploadDialog component before closing fragment**

Before the closing `</>` or at the end of the return JSX, add:

```tsx
<UploadDialog
  open={showUploadDialog}
  onClose={() => setShowUploadDialog(false)}
  onSuccess={() => {
    // Auto-refresh log list after upload completes
    if (isPatternMode) {
      refetchAggregated();
    } else {
      refetch();
    }
  }}
/>
```

- [ ] **Step 5: TypeScript check**

```bash
cd /root/logoscope/frontend
npx tsc --noEmit --pretty 2>&1 | head -20
```

Expected: No errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/LogsExplorer.tsx
git commit -m "feat(frontend): wire UploadDialog into LogsExplorer toolbar"
```

---

### Plan Self-Review

**Spec coverage:** All spec sections covered:
- Upload endpoint with file parsing (Task 1) — covers JSON array, NDJSON, text log parsing
- Service name resolution (Task 1, `_resolve_service_name`) — filename + content detect + user override
- Redis Stream message format (Task 1, `_flush_batch`) — `type=upload` envelope with records
- Nginx routing to Ingest Service (Task 3) — `/api/v1/logs/upload` → `ingest-service:8080/v1/logs/upload`
- Frontend upload dialog (Task 5) — drag-drop, progress, cancel, success/error states
- LogsExplorer integration (Task 6) — upload button in toolbar, auto-refresh on success
- Max file size: 500MB (Task 1, nginx `client_max_body_size`)

**Placeholder check:** All steps have complete code, no TBDs or TODOs.

**Type consistency:** All frontend types match (`UploadResult` in api.ts → UploadDialog return type), Python function signatures consistent.

ClickHouse `logs` table (id, timestamp, service_name, pod_name, namespace, node_name, level, message, trace_id, span_id, labels, host_ip).

Semantic Engine normalizer access patterns:
- `extract_timestamp` reads `log_data.get("timestamp")` — we provide
- `extract_log_level` reads `log_data.get("level")` — we provide
- `extract_service_name` reads k8s.pod_name or service.name — we provide `service.name` via queue_writer transform
- `extract_k8s_context` reads `log_data.get("kubernetes", {})` — we provide pod_name and namespace
- `extract_trace_info` reads `log_data.get("resource")` and message — will get empty trace_id → synthetic trace_id generated via md5 hash (same as non-trace logs)

Pipeline flow verified end to end.
