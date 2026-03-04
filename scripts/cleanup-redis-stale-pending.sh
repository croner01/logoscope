#!/usr/bin/env bash
set -euo pipefail

# Redis Stream 历史 pending 安全清理脚本
# 安全约束：
# 1) 仅处理指定 consumer
# 2) 仅处理 idle >= 阈值 的 pending 消息
# 3) 默认 dry-run，不执行写操作；需显式 --execute

NAMESPACE="${NAMESPACE:-islap}"
STREAM="${STREAM:-traces.raw}"
GROUP="${GROUP:-log-workers}"
CONSUMER=""
IDLE_MS_THRESHOLD="${IDLE_MS_THRESHOLD:-3600000}"
MAX_SCAN="${MAX_SCAN:-100000}"
BATCH_SIZE="${BATCH_SIZE:-300}"
REDIS_POD="${REDIS_POD:-}"
DRY_RUN=true
DELETE_CONSUMER=false

usage() {
  cat <<'EOF'
Usage:
  scripts/cleanup-redis-stale-pending.sh --consumer <name> [options]

Required:
  --consumer <name>          仅清理该 consumer 的 pending

Options:
  --namespace <ns>           K8s namespace (default: islap)
  --stream <name>            Redis stream (default: traces.raw)
  --group <name>             Consumer group (default: log-workers)
  --idle-ms-threshold <ms>   最小 idle 阈值，仅清理 >= 该值 (default: 3600000)
  --max-scan <n>             最大扫描条数 (default: 100000)
  --batch-size <n>           每次 XACK 的批大小 (default: 300)
  --redis-pod <name>         指定 Redis Pod（不传则按 app=redis 自动查找）
  --execute                  真正执行 XACK（默认 dry-run）
  --delete-consumer          执行后若该 consumer pending=0，尝试 DELCONSUMER
  -h, --help                 显示帮助

Examples:
  scripts/cleanup-redis-stale-pending.sh \
    --consumer consumer-129027368864400

  scripts/cleanup-redis-stale-pending.sh \
    --consumer consumer-129027368864400 \
    --idle-ms-threshold 3600000 \
    --execute --delete-consumer
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

is_positive_int() {
  [[ "${1:-}" =~ ^[1-9][0-9]*$ ]]
}

redis_raw() {
  kubectl -n "$NAMESPACE" exec "$REDIS_POD" -- redis-cli --raw "$@"
}

print_group_snapshot() {
  local title="$1"
  echo "=== ${title}: XPENDING ${STREAM}/${GROUP} ==="
  redis_raw XPENDING "$STREAM" "$GROUP" || true
  echo
  echo "=== ${title}: XINFO CONSUMERS ${STREAM}/${GROUP} ==="
  redis_raw XINFO CONSUMERS "$STREAM" "$GROUP" || true
  echo
  echo "=== ${title}: XINFO GROUPS ${STREAM} ==="
  redis_raw XINFO GROUPS "$STREAM" || true
  echo
}

get_consumer_pending_idle() {
  local info_raw="$1"
  printf '%s\n' "$info_raw" | awk -v target="$CONSUMER" '
    $0 == "name" { getline cur_name; next }
    $0 == "pending" { getline cur_pending; next }
    $0 == "idle" {
      getline cur_idle
      if (cur_name == target) {
        print cur_pending "\t" cur_idle
        found = 1
        exit
      }
      next
    }
    END {
      if (!found) exit 1
    }
  '
}

ack_ids_in_batches() {
  local ids_text="$1"
  local acked_total=0
  local -a batch=()
  local id=""

  while IFS= read -r id; do
    [[ -z "$id" ]] && continue
    batch+=("$id")
    if (( ${#batch[@]} >= BATCH_SIZE )); then
      local acked
      acked="$(redis_raw XACK "$STREAM" "$GROUP" "${batch[@]}" | tr -d '\r')"
      acked_total=$((acked_total + acked))
      batch=()
    fi
  done <<< "$ids_text"

  if (( ${#batch[@]} > 0 )); then
    local acked
    acked="$(redis_raw XACK "$STREAM" "$GROUP" "${batch[@]}" | tr -d '\r')"
    acked_total=$((acked_total + acked))
  fi

  printf '%s' "$acked_total"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --consumer)
      CONSUMER="$2"
      shift 2
      ;;
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    --stream)
      STREAM="$2"
      shift 2
      ;;
    --group)
      GROUP="$2"
      shift 2
      ;;
    --idle-ms-threshold)
      IDLE_MS_THRESHOLD="$2"
      shift 2
      ;;
    --max-scan)
      MAX_SCAN="$2"
      shift 2
      ;;
    --batch-size)
      BATCH_SIZE="$2"
      shift 2
      ;;
    --redis-pod)
      REDIS_POD="$2"
      shift 2
      ;;
    --execute)
      DRY_RUN=false
      shift
      ;;
    --delete-consumer)
      DELETE_CONSUMER=true
      shift
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

