#!/usr/bin/env bash
#
# Network Log Server -- one-command installer for Ubuntu 24.04 LTS.
#
#   sudo bash install.bash
#
# Design rules this script follows:
#   * Idempotent. Running it twice never destroys logs, configuration or
#     credentials. Existing installs are offered an upgrade or a repair.
#   * Fail loud, fail early. `set -euo pipefail`, and every external service is
#     verified after it is configured rather than assumed to have worked.
#   * No secret is ever hard-coded. Passwords and keys are generated here.
#   * Everything is logged to /var/log/network-log-server-install.log.
#
set -euo pipefail

APP_NAME="network-log-server"
APP_USER="netlog"
APP_GROUP="netlog"
INSTALL_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
LOG_DIR="/var/log/${APP_NAME}"
BACKUP_DIR="/var/backups/${APP_NAME}"
INSTALL_LOG="/var/log/${APP_NAME}-install.log"
VENV_DIR="${INSTALL_DIR}/venv"
CREDENTIALS_FILE="${CONFIG_DIR}/initial-credentials.txt"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MIN_CORES=2
MIN_RAM_GB=4
MIN_DISK_GB=40
REC_CORES=8
REC_RAM_GB=16
REC_DISK_GB=500

TOTAL_STEPS=15
STEP=0
MODE="install"          # install | upgrade | repair

# --------------------------------------------------------------- output ----
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

log()  { printf '%s\n' "$*" | tee -a "$INSTALL_LOG" >/dev/null; }
say()  { printf '%s\n' "$*"; log "$*"; }
warn() { printf '%sWARN%s  %s\n' "$YELLOW" "$RESET" "$*"; log "WARN: $*"; }
die()  {
  printf '\n%sERROR%s  %s\n\n' "$RED" "$RESET" "$*" >&2
  log "ERROR: $*"
  printf 'Installation log: %s\n' "$INSTALL_LOG" >&2
  exit 1
}

step() {
  STEP=$((STEP + 1))
  printf '%s[%2d/%d]%s %-42s' "$BOLD" "$STEP" "$TOTAL_STEPS" "$RESET" "$1"
  log ""
  log "=== [$STEP/$TOTAL_STEPS] $1"
}
ok()   { printf '%sOK%s\n' "$GREEN" "$RESET"; }
skip() { printf '%sSKIPPED%s (%s)\n' "$DIM" "$RESET" "$1"; }

run() {
  log "+ $*"
  if ! "$@" >>"$INSTALL_LOG" 2>&1; then
    printf '%sFAILED%s\n' "$RED" "$RESET"
    die "Command failed: $*
Look at the last lines of $INSTALL_LOG for the reason."
  fi
}

banner() {
  cat <<'EOF'
==================================================
        NETWORK LOG SERVER  ·  INSTALLER
==================================================
EOF
}

# ------------------------------------------------------------ preflight ----
require_root() {
  [[ ${EUID} -eq 0 ]] || die "Please run this installer with sudo or as root:

    sudo bash install.bash"
}

check_os() {
  step "Checking Ubuntu 24.04"
  [[ -r /etc/os-release ]] || die "Cannot read /etc/os-release; this does not look like Ubuntu."
  # shellcheck disable=SC1091
  . /etc/os-release
  local detected="${NAME:-unknown} ${VERSION_ID:-unknown}"
  log "detected: $detected ($(uname -m))"

  if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
    if [[ "${NLS_ALLOW_UNSUPPORTED_OS:-0}" == "1" ]]; then
      printf '%sOVERRIDDEN%s\n' "$YELLOW" "$RESET"
      warn "Unsupported OS ($detected). Continuing because NLS_ALLOW_UNSUPPORTED_OS=1."
      return
    fi
    printf '%sFAILED%s\n' "$RED" "$RESET"
    die "This application supports Ubuntu 24.04 LTS.
Detected OS: $detected

Installation aborted.
Set NLS_ALLOW_UNSUPPORTED_OS=1 to override at your own risk."
  fi

  local arch; arch="$(uname -m)"
  [[ "$arch" == "x86_64" || "$arch" == "aarch64" ]] || \
    die "Unsupported CPU architecture: $arch (x86_64 or aarch64 required)."
  ok
}

