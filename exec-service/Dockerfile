FROM python:3.11-slim AS builder

WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY exec-service/requirements-runtime.txt /app/exec-service/requirements-runtime.txt
RUN pip install --no-cache-dir --prefer-binary -r /app/exec-service/requirements-runtime.txt


FROM python:3.11-slim

WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ARG KUBECTL_VERSION=v1.30.6

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY exec-service/ /app/

EXPOSE 8095

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8095"]
