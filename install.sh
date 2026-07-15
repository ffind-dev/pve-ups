#!/usr/bin/env bash
#
# PVE-UPS installer — run this ON a Proxmox VE host (as root).
# Creates an unprivileged Debian 12 LXC, copies the app into it, and installs
# the service. Idempotent-ish: re-running with an existing CTID will refuse.
#
# Usage:
#   ./install.sh [--ctid 950] [--hostname pve-usv] [--storage local-lvm] \
#                [--bridge vmbr0] [--ip dhcp] [--memory 256] [--disk 4]
#
set -euo pipefail

# --- Self-bootstrap (one-liner installation) --------------------------------
# When the script is started without the app files next to it (typically as a
# one-liner straight in the Proxmox node shell, `curl ... | bash`), it fetches
# the release tarball, unpacks it and re-executes the install.sh contained in
# it. If the tree is already unpacked (deploy/ next to it), nothing happens.
# `curl -fsSL` follows GitHub's release redirects; both vars stay overridable.
PVE_USV_BASE_URL="${PVE_USV_BASE_URL:-https://github.com/ffind-dev/pve-ups/releases/latest/download}"
PVE_USV_TARBALL="${PVE_USV_TARBALL:-pve-usv-latest.tar.gz}"

_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -z "${_self_dir:-}" || ! -f "${_self_dir}/deploy/setup-in-container.sh" ]]; then
  command -v curl >/dev/null || { echo "curl is required (apt install -y curl)."; exit 1; }
  command -v tar  >/dev/null || { echo "tar is required.";  exit 1; }
  _tmp="$(mktemp -d)"
  echo ">> Fetching release package: ${PVE_USV_BASE_URL}/${PVE_USV_TARBALL}"
  curl -fsSL "${PVE_USV_BASE_URL}/${PVE_USV_TARBALL}" | tar -C "$_tmp" -xzf -
  [[ -f "${_tmp}/pve-usv/install.sh" ]] || { echo "Tarball does not contain pve-usv/install.sh."; exit 1; }
  exec bash "${_tmp}/pve-usv/install.sh" "$@"
fi
# ---------------------------------------------------------------------------

CTID=950
HOSTNAME=pve-usv
STORAGE=""              # empty = pick a rootdir-capable storage automatically
TEMPLATE_STORAGE=local
BRIDGE=vmbr0
IP=dhcp
GATEWAY=""
MEMORY=256
DISK=4
TEMPLATE="debian-12-standard"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ctid) CTID="$2"; shift 2;;
    --hostname) HOSTNAME="$2"; shift 2;;
    --storage) STORAGE="$2"; shift 2;;
    --bridge) BRIDGE="$2"; shift 2;;
    --ip) IP="$2"; shift 2;;          # e.g. 10.0.0.50/24
    --gateway) GATEWAY="$2"; shift 2;;
    --memory) MEMORY="$2"; shift 2;;
    --disk) DISK="$2"; shift 2;;
    *) echo "Unknown option: $1"; exit 1;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v pct >/dev/null || { echo "This must run on a Proxmox VE host (pct not found)."; exit 1; }
if pct status "$CTID" &>/dev/null; then
  echo "CTID $CTID already exists. Choose another with --ctid."; exit 1
fi

# --- Pick/verify storage (fail fast, before the template download) ----------
# Lists storage names that support a given content type.
_storages_for() { pvesm status --content "$1" 2>/dev/null | awk 'NR>1 {print $1}'; }

ROOTDIR_STORAGES="$(_storages_for rootdir)"
if [[ -n "$STORAGE" ]]; then
  grep -qx "$STORAGE" <<<"$ROOTDIR_STORAGES" || {
    echo "Storage '$STORAGE' does not exist or cannot hold containers (content 'rootdir')."
    echo "Available: $(echo $ROOTDIR_STORAGES)"; exit 1; }