check_resources() {
  step "Checking system resources"
  local cores ram_gb disk_gb
  cores="$(nproc)"
  ram_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
  disk_gb=$(( $(df -Pk /var | awk 'NR==2 {print $4}') / 1024 / 1024 ))

  log "cores=$cores ram=${ram_gb}G free_disk=${disk_gb}G"

  local problems=()
  (( cores  >= MIN_CORES ))  || problems+=("CPU cores: ${cores} (minimum ${MIN_CORES})")
  (( ram_gb >= MIN_RAM_GB )) || problems+=("RAM: ${ram_gb} GB (minimum ${MIN_RAM_GB} GB)")
  (( disk_gb >= MIN_DISK_GB )) || problems+=("Free disk on /var: ${disk_gb} GB (minimum ${MIN_DISK_GB} GB)")

  if (( ${#problems[@]} > 0 )); then
    printf '%sBELOW MINIMUM%s\n' "$RED" "$RESET"
    printf '\n'
    printf '  %s\n' "${problems[@]}"
    printf '\n  Recommended for production: %d cores, %d GB RAM, %d GB disk.\n\n' \
      "$REC_CORES" "$REC_RAM_GB" "$REC_DISK_GB"
    if [[ "${NLS_ASSUME_YES:-0}" != "1" ]]; then
      read -r -p "  Continue anyway? [y/N] " reply
      [[ "$reply" =~ ^[Yy]$ ]] || die "Installation aborted at your request."
    fi
  else
    ok
    printf '        %sCPU %s cores · RAM %s GB · free disk %s GB%s\n' \
      "$DIM" "$cores" "$ram_gb" "$disk_gb" "$RESET"
    if (( cores < REC_CORES || ram_gb < REC_RAM_GB )); then
      warn "Below the recommended ${REC_CORES} cores / ${REC_RAM_GB} GB for high-volume ISP traffic."
    fi
  fi

  # Ports the installer is about to claim.
  local busy=()
  for port in 80 8088 514; do
    if ss -Hlnt "sport = :${port}" 2>/dev/null | grep -q . || \
       ss -Hlnu "sport = :${port}" 2>/dev/null | grep -q .; then
      # Our own services holding the port on a re-run is fine.
      if ! systemctl is-active --quiet "${APP_NAME}-api" 2>/dev/null && \
         ! systemctl is-active --quiet nginx 2>/dev/null; then
        busy+=("$port")
      fi
    fi
  done
  (( ${#busy[@]} == 0 )) || warn "Ports already in use by another process: ${busy[*]}"
}

detect_existing() {
  [[ -d "$INSTALL_DIR" || -f "${CONFIG_DIR}/.env" ]] || return 0

  cat <<EOF

${BOLD}Existing Network Log Server installation detected.${RESET}

  Application : ${INSTALL_DIR}
  Config      : ${CONFIG_DIR}
  Data        : ${DATA_DIR}

Your ClickHouse log data and configuration will NOT be deleted by any option.

  1) Upgrade  -- update application files and dependencies, keep everything else
  2) Repair   -- re-apply services, nginx, permissions and database schema
  3) Abort

EOF
  local choice="${NLS_EXISTING_ACTION:-}"
  if [[ -z "$choice" ]]; then
    if [[ "${NLS_ASSUME_YES:-0}" == "1" ]]; then
      choice=1
    else
      read -r -p "Choose [1/2/3]: " choice
    fi
  fi
  case "$choice" in
    1) MODE="upgrade"; say "Mode: upgrade" ;;
    2) MODE="repair";  say "Mode: repair" ;;
    *) say "Aborted. Nothing was changed."; exit 0 ;;
  esac
}

# -------------------------------------------------------------- packages ----
install_packages() {
  step "Installing system packages"
  export DEBIAN_FRONTEND=noninteractive
  run apt-get update -o Acquire::Retries=3
  run apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg lsb-release apt-transport-https \
    python3 python3-venv python3-dev build-essential pkg-config \
    libffi-dev nginx redis-server jq unzip tar zstd \
    iproute2 ufw acl
  ok
}

