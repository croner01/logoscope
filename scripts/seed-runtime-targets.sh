#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/runtime-target-seed}"
HTTP_TARGETS="${HTTP_TARGETS:-}"
UPDATED_BY="${UPDATED_BY:-runtime-bootstrap}"
CLUSTER_ID="${CLUSTER_ID:-cluster-local}"
RISK_TIER="${RISK_TIER:-high}"
K8S_PROFILES="${K8S_PROFILES:-toolbox-k8s-readonly,toolbox-k8s-mutating}"
CLICKHOUSE_PROFILES="${CLICKHOUSE_PROFILES:-toolbox-k8s-readonly,toolbox-k8s-mutating}"
OPENSTACK_PROFILES="${OPENSTACK_PROFILES:-toolbox-openstack-readonly,toolbox-openstack-mutating}"
HOST_PROFILES="${HOST_PROFILES:-toolbox-node-readonly,toolbox-node-mutating}"
HTTP_PROFILES="${HTTP_PROFILES:-toolbox-http-readonly,toolbox-http-mutating}"
CLICKHOUSE_DATABASES="${CLICKHOUSE_DATABASES:-logs,default}"

mkdir -p "$ARTIFACT_DIR"
RUN_ID="runtime-target-seed-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

usage() {
  cat <<'EOF'
Seed runtime v4 target registry defaults.

Env vars:
  NAMESPACE    Kubernetes namespace (default: islap)
  ARTIFACT_DIR Report output dir (default: /root/logoscope/reports/runtime-target-seed)
  HTTP_TARGETS Comma-separated explicit HTTP URLs to register, e.g.
               "https://api.internal.local/health,https://example.com/status"
  UPDATED_BY   Audit updated_by field (default: runtime-bootstrap)
  CLUSTER_ID   Target metadata.cluster_id (default: cluster-local)
  RISK_TIER    Target metadata.risk_tier (default: high)
  K8S_PROFILES Preferred executor profiles for k8s target
  CLICKHOUSE_PROFILES Preferred executor profiles for clickhouse target
  OPENSTACK_PROFILES Preferred executor profiles for openstack target
  HOST_PROFILES Preferred executor profiles for host target
  HTTP_PROFILES Preferred executor profiles for http target
  CLICKHOUSE_DATABASES ClickHouse target db list, comma-separated (default: logs,default)

Examples:
  scripts/seed-runtime-targets.sh
  HTTP_TARGETS="https://example.com/health" scripts/seed-runtime-targets.sh
EOF
}

fail() {
  printf '[ERROR] %s\n' "$1" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_cmd kubectl
require_cmd python3

AI_POD="$(kubectl -n "$NAMESPACE" get pod -l app=ai-service -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$AI_POD" ]]; then
  fail "ai-service pod not found in namespace $NAMESPACE"
fi

PAYLOAD_JSON="$(
kubectl -n "$NAMESPACE" exec -i "$AI_POD" -c ai-service -- env \
  NAMESPACE="$NAMESPACE" \
  HTTP_TARGETS="$HTTP_TARGETS" \
  UPDATED_BY="$UPDATED_BY" \
  CLUSTER_ID="$CLUSTER_ID" \
  RISK_TIER="$RISK_TIER" \
  K8S_PROFILES="$K8S_PROFILES" \
  CLICKHOUSE_PROFILES="$CLICKHOUSE_PROFILES" \
  OPENSTACK_PROFILES="$OPENSTACK_PROFILES" \
  HOST_PROFILES="$HOST_PROFILES" \
  HTTP_PROFILES="$HTTP_PROFILES" \
  CLICKHOUSE_DATABASES="$CLICKHOUSE_DATABASES" \
  RUN_ID="$RUN_ID" \
  python - <<'PY'
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def post_json(url: str, payload: dict, timeout: int = 20):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            return int(resp.status), data
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            data = {"raw": raw}
        return int(exc.code), data
    except Exception as exc:
        return 599, {"error": str(exc)}


namespace = (os.getenv("NAMESPACE") or "islap").strip() or "islap"
http_targets_raw = os.getenv("HTTP_TARGETS") or ""
updated_by = (os.getenv("UPDATED_BY") or "runtime-bootstrap").strip() or "runtime-bootstrap"
run_id = (os.getenv("RUN_ID") or "").strip()
cluster_id = (os.getenv("CLUSTER_ID") or "cluster-local").strip() or "cluster-local"
risk_tier = (os.getenv("RISK_TIER") or "high").strip().lower() or "high"
if risk_tier not in {"low", "medium", "high", "critical"}:
    risk_tier = "high"
base = "http://127.0.0.1:8090/api/v2/targets"


def parse_profiles(raw: str, fallback: list[str]) -> list[str]:
    seen = set()
    profiles = []
    for token in [item.strip().lower() for item in (raw or "").split(",")]:
        if not token or token in seen:
            continue
        seen.add(token)
        profiles.append(token)
    return profiles or list(fallback)


def parse_databases(raw: str) -> list[str]:
    seen = set()
    dbs = []
    for token in [item.strip() for item in (raw or "").split(",")]:
        if not token or token in seen:
            continue
        seen.add(token)
        dbs.append(token)
    return dbs or ["logs", "default"]


def stable_target_id(target_kind: str, target_identity: str) -> str:
    raw = f"{(target_kind or '').strip().lower()}-{(target_identity or '').strip().lower()}"
    normalized = "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-")
    compact = "-".join([part for part in normalized.split("-") if part])
    compact = compact[:64] if compact else "runtime-target"
    return f"auto-{compact}"


