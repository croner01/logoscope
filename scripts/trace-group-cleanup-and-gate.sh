#!/usr/bin/env bash
set -euo pipefail

# traces.raw 遗留 group 清理 + release gate 一键脚本
#
# 设计目标：
# 1) 默认 dry-run，避免误删
# 2) 仅清理指定遗留 group（默认 trace-processors）
# 3) 清理前后打印 Redis 指标
# 4) 可串行执行 release-gate 进行发布收口验证

NAMESPACE="${NAMESPACE:-islap}"
STREAM="${STREAM:-traces.raw}"
STALE_GROUP="${STALE_GROUP:-trace-processors}"
ACTIVE_GROUP="${ACTIVE_GROUP:-log-workers}"
REDIS_POD="${REDIS_POD:-}"

EXECUTE=false
RUN_GATE=true
ALLOW_PENDING=false

GATE_CANDIDATE="${GATE_CANDIDATE:-trace-group-cleanup}"
GATE_TAG="${GATE_TAG:-manual}"
GATE_TARGET="${GATE_TARGET:-all}"
MAX_PENDING="${MAX_PENDING:-0}"
declare -a GATE_EXTRA_ARGS=()

usage() {
  cat <<'EOF'
Usage:
  scripts/trace-group-cleanup-and-gate.sh [options]

Options:
  --namespace <ns>             K8s namespace (default: islap)
  --stream <name>              Redis stream (default: traces.raw)
  --stale-group <name>         待清理 group (default: trace-processors)
  --active-group <name>        活跃 group (default: log-workers)
  --redis-pod <name>           指定 Redis pod（不传则按 app=redis 自动发现）
  --execute                    真正执行 XGROUP DESTROY（默认 dry-run）
  --allow-pending              stale-group pending>0 时仍允许删除（默认不允许）
  --skip-gate                  只做清理，不跑 release-gate
  --gate-candidate <name>      透传 release-gate --candidate
  --gate-tag <tag>             透传 release-gate --tag
  --gate-target <target>       透传 release-gate --target
  --max-pending <n>            透传 release-gate --max-pending（默认 0）
  --gate-arg <arg>             额外透传给 release-gate（可重复）
  -h, --help                   显示帮助

Examples:
  # 先演练（不执行删除）
  scripts/trace-group-cleanup-and-gate.sh

  # 一键执行清理 + 全量 gate
  scripts/trace-group-cleanup-and-gate.sh \
    --execute \
    --gate-candidate trace-group-cleanup-$(date +%Y%m%d-%H%M%S) \
    --gate-tag stream-singlepath-20260302-091813 \
    --gate-target all
EOF
}

log() {
  printf '[INFO] %s\n' "$1"
}

warn() {
  printf '[WARN] %s\n' "$1"
}

