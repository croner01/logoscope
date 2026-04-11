#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage: DB_PROFILE=single|ha|auto $0 <command> [args]

Commands:
  status                 Show clickhouse + redis HA status
  check                  Run clickhouse + redis consistency checks
  bootstrap-clickhouse   Apply clickhouse schema by profile
  sync-clickhouse [...]  Run clickhouse replica sync for tables (ha only)
  promote-redis <pod>    Promote redis replica and patch service (ha only)
  restart-clickhouse     Rolling restart clickhouse with checks
  restart-redis          Rolling restart redis with checks
EOF
}

cmd="${1:-}"
shift || true

case "$cmd" in
  status)
    "$SCRIPT_DIR/clickhouse-ha-control.sh" status
    "$SCRIPT_DIR/redis-ha-control.sh" status
    ;;
  check)
    "$SCRIPT_DIR/clickhouse-ha-control.sh" check
    "$SCRIPT_DIR/redis-ha-control.sh" check
    ;;
  bootstrap-clickhouse)
    "$SCRIPT_DIR/clickhouse-ha-control.sh" bootstrap
    ;;
  sync-clickhouse)
    "$SCRIPT_DIR/clickhouse-ha-control.sh" sync "$@"
    ;;
  promote-redis)
    "$SCRIPT_DIR/redis-ha-control.sh" promote "$@"
    ;;
  restart-clickhouse)
    "$SCRIPT_DIR/clickhouse-ha-control.sh" rolling-restart
    ;;
  restart-redis)
    "$SCRIPT_DIR/redis-ha-control.sh" rolling-restart
    ;;
  *)
    usage
    exit 1
    ;;
esac