install_clickhouse() {
  step "Installing ClickHouse"
  if command -v clickhouse-server >/dev/null 2>&1; then
    skip "already installed"
  else
    local keyring=/usr/share/keyrings/clickhouse-keyring.gpg
    run bash -c "curl -fsSL 'https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key' \
        | gpg --dearmor --yes -o ${keyring} || \
      curl -fsSL 'https://packages.clickhouse.com/deb/pubkey.gpg' \
        | gpg --dearmor --yes -o ${keyring}"
    run chmod 644 "$keyring"
    echo "deb [signed-by=${keyring}] https://packages.clickhouse.com/deb stable main" \
      > /etc/apt/sources.list.d/clickhouse.list
    run apt-get update -o Acquire::Retries=3
    # The package prompts for a default-user password unless preseeded.
    run bash -c "echo 'clickhouse-server clickhouse-server/default-password password' | debconf-set-selections"
    run apt-get install -y --no-install-recommends clickhouse-server clickhouse-client
    ok
  fi
}

# --------------------------------------------------------------- secrets ----
gen_secret() { openssl rand -hex 32 2>/dev/null || head -c 48 /dev/urandom | base64 | tr -d '/+=' ; }

load_or_create_secrets() {
  # Reuse existing secrets on upgrade/repair. Regenerating them would lock the
  # operator out of ClickHouse and invalidate every active session.
  if [[ -f "${CONFIG_DIR}/.env" ]]; then
    # shellcheck disable=SC1090
    set -a; . "${CONFIG_DIR}/.env"; set +a
  fi
  NLS_SECRET_KEY="${NLS_SECRET_KEY:-$(gen_secret)}"
  NLS_CLICKHOUSE_PASSWORD="${NLS_CLICKHOUSE_PASSWORD:-$(gen_secret)}"
  NLS_REDIS_PASSWORD="${NLS_REDIS_PASSWORD:-$(gen_secret)}"
}

# ----------------------------------------------------------- application ----
create_user_and_dirs() {
  step "Creating service account and directories"
  if ! getent group "$APP_GROUP" >/dev/null; then run groupadd --system "$APP_GROUP"; fi
  if ! id -u "$APP_USER" >/dev/null 2>&1; then
    run useradd --system --gid "$APP_GROUP" --home-dir "$DATA_DIR" \
        --shell /usr/sbin/nologin --comment "Network Log Server" "$APP_USER"
  fi

  for dir in "$INSTALL_DIR" "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR" \
             "$BACKUP_DIR" "${BACKUP_DIR}/archive" "${DATA_DIR}/branding"; do
    run install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$dir"
  done
  # Config is readable by the service but writable only by root.
  run chown root:"$APP_GROUP" "$CONFIG_DIR"
  run chmod 0750 "$CONFIG_DIR"
  ok
}

install_application() {
  step "Installing application files"
  run install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "${INSTALL_DIR}"
  for item in backend frontend database scripts systemd nginx docs; do
    [[ -e "${SRC_DIR}/${item}" ]] || continue
    run rm -rf "${INSTALL_DIR}/${item}"
    run cp -a "${SRC_DIR}/${item}" "${INSTALL_DIR}/"
  done
  for item in README.md LICENSE CHANGELOG.md VERSION; do
    [[ -f "${SRC_DIR}/${item}" ]] && run cp -a "${SRC_DIR}/${item}" "${INSTALL_DIR}/"
  done
  run chown -R "$APP_USER":"$APP_GROUP" "$INSTALL_DIR"
  run chmod -R go-w "$INSTALL_DIR"

  if [[ ! -d "$VENV_DIR" ]]; then
    run python3 -m venv "$VENV_DIR"
  fi
  run "${VENV_DIR}/bin/pip" install --upgrade pip wheel setuptools
  run "${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/backend/requirements.txt"
  # uvloop is a large throughput win for the receiver but must never be a
  # hard requirement: the services fall back to the stock event loop.
  "${VENV_DIR}/bin/pip" install uvloop >>"$INSTALL_LOG" 2>&1 || \
    warn "uvloop could not be installed; the receiver will use the default event loop."
  run chown -R "$APP_USER":"$APP_GROUP" "$VENV_DIR"

  # Console entry points.
  cat > /usr/local/bin/nls-admin <<EOF
#!/bin/sh
exec env NLS_CONFIG_DIR="${CONFIG_DIR}" PYTHONPATH="${INSTALL_DIR}/backend" \\
  "${VENV_DIR}/bin/python" -m app.cli "\$@"
EOF
  run chmod 0755 /usr/local/bin/nls-admin
  ok
}

