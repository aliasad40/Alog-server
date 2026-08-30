#!/usr/bin/env bash
# Run the monthly archive immediately instead of waiting for the timer.
set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
exec systemctl start --wait network-log-server-maintenance.service
