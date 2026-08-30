#!/bin/sh
# Wait for dependencies, initialise the schema, then exec the requested role.
set -e

echo "waiting for ClickHouse..."
until python -c "
import sys
from app.database.clickhouse import ClickHouseService
from app.config import get_config
sys.exit(0 if ClickHouseService(get_config()).ping() else 1)" 2>/dev/null; do
  sleep 2
done

if [ "${NLS_ROLE}" = "api" ]; then
  python -m app.cli init-db --schema /app/database/schema/clickhouse.sql
  if [ -n "${NLS_ADMIN_PASSWORD}" ]; then
    python -m app.cli create-admin admin --password "${NLS_ADMIN_PASSWORD}" || true
  fi
fi

case "${NLS_ROLE}" in
  receiver) exec python -m app.services.receiver ;;
  worker)   exec python -m app.services.worker ;;
  *)        exec uvicorn app.api.app:get_app --factory --host 0.0.0.0 --port 8088 ;;
esac
