#!/usr/bin/env bash
#
# Upgrade an existing Network Log Server installation in place.
#
#   cd network-log-server && git pull && sudo bash update.bash
#
# Log data, configuration, users and routers are preserved. If anything fails,
# the previous application directory is still on disk as .rollback so you can
# put it back by hand.
#
set -euo pipefail

APP_NAME="network-log-server"
APP_USER="netlog"
INSTALL_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
VENV_DIR="${INSTALL_DIR}/venv"
BACKUP_DIR="/var/backups/${APP_NAME}"
LOG="/var/log/${APP_NAME}-update.log"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SERVICES=("${APP_NAME}-receiver" "${APP_NAME}-worker" "${APP_NAME}-api")

if [[ -t 1 ]]; then BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
else BOLD=""; GREEN=""; RED=""; RESET=""; fi

say() { printf '%s\n' "$*" | tee -a "$LOG"; }
die() { printf '\n%sERROR%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }
run() { echo "+ $*" >>"$LOG"; "$@" >>"$LOG" 2>&1 || die "Failed: $*  (see $LOG)"; }
step() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$1"; echo "=== $1" >>"$LOG"; }

[[ ${EUID} -eq 0 ]] || die "Run with sudo."
[[ -d "$INSTALL_DIR" ]] || die "No installation found at ${INSTALL_DIR}. Run install.bash first."

touch "$LOG"; chmod 0600 "$LOG"
say "=== update started $(date -Is) ==="

step "Backing up configuration and metadata"
run bash "${SRC_DIR}/scripts/backup.bash" "$BACKUP_DIR"

step "Stopping services"
# The receiver stops first so nothing new is accepted, then the worker drains.
for svc in "${SERVICES[@]}"; do systemctl stop "$svc" || true; done

step "Replacing application files"
ROLLBACK="${INSTALL_DIR}.rollback"
run rm -rf "$ROLLBACK"
run mkdir -p "$ROLLBACK"
for item in backend frontend database scripts systemd nginx docs; do
  [[ -e "${INSTALL_DIR}/${item}" ]] && run cp -a "${INSTALL_DIR}/${item}" "${ROLLBACK}/"
done
for item in backend frontend database scripts systemd nginx docs; do
  [[ -e "${SRC_DIR}/${item}" ]] || continue
  run rm -rf "${INSTALL_DIR}/${item}"
  run cp -a "${SRC_DIR}/${item}" "${INSTALL_DIR}/"
done
for item in README.md LICENSE CHANGELOG.md VERSION; do
  [[ -f "${SRC_DIR}/${item}" ]] && run cp -a "${SRC_DIR}/${item}" "${INSTALL_DIR}/"
done
run chown -R "${APP_USER}":"${APP_USER}" "$INSTALL_DIR"

step "Updating dependencies"
run "${VENV_DIR}/bin/pip" install --upgrade pip wheel
run "${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/backend/requirements.txt"
"${VENV_DIR}/bin/pip" install --upgrade uvloop >>"$LOG" 2>&1 || true
run chown -R "${APP_USER}":"${APP_USER}" "$VENV_DIR"

step "Applying database migrations"
# init-db uses CREATE ... IF NOT EXISTS throughout: safe on a populated table.
run nls-admin init-db --schema "${INSTALL_DIR}/database/schema/clickhouse.sql"
for migration in "${INSTALL_DIR}"/database/migrations/*.sql; do
  [[ -e "$migration" ]] || continue
  say "  applying $(basename "$migration")"
  run bash -c "clickhouse-client --user netlog \
    --password \"\$(grep -oP '(?<=^NLS_CLICKHOUSE_PASSWORD=).*' ${CONFIG_DIR}/.env)\" \
    --multiquery < '$migration'"
done

step "Reinstalling services"
for unit in "${INSTALL_DIR}"/systemd/*.service "${INSTALL_DIR}"/systemd/*.timer; do
  [[ -e "$unit" ]] && run install -m 0644 "$unit" /etc/systemd/system/
done
run systemctl daemon-reload
run install -m 0644 "${INSTALL_DIR}/nginx/${APP_NAME}.conf" "/etc/nginx/sites-available/${APP_NAME}"
run setfacl -R -m u:www-data:rX "${INSTALL_DIR}/frontend"
run nginx -t
run systemctl reload nginx

step "Starting services"
for svc in "${SERVICES[@]}"; do run systemctl start "$svc"; done
run systemctl restart "${APP_NAME}-maintenance.timer"
sleep 4

step "Health check"
if bash "${INSTALL_DIR}/scripts/health-check.bash"; then
  run rm -rf "$ROLLBACK"
  printf '\n%sUpdate completed successfully.%s\n\n' "$GREEN" "$RESET"
else
  die "Health checks failed after the update.

The previous application files are at ${ROLLBACK}.
To roll back:
  systemctl stop ${SERVICES[*]}
  rm -rf ${INSTALL_DIR}/backend ${INSTALL_DIR}/frontend
  cp -a ${ROLLBACK}/* ${INSTALL_DIR}/
  systemctl start ${SERVICES[*]}"
fi
say "=== update finished $(date -Is) ==="
