# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Logoscope is a Kubernetes-based observability platform. Data flows as:

```
Fluent Bit → OTel Collector → Ingest Service → Semantic Engine → ClickHouse / Neo4j
                                                              ↘ AI Service
Frontend ← Query Service / Topology Service ← ClickHouse / Neo4j
```

**Namespace:** `islap` | **Container registry:** `localhost:5000/logoscope/`

## Services

| Service | Path | Language | Description |
|---------|------|----------|-------------|
| Semantic Engine | `semantic-engine/` | Python | Core intelligence: normalization, classification, correlation, relation extraction, topology building, alerting, label discovery |
| AI Service | `ai-service/` | Python | LLM analysis, conversation history, follow-up |
| Ingest Service | `ingest-service/` | Python | OTLP data reception → Redis queue |
| Query Service | `query-service/` | Python | Logs/events/traces query API |
| Topology Service | `topology-service/` | Python | Topology query, snapshots, WebSocket streaming |
| Exec Service | `exec-service/` | Python | Command execution proxy (kubectl, ClickHouse client) |
| Toolbox Gateway | `toolbox-gateway/` | Python | Controlled shell command gateway with allowlist |
| Frontend | `frontend/` | React + TypeScript + Vite | Dashboard UI |
| Shared Library | `shared_src/` | Python | Cross-service utilities (config base, FastAPI kernel, logging, storage) |

## Commands

### Python Services (semantic-engine, ai-service, ingest-service, query-service, topology-service)

```bash
# Install deps (use the service's own requirements files)
cd semantic-engine
pip install -r requirements-runtime.txt -r requirements-test.txt

# Run service locally
python main.py

# Run all tests (with coverage — configured in pytest.ini)
cd semantic-engine && pytest

# Run single test file
cd semantic-engine && pytest tests/test_normalizer.py

# Run single test case
cd semantic-engine && pytest tests/test_normalizer.py::TestExtractServiceName::test_extract_from_kubernetes_pod_name -v

# Run by marker
cd semantic-engine && pytest -m unit
cd semantic-engine && pytest -m integration
cd semantic-engine && pytest -m "not slow"
```

`pytest.ini` in `semantic-engine/` auto-adds coverage flags covering `normalize`, `storage`, and `api` modules.

### Frontend

```bash
cd frontend
npm install
npm run dev          # Dev server
npm run build        # Production build (tsc + vite build)
npm run lint         # ESLint
npm run lint:strict  # ESLint with zero warnings
npm run typecheck    # tsc --noEmit
```

### Kubernetes Image Operations

```bash
# Build and push all services
scripts/k8s-image-ops.sh build-push all latest

# Build and push single service
scripts/k8s-image-ops.sh build-push semantic-engine latest

# Full release (build → push → set-image → rollout → gate checks)
scripts/k8s-image-ops.sh release all latest

# Update running deployment image
scripts/k8s-image-ops.sh set-image semantic-engine latest
scripts/k8s-image-ops.sh rollout-status all

# Apply manifests without rebuilding
scripts/k8s-image-ops.sh apply semantic-engine
```

Available services: `semantic-engine ai-service exec-service toolbox-gateway ingest-service query-service topology-service frontend`

Updating `semantic-engine` also updates the `semantic-engine-worker` deployment automatically.

### Deployment Management

```bash
./deploy.sh all           # Deploy everything
./deploy.sh init-db       # Initialize databases
./deploy.sh status        # Pod status
./deploy.sh health        # Health checks
./deploy.sh logs <svc>    # Tail logs
./deploy.sh restart <svc> # Rolling restart
```

## Architecture

### Component Responsibilities (strict boundaries)

- **Fluent Bit**: Collect + transport + lightweight field enrichment only. Not a processing layer.
- **OTel Collector**: Protocol unification, routing, batching. No business semantics, no relation inference, no topology.
- **Semantic Engine**: The only intelligent core. Contains: Ingress → Normalize → Classifier → Correlator → Relation Extractor → Graph Builder → Storage Adapter.
- **AI Service**: LLM calls, session/conversation management, case library.
- **Ingest Service**: Receives OTLP, writes to Redis queue; no semantic processing.
- **ClickHouse**: Time-series storage (logs, events, traces, metrics).
- **Neo4j**: Graph storage (service dependencies, topology).
- **Redis**: Cache and message queue between Ingest and Semantic Engine.

### shared_src

Shared Python library used by all Python services. Located at `shared_src/` and mounted at `/app/shared_lib` in containers. Key modules:

- `platform_kernel/fastapi_kernel.py` — standard FastAPI middleware, CORS, error handling, request-id propagation
- `platform_kernel/config_base.py` — base `Config` class
- `utils/logging_config.py` — structured logging with request context

Each service resolves `shared_src` at startup via `LOGOSCOPE_SHARED_LIB` env var or a relative path fallback.

### Configuration Pattern

Each service has a `config.py` with a `Config` class loaded from environment variables. Example:

```python
class Config:
    def __init__(self):
        self.clickhouse_host = os.getenv("CLICKHOUSE_HOST", "localhost")

config = Config()  # module-level singleton
```

Kubernetes ConfigMaps hold service-specific config to avoid unnecessary image rebuilds.

### API Conventions

- All endpoints use `/api/v1/` prefix
- Health checks at `/health` — must not create OpenTelemetry spans
- Standard error response shape via `fastapi_kernel.error_payload()`

### Frontend Structure

```
frontend/src/
  features/ai-runtime/   # AI agent runtime (streaming, transcript, projections)
  components/            # Shared UI components
  pages/                 # Route-level page components
  hooks/                 # Data-fetching hooks (useApi pattern)
  utils/                 # Formatters, API client, AI runtime utilities
  types/                 # TypeScript interfaces
```

Uses React 18 + React Router 6 + TailwindCSS + lucide-react icons.

## Code Style

### Python

- Three-group imports: stdlib → third-party → local
- Type hints on all function parameters and return types
- Pydantic models for data validation/serialization
- Docstrings on classes and public methods
- `HTTPException` for API errors; `try/except` with logging for internal operations
- Test classes: `class TestFoo:` with `def test_<description>` methods; shared fixtures in `conftest.py`

### TypeScript / React

- Import order: React → external libs → internal modules → types
- Functional components with explicit `React.FC<Props>` typing
- Naming: PascalCase components, `use*` hooks, camelCase utilities, `UPPER_SNAKE_CASE` constants
- TailwindCSS utility classes for styling

## Notes

- Some files contain Chinese comments — maintain consistency with the surrounding file's style.
- Run `npm run lint` and `npm run typecheck` before committing frontend changes.
- Run `pytest` with coverage before committing Python changes.
- `/health` endpoints must not create OpenTelemetry spans (to avoid blocking).
- Trace from first principles — don't patch symptoms. Every decision should answer "why".
- When the goal is clear but the path isn't shortest, say so and suggest a better approach.
