#!/usr/bin/env bash
set -euo pipefail

# Reconcile /api/v1/logs paginated total with ClickHouse logs.logs count.
#
# Default behavior:
# - API query runs inside query-service pod (avoids external network/path issues).
# - DB query runs inside clickhouse pod.
#
# Exit code:
# - 0: counts match
# - 2: counts mismatch
# - 1: script/runtime error

NAMESPACE="${NAMESPACE:-islap}"
MODE="${MODE:-inpod}" # inpod | direct
API_URL="${API_URL:-http://127.0.0.1:8002/api/v1/logs}" # used in direct mode
LIMIT="${LIMIT:-1000}"
MAX_PAGES="${MAX_PAGES:-20000}"

START_TIME="${START_TIME:-}"
END_TIME="${END_TIME:-}"
ANCHOR_TIME="${ANCHOR_TIME:-}"
SERVICE_NAME="${SERVICE_NAME:-}"
NAMESPACE_FILTER="${NAMESPACE_FILTER:-}"
SEARCH="${SEARCH:-}"
EXCLUDE_HEALTH_CHECK="${EXCLUDE_HEALTH_CHECK:-false}"

OUTPUT_JSON="${OUTPUT_JSON:-}"

usage() {
  cat <<'EOF'
Usage:
  scripts/logs-api-db-reconcile.sh --start <ISO8601> --end <ISO8601> [options]

Required:
  --start <ts>               e.g. 2026-03-07T07:10:00Z
  --end <ts>                 e.g. 2026-03-07T08:11:30Z

Optional:
  --service <name>           service_name filter
  --namespace-filter <name>  namespace filter
  --search <text>            message substring filter (same as /api/v1/logs search)
  --exclude-health-check     enable health-check exclusion
  --anchor-time <ts>         force first-page anchor_time
  --limit <n>                page size, default 1000
  --max-pages <n>            safety guard, default 20000
  --mode <inpod|direct>      API call mode, default inpod
  --api-url <url>            required in direct mode, default http://127.0.0.1:8002/api/v1/logs
  --output-json <path>       write summary JSON to file

Examples:
  scripts/logs-api-db-reconcile.sh \
    --start 2026-03-07T07:10:00Z \
    --end 2026-03-07T08:11:30Z \
    --service otel-collector

  MODE=direct API_URL="http://10.222.109.157:8002/api/v1/logs" \
  scripts/logs-api-db-reconcile.sh \
    --start 2026-03-07T14:00:00Z \
    --end 2026-03-07T14:50:00Z \
    --service nova-compute
EOF
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "[ERROR] missing command: $1" >&2
    exit 1
  }
}

to_bool() {
  local raw="${1:-false}"
  case "${raw,,}" in
    1|true|yes|on) echo "true" ;;
    *) echo "false" ;;
  esac
}

sql_escape() {
  local s="${1:-}"
  s="${s//\\/\\\\}"
  s="${s//\'/\\\'}"
  printf '%s' "$s"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --start)
        START_TIME="${2:-}"; shift 2 ;;
      --end)
        END_TIME="${2:-}"; shift 2 ;;
      --service)
        SERVICE_NAME="${2:-}"; shift 2 ;;
      --namespace-filter)
        NAMESPACE_FILTER="${2:-}"; shift 2 ;;
      --search)
        SEARCH="${2:-}"; shift 2 ;;
      --exclude-health-check)
        EXCLUDE_HEALTH_CHECK="true"; shift 1 ;;
      --anchor-time)
        ANCHOR_TIME="${2:-}"; shift 2 ;;
      --limit)
        LIMIT="${2:-}"; shift 2 ;;
      --max-pages)
        MAX_PAGES="${2:-}"; shift 2 ;;
      --mode)
        MODE="${2:-}"; shift 2 ;;
      --api-url)
        API_URL="${2:-}"; shift 2 ;;
      --output-json)
        OUTPUT_JSON="${2:-}"; shift 2 ;;
      -h|--help)
        usage; exit 0 ;;
      *)
        echo "[ERROR] unknown argument: $1" >&2
        usage
        exit 1 ;;
    esac
  done
}

