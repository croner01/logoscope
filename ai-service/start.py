"""AI Service 启动脚本。"""
import uvicorn

from config import config
from main import app


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=config.log_level,
        access_log=False,
        log_config=None,
    )
