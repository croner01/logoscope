FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/shared_lib

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY semantic-engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ⭐ 复制共享代码到独立目录（避免覆盖服务的本地代码）
COPY shared_src/ /app/shared_lib/
RUN ln -s /app/shared_lib /app/shared_lib/shared_src

# 复制服务特有代码
COPY semantic-engine/ .

# 暴露端口
EXPOSE 8080

# 批量处理参数（可通过环境变量覆盖）
ENV REDIS_BATCH_SIZE=100
ENV REDIS_BLOCK_MS=100
ENV CH_BATCH_SIZE=2000
ENV CH_FLUSH_INTERVAL=0.5

# 启动应用
CMD ["python", "start.py"]