api_pager_python() {
  cat <<'PY'
import json
import os
import sys
import urllib.parse
import urllib.request

base_url = os.environ["API_URL"]
limit = int(os.environ.get("LIMIT", "1000"))
max_pages = int(os.environ.get("MAX_PAGES", "20000"))

params = {}
if os.environ.get("START_TIME"):
    params["start_time"] = os.environ["START_TIME"]
if os.environ.get("END_TIME"):
    params["end_time"] = os.environ["END_TIME"]
if os.environ.get("SERVICE_NAME"):
    params["service_name"] = os.environ["SERVICE_NAME"]
if os.environ.get("NAMESPACE_FILTER"):
    params["namespace"] = os.environ["NAMESPACE_FILTER"]
if os.environ.get("SEARCH"):
    params["search"] = os.environ["SEARCH"]
if os.environ.get("EXCLUDE_HEALTH_CHECK") == "true":
    params["exclude_health_check"] = "true"
if os.environ.get("ANCHOR_TIME"):
    params["anchor_time"] = os.environ["ANCHOR_TIME"]

pages = 0
total = 0
cursor = ""
frozen_anchor = ""
first_page_count = 0
last_page_count = 0

while True:
    pages += 1
    if pages > max_pages:
        print("ERROR: max_pages reached", file=sys.stderr)
        sys.exit(1)

    req_params = dict(params)
    req_params["limit"] = str(limit)
    if cursor:
        req_params["cursor"] = cursor
        req_params["anchor_time"] = frozen_anchor

    url = base_url + "?" + urllib.parse.urlencode(req_params, doseq=True)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except Exception as exc:
        print(f"ERROR: request failed page={pages}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        payload = json.loads(body)
    except Exception as exc:
        print(f"ERROR: invalid json page={pages}: {exc}", file=sys.stderr)
        print(body[:400], file=sys.stderr)
        sys.exit(1)

    page_count = int(payload.get("count") or len(payload.get("data") or []))
    total += page_count
    if pages == 1:
        first_page_count = page_count
        frozen_anchor = str(payload.get("anchor_time") or "")
    else:
        current_anchor = str(payload.get("anchor_time") or "")
        if current_anchor != frozen_anchor:
            print(
                f"ERROR: anchor drift page={pages} expected={frozen_anchor} got={current_anchor}",
                file=sys.stderr,
            )
            sys.exit(1)

    last_page_count = page_count
    has_more = bool(payload.get("has_more"))
    cursor = str(payload.get("next_cursor") or "")
    if has_more and not cursor:
        print(f"ERROR: has_more=true but next_cursor empty page={pages}", file=sys.stderr)
        sys.exit(1)
    if not has_more:
        break

print(f"pages={pages}")
print(f"total={total}")
print(f"first_page_count={first_page_count}")
print(f"last_page_count={last_page_count}")
print(f"anchor_time={frozen_anchor}")
print(f"has_more_final={'true' if has_more else 'false'}")
PY
}

fetch_api_total_direct() {
  require_cmd python3
  API_SUMMARY_JSON="$(
    START_TIME="${START_TIME}" \
    END_TIME="${END_TIME}" \
    SERVICE_NAME="${SERVICE_NAME}" \
    NAMESPACE_FILTER="${NAMESPACE_FILTER}" \
    SEARCH="${SEARCH}" \
    EXCLUDE_HEALTH_CHECK="$(to_bool "${EXCLUDE_HEALTH_CHECK}")" \
    ANCHOR_TIME="${ANCHOR_TIME}" \
    LIMIT="${LIMIT}" \
    MAX_PAGES="${MAX_PAGES}" \
    API_URL="${API_URL}" \
    python3 -c "$(api_pager_python)"
  )"
}

