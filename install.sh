#!/usr/bin/env bash
#
# PVE-UPS installer — run this ON a Proxmox VE host (as root).
# Creates an unprivileged Debian 12 LXC, copies the app into it, and installs
# the service. Idempotent-ish: re-running with an existing CTID will refuse.
#
# Usage:
#   ./install.sh [--ctid 950] [--hostname pve-usv] [--storage local-lvm] \
#                [--template-storage local] [--bridge vmbr0] \
#                [--ip dhcp | --ip 10.0.0.50/24 --gateway 10.0.0.1] \
#                [--memory 256] [--disk 4] [--allow-ceph-storage]
#   ./install.sh --help      (all options with their defaults)
#
# The container must NOT live on Ceph storage: it has to keep running while the
# cluster it is shutting down goes away. Ceph-backed storages are therefore skipped
# when picking one automatically and refused when named with --storage, unless
# --allow-ceph-storage is given.
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
TEMPLATE_STORAGE_GIVEN=0  # 1 = named with --template-storage: refuse instead of falling back
BRIDGE=vmbr0
IP=dhcp
GATEWAY=""
MEMORY=256
DISK=4
TEMPLATE="debian-12-standard"
ALLOW_CEPH_STORAGE=0    # see the Ceph guard below

usage() {
  cat <<USAGE
PVE-UPS installer - creates an unprivileged Debian 12 LXC on this Proxmox VE host.

Usage: install.sh [options]

  --ctid <id>                Container ID                         (default: $CTID)
  --hostname <name>          Container hostname                   (default: $HOSTNAME)
  --storage <name>           Storage for the container disk (content 'rootdir').
                             Picked automatically when omitted: local-lvm, then
                             local-zfs, then the first other non-Ceph storage.
  --template-storage <name>  Storage the Debian 12 template is downloaded to
                             (content 'vztmpl')                   (default: $TEMPLATE_STORAGE)
                             Only the default falls back to another suitable
                             storage on its own; a named one is checked and refused.
  --bridge <bridge>          Network bridge                       (default: $BRIDGE)
  --ip <dhcp|address/cidr>   'dhcp' or a static address, e.g. 10.0.0.50/24
                                                                  (default: $IP)
  --gateway <ip>             Default gateway; only used with a static --ip
  --memory <MB>              RAM of the container                 (default: $MEMORY)
  --disk <GB>                Size of the container disk           (default: $DISK)
  --allow-ceph-storage       Accept a Ceph-backed --storage. Refused by default:
                             the container has to outlive the cluster it shuts down.
  -h, --help                 Show this help and exit

Started as a one-liner, pass the options after 'bash -s --', e.g.:
  curl -fsSL .../install.sh | bash -s -- --ip 10.0.0.50/24 --gateway 10.0.0.1
USAGE
}

# An option that takes a value must get one. Without this check, "--ip" as the last word
# died on `set -u` with an error about "\$2" instead of naming the option.
_need_value() {
  [[ -n "${2:-}" && "${2:0:2}" != "--" ]] || { echo "Option $1 needs a value (see --help)."; exit 1; }
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ctid) _need_value "$@"; CTID="$2"; shift 2;;
    --hostname) _need_value "$@"; HOSTNAME="$2"; shift 2;;
    --storage) _need_value "$@"; STORAGE="$2"; shift 2;;
    --template-storage) _need_value "$@"; TEMPLATE_STORAGE="$2"; TEMPLATE_STORAGE_GIVEN=1; shift 2;;
    --bridge) _need_value "$@"; BRIDGE="$2"; shift 2;;
    --ip) _need_value "$@"; IP="$2"; shift 2;;          # e.g. 10.0.0.50/24
    --gateway) _need_value "$@"; GATEWAY="$2"; shift 2;;
    --memory) _need_value "$@"; MEMORY="$2"; shift 2;;
    --disk) _need_value "$@"; DISK="$2"; shift 2;;
    --allow-ceph-storage) ALLOW_CEPH_STORAGE=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown option: $1 (see --help)"; exit 1;;
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