else
  # Prefer the usual defaults, otherwise the first suitable storage.
  for _cand in local-lvm local-zfs $ROOTDIR_STORAGES; do
    if grep -qx "$_cand" <<<"$ROOTDIR_STORAGES"; then STORAGE="$_cand"; break; fi
  done
  [[ -n "$STORAGE" ]] || {
    echo "No storage with content 'rootdir' found. Specify one with --storage <name>."; exit 1; }
  echo ">> Storage picked automatically: $STORAGE"
fi

# Secure the template storage (content 'vztmpl') the same way.
if ! grep -qx "$TEMPLATE_STORAGE" <<<"$(_storages_for vztmpl)"; then
  _new_tmpl="$(_storages_for vztmpl | head -n1)"
  [[ -n "$_new_tmpl" ]] || { echo "No storage with content 'vztmpl' found for the template."; exit 1; }
  echo ">> Template storage '$TEMPLATE_STORAGE' not usable, using '$_new_tmpl'."
  TEMPLATE_STORAGE="$_new_tmpl"
fi
# ---------------------------------------------------------------------------

echo ">> Ensuring Debian 12 template is available"
pveam update >/dev/null 2>&1 || true
TMPL=$(pveam available --section system | grep -o "${TEMPLATE}[^ ]*" | sort -V | tail -n1)
[[ -n "$TMPL" ]] || { echo "No $TEMPLATE template found via pveam."; exit 1; }
if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TMPL"; then
  echo ">> Downloading $TMPL"
  pveam download "$TEMPLATE_STORAGE" "$TMPL"
fi

NET="name=eth0,bridge=${BRIDGE}"
if [[ "$IP" == "dhcp" ]]; then
  NET="${NET},ip=dhcp"
else
  NET="${NET},ip=${IP}"
  [[ -n "$GATEWAY" ]] && NET="${NET},gw=${GATEWAY}"
fi

echo ">> Creating unprivileged LXC $CTID ($HOSTNAME)"
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TMPL}" \
  --hostname "$HOSTNAME" \
  --cores 1 --memory "$MEMORY" --swap 256 \
  --rootfs "${STORAGE}:${DISK}" \
  --net0 "$NET" \
  --unprivileged 1 --features nesting=0 \
  --onboot 1 --start 1

echo ">> Waiting for container network"
for _ in $(seq 1 30); do pct exec "$CTID" -- ping -c1 -W1 deb.debian.org &>/dev/null && break; sleep 2; done

echo ">> Copying application into container"
pct exec "$CTID" -- mkdir -p /opt/pve-usv
# Stream the app tree into the container. The producer tar can exit 1 on a *benign*
# "file changed as we read it" warning; under `set -o pipefail` that would otherwise abort
# the whole script silently (the script just stops after this line). So we suppress that
# warning and judge success by the extractor's (consumer) exit status, treating only a
# fatal producer error (>=2) as failure.
set +e
tar -C "$SCRIPT_DIR" \
  --exclude='./.git' --exclude='__pycache__' --exclude='*.pyc' \
  --warning=no-file-changed -czf - . \
  | pct exec "$CTID" -- tar -C /opt/pve-usv -xzf -
_pipe=("${PIPESTATUS[@]}"); _prod=${_pipe[0]:-0}; _cons=${_pipe[1]:-0}
set -e
if [[ "$_cons" -ne 0 || "$_prod" -ge 2 ]]; then
  echo "ERROR copying into the container (tar create=$_prod, extract=$_cons)." >&2
  echo "      Check: is the container running (pct status $CTID) and is there enough free space?" >&2
  exit 1
fi

echo ">> Running in-container setup"
pct exec "$CTID" -- bash /opt/pve-usv/deploy/setup-in-container.sh

echo ""
echo "============================================================"
echo " PVE-UPS is running in CT $CTID."
echo " Open the web UI on port 8080 of the container IP."
echo " Then: set the password -> wizard (SNMP, hosts, thresholds)."
echo "============================================================"
