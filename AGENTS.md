# AGENTS.md

Agent guidelines for working in the Logoscope codebase - a Kubernetes-based observability platform.

## Project Overview

Logoscope is a microservices-based observability platform with the following architecture:

```
Fluent Bit → OTel Collector → Semantic Engine → ClickHouse/Neo4j
```

### Services

| Service | Path | Description |
|---------|------|-------------|
| Frontend | `frontend/` | React + TypeScript + Vite dashboard |
| Semantic Engine | `semantic-engine/` | Core intelligence (normalization, classification, correlation, topology) |
| Ingest Service | `ingest-service/` | OTLP data ingestion |
| Query Service | `query-service/` | Query API |
| Topology Service | `topology-service/` | Topology management |

---

## Build, Lint, and Test Commands

### Frontend (`frontend/`)

```bash
# Development server
npm run dev

# Build for production
npm run build

# Lint (ESLint)
npm run lint

# Type check
npm run typecheck
```

### Python Services (semantic-engine, ingest-service, etc.)

```bash
# Run all tests in semantic-engine
cd semantic-engine && pytest

# Run tests with coverage
cd semantic-engine && pytest -v --cov=normalize --cov=storage --cov=api --cov-report=term-missing

# Run a single test file
cd semantic-engine && pytest tests/test_normalizer.py

# Run a single test case
cd semantic-engine && pytest tests/test_normalizer.py::TestExtractServiceName::test_extract_from_kubernetes_pod_name -v

# Run tests by marker
cd semantic-engine && pytest -m unit          # Unit tests only
cd semantic-engine && pytest -m integration   # Integration tests only
cd semantic-engine && pytest -m "not slow"    # Skip slow tests

# Run specific service
cd semantic-engine && python main.py
cd ingest-service && python main.py
```

### Kubernetes Deployment

```bash
# Deploy all services
./deploy.sh all

# Initialize database
./deploy.sh init-db

# Verify deployment
./deploy.sh status
./deploy.sh health

# View logs
./deploy.sh logs semantic-engine
```

---

## Code Style Guidelines

### Python

#### Imports

Organize imports in three groups, separated by blank lines:
1. Standard library
2. Third-party packages
3. Local modules

```python
"""
Module docstring describing purpose
"""
import os
import sys
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from config import config
from storage.adapter import StorageAdapter
```

#### Type Hints

Always use type hints for function parameters and return types:

```python
def normalize_log(log_data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize raw log data into standardized event format."""
    ...

def get_clickhouse_config(self) -> Dict[str, Any]:
    """Return ClickHouse connection configuration."""
    return {
        "host": self.clickhouse_host,
        "port": self.clickhouse_port,
    }
```

#### Pydantic Models

Use Pydantic for data validation and serialization:

```python
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional

class Entity(BaseModel):
    """Entity model representing a service or component."""
    type: str = Field(default="service")
    name: str = Field(default="unknown")
    instance: str = Field(default="unknown")

class EventModel(BaseModel):
    """Event model for normalized log entries."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    entity: Entity = Field(default_factory=Entity)
    relations: List[Relation] = Field(default_factory=list)
```

#### Classes

Use docstrings for classes and methods:

```python
class Config:
    """
    Configuration class.
    
    Loads settings from environment variables with defaults.
    """
    
    def __init__(self):
        """Initialize configuration from environment."""
        self.app_name = os.getenv("APP_NAME", "semantic-engine")
        self.port = int(os.getenv("PORT", "8080"))
```

#### Error Handling

Use HTTPException for API errors, try/except for internal operations:

```python
from fastapi import HTTPException

@app.get("/api/v1/items/{item_id}")
async def get_item(item_id: str):
    try:
        item = storage.get_item(item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        return item
    except Exception as e:
        log_format("ERROR", "api", f"Failed to get item: {e}")
        raise HTTPException(status_code=500, detail=str(e))
```

#### Testing

Use pytest with class-based test organization:

