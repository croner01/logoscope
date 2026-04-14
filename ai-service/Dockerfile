FROM python:3.11-slim AS builder

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH

RUN python -m venv /opt/venv

COPY ai-service/requirements-runtime.txt /app/ai-service/requirements-runtime.txt
RUN pip install  --prefer-binary -r /app/ai-service/requirements-runtime.txt

FROM python:3.11-slim

WORKDIR /app

ARG KUBECTL_VERSION=v1.31.13

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/shared_lib \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

COPY shared_src/ /app/shared_lib/
RUN ln -s /app/shared_lib /app/shared_lib/shared_src
COPY ai-service/ .
COPY docs/superpowers/knowledge/ /app/docs/superpowers/knowledge/

EXPOSE 8090

CMD ["python", "start.py"]
