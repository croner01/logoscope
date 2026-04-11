#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  replay-run-check.sh <run_id> [--ai-base <url>] [--exec-base <url>] [--events-out <path>] [--no-exec-check]
  replay-run-check.sh <run_id> --via-kubectl [--k8s-namespace <ns>] [--ai-service-ref <ref>] [--ai-service-port <port>] [--no-exec-check]

Defaults:
  ai-base   = http://127.0.0.1:8090/api/v2
  exec-base = http://127.0.0.1:8095
  k8s-namespace = islap
  ai-service-ref = deploy/ai-service
  ai-service-port = 8090

Example:
  ./scripts/replay-run-check.sh run-4217486121b8
  ./scripts/replay-run-check.sh run-4217486121b8 --via-kubectl --no-exec-check
USAGE
}

if [[ $# -ge 1 && ( "$1" == "-h" || "$1" == "--help" ) ]]; then
  usage
  exit 0
fi

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

RUN_ID="$1"
shift

AI_BASE="http://127.0.0.1:8090/api/v2"
EXEC_BASE="http://127.0.0.1:8095"
EVENTS_OUT=""
EXEC_CHECK="1"
VIA_KUBECTL="0"
K8S_NAMESPACE="islap"
AI_SERVICE_REF="deploy/ai-service"
AI_SERVICE_PORT="8090"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ai-base)
      AI_BASE="$2"
      shift 2
      ;;
    --exec-base)
      EXEC_BASE="$2"
      shift 2
      ;;
    --events-out)
      EVENTS_OUT="$2"
      shift 2
      ;;
    --no-exec-check)
      EXEC_CHECK="0"
      shift 1
      ;;
    --via-kubectl)
      VIA_KUBECTL="1"
      shift 1
      ;;
    --k8s-namespace)
      K8S_NAMESPACE="$2"
      shift 2
      ;;
    --ai-service-ref)
      AI_SERVICE_REF="$2"
      shift 2
      ;;
    --ai-service-port)
      AI_SERVICE_PORT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if ! command -v jq >/dev/null 2>&1; then
  echo "[ERROR] jq is required" >&2
  exit 2
fi

if [[ "$VIA_KUBECTL" == "1" ]] && ! command -v kubectl >/dev/null 2>&1; then
  echo "[ERROR] kubectl is required when --via-kubectl is enabled" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSERT_SCRIPT="$SCRIPT_DIR/assert-no-duplicate-command.py"
if [[ ! -f "$ASSERT_SCRIPT" ]]; then
  echo "[ERROR] missing script: $ASSERT_SCRIPT" >&2
  exit 2
fi

cleanup_file="0"
if [[ -z "$EVENTS_OUT" ]]; then
  EVENTS_OUT="$(mktemp)"
  cleanup_file="1"
fi

cleanup() {
  if [[ "$cleanup_file" == "1" ]]; then
    rm -f "$EVENTS_OUT"
  fi
}
trap cleanup EXIT

EVENTS_URL="${AI_BASE%/}/runs/${RUN_ID}/events?after_seq=0&limit=5000&visibility=debug"
if [[ "$VIA_KUBECTL" == "1" ]]; then
  POD_URL="http://127.0.0.1:${AI_SERVICE_PORT}/api/v2/runs/${RUN_ID}/events?after_seq=0&limit=5000&visibility=debug"
  echo "[INFO] fetching events via kubectl: ns=${K8S_NAMESPACE} ref=${AI_SERVICE_REF} url=${POD_URL}"
  kubectl -n "$K8S_NAMESPACE" exec "$AI_SERVICE_REF" -- curl -fsS "$POD_URL" > "$EVENTS_OUT"
else
  echo "[INFO] fetching events: $EVENTS_URL"
  curl -fsS "$EVENTS_URL" > "$EVENTS_OUT"
fi

echo "[INFO] events saved: $EVENTS_OUT"
echo "[INFO] quick summary:"
jq -r '
  "  total_events=\(.events|length)",
  "  next_after_seq=\(.next_after_seq)",
  "  approval_required=\([.events[]|select(.event_type=="approval_required")]|length)",
  "  approval_resolved=\([.events[]|select(.event_type=="approval_resolved")]|length)",
  "  approval_timeout=\([.events[]|select(.event_type=="approval_timeout")]|length)",
  "  command_run_ids=\([.events[]|.payload.command_run_id // empty]|map(select(length>0))|unique|length)"
' "$EVENTS_OUT"

echo "[INFO] command run overview:"
jq -r '
  .events
  | map(select((.payload.command_run_id // "") != ""))
  | group_by(.payload.command_run_id)
  | .[]
  | {
      run_id: .[0].payload.command_run_id,
      started: (map(select(.event_type=="tool_call_started"))|length),
      finished: (map(select(.event_type=="tool_call_finished"))|length),
      chunks: (map(select(.event_type=="tool_call_output_delta"))|length),
      chars: (map(select(.event_type=="tool_call_output_delta")|(.payload.text // "" )|length)|add // 0)
    }
  | "  \(.run_id)\tstarted=\(.started)\tfinished=\(.finished)\tchunks=\(.chunks)\tchars=\(.chars)"
' "$EVENTS_OUT"

assert_args=(--events-file "$EVENTS_OUT" --run-id "$RUN_ID")
if [[ "$EXEC_CHECK" == "1" ]]; then
  assert_args+=(--exec-base-url "$EXEC_BASE")
fi

python3 "$ASSERT_SCRIPT" "${assert_args[@]}"
