#!/usr/bin/env bash
# Restore a configuration backup produced by backup.bash.
set -euo pipefail

APP_NAME="network-log-server"
CONFIG_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
ARCHIVE="${1:-}"

[[ ${EUID} -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -f "$ARCHIVE" ]] || { echo "Usage: restore.bash /path/to/config-YYYYMMDD-HHMMSS.tar.gz" >&2; exit 1; }

cat <<WARN

This will replace:
  ${CONFIG_DIR}                    (configuration and secrets)
  ${DATA_DIR}/metadata.sqlite3     (users, routers, settings)
  ${DATA_DIR}/branding             (company logo)

Your ClickHouse log data is NOT touched.

WARN
read -r -p "Continue? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
tar -xzf "$ARCHIVE" -C "$TMP"

systemctl stop "${APP_NAME}-receiver" "${APP_NAME}-worker" "${APP_NAME}-api" || true

[[ -d "${TMP}/config" ]]   && { rm -rf "${CONFIG_DIR}"; cp -a "${TMP}/config" "${CONFIG_DIR}"; }
[[ -f "${TMP}/metadata.sqlite3" ]] && cp -a "${TMP}/metadata.sqlite3" "${DATA_DIR}/"
[[ -d "${TMP}/branding" ]] && { rm -rf "${DATA_DIR}/branding"; cp -a "${TMP}/branding" "${DATA_DIR}/"; }

chown -R netlog:netlog "${DATA_DIR}"
chown root:netlog "${CONFIG_DIR}" "${CONFIG_DIR}/.env" "${CONFIG_DIR}/log-server.yaml"
chmod 0750 "${CONFIG_DIR}"; chmod 0640 "${CONFIG_DIR}/.env" "${CONFIG_DIR}/log-server.yaml"

systemctl start "${APP_NAME}-api" "${APP_NAME}-worker" "${APP_NAME}-receiver"
echo "Restored. Verify with: scripts/health-check.bash"