# Storage TYPE, from column 2 of `pvesm status` (Name Type Status Total Used Free %).
# The content check above says a storage can hold a container; only the type says what
# it is made of.
_storage_type() { pvesm status 2>/dev/null | awk -v s="$1" 'NR>1 && $1==s {print $2}'; }

# This container is the one guest that has to OUTLIVE the cluster it shuts down. On Ceph
# it does not: once the OSDs of the nodes going down drop the pool below min_size, the
# appliance's own IO blocks and it can no longer tell anything to power off - halfway
# through the outage, with the battery draining. So a Ceph-backed rootfs is refused
# rather than warned about, and --allow-ceph-storage is the deliberate way past it.
CEPH_STORAGE_TYPES="rbd cephfs"
_is_ceph_storage() {
  local _t; _t="$(_storage_type "$1")"
  [[ -n "$_t" ]] && grep -qw -- "$_t" <<<"$CEPH_STORAGE_TYPES"
}
_ceph_storage_note() {
  echo "Storage '$1' is Ceph-backed (type '$(_storage_type "$1")')."
  echo "This container must not live on Ceph: when the cluster is shut down its own"
  echo "disk stops answering, so it can no longer shut anything down. Use a local"
  echo "storage (local-lvm, local-zfs, dir) instead."
  echo "If you really mean it, re-run with --allow-ceph-storage."
}

ROOTDIR_STORAGES="$(_storages_for rootdir)"
if [[ -n "$STORAGE" ]]; then
  grep -qx "$STORAGE" <<<"$ROOTDIR_STORAGES" || {
    echo "Storage '$STORAGE' does not exist or cannot hold containers (content 'rootdir')."
    echo "Available: $(echo $ROOTDIR_STORAGES)"; exit 1; }
  if _is_ceph_storage "$STORAGE"; then
    if [[ "$ALLOW_CEPH_STORAGE" -eq 1 ]]; then
      echo "!! WARNING: '$STORAGE' is Ceph-backed and you asked for it anyway."
      echo "!! This appliance will freeze mid-shutdown once the pool loses min_size."
    else
      # Refused, never prompted: the documented install path is `curl ... | bash`, where
      # stdin IS the script - a prompt would either hang or eat the rest of it.
      _ceph_storage_note "$STORAGE"; exit 1
    fi
  fi
else
  # Prefer the usual defaults, otherwise the first suitable storage - skipping anything
  # Ceph-backed, so the automatic path can never land there.
  for _cand in local-lvm local-zfs $ROOTDIR_STORAGES; do
    if grep -qx "$_cand" <<<"$ROOTDIR_STORAGES" && ! _is_ceph_storage "$_cand"; then
      STORAGE="$_cand"; break
    fi
  done
  [[ -n "$STORAGE" ]] || {
    echo "No local storage with content 'rootdir' found."
    if [[ -n "$ROOTDIR_STORAGES" ]]; then
      echo "Only Ceph-backed ones are available: $(echo $ROOTDIR_STORAGES)"
      echo "This container must not live on Ceph - it has to outlive the cluster it"
      echo "shuts down. Add a local storage, or force it with"
      echo "--storage <name> --allow-ceph-storage."
    else
      echo "Specify one with --storage <name>."
    fi
    exit 1; }
  echo ">> Storage picked automatically: $STORAGE"
fi

# Secure the template storage (content 'vztmpl') the same way. Only the default falls
# back on its own: a storage named with --template-storage is the user's choice, and
# quietly downloading somewhere else would be the surprise --storage avoids as well.
TMPL_STORAGES="$(_storages_for vztmpl)"
if ! grep -qx "$TEMPLATE_STORAGE" <<<"$TMPL_STORAGES"; then
  if [[ "$TEMPLATE_STORAGE_GIVEN" -eq 1 ]]; then
    echo "Storage '$TEMPLATE_STORAGE' does not exist or cannot hold templates (content 'vztmpl')."
    echo "Available: $(echo $TMPL_STORAGES)"; exit 1
  fi
  _new_tmpl="$(head -n1 <<<"$TMPL_STORAGES")"
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
echo " Then: set the password -> wizard (UPS devices, hosts, thresholds)."
echo "============================================================"
