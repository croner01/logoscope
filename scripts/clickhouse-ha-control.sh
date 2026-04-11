#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-islap}"
CLICKHOUSE_STS="${CLICKHOUSE_STS:-clickhouse}"
KEEPER_STS="${KEEPER_STS:-clickhouse-keeper}"
CLICKHOUSE_LABEL="${CLICKHOUSE_LABEL:-app=clickhouse}"
SYNC_RETRIES="${SYNC_RETRIES:-3}"
SYNC_RETRY_SLEEP="${SYNC_RETRY_SLEEP:-3}"
MAX_QUEUE_SIZE="${CLICKHOUSE_MAX_QUEUE_SIZE:-200}"
MAX_ABSOLUTE_DELAY="${CLICKHOUSE_MAX_ABSOLUTE_DELAY:-120}"
DB_PROFILE_RAW="${DB_PROFILE:-auto}"
DB_PROFILE="$(echo "$DB_PROFILE_RAW" | tr '[:upper:]' '[:lower:]')"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEFAULT_TABLES=(
  logs.events
  logs.logs
  logs.traces
  logs.metrics
  logs.obs_counts_1m
  logs.obs_traces_1m
  logs.ai_analysis_sessions
  logs.ai_analysis_messages
  logs.ai_cases
  logs.ai_case_change_history
  logs.value_kpi_snapshots
  logs.release_gate_reports
)

info() { echo "[INFO] $*"; }
warn() { echo "[WARN] $*"; }
err() { echo "[ERROR] $*" >&2; }

die() {
  err "$*"
  exit 1
}

require_tools() {
  command -v kubectl >/dev/null 2>&1 || die "kubectl not found"
}

resolve_profile() {
  case "$DB_PROFILE" in
    single|ha)
      ;;
    auto|"")
      if kubectl -n "$NAMESPACE" get statefulset "$KEEPER_STS" >/dev/null 2>&1; then
        DB_PROFILE="ha"
      elif kubectl -n "$NAMESPACE" get deployment "$CLICKHOUSE_STS" >/dev/null 2>&1; then
        DB_PROFILE="single"
      elif kubectl -n "$NAMESPACE" get statefulset "$CLICKHOUSE_STS" >/dev/null 2>&1; then
        DB_PROFILE="ha"
      else
        DB_PROFILE="single"
      fi
      ;;
    *)
      die "invalid DB_PROFILE=$DB_PROFILE (expected single|ha|auto)"
      ;;
  esac
}

default_sql_file() {
  if [[ "$DB_PROFILE" == "ha" ]]; then
    echo "$SCRIPT_DIR/../deploy/clickhouse-init-replicated.sql"
  else
    echo "$SCRIPT_DIR/../deploy/clickhouse-init-single.sql"
  fi
}

pod_list() {
  kubectl -n "$NAMESPACE" get pods -l "$CLICKHOUSE_LABEL" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort
}

first_pod() {
  pod_list | head -n1
}

wait_ready() {
  if [[ "$DB_PROFILE" == "ha" ]]; then
    info "[profile=ha] Waiting StatefulSet/${KEEPER_STS} ready..."
    kubectl -n "$NAMESPACE" rollout status "statefulset/${KEEPER_STS}" --timeout=10m
    info "[profile=ha] Waiting StatefulSet/${CLICKHOUSE_STS} ready..."
    kubectl -n "$NAMESPACE" rollout status "statefulset/${CLICKHOUSE_STS}" --timeout=10m
  else
    info "[profile=single] Waiting Deployment/${CLICKHOUSE_STS} ready..."
    kubectl -n "$NAMESPACE" rollout status "deployment/${CLICKHOUSE_STS}" --timeout=10m
  fi
}

status() {
  info "DB_PROFILE=$DB_PROFILE"
  if [[ "$DB_PROFILE" == "ha" ]]; then
    kubectl -n "$NAMESPACE" get statefulset "$KEEPER_STS" "$CLICKHOUSE_STS" -o wide
    kubectl -n "$NAMESPACE" get svc clickhouse clickhouse-headless clickhouse-keeper clickhouse-keeper-headless -o wide
  else
    kubectl -n "$NAMESPACE" get deployment "$CLICKHOUSE_STS" -o wide
    kubectl -n "$NAMESPACE" get svc clickhouse -o wide
    kubectl -n "$NAMESPACE" get pvc clickhouse-pvc -o wide || true
  fi
  kubectl -n "$NAMESPACE" get pods -l "$CLICKHOUSE_LABEL" -o wide
}

replication_overview() {
  local pod
  pod="$(first_pod)"
  [[ -n "$pod" ]] || die "No clickhouse pod found"

  kubectl -n "$NAMESPACE" exec "$pod" -- clickhouse-client --query "
    SELECT
      database,
      table,
      is_leader,
      is_readonly,
      queue_size,
      inserts_in_queue,
      merges_in_queue,
      absolute_delay
    FROM system.replicas
    WHERE database='logs'
    ORDER BY table
    FORMAT PrettyCompactMonoBlock
  "
}

