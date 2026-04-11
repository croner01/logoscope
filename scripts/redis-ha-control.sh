#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-islap}"
REDIS_STS="${REDIS_STS:-redis}"
REDIS_LABEL="${REDIS_LABEL:-app=redis}"
REDIS_SERVICE="${REDIS_SERVICE:-redis}"
REPLICA_LAG_MAX="${REDIS_MAX_REPLICA_LAG_BYTES:-1048576}"
DB_PROFILE_RAW="${DB_PROFILE:-auto}"
DB_PROFILE="$(echo "$DB_PROFILE_RAW" | tr '[:upper:]' '[:lower:]')"

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
      if kubectl -n "$NAMESPACE" get statefulset "$REDIS_STS" >/dev/null 2>&1; then
        DB_PROFILE="ha"
      elif kubectl -n "$NAMESPACE" get deployment "$REDIS_STS" >/dev/null 2>&1; then
        DB_PROFILE="single"
      else
        DB_PROFILE="single"
      fi
      ;;
    *)
      die "invalid DB_PROFILE=$DB_PROFILE (expected single|ha|auto)"
      ;;
  esac
}

pod_list() {
  kubectl -n "$NAMESPACE" get pods -l "$REDIS_LABEL" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort
}

first_pod() {
  pod_list | head -n1
}

master_pod_from_service() {
  kubectl -n "$NAMESPACE" get svc "$REDIS_SERVICE" -o jsonpath='{.spec.selector.statefulset\.kubernetes\.io/pod-name}' 2>/dev/null || true
}

pod_fqdn() {
  local pod="$1"
  echo "${pod}.redis-headless.${NAMESPACE}.svc.cluster.local"
}

wait_ready() {
  if [[ "$DB_PROFILE" == "ha" ]]; then
    kubectl -n "$NAMESPACE" rollout status "statefulset/${REDIS_STS}" --timeout=10m
  else
    kubectl -n "$NAMESPACE" rollout status "deployment/${REDIS_STS}" --timeout=10m
  fi
}

status_single() {
  kubectl -n "$NAMESPACE" get deployment "$REDIS_STS" -o wide
  kubectl -n "$NAMESPACE" get svc redis -o wide
  kubectl -n "$NAMESPACE" get pvc redis-pvc -o wide || true
  kubectl -n "$NAMESPACE" get pods -l "$REDIS_LABEL" -o wide

  local pod
  pod="$(first_pod)"
  [[ -n "$pod" ]] || return 0
  echo "--- $pod ---"
  kubectl -n "$NAMESPACE" exec "$pod" -- redis-cli INFO replication | awk -F: '
    /^role/ || /^master_repl_offset/ { gsub("\r", "", $2); print $1 ":" $2 }
  '
}

status_ha() {
  kubectl -n "$NAMESPACE" get statefulset "$REDIS_STS" -o wide
  kubectl -n "$NAMESPACE" get svc redis redis-headless -o wide
  kubectl -n "$NAMESPACE" get pods -l "$REDIS_LABEL" -o wide

  local pod
  while IFS= read -r pod; do
    [[ -n "$pod" ]] || continue
    echo "--- $pod ---"
    kubectl -n "$NAMESPACE" exec "$pod" -- redis-cli INFO replication | awk -F: '
      /^role/ || /^master_host/ || /^master_link_status/ || /^master_repl_offset/ || /^slave_repl_offset/ { gsub("\r", "", $2); print $1 ":" $2 }
    '
  done < <(pod_list)
}

status() {
  info "DB_PROFILE=$DB_PROFILE"
  if [[ "$DB_PROFILE" == "ha" ]]; then
    status_ha
  else
    status_single
  fi
}

check_single() {
  wait_ready

  local pod
  pod="$(first_pod)"
  [[ -n "$pod" ]] || die "No redis pod found"

  kubectl -n "$NAMESPACE" exec "$pod" -- redis-cli PING | grep -q "PONG"
  local role
  role="$(kubectl -n "$NAMESPACE" exec "$pod" -- redis-cli INFO replication | awk -F: '/^role/{gsub("\r", "", $2); print $2; exit}')"
  [[ "$role" == "master" ]] || die "single mode redis role is $role"

  info "Redis single check passed"
}

