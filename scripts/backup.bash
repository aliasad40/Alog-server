#!/usr/bin/env bash
#
# Back up everything that is NOT the log database: configuration, the metadata
# database (routers, users, settings) and branding assets.
#
# The ClickHouse log data is deliberately excluded. At ISP volume it is far too
# large for a tar file, and copying its data directory while the server is
# running produces a corrupt copy. Use `nls-admin archive` for monthly exports,
# or ClickHouse BACKUP for a full copy. See docs/database.md.
#
set -euo pipefail

APP_NAME="network-log-server"
CONFIG_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
BACKUP_DIR="${1:-/var/backups/${APP_NAME}}"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="${BACKUP_DIR}/config-${STAMP}.tar.gz"

[[ ${EUID} -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
mkdir -p "$BACKUP_DIR"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# SQLite is in WAL mode; .backup takes a consistent snapshot of a live database
# where a plain file copy would not.
if [[ -f "${DATA_DIR}/metadata.sqlite3" ]]; then
  sqlite3 "${DATA_DIR}/metadata.sqlite3" ".backup '${TMP}/metadata.sqlite3'" 2>/dev/null \
    || cp "${DATA_DIR}/metadata.sqlite3" "${TMP}/metadata.sqlite3"
fi
[[ -d "${DATA_DIR}/branding" ]] && cp -a "${DATA_DIR}/branding" "$TMP/"
cp -a "${CONFIG_DIR}" "${TMP}/config"

tar -czf "$TARGET" -C "$TMP" .
chmod 0600 "$TARGET"

# Keep the last 30; these are small.
ls -1t "${BACKUP_DIR}"/config-*.tar.gz 2>/dev/null | tail -n +31 | xargs -r rm -f

echo "Backup written: ${TARGET} ($(du -h "$TARGET" | cut -f1))"
echo
echo "This backup contains configuration, users, routers and branding."
echo "It does NOT contain the ClickHouse log data — see docs/database.md."
