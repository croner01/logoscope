FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/shared_lib

# 安装依赖
COPY topology-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ⭐ 复制共享代码到独立目录
COPY shared_src/ /app/shared_lib/
RUN ln -s /app/shared_lib /app/shared_lib/shared_src

# 复制服务特有代码
COPY topology-service/ .

# 暴露端口
EXPOSE 8003

# 批量处理参数
ENV CH_BATCH_SIZE=2000
ENV CH_FLUSH_INTERVAL=0.5

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8003/health')" || exit 1

# 启动应用
CMD ["python", "main.py"]