```python
"""
Tests for normalize/normalizer.py module
"""
import pytest
from normalize.normalizer import normalize_log, extract_service_name


class TestExtractServiceName:
    """Test service name extraction."""

    def test_extract_from_kubernetes_pod_name(self):
        """Extract from kubernetes.pod_name."""
        log_data = {"kubernetes": {"pod_name": "test-service-abc123"}}
        result = extract_service_name(log_data)
        assert result == "test-service-abc123"

    def test_extract_fallback_to_unknown(self):
        """Return 'unknown' when extraction fails."""
        log_data = {}
        result = extract_service_name(log_data)
        assert result == "unknown"
```

Use fixtures in `conftest.py` for shared test data.

---

### TypeScript / React

#### Imports

Group imports: React → External libraries → Internal modules → Types

```typescript
/**
 * Component description
 */
import React from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Activity, AlertTriangle } from 'lucide-react';

import { useEvents, useMetrics } from '../hooks/useApi';
import { MetricCard } from '../components/MetricCard';
import LoadingState from '../components/common/LoadingState';
import { formatTime } from '../utils/formatters';
```

#### Components

Use functional components with TypeScript:

```typescript
interface DashboardProps {
  title?: string;
}

const Dashboard: React.FC<DashboardProps> = ({ title = 'Dashboard' }) => {
  const navigate = useNavigate();
  const { data, loading, error, refetch } = useEvents({ limit: 50 });

  if (loading) {
    return <LoadingState message="Loading..." />;
  }

  if (error) {
    return <ErrorState message={error.message} onRetry={refetch} />;
  }

  return (
    <div className="space-y-6">
      {/* Content */}
    </div>
  );
};

export default Dashboard;
```

#### Naming Conventions

- Components: PascalCase (`Dashboard.tsx`, `MetricCard.tsx`)
- Hooks: camelCase with `use` prefix (`useApi.ts`, `useEvents`)
- Utilities: camelCase (`formatters.ts`, `api.ts`)
- Constants: UPPER_SNAKE_CASE inside files

#### Styling

Use TailwindCSS utility classes:

```tsx
<div className="bg-white rounded-lg shadow-md p-4">
  <h2 className="text-lg font-semibold text-gray-900">Title</h2>
  <p className="text-sm text-gray-500 mt-1">Description</p>
</div>
```

---

## Architecture Guidelines

### Component Responsibilities

| Component | Responsibility |
|-----------|---------------|
| Fluent Bit | Log collection, transport, lightweight field enrichment |
| OTel Collector | Protocol unification, data routing, basic processing |
| Semantic Engine | Normalization, classification, correlation, relation extraction, topology building |
| ClickHouse | Time-series storage (logs, events, traces, metrics) |
| Neo4j | Graph storage (service graph, dependencies, topology) |

### API Versioning

All API endpoints use `/api/v1/` prefix:

```python
@app.get("/api/v1/events")
@app.post("/api/v1/alerts/rules")
```

### Configuration

Use environment variables with `config.py` classes:

```python
# In config.py
class Config:
    def __init__(self):
        self.clickhouse_host = os.getenv("CLICKHOUSE_HOST", "localhost")
        self.neo4j_port = int(os.getenv("NEO4J_PORT", "7687"))

# Global instance
config = Config()
```

---

## Important Files

| File | Purpose |
|------|---------|
| `semantic-engine/pytest.ini` | Pytest configuration |
| `semantic-engine/tests/conftest.py` | Shared fixtures |
| `frontend/tsconfig.json` | TypeScript configuration |
| `frontend/package.json` | npm scripts and dependencies |
| `.trae/rules/project_rules.md` | Project-specific rules |

---

## Container Registry

Local container registry: `localhost:5000/logoscope/`

Images:
- `semantic-engine`
- `otel-agent:1.0.0`
- `otel-gateway:1.0.0`
- `fluent-bit:1.0.0`
- `neo4j:5.12-community2-community`

---

## Notes

- The codebase uses Chinese comments in some files - maintain consistency with existing style
- Always run `npm run lint` and `npm run typecheck` before committing frontend changes
- Run `pytest` with coverage before committing Python changes
- Health check endpoints (`/health`) must not create OpenTelemetry spans to avoid blocking
