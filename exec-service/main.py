"""
Exec service main entry.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI

from api.execute import router as exec_router


logger = logging.getLogger(__name__)
logging.basicConfig(
    level=getattr(logging, str(os.getenv("LOG_LEVEL", "INFO")).upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


app = FastAPI(
    title="exec-service",
    description="Logoscope command execution service",
    version=os.getenv("APP_VERSION", "1.0.0"),
)
app.include_router(exec_router)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "healthy",
        "service": "exec-service",
        "write_enabled": os.getenv("EXEC_WRITE_ENABLED", "true"),
        "controlled_gateway_required": os.getenv("EXEC_CONTROLLED_GATEWAY_REQUIRED", "true"),
        "timestamp": utc_now_iso(),
    }
