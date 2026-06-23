"""One-time cleanup for stuck AgentRuns (running / waiting_user_input).

背景：在修复 bridge 未调用 finish_run() 的问题之前，
部分 run 在 backend.run() 同步执行完毕后没有被 finalize，
永久 stuck 在 running 或 waiting_user_input 状态。

此脚本：
1. 扫描所有 session 下非 terminal 状态的 run
2. 对 stuck run 调用 fail_run() 标记为 failed
3. 同步更新 AISession 状态为 completed（会话已完成分析，只是 run 没正常结束）

使用方法：
    cd ai-service && python scripts/cleanup_stuck_runs.py
"""

import logging
import os
import sys

# 确保 shared_src 可导入
_SHARED_LIB_CANDIDATES = (
    os.getenv("LOGOSCOPE_SHARED_LIB", ""),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "shared_src")),
    "/app/shared_lib",
)
for _candidate in _SHARED_LIB_CANDIDATES:
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.append(_candidate)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("cleanup_stuck_runs")


def _as_str(value, default=""):
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def main():
    from config import config
    from storage.adapter import StorageAdapter

    storage = StorageAdapter(config.get_storage_config())

    from ai.agent_runtime.store import AgentRuntimeStore
    from ai.agent_runtime.service import get_agent_runtime_service

    store = AgentRuntimeStore(storage)
    svc = get_agent_runtime_service(storage)

    # 从 ClickHouse 扫描所有非 terminal 的 run
    source_table, need_final = store._get_run_read_source()
    final_clause = " FINAL" if need_final else ""

    sql = f"""
    SELECT run_id, session_id, status
    FROM {source_table}{final_clause}
    WHERE status NOT IN ('completed', 'failed', 'cancelled', 'blocked')
    ORDER BY updated_at DESC
    """
    rows = store.storage.ch_client.execute(sql)

    fixed_count = 0
    skipped_count = 0

    for row in rows:
        run_id = _as_str(row[0])
        session_id = _as_str(row[1])
        status = _as_str(row[2]).strip().lower()

        # 再次确认状态，避免并发竞争
        run = store.get_run(run_id)
        if run is None:
            logger.info("SKIP  %s  run not found in memory", run_id)
            skipped_count += 1
            continue
        if run.status in {"completed", "failed", "cancelled", "blocked"}:
            logger.info("SKIP  %s  already terminal (status=%s)", run_id, run.status)
            skipped_count += 1
            continue

        logger.info("FIX   %s  session=%s  status=%s -> failed", run_id, session_id, run.status)
        svc.fail_run(
            run_id,
            error_code="stale_run_cleanup",
            error_detail=f"Run stuck in {run.status} — cleaned up by post-fix script",
            summary_updates={"stale_run_cleanup": True},
        )
        fixed_count += 1

        # 同步更新 AISession 状态
        if session_id:
            try:
                from ai.session_history import get_ai_session_store

                ais_store = get_ai_session_store(storage)
                session = ais_store.get_session(session_id)
                if session is not None:
                    cur = _as_str(session.status).strip().lower()
                    if cur not in {"completed", "failed", "cancelled", "deleted"}:
                        ais_store.update_session(session_id, status="completed")
                        logger.info("     AISession %s status set to completed", session_id)
            except Exception as exc:
                logger.warning("     Failed to update AISession %s: %s", session_id, exc)

    logger.info("Done: fixed=%d, skipped=%d", fixed_count, skipped_count)


if __name__ == "__main__":
    main()
