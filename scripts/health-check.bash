#!/usr/bin/env bash
# Standalone health check. Exit 0 when everything is up.
# Safe to run from cron/Nagios/Zabbix:  scripts/health-check.bash --quiet
set -uo pipefail

APP_NAME="network-log-server"
CONFIG_DIR="/etc/${APP_NAME}"
QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

[[ -r "${CONFIG_DIR}/.env" ]] && { set -a; . "${CONFIG_DIR}/.env"; set +a; }

failures=0
report() {
  local label="$1" status="$2" detail="${3:-}"
  if [[ "$status" != "OK" ]]; then failures=$((failures+1)); fi
  (( QUIET )) && [[ "$status" == "OK" ]] && return
  printf '%-28s %-8s %s\n' "$label" "$status" "$detail"
}

for svc in "${APP_NAME}-receiver" "${APP_NAME}-worker" "${APP_NAME}-api" nginx \
           redis-server clickhouse-server; do
  if systemctl is-active --quiet "$svc"; then
    report "$svc" OK
  else
    report "$svc" FAILED "systemctl status $svc"
  fi
done

if redis-cli -a "${NLS_REDIS_PASSWORD:-}" --no-auth-warning ping 2>/dev/null | grep -q PONG; then
  depth=$(redis-cli -a "${NLS_REDIS_PASSWORD:-}" --no-auth-warning llen nls:queue 2>/dev/null)
  report "redis queue" OK "${depth:-0} pending"
else
  report "redis queue" FAILED "cannot reach redis"
fi

if curl -fsS --max-time 5 http://127.0.0.1:8088/api/health >/dev/null 2>&1; then
  report "api health" OK
else
  report "api health" FAILED "curl http://127.0.0.1:8088/api/health"
fi

rows=$(clickhouse-client --user netlog --password "${NLS_CLICKHOUSE_PASSWORD:-}" \
       --query "SELECT count() FROM network_logs.nat_logs" 2>/dev/null)
if [[ -n "$rows" ]]; then
  report "clickhouse" OK "${rows} rows"
else
  report "clickhouse" FAILED "query failed"
fi

usage=$(df -P /var/lib/${APP_NAME} 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
if [[ -n "$usage" ]] && (( usage < 90 )); then
  report "disk" OK "${usage}% used"
else
  report "disk" FAILED "${usage:-?}% used"
fi

(( QUIET )) || printf '\n%s\n' "$( ((failures)) && echo "${failures} check(s) failed" || echo "All checks passed" )"
exit $(( failures > 0 ))
