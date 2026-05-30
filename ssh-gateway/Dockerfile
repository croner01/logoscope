FROM python:3.11-slim

WORKDIR /app

# Install SSH client (required for SSH Gateway)
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY app.py .
COPY core/ ./core/
COPY api/ ./api/

EXPOSE 8096

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8096"]