fail() {
  printf '[ERROR] %s\n' "$1" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

redis_raw() {
  kubectl -n "$NAMESPACE" exec "$REDIS_POD" -- redis-cli --raw "$@"
}

print_snapshot() {
  local title="$1"
  echo "=== ${title}: XINFO GROUPS ${STREAM} ==="
  redis_raw XINFO GROUPS "$STREAM" || true
  echo
  echo "=== ${title}: XPENDING ${STREAM}/${ACTIVE_GROUP} ==="
  redis_raw XPENDING "$STREAM" "$ACTIVE_GROUP" || true
  echo
  echo "=== ${title}: XPENDING ${STREAM}/${STALE_GROUP} ==="
  redis_raw XPENDING "$STREAM" "$STALE_GROUP" || true
  echo
}

extract_group_metric() {
  local groups_raw="$1"
  local group_name="$2"
  local metric_name="$3"
  printf '%s\n' "$groups_raw" | awk -v target_group="$group_name" -v target_metric="$metric_name" '
    $0 == "name" {
      getline
      current_group = $0
      next
    }
    $0 == target_metric {
      getline
      if (current_group == target_group) {
        print $0
        found = 1
        exit
      }
    }
    END {
      if (!found) exit 1
    }
  ' || true
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    --stream)
      STREAM="$2"
      shift 2
      ;;
    --stale-group)
      STALE_GROUP="$2"
      shift 2
      ;;
    --active-group)
      ACTIVE_GROUP="$2"
      shift 2
      ;;
    --redis-pod)
      REDIS_POD="$2"
      shift 2
      ;;
    --execute)
      EXECUTE=true
      shift
      ;;
    --allow-pending)
      ALLOW_PENDING=true
      shift
      ;;
    --skip-gate)
      RUN_GATE=false
      shift
      ;;
    --gate-candidate)
      GATE_CANDIDATE="$2"
      shift 2
      ;;
    --gate-tag)
      GATE_TAG="$2"
      shift 2
      ;;
    --gate-target)
      GATE_TARGET="$2"
      shift 2
      ;;
    --max-pending)
      MAX_PENDING="$2"
      shift 2
      ;;
    --gate-arg)
      GATE_EXTRA_ARGS+=("$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

require_cmd kubectl
require_cmd awk

if [[ -z "$REDIS_POD" ]]; then
  REDIS_POD="$(kubectl -n "$NAMESPACE" get pod -l app=redis -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi
[[ -n "$REDIS_POD" ]] || fail "redis pod not found in namespace=${NAMESPACE}"

log "namespace=${NAMESPACE} redis_pod=${REDIS_POD}"
log "stream=${STREAM} stale_group=${STALE_GROUP} active_group=${ACTIVE_GROUP}"
log "execute=${EXECUTE} run_gate=${RUN_GATE} allow_pending=${ALLOW_PENDING}"

print_snapshot "BEFORE"

groups_before="$(redis_raw XINFO GROUPS "$STREAM" || true)"
if [[ -z "$groups_before" ]]; then
  fail "XINFO GROUPS returned empty for stream=${STREAM}"
fi

stale_pending="$(extract_group_metric "$groups_before" "$STALE_GROUP" "pending")"
stale_lag="$(extract_group_metric "$groups_before" "$STALE_GROUP" "lag")"

if [[ -z "$stale_pending" ]]; then
  warn "stale group not found: ${STALE_GROUP} (nothing to cleanup)"
else
  log "stale group metrics: pending=${stale_pending} lag=${stale_lag:-unknown}"
  if (( stale_pending > 0 )) && [[ "$ALLOW_PENDING" != "true" ]]; then
    fail "stale group has pending=${stale_pending}; refuse destroy. Use --allow-pending only if you confirm no data loss risk."
  fi

  if [[ "$EXECUTE" == "true" ]]; then
    log "destroying group: XGROUP DESTROY ${STREAM} ${STALE_GROUP}"
    destroy_result="$(redis_raw XGROUP DESTROY "$STREAM" "$STALE_GROUP" || true)"
    destroy_result="$(printf '%s' "$destroy_result" | tr -d '\r')"
    log "destroy_result=${destroy_result}"
    if [[ "$destroy_result" != "1" && "$destroy_result" != "0" ]]; then
      warn "unexpected destroy result: ${destroy_result}"
    fi
  else
    warn "dry-run mode: skip XGROUP DESTROY (add --execute to apply)"
  fi
fi

print_snapshot "AFTER"

if [[ "$RUN_GATE" == "true" ]]; then
  gate_cmd=(
    scripts/release-gate.sh
    --candidate "$GATE_CANDIDATE"
    --tag "$GATE_TAG"
    --target "$GATE_TARGET"
    --max-pending "$MAX_PENDING"
  )
  if (( ${#GATE_EXTRA_ARGS[@]} > 0 )); then
    gate_cmd+=("${GATE_EXTRA_ARGS[@]}")
  fi

  log "running release gate: ${gate_cmd[*]}"
  "${gate_cmd[@]}"
  log "release gate completed successfully"
else
  warn "skip release-gate by --skip-gate"
fi

log "done"