check() {
  wait_ready

  local pod
  pod="$(first_pod)"
  [[ -n "$pod" ]] || die "No clickhouse pod found"

  # 通用连通性检查
  kubectl -n "$NAMESPACE" exec "$pod" -- clickhouse-client --query "SELECT 1" >/dev/null

  if [[ "$DB_PROFILE" == "ha" ]]; then
    local bad
    bad="$(kubectl -n "$NAMESPACE" exec "$pod" -- clickhouse-client --query "
      SELECT count()
      FROM system.replicas
      WHERE database='logs'
        AND (
          is_readonly = 1
          OR queue_size > ${MAX_QUEUE_SIZE}
          OR absolute_delay > ${MAX_ABSOLUTE_DELAY}
        )
    ")"

    replication_overview

    if [[ "$bad" != "0" ]]; then
      die "Replication check failed: found $bad unhealthy replica rows"
    fi

    info "ClickHouse replication check passed"
  else
    local table_count
    table_count="$(kubectl -n "$NAMESPACE" exec "$pod" -- clickhouse-client --query "SELECT count() FROM system.tables WHERE database='logs'")"
    info "[profile=single] ClickHouse check passed (logs tables=$table_count)"
  fi
}

bootstrap() {
  wait_ready

  local sql_file
  sql_file="${CLICKHOUSE_BOOTSTRAP_SQL:-$(default_sql_file)}"
  [[ -f "$sql_file" ]] || die "Bootstrap SQL not found: $sql_file"

  if [[ "$DB_PROFILE" == "ha" ]]; then
    local pod
    while IFS= read -r pod; do
      [[ -n "$pod" ]] || continue
      info "Applying schema on $pod (profile=ha)"
      kubectl -n "$NAMESPACE" exec -i "$pod" -- clickhouse-client --multiquery < "$sql_file"
    done < <(pod_list)
  else
    local pod
    pod="$(first_pod)"
    [[ -n "$pod" ]] || die "No clickhouse pod found"
    info "Applying schema on $pod (profile=single)"
    kubectl -n "$NAMESPACE" exec -i "$pod" -- clickhouse-client --multiquery < "$sql_file"
  fi

  info "Schema bootstrap finished"
}

sync_tables() {
  if [[ "$DB_PROFILE" != "ha" ]]; then
    info "[profile=single] sync skipped (no replication queue)"
    check
    return 0
  fi

  wait_ready

  local tables=("$@")
  if [[ ${#tables[@]} -eq 0 ]]; then
    tables=("${DEFAULT_TABLES[@]}")
  fi

  local pod table attempt ok
  while IFS= read -r pod; do
    [[ -n "$pod" ]] || continue
    for table in "${tables[@]}"; do
      ok=0
      for attempt in $(seq 1 "$SYNC_RETRIES"); do
        if kubectl -n "$NAMESPACE" exec "$pod" -- clickhouse-client --query "SYSTEM SYNC REPLICA ${table}" >/dev/null 2>&1; then
          info "SYNC OK pod=$pod table=$table attempt=$attempt"
          ok=1
          break
        fi
        warn "SYNC RETRY pod=$pod table=$table attempt=$attempt"
        sleep "$SYNC_RETRY_SLEEP"
      done
      [[ "$ok" == "1" ]] || die "SYNC failed pod=$pod table=$table"
    done
  done < <(pod_list)

  check
}

rolling_restart() {
  wait_ready

  if [[ "$DB_PROFILE" == "ha" ]]; then
    local replicas
    replicas="$(kubectl -n "$NAMESPACE" get statefulset "$CLICKHOUSE_STS" -o jsonpath='{.spec.replicas}')"
    [[ -n "$replicas" ]] || die "Cannot read replica count"

    local i pod
    for i in $(seq 0 $((replicas - 1))); do
      pod="${CLICKHOUSE_STS}-${i}"
      info "Restarting $pod"
      kubectl -n "$NAMESPACE" delete pod "$pod" --wait=true
      kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$pod" --timeout=10m
      sync_tables logs.logs logs.traces
    done
  else
    info "[profile=single] rolling restart deployment/${CLICKHOUSE_STS}"
    kubectl -n "$NAMESPACE" rollout restart "deployment/${CLICKHOUSE_STS}"
    kubectl -n "$NAMESPACE" rollout status "deployment/${CLICKHOUSE_STS}" --timeout=10m
    check
  fi

  info "Rolling restart completed"
}

usage() {
  cat <<EOF
Usage: DB_PROFILE=single|ha|auto $0 <command> [args]

Commands:
  status                          Show database resources status
  check                           Validate clickhouse health/replication
  bootstrap                       Apply schema (single/ha engine by profile)
  sync [db.table ...]             SYSTEM SYNC REPLICA (ha only)
  rolling-restart                 Rolling restart with post-check
EOF
}

main() {
  require_tools
  resolve_profile

  local cmd="${1:-}"
  shift || true

  case "$cmd" in
    status) status ;;
    check) check ;;
    bootstrap) bootstrap ;;
    sync) sync_tables "$@" ;;
    rolling-restart) rolling_restart ;;
    *) usage; exit 1 ;;
  esac
}

main "$@"