[[ -n "$CONSUMER" ]] || fail "--consumer is required"
is_positive_int "$IDLE_MS_THRESHOLD" || fail "--idle-ms-threshold must be positive integer"
is_positive_int "$MAX_SCAN" || fail "--max-scan must be positive integer"
is_positive_int "$BATCH_SIZE" || fail "--batch-size must be positive integer"

if [[ -z "$REDIS_POD" ]]; then
  REDIS_POD="$(kubectl -n "$NAMESPACE" get pod -l app=redis -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi
[[ -n "$REDIS_POD" ]] || fail "redis pod not found in namespace=${NAMESPACE}"

log "namespace=${NAMESPACE} redis_pod=${REDIS_POD}"
log "stream=${STREAM} group=${GROUP} consumer=${CONSUMER}"
log "idle_ms_threshold=${IDLE_MS_THRESHOLD} max_scan=${MAX_SCAN} batch_size=${BATCH_SIZE} dry_run=${DRY_RUN}"

print_group_snapshot "BEFORE"

consumer_info_raw="$(redis_raw XINFO CONSUMERS "$STREAM" "$GROUP" 2>/dev/null || true)"
if [[ -z "$consumer_info_raw" ]]; then
  fail "failed to read consumer info for stream=${STREAM} group=${GROUP}"
fi

if ! consumer_stats="$(get_consumer_pending_idle "$consumer_info_raw")"; then
  fail "consumer not found: ${CONSUMER}"
fi

consumer_pending="$(printf '%s' "$consumer_stats" | cut -f1)"
consumer_idle_ms="$(printf '%s' "$consumer_stats" | cut -f2)"
log "consumer_pending=${consumer_pending} consumer_idle_ms=${consumer_idle_ms}"

if (( consumer_pending == 0 )); then
  log "consumer has no pending, nothing to do."
  exit 0
fi

if (( consumer_idle_ms < IDLE_MS_THRESHOLD )); then
  fail "consumer idle (${consumer_idle_ms}ms) < threshold (${IDLE_MS_THRESHOLD}ms), refuse cleanup"
fi

pending_detail="$(redis_raw XPENDING "$STREAM" "$GROUP" - + "$MAX_SCAN" "$CONSUMER" 2>/dev/null || true)"
if [[ -z "$pending_detail" ]]; then
  warn "no pending details returned by XPENDING range"
  exit 0
fi

candidate_ids="$(
  printf '%s\n' "$pending_detail" | awk -v threshold="$IDLE_MS_THRESHOLD" '
    NR % 4 == 1 { id = $0; next }
    NR % 4 == 3 { idle = $0; next }
    NR % 4 == 0 {
      if ((idle + 0) >= threshold) print id
      next
    }
  '
)"

candidate_count="$(printf '%s\n' "$candidate_ids" | awk 'NF>0{c++} END{print c+0}')"
log "candidate_count(idle>=threshold)=${candidate_count}"

if (( candidate_count == 0 )); then
  log "no pending IDs matched idle threshold, nothing to do."
  exit 0
fi

echo "=== CANDIDATE SAMPLE (first 20 ids) ==="
printf '%s\n' "$candidate_ids" | sed -n '1,20p'
echo

acked_total=0
if [[ "$DRY_RUN" == "false" ]]; then
  acked_total="$(ack_ids_in_batches "$candidate_ids")"
  log "acked_total=${acked_total}"

  if [[ "$DELETE_CONSUMER" == "true" ]]; then
    post_info_raw="$(redis_raw XINFO CONSUMERS "$STREAM" "$GROUP" 2>/dev/null || true)"
    if post_stats="$(get_consumer_pending_idle "$post_info_raw" 2>/dev/null)"; then
      post_pending="$(printf '%s' "$post_stats" | cut -f1)"
      if (( post_pending == 0 )); then
        removed="$(redis_raw XGROUP DELCONSUMER "$STREAM" "$GROUP" "$CONSUMER" | tr -d '\r' || true)"
        log "delconsumer_removed=${removed}"
      else
        warn "skip DELCONSUMER because pending still > 0 (pending=${post_pending})"
      fi
    else
      log "consumer already absent, skip DELCONSUMER"
    fi
  fi
else
  log "dry-run mode: no XACK executed"
fi

print_group_snapshot "AFTER"

echo "=== SUMMARY ==="
echo "stream=${STREAM}"
echo "group=${GROUP}"
echo "consumer=${CONSUMER}"
echo "idle_ms_threshold=${IDLE_MS_THRESHOLD}"
echo "candidate_count=${candidate_count}"
echo "acked_total=${acked_total}"
echo "dry_run=${DRY_RUN}"