write_configuration() {
  step "Writing configuration"
  if [[ ! -f "${CONFIG_DIR}/log-server.yaml" ]]; then
    local cores workers
    cores="$(nproc)"
    workers=$(( cores / 2 )); (( workers < 1 )) && workers=1
    cat > "${CONFIG_DIR}/log-server.yaml" <<EOF
# Network Log Server configuration.
# Secrets live in .env, never here. Restart the services after editing:
#   systemctl restart ${APP_NAME}-receiver ${APP_NAME}-worker ${APP_NAME}-api

log_level: INFO

server:
  bind: 127.0.0.1        # nginx is the only thing that should reach the API
  port: 8088
  workers: 2

receiver:
  udp_enabled: true
  tcp_enabled: true
  bind_address: 0.0.0.0
  udp_port: 514
  tcp_port: 514
  workers: ${workers}    # SO_REUSEPORT processes; 0 = auto (cores / 2)
  so_rcvbuf: 16777216    # raise net.core.rmem_max to match
  push_batch: 500
  push_interval_ms: 200
  router_refresh_seconds: 15

parser:
  require_nat: true              # drop forward logs with no NAT translation
  subscriber_from_interface: false

redis:
  host: 127.0.0.1
  port: 6379
  db: 0
  queue_key: "nls:queue"
  max_queue_length: 5000000      # shed load past this rather than OOM Redis

clickhouse:
  host: 127.0.0.1
  port: 8123
  user: netlog
  database: network_logs
  table: nat_logs
  batch_size: 20000
  batch_interval_ms: 1000
  max_execution_time: 30
  max_result_rows: 10000

retention:
  hot_days: 30
  retention_months: 12
  archive_enabled: true
  archive_dir: ${BACKUP_DIR}/archive
  drop_after_archive: false      # true only once you trust your archives

auth:
  session_hours: 12
  cookie_secure: false           # set true after putting HTTPS in front
  max_failed_logins: 5
  lockout_seconds: 300

paths:
  data_dir: ${DATA_DIR}
  log_dir: ${LOG_DIR}
  metadata_db: ${DATA_DIR}/metadata.sqlite3
  logo_dir: ${DATA_DIR}/branding
EOF
  fi
  run chown root:"$APP_GROUP" "${CONFIG_DIR}/log-server.yaml"
  run chmod 0640 "${CONFIG_DIR}/log-server.yaml"

  # umask runs in a subshell: a bare `umask` is process-global and would
  # otherwise leak into every file created later in this script -- which is
  # exactly how the Redis drop-in below once ended up unreadable by Redis.
  ( umask 077
    cat > "${CONFIG_DIR}/.env" <<EOF
# Generated by install.bash. Do not commit this file.
NLS_SECRET_KEY=${NLS_SECRET_KEY}
NLS_CLICKHOUSE_PASSWORD=${NLS_CLICKHOUSE_PASSWORD}
NLS_REDIS_PASSWORD=${NLS_REDIS_PASSWORD}
EOF
  )
  run chown root:"$APP_GROUP" "${CONFIG_DIR}/.env"
  run chmod 0640 "${CONFIG_DIR}/.env"
  ok
}

# ----------------------------------------------------------------- redis ----
configure_redis() {
  step "Configuring Redis"
  local conf=/etc/redis/redis.conf
  [[ -f "$conf" ]] || die "Redis configuration not found at $conf"
  [[ -f "${conf}.nls-backup" ]] || run cp "$conf" "${conf}.nls-backup"

  # Drop-in rather than rewriting the distro file.
  cat > /etc/redis/redis.conf.d-nls.conf <<EOF
# Managed by ${APP_NAME}. Edited by hand? Your changes survive upgrades.
bind 127.0.0.1 -::1
protected-mode yes
requirepass ${NLS_REDIS_PASSWORD}

# The queue is a durability boundary: if the box loses power we want at most
# one second of buffered logs gone, not the whole backlog.
appendonly yes
appendfsync everysec

# Never evict queued logs to make room. If Redis fills up we want writes to
# fail loudly so the receiver counts the drop, not silent data loss.
maxmemory-policy noeviction

save 900 1
stop-writes-on-bgsave-error no
tcp-keepalive 60
EOF
  # This file holds the Redis password, so it must not be world-readable --
  # but Redis itself has to be able to read its own config. Set both
  # explicitly rather than trusting whatever umask happens to be in effect:
  # if Redis cannot read an included file it refuses to start.
  run chown root:redis /etc/redis/redis.conf.d-nls.conf
  run chmod 0640 /etc/redis/redis.conf.d-nls.conf

  if ! grep -q 'redis.conf.d-nls.conf' "$conf"; then
    echo "include /etc/redis/redis.conf.d-nls.conf" >> "$conf"
  fi

  # Redis warns loudly and drops connections under load without this.
  if ! grep -q '^vm.overcommit_memory' /etc/sysctl.d/99-${APP_NAME}.conf 2>/dev/null; then
    cat > /etc/sysctl.d/99-${APP_NAME}.conf <<'EOF'
# Network Log Server tuning
vm.overcommit_memory = 1
net.core.rmem_max = 33554432
net.core.rmem_default = 16777216
net.core.netdev_max_backlog = 5000
EOF
    run sysctl -q --system
  fi

  run systemctl enable redis-server
  run systemctl restart redis-server
  sleep 1
  redis-cli -a "${NLS_REDIS_PASSWORD}" --no-auth-warning ping 2>/dev/null | grep -q PONG \
    || die "Redis did not answer after configuration. Check: systemctl status redis-server"
  ok
}