check_ha() {
  wait_ready

  local master_pod
  master_pod="$(master_pod_from_service)"
  if [[ -z "$master_pod" ]]; then
    warn "redis service selector has no explicit master pod, fallback to redis-0"
    master_pod="redis-0"
  fi

  kubectl -n "$NAMESPACE" get pod "$master_pod" >/dev/null 2>&1 || die "master pod not found: $master_pod"

  local role
  role="$(kubectl -n "$NAMESPACE" exec "$master_pod" -- redis-cli INFO replication | awk -F: '/^role/{gsub("\r", "", $2); print $2; exit}')"
  [[ "$role" == "master" ]] || die "service master pod $master_pod role=$role"

  local master_offset
  master_offset="$(kubectl -n "$NAMESPACE" exec "$master_pod" -- redis-cli INFO replication | awk -F: '/^master_repl_offset/{gsub("\r", "", $2); print $2; exit}')"
  [[ -n "$master_offset" ]] || die "cannot read master offset"

  local pod rrole link r_offset lag
  while IFS= read -r pod; do
    [[ -n "$pod" ]] || continue
    if [[ "$pod" == "$master_pod" ]]; then
      continue
    fi

    rrole="$(kubectl -n "$NAMESPACE" exec "$pod" -- redis-cli INFO replication | awk -F: '/^role/{gsub("\r", "", $2); print $2; exit}')"
    [[ "$rrole" == "slave" ]] || die "replica pod $pod role=$rrole"

    link="$(kubectl -n "$NAMESPACE" exec "$pod" -- redis-cli INFO replication | awk -F: '/^master_link_status/{gsub("\r", "", $2); print $2; exit}')"
    [[ "$link" == "up" ]] || die "replica pod $pod link=$link"

    r_offset="$(kubectl -n "$NAMESPACE" exec "$pod" -- redis-cli INFO replication | awk -F: '/^slave_repl_offset/{gsub("\r", "", $2); print $2; exit}')"
    if [[ -z "$r_offset" ]]; then
      r_offset="$(kubectl -n "$NAMESPACE" exec "$pod" -- redis-cli INFO replication | awk -F: '/^master_repl_offset/{gsub("\r", "", $2); print $2; exit}')"
    fi
    [[ -n "$r_offset" ]] || die "replica pod $pod offset missing"

    lag=$((master_offset - r_offset))
    if (( lag < 0 )); then
      lag=$(( -lag ))
    fi

    if (( lag > REPLICA_LAG_MAX )); then
      die "replica lag too high pod=$pod lag=$lag bytes"
    fi
  done < <(pod_list)

  info "Redis replication check passed"
}

check() {
  if [[ "$DB_PROFILE" == "ha" ]]; then
    check_ha
  else
    check_single
  fi
}

reconfigure() {
  [[ "$DB_PROFILE" == "ha" ]] || die "reconfigure is only supported in DB_PROFILE=ha"
  wait_ready

  local new_master="${1:-redis-0}"
  kubectl -n "$NAMESPACE" get pod "$new_master" >/dev/null 2>&1 || die "master pod not found: $new_master"

  local master_fqdn
  master_fqdn="$(pod_fqdn "$new_master")"

  info "Promoting $new_master as master"
  kubectl -n "$NAMESPACE" exec "$new_master" -- redis-cli REPLICAOF NO ONE >/dev/null

  local pod
  while IFS= read -r pod; do
    [[ -n "$pod" ]] || continue
    if [[ "$pod" == "$new_master" ]]; then
      continue
    fi
    info "Repointing replica $pod -> $master_fqdn"
    kubectl -n "$NAMESPACE" exec "$pod" -- redis-cli REPLICAOF "$master_fqdn" 6379 >/dev/null
  done < <(pod_list)

  info "Patching redis service selector to $new_master"
  kubectl -n "$NAMESPACE" patch svc "$REDIS_SERVICE" --type='merge' -p \
    "{\"spec\":{\"selector\":{\"app\":\"redis\",\"statefulset.kubernetes.io/pod-name\":\"${new_master}\"}}}"

  sleep 3
  check
}

promote() {
  local target="${1:-}"
  [[ -n "$target" ]] || die "usage: promote <redis-pod-name>"
  reconfigure "$target"
}

rolling_restart() {
  wait_ready

  if [[ "$DB_PROFILE" == "ha" ]]; then
    local master
    master="$(master_pod_from_service)"
    [[ -n "$master" ]] || master="redis-0"

    local replicas
    replicas="$(kubectl -n "$NAMESPACE" get statefulset "$REDIS_STS" -o jsonpath='{.spec.replicas}')"
    [[ -n "$replicas" ]] || die "cannot read replica count"

    local i pod
    for i in $(seq 0 $((replicas - 1))); do
      pod="${REDIS_STS}-${i}"
      if [[ "$pod" == "$master" ]]; then
        continue
      fi
      info "Restarting replica pod $pod"
      kubectl -n "$NAMESPACE" delete pod "$pod" --wait=true
      kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$pod" --timeout=10m
      check
    done

    info "Restarting master pod $master"
    kubectl -n "$NAMESPACE" delete pod "$master" --wait=true
    kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$master" --timeout=10m
    check
  else
    info "[profile=single] rolling restart deployment/${REDIS_STS}"
    kubectl -n "$NAMESPACE" rollout restart "deployment/${REDIS_STS}"
    kubectl -n "$NAMESPACE" rollout status "deployment/${REDIS_STS}" --timeout=10m
    check
  fi
}

usage() {
  cat <<EOF
Usage: DB_PROFILE=single|ha|auto $0 <command> [args]

Commands:
  status                        Show redis resources and replication role
  check                         Validate redis health/replication
  reconfigure [redis-pod]       Force HA topology to target master (ha only)
  promote <redis-pod>           Promote target pod to master (ha only)
  rolling-restart               Rolling restart with post-check
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
    reconfigure) reconfigure "$@" ;;
    promote) promote "$@" ;;
    rolling-restart) rolling_restart ;;
    *) usage; exit 1 ;;
  esac
}

main "$@"
