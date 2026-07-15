#!/usr/bin/env bash
# Runs INSIDE the container/VM. Installs the PVE-USV app + systemd service.
# Expects the application source to already be present at /opt/pve-usv.
set -euo pipefail

APP_DIR=/opt/pve-usv

echo ">> Installing OS dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates

echo ">> Creating service user + state dirs"
id pveusv &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin pveusv
install -d -o pveusv -g pveusv -m 0750 /etc/pve-usv /var/lib/pve-usv
# Spool dirs the unprivileged app writes to and the root agent consumes.
install -d -o pveusv -g pveusv -m 0750 \
  /var/lib/pve-usv/agent /var/lib/pve-usv/agent/queue /var/lib/pve-usv/updates

echo ">> Creating virtualenv + installing app"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet "$APP_DIR"

echo ">> Installing systemd service"
install -m 0644 "$APP_DIR/deploy/pve-usv.service" /etc/systemd/system/pve-usv.service
# Privileged deploy agent (applies uploaded updates + NTP changes for the app).
chmod 0755 "$APP_DIR/deploy/pve-usv-agent.sh"
install -m 0644 "$APP_DIR/deploy/pve-usv-agent.service" /etc/systemd/system/pve-usv-agent.service
install -m 0644 "$APP_DIR/deploy/pve-usv-agent.path" /etc/systemd/system/pve-usv-agent.path
install -m 0644 "$APP_DIR/deploy/pve-usv-agent.timer" /etc/systemd/system/pve-usv-agent.timer
systemctl daemon-reload
systemctl enable --now pve-usv.service
systemctl enable --now pve-usv-agent.path
# The timer is the reliable queue-drainer (the .path can miss inotify edges).
systemctl enable --now pve-usv-agent.timer

IP=$(hostname -I | awk '{print $1}')
echo ""
echo ">> Done. Webinterface:  http://${IP}:8080"
echo ">> On first access you set a password, then the setup wizard starts."
