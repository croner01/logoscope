#!/usr/bin/env bash
set -euo pipefail

# Logoscope K8s image operations helper
# Fixed environment assumptions:
# - Namespace: islap
# - Registry: localhost:5000/logoscope
# - Deploy manifests: deploy/

NAMESPACE="${NAMESPACE:-islap}"
REGISTRY_PREFIX="${REGISTRY_PREFIX:-localhost:5000/logoscope}"
DEFAULT_TAG="${DEFAULT_TAG:-latest}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SERVICES=(semantic-engine ai-service ingest-service query-service topology-service frontend)

usage() {
  cat <<'EOF'
Usage:
  scripts/k8s-image-ops.sh build <service|all> [tag]
  scripts/k8s-image-ops.sh push <service|all> [tag]
  scripts/k8s-image-ops.sh build-push <service|all> [tag]
  scripts/k8s-image-ops.sh gate <service|all> [tag]
  scripts/k8s-image-ops.sh release <service|all> [tag]
  scripts/k8s-image-ops.sh set-image <service|all> [tag]
  scripts/k8s-image-ops.sh apply <service|all>
  scripts/k8s-image-ops.sh restart <service|all>
  scripts/k8s-image-ops.sh rollout-status <service|all>

Service list:
  semantic-engine ai-service ingest-service query-service topology-service frontend

Notes:
  1) semantic-engine image update also updates semantic-engine-worker deployment.
  2) Tag defaults to "latest" if omitted.
  3) release 动作: build -> push -> set-image -> rollout-status -> gate（trace smoke + ai contract + query contract + sql safety + backend pytest + p0p1 regression + perf baseline，失败阻断）
EOF
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "[ERROR] command not found: $cmd" >&2
    exit 1
  }
}

service_exists() {
  local service="$1"
  for s in "${SERVICES[@]}"; do
    if [[ "$s" == "$service" ]]; then
      return 0
    fi
  done
  return 1
}

expand_target() {
  local target="$1"
  if [[ "$target" == "all" ]]; then
    printf '%s\n' "${SERVICES[@]}"
    return 0
  fi

  if service_exists "$target"; then
    printf '%s\n' "$target"
    return 0
  fi

  echo "[ERROR] unknown service: $target" >&2
  exit 1
}

image_ref() {
  local service="$1"
  local tag="$2"
  echo "${REGISTRY_PREFIX}/${service}:${tag}"
}

build_one() {
  local service="$1"
  local tag="$2"
  local image
  image="$(image_ref "$service" "$tag")"

  echo "[INFO] docker build ${image} (${service}/)"

  cd /root/logoscope && docker build -t "$image"  -f "${service}/Dockerfile" .
}

push_one() {
  local service="$1"
  local tag="$2"
  local image
  image="$(image_ref "$service" "$tag")"

  echo "[INFO] docker push ${image}"
  docker push "$image"
}

set_image_one() {
  local service="$1"
  local tag="$2"
  local image
  image="$(image_ref "$service" "$tag")"

  case "$service" in
    semantic-engine)
      kubectl -n "$NAMESPACE" set image deployment/semantic-engine semantic-engine="$image"
      kubectl -n "$NAMESPACE" set image deployment/semantic-engine-worker worker="$image"
      ;;
    ai-service)
      kubectl -n "$NAMESPACE" set image deployment/ai-service ai-service="$image"
      ;;
    ingest-service)
      kubectl -n "$NAMESPACE" set image deployment/ingest-service ingest-service="$image"
      ;;
    query-service)
      kubectl -n "$NAMESPACE" set image deployment/query-service query-service="$image"
      ;;
    topology-service)
      kubectl -n "$NAMESPACE" set image deployment/topology-service topology-service="$image"
      ;;
    frontend)
      kubectl -n "$NAMESPACE" set image deployment/frontend frontend="$image"
      ;;
    *)
      echo "[ERROR] unsupported service for set-image: $service" >&2
      exit 1
      ;;
  esac
}