# ------------------------------------------------------------ clickhouse ----
configure_clickhouse() {
  step "Configuring ClickHouse"
  mkdir -p /etc/clickhouse-server/config.d /etc/clickhouse-server/users.d

  cat > /etc/clickhouse-server/config.d/${APP_NAME}.xml <<'EOF'
<clickhouse>
    <!-- Managed by network-log-server. -->
    <listen_host>127.0.0.1</listen_host>
    <logger><level>warning</level></logger>
    <!-- Log ingestion is append-heavy and read-light. A large uncompressed
         cache buys nothing here; the mark cache does the work. -->
    <mark_cache_size>2147483648</mark_cache_size>
    <merge_tree>
        <!-- Batches arrive from one worker at a time; the default part limit
             is tuned for many concurrent writers and trips too early. -->
        <parts_to_delay_insert>600</parts_to_delay_insert>
        <parts_to_throw_insert>1200</parts_to_throw_insert>
        <max_suspicious_broken_parts>10</max_suspicious_broken_parts>
    </merge_tree>
</clickhouse>
EOF

  cat > /etc/clickhouse-server/users.d/${APP_NAME}.xml <<EOF
<clickhouse>
    <users>
        <netlog>
            <password>${NLS_CLICKHOUSE_PASSWORD}</password>
            <networks><ip>127.0.0.1</ip><ip>::1</ip></networks>
            <profile>netlog</profile>
            <quota>default</quota>
            <access_management>0</access_management>
        </netlog>
    </users>
    <profiles>
        <netlog>
            <max_memory_usage>4000000000</max_memory_usage>
            <max_execution_time>60</max_execution_time>
            <max_threads>8</max_threads>
            <use_uncompressed_cache>0</use_uncompressed_cache>
            <load_balancing>random</load_balancing>
        </netlog>
    </profiles>
</clickhouse>
EOF
  chmod 0640 /etc/clickhouse-server/users.d/${APP_NAME}.xml
  chown root:clickhouse /etc/clickhouse-server/users.d/${APP_NAME}.xml 2>/dev/null || true

  run systemctl enable clickhouse-server
  run systemctl restart clickhouse-server

  printf 'waiting'
  local i
  for i in $(seq 1 60); do
    if clickhouse-client --user netlog --password "${NLS_CLICKHOUSE_PASSWORD}" \
         --query "SELECT 1" >/dev/null 2>&1; then
      printf '\r%-42s' ""
      printf '\r%s[%2d/%d]%s %-42s' "$BOLD" "$STEP" "$TOTAL_STEPS" "$RESET" "Configuring ClickHouse"
      ok
      return
    fi
    printf '.'
    sleep 1
  done
  printf '\n'
  die "ClickHouse did not become reachable within 60 seconds.

Check:
  systemctl status clickhouse-server
  journalctl -u clickhouse-server -n 50"
}

initialise_database() {
  step "Creating database schema"
  run nls-admin init-db --schema "${INSTALL_DIR}/database/schema/clickhouse.sql"
  run chown -R "$APP_USER":"$APP_GROUP" "$DATA_DIR"
  ok
}