fetch_api_total_inpod() {
  require_cmd kubectl
  local query_pod
  query_pod="$(kubectl -n "${NAMESPACE}" get pod -l app=query-service -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -z "${query_pod}" ]]; then
    echo "[ERROR] query-service pod not found in namespace ${NAMESPACE}" >&2
    exit 1
  fi

  API_SUMMARY_JSON="$(
    kubectl -n "${NAMESPACE}" exec "${query_pod}" -- env \
      START_TIME="${START_TIME}" \
      END_TIME="${END_TIME}" \
      SERVICE_NAME="${SERVICE_NAME}" \
      NAMESPACE_FILTER="${NAMESPACE_FILTER}" \
      SEARCH="${SEARCH}" \
      EXCLUDE_HEALTH_CHECK="$(to_bool "${EXCLUDE_HEALTH_CHECK}")" \
      ANCHOR_TIME="${ANCHOR_TIME}" \
      LIMIT="${LIMIT}" \
      MAX_PAGES="${MAX_PAGES}" \
      API_URL="http://127.0.0.1:8002/api/v1/logs" \
      python -c "$(api_pager_python)"
  )"
}

fetch_db_total() {
  require_cmd kubectl

  local clickhouse_pod
  clickhouse_pod="$(kubectl -n "${NAMESPACE}" get pod -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -z "${clickhouse_pod}" ]]; then
    echo "[ERROR] clickhouse pod not found in namespace ${NAMESPACE}" >&2
    exit 1
  fi

  local where_clauses
  where_clauses=()
  where_clauses+=("timestamp >= parseDateTime64BestEffort('$(sql_escape "${START_TIME}")', 9, 'UTC')")
  where_clauses+=("timestamp <= parseDateTime64BestEffort('$(sql_escape "${END_TIME}")', 9, 'UTC')")

  if [[ -n "${SERVICE_NAME}" ]]; then
    where_clauses+=("service_name = '$(sql_escape "${SERVICE_NAME}")'")
  fi
  if [[ -n "${NAMESPACE_FILTER}" ]]; then
    where_clauses+=("namespace = '$(sql_escape "${NAMESPACE_FILTER}")'")
  fi
  if [[ -n "${SEARCH}" ]]; then
    where_clauses+=("message ILIKE concat('%', '$(sql_escape "${SEARCH}")', '%')")
  fi
  if [[ "$(to_bool "${EXCLUDE_HEALTH_CHECK}")" == "true" ]]; then
    where_clauses+=("multiSearchAnyCaseInsensitiveUTF8(message, ['kube-probe','/health','/healthz','/ready','/readiness','/live','/liveness','readiness probe','liveness probe']) = 0")
  fi

  local sql
  sql="SELECT count() FROM logs.logs WHERE ${where_clauses[0]}"
  local i
  for (( i=1; i<${#where_clauses[@]}; i++ )); do
    sql+=" AND ${where_clauses[$i]}"
  done

  DB_TOTAL_RAW="$(kubectl -n "${NAMESPACE}" exec "${clickhouse_pod}" -- clickhouse-client -q "${sql}")"
  DB_TOTAL="$(echo "${DB_TOTAL_RAW}" | tr -d '[:space:]')"
}

parse_api_summary_kv() {
  API_PAGES="$(echo "${API_SUMMARY_JSON}" | sed -n 's/^pages=//p' | tail -n1)"
  API_TOTAL="$(echo "${API_SUMMARY_JSON}" | sed -n 's/^total=//p' | tail -n1)"
  API_FIRST_PAGE_COUNT="$(echo "${API_SUMMARY_JSON}" | sed -n 's/^first_page_count=//p' | tail -n1)"
  API_LAST_PAGE_COUNT="$(echo "${API_SUMMARY_JSON}" | sed -n 's/^last_page_count=//p' | tail -n1)"
  API_ANCHOR_TIME="$(echo "${API_SUMMARY_JSON}" | sed -n 's/^anchor_time=//p' | tail -n1)"
  API_HAS_MORE_FINAL="$(echo "${API_SUMMARY_JSON}" | sed -n 's/^has_more_final=//p' | tail -n1)"

  API_PAGES="${API_PAGES:-0}"
  API_TOTAL="${API_TOTAL:-0}"
  API_FIRST_PAGE_COUNT="${API_FIRST_PAGE_COUNT:-0}"
  API_LAST_PAGE_COUNT="${API_LAST_PAGE_COUNT:-0}"
  API_ANCHOR_TIME="${API_ANCHOR_TIME:-}"
  API_HAS_MORE_FINAL="${API_HAS_MORE_FINAL:-false}"
}

build_summary_json() {
  local pass_value="false"
  local delta
  delta=$(( API_TOTAL - DB_TOTAL ))
  if [[ "${API_TOTAL}" -eq "${DB_TOTAL}" ]]; then
    pass_value="true"
  fi

  SUMMARY_JSON=$(cat <<EOF
{"pass":${pass_value},"mode":"${MODE}","k8s_namespace":"${NAMESPACE}","filters":{"start_time":"${START_TIME}","end_time":"${END_TIME}","service_name":$([[ -n "${SERVICE_NAME}" ]] && printf '"%s"' "${SERVICE_NAME}" || printf 'null'),"namespace":$([[ -n "${NAMESPACE_FILTER}" ]] && printf '"%s"' "${NAMESPACE_FILTER}" || printf 'null'),"search":$([[ -n "${SEARCH}" ]] && printf '"%s"' "${SEARCH}" || printf 'null'),"exclude_health_check":$(to_bool "${EXCLUDE_HEALTH_CHECK}"),"limit":${LIMIT}},"api":{"total":${API_TOTAL},"pages":${API_PAGES},"first_page_count":${API_FIRST_PAGE_COUNT},"last_page_count":${API_LAST_PAGE_COUNT},"anchor_time":"${API_ANCHOR_TIME}","has_more_final":$(to_bool "${API_HAS_MORE_FINAL}")},"db":{"total":${DB_TOTAL}},"delta":${delta}}
EOF
)
}

main() {
  parse_args "$@"

  require_cmd kubectl

  if [[ -z "${START_TIME}" || -z "${END_TIME}" ]]; then
    echo "[ERROR] --start and --end are required" >&2
    usage
    exit 1
  fi

  if [[ "${MODE}" != "inpod" && "${MODE}" != "direct" ]]; then
    echo "[ERROR] --mode must be inpod or direct" >&2
    exit 1
  fi

  if [[ "${MODE}" == "direct" ]]; then
    require_cmd python3
    fetch_api_total_direct
  else
    fetch_api_total_inpod
  fi

  fetch_db_total
  parse_api_summary_kv
  build_summary_json

  if [[ -n "${OUTPUT_JSON}" ]]; then
    mkdir -p "$(dirname "${OUTPUT_JSON}")"
    printf '%s\n' "${SUMMARY_JSON}" > "${OUTPUT_JSON}"
    echo "[INFO] wrote summary json: ${OUTPUT_JSON}"
  fi

  local delta
  delta=$(( API_TOTAL - DB_TOTAL ))
  echo "[INFO] mode=${MODE} namespace=${NAMESPACE}"
  echo "[INFO] filters start=${START_TIME} end=${END_TIME} service=${SERVICE_NAME:-None} namespace=${NAMESPACE_FILTER:-None} search=${SEARCH:-None}"
  echo "[INFO] api_total=${API_TOTAL} pages=${API_PAGES} first_page=${API_FIRST_PAGE_COUNT} last_page=${API_LAST_PAGE_COUNT} anchor=${API_ANCHOR_TIME}"
  echo "[INFO] db_total=${DB_TOTAL} delta=${delta}"

  if [[ "${API_TOTAL}" -eq "${DB_TOTAL}" ]]; then
    echo "[RESULT] PASS"
  else
    echo "[RESULT] FAIL"
    exit 2
  fi
}

main "$@"