apply_one() {
  local service="$1"
  case "$service" in
    semantic-engine)
      kubectl apply -f "${PROJECT_ROOT}/deploy/semantic-engine.yaml"
      kubectl apply -f "${PROJECT_ROOT}/deploy/semantic-engine-worker.yaml"
      ;;
    ai-service)
      kubectl apply -f "${PROJECT_ROOT}/deploy/ai-service.yaml"
      ;;
    ingest-service)
      kubectl apply -f "${PROJECT_ROOT}/deploy/ingest-service.yaml"
      ;;
    query-service)
      kubectl apply -f "${PROJECT_ROOT}/deploy/query-service.yaml"
      ;;
    topology-service)
      kubectl apply -f "${PROJECT_ROOT}/deploy/topology-service.yaml"
      ;;
    frontend)
      kubectl apply -f "${PROJECT_ROOT}/deploy/frontend.yaml"
      ;;
    *)
      echo "[ERROR] unsupported service for apply: $service" >&2
      exit 1
      ;;
  esac
}

restart_one() {
  local service="$1"
  case "$service" in
    semantic-engine)
      kubectl -n "$NAMESPACE" rollout restart deployment/semantic-engine
      kubectl -n "$NAMESPACE" rollout restart deployment/semantic-engine-worker
      ;;
    ai-service|ingest-service|query-service|topology-service|frontend)
      kubectl -n "$NAMESPACE" rollout restart "deployment/${service}"
      ;;
    *)
      echo "[ERROR] unsupported service for restart: $service" >&2
      exit 1
      ;;
  esac
}

rollout_status_one() {
  local service="$1"
  case "$service" in
    semantic-engine)
      kubectl -n "$NAMESPACE" rollout status deployment/semantic-engine
      kubectl -n "$NAMESPACE" rollout status deployment/semantic-engine-worker
      ;;
    ai-service|ingest-service|query-service|topology-service|frontend)
      kubectl -n "$NAMESPACE" rollout status "deployment/${service}"
      ;;
    *)
      echo "[ERROR] unsupported service for rollout-status: $service" >&2
      exit 1
      ;;
  esac
}

gate_release() {
  local target="$1"
  local tag="$2"
  "${PROJECT_ROOT}/scripts/release-gate.sh" \
    --candidate "k8s-image-ops-release" \
    --tag "$tag" \
    --target "$target"
}

main() {
  if [[ $# -lt 2 ]]; then
    usage
    exit 1
  fi

  local action="$1"
  local target="$2"
  local tag="${3:-$DEFAULT_TAG}"

  case "$action" in
    build|push|build-push)
      require_cmd docker
      ;;
    gate)
      require_cmd kubectl
      gate_release "$target" "$tag"
      return 0
      ;;
    release)
      require_cmd docker
      require_cmd kubectl
      ;;
    set-image|apply|restart|rollout-status)
      require_cmd kubectl
      ;;
    *)
      echo "[ERROR] unknown action: $action" >&2
      usage
      exit 1
      ;;
  esac

  while IFS= read -r service; do
    case "$action" in
      build)
        build_one "$service" "$tag"
        ;;
      push)
        push_one "$service" "$tag"
        ;;
      build-push)
        build_one "$service" "$tag"
        push_one "$service" "$tag"
        ;;
      set-image)
        set_image_one "$service" "$tag"
        ;;
      apply)
        apply_one "$service"
        ;;
      restart)
        restart_one "$service"
        ;;
      rollout-status)
        rollout_status_one "$service"
        ;;
      release)
        build_one "$service" "$tag"
        push_one "$service" "$tag"
        set_image_one "$service" "$tag"
        rollout_status_one "$service"
        ;;
    esac
  done < <(expand_target "$target")

  if [[ "$action" == "release" ]]; then
    gate_release "$target" "$tag"
  fi
}

main "$@"