k8s_profiles = parse_profiles(
    os.getenv("K8S_PROFILES", ""),
    ["toolbox-k8s-readonly", "toolbox-k8s-mutating"],
)
clickhouse_profiles = parse_profiles(
    os.getenv("CLICKHOUSE_PROFILES", ""),
    ["toolbox-k8s-readonly", "toolbox-k8s-mutating"],
)
openstack_profiles = parse_profiles(
    os.getenv("OPENSTACK_PROFILES", ""),
    ["toolbox-openstack-readonly", "toolbox-openstack-mutating"],
)
host_profiles = parse_profiles(
    os.getenv("HOST_PROFILES", ""),
    ["toolbox-node-readonly", "toolbox-node-mutating"],
)
http_profiles = parse_profiles(
    os.getenv("HTTP_PROFILES", ""),
    ["toolbox-http-readonly", "toolbox-http-mutating"],
)
clickhouse_databases = parse_databases(os.getenv("CLICKHOUSE_DATABASES", ""))

targets = [
    {
        "target_id": stable_target_id("k8s_cluster", f"namespace:{namespace}"),
        "target_kind": "k8s_cluster",
        "target_identity": f"namespace:{namespace}",
        "display_name": f"{namespace} namespace",
        "description": "kubernetes workload/log diagnosis target",
        "capabilities": ["read_logs", "restart_workload", "helm_read", "helm_mutation"],
        "metadata": {
            "cluster_id": cluster_id,
            "namespace": namespace,
            "risk_tier": risk_tier,
            "preferred_executor_profiles": k8s_profiles,
        },
        "credential_scope": {"namespace": namespace},
    },
    {
        "target_id": "tgt-openstack-default",
        "target_kind": "openstack_project",
        "target_identity": "project:default",
        "display_name": "openstack default project",
        "description": "openstack control plane target",
        "capabilities": ["read_cloud", "openstack_mutation"],
        "metadata": {
            "cluster_id": cluster_id,
            "risk_tier": risk_tier,
            "preferred_executor_profiles": openstack_profiles,
        },
    },
    {
        "target_id": "tgt-host-primary",
        "target_kind": "host_node",
        "target_identity": "host:primary",
        "display_name": "primary host",
        "description": "node/system diagnosis target",
        "capabilities": ["host_control_read", "host_control_mutation", "read_host_state", "host_mutation"],
        "metadata": {
            "cluster_id": cluster_id,
            "node_name": "primary",
            "risk_tier": risk_tier,
            "preferred_executor_profiles": host_profiles,
        },
    },
]

for db_name in clickhouse_databases:
    targets.append(
        {
            "target_id": stable_target_id("clickhouse_cluster", f"database:{db_name}"),
            "target_kind": "clickhouse_cluster",
            "target_identity": f"database:{db_name}",
            "display_name": f"clickhouse {db_name}",
            "description": "clickhouse query/mutation target",
            "capabilities": ["run_query", "clickhouse_mutation"],
            "metadata": {
                "cluster_id": cluster_id,
                "risk_tier": risk_tier,
                "preferred_executor_profiles": clickhouse_profiles,
            },
            "credential_scope": {"database": db_name},
        }
    )

http_targets = []
for token in [item.strip() for item in http_targets_raw.split(",")]:
    if not token:
        continue
    if not token.startswith("http://") and not token.startswith("https://"):
        continue
    http_targets.append(token)

for index, endpoint in enumerate(http_targets, start=1):
    targets.append(
        {
            "target_id": f"tgt-http-{index:02d}",
            "target_kind": "http_endpoint",
            "target_identity": endpoint,
            "display_name": endpoint,
            "description": "explicit http endpoint target",
            "capabilities": ["http_read", "http_mutation"],
            "metadata": {
                "risk_tier": risk_tier,
                "preferred_executor_profiles": http_profiles,
            },
        }
    )

results = []
for item in targets:
    payload = {
        **item,
        "updated_by": updated_by,
        "reason": "seed runtime target defaults",
        "run_id": run_id,
    }
    status, body = post_json(base, payload)
    target = body.get("target") if isinstance(body, dict) else {}
    change = body.get("change") if isinstance(body, dict) else {}
    results.append(
        {
            "request": payload,
            "status_code": status,
            "ok": status == 200 and isinstance(target, dict) and bool(target.get("target_id")),
            "target_id": target.get("target_id") if isinstance(target, dict) else "",
            "target_identity": target.get("target_identity") if isinstance(target, dict) else "",
            "version": target.get("version") if isinstance(target, dict) else 0,
            "change_id": change.get("change_id") if isinstance(change, dict) else "",
            "error": body.get("detail") if isinstance(body, dict) else "",
        }
    )

ok_count = len([item for item in results if item.get("ok")])
print(
    json.dumps(
        {
            "ok": ok_count == len(results),
            "generated_at": now_iso(),
            "namespace": namespace,
            "total": len(results),
            "ok_count": ok_count,
            "results": results,
        },
        ensure_ascii=False,
    )
)
PY
)"

printf '%s\n' "$PAYLOAD_JSON" > "$REPORT_FILE"

python3 - "$REPORT_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

if not data.get("ok"):
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(1)

print(
    json.dumps(
        {
            "ok": True,
            "report_file": path,
            "namespace": data.get("namespace"),
            "total": data.get("total"),
            "ok_count": data.get("ok_count"),
        },
        ensure_ascii=False,
    )
)
PY