create_admin_account() {
  step "Creating administrator account"
  if [[ "$MODE" != "install" ]] && nls-admin list-routers >/dev/null 2>&1 \
     && [[ -f "$CREDENTIALS_FILE" || -f "${DATA_DIR}/metadata.sqlite3" ]]; then
    # An upgrade must not reset the operator's password.
    if "${VENV_DIR}/bin/python" - <<PYEOF >/dev/null 2>&1
import os, sys
os.environ["NLS_CONFIG_DIR"] = "${CONFIG_DIR}"
sys.path.insert(0, "${INSTALL_DIR}/backend")
from app.config import get_config
from app.database.meta import MetadataStore
sys.exit(0 if MetadataStore(get_config().paths.metadata_db).user_count() else 1)
PYEOF
    then
      skip "administrator already exists"
      return
    fi
  fi
  ADMIN_PASSWORD="$(nls-admin create-admin admin --random 2>>"$INSTALL_LOG" | tail -n 1)"
  [[ -n "$ADMIN_PASSWORD" ]] || die "Could not create the administrator account. See $INSTALL_LOG"

  ( umask 077
    cat > "$CREDENTIALS_FILE" <<EOF
Network Log Server -- initial credentials
Generated $(date -Is)

  URL      : http://$(hostname -I | awk '{print $1}')/
  Username : admin
  Password : ${ADMIN_PASSWORD}

Change this password after the first sign-in, then delete this file.
EOF
  )
  run chown root:root "$CREDENTIALS_FILE"
  run chmod 0600 "$CREDENTIALS_FILE"
  run chown -R "$APP_USER":"$APP_GROUP" "$DATA_DIR"
  ok
}

