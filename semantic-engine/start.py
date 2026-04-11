"""
Semantic Engine 启动脚本

根据命令行参数或环境变量选择启动模式：
- API模式：启动 FastAPI 服务
- Worker模式：启动队列处理 Worker
"""
import logging
import sys
import os

_SEMANTIC_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SEMANTIC_ENGINE_DIR, ".."))
_SHARED_SRC_DIR = os.path.join(_PROJECT_ROOT, "shared_src")
for _path in (_PROJECT_ROOT, _SHARED_SRC_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.append(_path)

try:
    from shared_src.utils.logging_config import setup_logging, get_logger
except ImportError:
    from utils.logging_config import setup_logging, get_logger


setup_logging(
    service_name=os.getenv("APP_NAME", "semantic-engine"),
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    log_format=os.getenv("LOG_FORMAT", "text"),
)
logger = get_logger("start")


def main():
    """主函数"""
    # 获取启动模式
    mode = sys.argv[1] if len(sys.argv) > 1 else os.getenv("START_MODE", "api")

    if mode == "worker":
        logger.info("Starting Semantic Engine in Worker mode")
        # 启动 Worker
        from msgqueue.worker import main as worker_main
        import asyncio
        try:
            asyncio.run(worker_main())
        except KeyboardInterrupt:
            logger.info("Worker interrupted")
            sys.exit(0)
    elif mode == "api":
        logger.info("Starting Semantic Engine in API mode")
        # 启动 API 服务
        import uvicorn
        from config import config
        from main import app

        uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_level=config.log_level,
            access_log=False,
            log_config=None,
        )
    else:
        logger.error("Unknown mode: %s", mode)
        logger.info("Valid modes: api, worker")
        sys.exit(1)


if __name__ == "__main__":
    main()
