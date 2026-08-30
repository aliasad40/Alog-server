# Installation

## Requirements

|  | Minimum | Recommended |
|---|---|---|
| OS | Ubuntu 24.04 LTS | Ubuntu 24.04 LTS |
| CPU | 2 cores | 8+ cores |
| RAM | 4 GB | 16+ GB |
| Disk | 40 GB | 500 GB+ SSD/NVMe |
| Arch | x86_64 or aarch64 | x86_64 |

Disk is the one to think hardest about. Run for a day, then check
`nls-admin status` for bytes per row and multiply by your expected volume and
retention.

## Install

```bash
git clone <repository-url>
cd network-log-server
sudo bash install.bash
```

The installer prints a temporary admin password at the end and also writes it
to `/etc/network-log-server/initial-credentials.txt` (root-readable only).

It handles: OS and resource checks, all system packages, ClickHouse, Redis, the
Python environment, configuration with generated secrets, the database schema,
the admin account, systemd services, nginx and a full health check.

### Non-interactive install

```bash
sudo NLS_ASSUME_YES=1 bash install.bash
```

### Re-running it

Running `install.bash` again on an existing installation offers **Upgrade**,
**Repair** or **Abort**. It never drops the ClickHouse database and never
regenerates secrets — that would lock you out of your own database.

## After installing

1. Open `http://<server-ip>/` and sign in as `admin`.
2. Change the password (Settings → Change your password).
3. Add your routers under the **Routers** tab. Until you do, every incoming log
   is discarded.
4. Configure the routers — see `docs/mikrotik.md`.
5. Restrict port 514 to those routers:
   ```bash
   sudo ufw allow from 10.10.10.1 to any port 514 proto udp
   ```
6. Delete `/etc/network-log-server/initial-credentials.txt`.

## Enabling HTTPS

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d logs.example.com
```

Then set `auth.cookie_secure: true` in
`/etc/network-log-server/log-server.yaml` and restart the API, so session
cookies stop being sent over plaintext:

```bash
sudo systemctl restart network-log-server-api
```

## Layout

```
/opt/network-log-server/          application and virtualenv
/etc/network-log-server/          configuration (.env is 0640 root:netlog)
/var/lib/network-log-server/      metadata database, branding
/var/log/network-log-server/      service logs
/var/backups/network-log-server/  config backups and monthly archives
/var/log/network-log-server-install.log
```

## Services

```bash
systemctl status network-log-server-receiver    # syslog ingest
systemctl status network-log-server-worker      # Redis → ClickHouse
systemctl status network-log-server-api         # web API
systemctl list-timers network-log-server-*      # daily maintenance
```

## Upgrading

```bash
cd network-log-server && git pull
sudo bash update.bash
```

Backs up config first, applies migrations, restarts, health-checks. On failure
the previous application directory is left at `/opt/network-log-server.rollback`.

## Uninstalling

```bash
sudo bash uninstall.bash
```

Preserves log data by default. Deleting it requires typing `DELETE` in capitals.

## Docker

For evaluation and development:

```bash
cp .env.example .env      # edit the secrets first
docker compose up -d
```

For production on a dedicated host, the native install is the supported path —
it is what the tuning, sandboxing and systemd sandboxing target.
