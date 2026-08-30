#!/usr/bin/env bash
#
# Remove Network Log Server. Log data is preserved unless you explicitly ask
# for it to be deleted -- and then you have to type the word DELETE.
#
set -euo pipefail

APP_NAME="network-log-server"
APP_USER="netlog"
INSTALL_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
LOG_DIR="/var/log/${APP_NAME}"
BACKUP_DIR="/var/backups/${APP_NAME}"

if [[ -t 1 ]]; then BOLD=$'\033[1m'; RED=$'\033[31m'; RESET=$'\033[0m'
else BOLD=""; RED=""; RESET=""; fi

[[ ${EUID} -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }

ROWS="?"
if command -v clickhouse-client >/dev/null 2>&1 && [[ -r "${CONFIG_DIR}/.env" ]]; then
  # shellcheck disable=SC1090
  . "${CONFIG_DIR}/.env"
  ROWS="$(clickhouse-client --user netlog --password "${NLS_CLICKHOUSE_PASSWORD:-}" \
    --query "SELECT count() FROM network_logs.nat_logs" 2>/dev/null || echo '?')"
fi

cat <<EOM

${BOLD}${RED}WARNING${RESET}

This will remove the Network Log Server application:
  services, ${INSTALL_DIR}, the nginx site and /usr/local/bin/nls-admin

Your ClickHouse log data (${ROWS} rows in network_logs.nat_logs) can be:

  [x] Preserved   -- the default. ClickHouse and Redis stay installed,
                     the database is untouched, and re-running install.bash
                     brings everything back.
  [ ] Deleted     -- permanent. There is no undo.

EOM
read -r -p "Continue with uninstall? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted. Nothing was changed."; exit 0; }

DELETE_DATA=0
echo
read -r -p "Also permanently delete the log database and all configuration? [y/N] " reply
if [[ "$reply" =~ ^[Yy]$ ]]; then
  echo
  echo "${BOLD}This destroys ${ROWS} log rows and cannot be undone.${RESET}"
  read -r -p "Type DELETE in capitals to confirm: " confirm
  [[ "$confirm" == "DELETE" ]] && DELETE_DATA=1 || echo "Not confirmed — data will be preserved."
fi

echo
echo "==> Stopping and removing services"
for svc in "${APP_NAME}-receiver" "${APP_NAME}-worker" "${APP_NAME}-api" \
           "${APP_NAME}-maintenance.timer" "${APP_NAME}-maintenance"; do
  systemctl stop "$svc" 2>/dev/null || true
  systemctl disable "$svc" 2>/dev/null || true
  rm -f "/etc/systemd/system/${svc}"* 
done
systemctl daemon-reload

echo "==> Removing nginx site"
rm -f "/etc/nginx/sites-enabled/${APP_NAME}" "/etc/nginx/sites-available/${APP_NAME}"
nginx -t >/dev/null 2>&1 && systemctl reload nginx || true

echo "==> Removing application files"
rm -rf "$INSTALL_DIR" "${INSTALL_DIR}.rollback" /usr/local/bin/nls-admin

if (( DELETE_DATA )); then
  echo "==> Deleting log database"
  clickhouse-client --user netlog --password "${NLS_CLICKHOUSE_PASSWORD:-}" \
    --query "DROP DATABASE IF EXISTS network_logs" 2>/dev/null || true
  echo "==> Deleting configuration and data"
  rm -rf "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR" "$BACKUP_DIR"
  rm -f /etc/clickhouse-server/users.d/${APP_NAME}.xml \
        /etc/clickhouse-server/config.d/${APP_NAME}.xml \
        /etc/redis/redis.conf.d-nls.conf \
        /etc/sysctl.d/99-${APP_NAME}.conf
  sed -i "\|redis.conf.d-nls.conf|d" /etc/redis/redis.conf 2>/dev/null || true
  id -u "$APP_USER" >/dev/null 2>&1 && userdel "$APP_USER" 2>/dev/null || true
  echo
  echo "Removed. ClickHouse and Redis packages are still installed; remove them with:"
  echo "  apt-get purge clickhouse-server clickhouse-client redis-server"
else
  echo
  echo "Removed the application. Preserved:"
  echo "  ${CONFIG_DIR}   configuration and secrets"
  echo "  ${DATA_DIR}   metadata database and branding"
  echo "  ClickHouse database network_logs (${ROWS} rows)"
  echo
  echo "Re-run 'sudo bash install.bash' to bring it all back."
fi