# --------------------------------------------------------------- systemd ----
install_services() {
  step "Installing system services"
  for unit in "${INSTALL_DIR}"/systemd/*.service "${INSTALL_DIR}"/systemd/*.timer; do
    [[ -e "$unit" ]] || continue
    run install -m 0644 "$unit" /etc/systemd/system/
  done
  run systemctl daemon-reload
  run systemctl enable "${APP_NAME}-receiver" "${APP_NAME}-worker" "${APP_NAME}-api" \
      "${APP_NAME}-maintenance.timer"
  run systemctl restart "${APP_NAME}-api" "${APP_NAME}-worker" "${APP_NAME}-receiver"
  run systemctl restart "${APP_NAME}-maintenance.timer"
  ok
}

configure_nginx() {
  step "Configuring Nginx"
  run install -m 0644 "${INSTALL_DIR}/nginx/${APP_NAME}.conf" \
      "/etc/nginx/sites-available/${APP_NAME}"
  run ln -sf "/etc/nginx/sites-available/${APP_NAME}" "/etc/nginx/sites-enabled/${APP_NAME}"
  # The Ubuntu default vhost is also a catch-all on :80 and would shadow us.
  [[ -L /etc/nginx/sites-enabled/default ]] && run rm -f /etc/nginx/sites-enabled/default
  # nginx must be able to traverse to the static files.
  run setfacl -m u:www-data:rx "$INSTALL_DIR"
  run setfacl -R -m u:www-data:rX "${INSTALL_DIR}/frontend"
  run nginx -t
  run systemctl enable nginx
  run systemctl reload-or-restart nginx
  ok
}

configure_firewall() {
  step "Configuring firewall"
  if ! command -v ufw >/dev/null 2>&1; then
    skip "ufw not installed"
    return
  fi
  if ! ufw status 2>/dev/null | head -1 | grep -qi active; then
    skip "ufw inactive — no rules changed"
    warn "ufw is inactive. Restrict UDP/TCP 514 to your routers before going live."
    return
  fi
  run ufw allow 22/tcp
  run ufw allow 80/tcp
  run ufw allow 443/tcp
  # Syslog is deliberately not opened to the world: the operator scopes it to
  # their routers. Application-level authorisation still applies regardless.
  warn "Syslog ports were NOT opened globally. For each router, run:
         ufw allow from <ROUTER_IP> to any port 514 proto udp
         ufw allow from <ROUTER_IP> to any port 514 proto tcp"
  ok
}

# ---------------------------------------------------------- health check ----
health_check() {
  step "Running health checks"
  printf '\n'
  local failures=0

  check() {
    local label="$1"; shift
    printf '        %-32s' "$label"
    if "$@" >>"$INSTALL_LOG" 2>&1; then
      printf '%sOK%s\n' "$GREEN" "$RESET"
    else
      printf '%sFAILED%s\n' "$RED" "$RESET"
      failures=$((failures + 1))
    fi
  }

  check "Redis"            bash -c "redis-cli -a '${NLS_REDIS_PASSWORD}' --no-auth-warning ping | grep -q PONG"
  check "ClickHouse"       bash -c "clickhouse-client --user netlog --password '${NLS_CLICKHOUSE_PASSWORD}' --query 'SELECT 1' | grep -q 1"
  check "Database table"   bash -c "clickhouse-client --user netlog --password '${NLS_CLICKHOUSE_PASSWORD}' --query 'SELECT count() FROM network_logs.nat_logs' >/dev/null"
  check "Receiver service" systemctl is-active --quiet "${APP_NAME}-receiver"
  check "Worker service"   systemctl is-active --quiet "${APP_NAME}-worker"
  check "API service"      systemctl is-active --quiet "${APP_NAME}-api"
  check "Maintenance timer" systemctl is-active --quiet "${APP_NAME}-maintenance.timer"
  check "Nginx"            systemctl is-active --quiet nginx

  # Give the API a moment; uvicorn workers take a beat to bind.
  local i
  for i in $(seq 1 15); do
    curl -fsS --max-time 2 http://127.0.0.1:8088/api/health >/dev/null 2>&1 && break
    sleep 1
  done
  check "Backend API"      curl -fsS --max-time 5 http://127.0.0.1:8088/api/health
  check "Web interface"    curl -fsS --max-time 5 -o /dev/null http://127.0.0.1/
  check "Syslog UDP 514"   bash -c "ss -Hlnu 'sport = :514' | grep -q ."
  check "Syslog TCP 514"   bash -c "ss -Hlnt 'sport = :514' | grep -q ."

  printf '\n'
  if (( failures > 0 )); then
    die "${failures} health check(s) failed.

Investigate with:
  systemctl status ${APP_NAME}-receiver ${APP_NAME}-worker ${APP_NAME}-api
  journalctl -u ${APP_NAME}-api -n 50
  tail -n 50 ${INSTALL_LOG}"
  fi
  printf '%s[%2d/%d]%s %-42s' "$BOLD" "$STEP" "$TOTAL_STEPS" "$RESET" "Health checks"
  ok
}

summary() {
  local ip; ip="$(hostname -I | awk '{print $1}')"
  cat <<EOF

==================================================
      INSTALLATION COMPLETED SUCCESSFULLY
==================================================

  Web interface
    http://${ip}/

EOF
  if [[ -n "${ADMIN_PASSWORD:-}" ]]; then
    cat <<EOF
  Username
    admin

  Temporary password
    ${ADMIN_PASSWORD}

  ${BOLD}Change this password after your first sign-in.${RESET}
  Also saved to ${CREDENTIALS_FILE} (root only).

EOF
  else
    cat <<EOF
  Your existing administrator account and password are unchanged.

EOF
  fi
  cat <<EOF
  Syslog listeners
    UDP 514, TCP 514

  Next steps
    1. Sign in and change the password.
    2. Add your routers under the Routers tab.
       Logs from any address not listed there are discarded.
    3. Point RouterOS at this server:
         /system logging action set remote remote=${ip} remote-port=514
         /system logging add topics=firewall action=remote
       Full instructions: ${INSTALL_DIR}/docs/mikrotik.md
    4. Restrict the syslog port to your routers:
         ufw allow from <ROUTER_IP> to any port 514 proto udp

  Useful commands
    nls-admin status
    nls-admin list-routers
    systemctl status ${APP_NAME}-receiver
    journalctl -u ${APP_NAME}-worker -f

  Installation log
    ${INSTALL_LOG}

==================================================
EOF
}

# ------------------------------------------------------------------ main ----
main() {
  require_root
  touch "$INSTALL_LOG"; chmod 0600 "$INSTALL_LOG"
  log "=== Network Log Server install started $(date -Is) ==="

  banner
  detect_existing
  printf '\n'

  check_os
  check_resources
  install_packages
  install_clickhouse
  load_or_create_secrets
  create_user_and_dirs
  install_application
  write_configuration
  configure_redis
  configure_clickhouse
  initialise_database
  create_admin_account
  install_services
  configure_nginx
  configure_firewall
  health_check

  summary
  log "=== install finished $(date -Is) mode=$MODE ==="
}

main "$@"
