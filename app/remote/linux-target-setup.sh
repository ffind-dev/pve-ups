#!/bin/sh
# PVE-UPS — prepare a plain Linux server to be shut down by the appliance over SSH.
#
# Run once as root on the server (GPU node, NAS, any systemd or SysV Linux). It creates:
#
#   * a system user (default "upsshutdown") with a locked password;
#   * /usr/local/sbin/pve-ups-remote — the forced command of the appliance's key. Whatever
#     the appliance sends, only "pve-ups test" and "pve-ups shutdown" do anything;
#   * /usr/local/sbin/pve-ups-poweroff — the one root-owned script that user may run via
#     sudo. It runs the hooks in /etc/pve-ups/pre-shutdown.d/ and then powers off;
#   * /etc/sudoers.d/pve-ups — exactly that one command, nothing else;
#   * the appliance's public key in an authorized_keys file owned by root, restricted to
#     the forced command (no shell, no PTY, no forwarding), so the account itself cannot
#     loosen its own restrictions.
#
# Re-running it is safe and replaces the key. "--uninstall" removes all of the above.
#
#   sh linux-target-setup.sh [--user NAME] [--from CIDR] [--uninstall] ['ssh-ed25519 AAAA…']
#
# The appliance's web UI (Hosts → type "Linux server (SSH)" → "Setup script") hands out
# this file with its public key already filled in below.
#
# Copyright 2026 Florian Finder
set -eu

PUBKEY='__PVE_UPS_PUBKEY__'
USER_NAME=upsshutdown
FROM=""
UNINSTALL=0

REMOTE=/usr/local/sbin/pve-ups-remote
POWEROFF=/usr/local/sbin/pve-ups-poweroff
SUDOERS=/etc/sudoers.d/pve-ups
CONF_DIR=/etc/pve-ups
HOOK_DIR=$CONF_DIR/pre-shutdown.d
KEY_DIR=$CONF_DIR/ssh

die() { echo "pve-ups: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --user) [ $# -ge 2 ] || die "--user needs a value"; USER_NAME=$2; shift 2 ;;
    --from) [ $# -ge 2 ] || die "--from needs a value"; FROM=$2; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    ssh-*|ecdsa-*) PUBKEY=$1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run this as root"
case "$USER_NAME" in *[!a-z0-9_-]*|"") die "invalid user name: $USER_NAME" ;; esac

if [ "$UNINSTALL" -eq 1 ]; then
  rm -f "$REMOTE" "$POWEROFF" "$SUDOERS" "$KEY_DIR/$USER_NAME.authorized_keys"
  rmdir "$KEY_DIR" 2>/dev/null || true
  if id "$USER_NAME" >/dev/null 2>&1; then userdel "$USER_NAME" 2>/dev/null || true; fi
  echo "pve-ups: removed. Hooks in $HOOK_DIR were left in place."
  exit 0
fi

case "$PUBKEY" in
  ssh-*" "*|ecdsa-*" "*) ;;
  *) die "no public key given — pass the appliance's key as the last argument" ;;
esac
case "$PUBKEY" in *'
'*|*'"'*) die "the public key must be a single line without quotes" ;; esac
case "$FROM" in *[!0-9a-fA-F.:/,*?]*) die "invalid --from value: $FROM" ;; esac

# --- sudo is the only privilege boundary; without it there is nothing to delegate to ---
if ! command -v sudo >/dev/null 2>&1; then
  echo "pve-ups: installing sudo"
  if command -v apt-get >/dev/null 2>&1; then apt-get install -y sudo >/dev/null
  elif command -v dnf >/dev/null 2>&1; then dnf install -y sudo >/dev/null
  elif command -v yum >/dev/null 2>&1; then yum install -y sudo >/dev/null
  elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install sudo >/dev/null
  elif command -v apk >/dev/null 2>&1; then apk add sudo >/dev/null
  else die "sudo is missing and no known package manager was found — install sudo first"
  fi
fi

# --- the account: no password, a plain shell (sshd runs the forced command through it) ---
if ! id "$USER_NAME" >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir / --shell /bin/sh "$USER_NAME"
  else
    adduser -S -D -H -h / -s /bin/sh "$USER_NAME"  # BusyBox / Alpine
  fi
fi
passwd -l "$USER_NAME" >/dev/null 2>&1 || true

# --- the forced command -----------------------------------------------------------------
cat >"$REMOTE" <<'EOF'
#!/bin/sh
# Forced command of the PVE-UPS appliance key (installed by linux-target-setup.sh).
# Answers on one line, "PVE-UPS-REMOTE 1 <status> [...]", which the appliance parses.
POWEROFF=/usr/local/sbin/pve-ups-poweroff
case "${SSH_ORIGINAL_COMMAND:-}" in
  "pve-ups test")
    if sudo -n -l "$POWEROFF" --schedule >/dev/null 2>&1; then p=yes; else p=no; fi
    echo "PVE-UPS-REMOTE 1 ok power=$p host=$(hostname)"
    ;;
  "pve-ups shutdown")
    if sudo -n "$POWEROFF" --schedule; then
      echo "PVE-UPS-REMOTE 1 accepted"
    else
      echo "PVE-UPS-REMOTE 1 error sudo-failed"
      exit 1
    fi
    ;;
  *)
    echo "PVE-UPS-REMOTE 1 error unknown-command"
    exit 2
    ;;
esac
EOF

# --- the one thing root does for that account -------------------------------------------
cat >"$POWEROFF" <<'EOF'
#!/bin/sh
# Power-off for the PVE-UPS appliance (installed by linux-target-setup.sh).
#   --schedule  detach and return at once, so the SSH caller gets its answer before the
#               network goes down (this is what the sudo rule allows);
#   --run       the actual sequence: pre-shutdown hooks, then poweroff.
HOOK_DIR=/etc/pve-ups/pre-shutdown.d
HOOK_TIMEOUT=120
[ -r /etc/pve-ups/remote.conf ] && . /etc/pve-ups/remote.conf
UNIT=pve-ups-poweroff

case "${1:-}" in
  --schedule)
    if command -v systemd-run >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
      # A second request while the first is still running is not an error: the machine
      # is already on its way down.
      systemd-run --quiet --no-block --unit="$UNIT" "$0" --run \
        || systemctl is-active --quiet "$UNIT" \
        || exit 1
    else
      nohup "$0" --run >/dev/null 2>&1 &
    fi
    ;;
  --run)
    logger -t pve-ups "Shutdown requested by the PVE-UPS appliance (UPS on battery)" || true
    if [ -d "$HOOK_DIR" ]; then
      for hook in "$HOOK_DIR"/*; do
        [ -f "$hook" ] && [ -x "$hook" ] || continue
        logger -t pve-ups "pre-shutdown hook: $hook" || true
        if command -v timeout >/dev/null 2>&1; then
          timeout "$HOOK_TIMEOUT" "$hook" || logger -t pve-ups "hook failed or timed out: $hook" || true
        else
          "$hook" || logger -t pve-ups "hook failed: $hook" || true
        fi
      done
    fi
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
      exec systemctl poweroff
    fi
    exec shutdown -h now
    ;;
  *)
    echo "usage: $0 --schedule | --run" >&2
    exit 2
    ;;
esac
EOF
chown root:root "$REMOTE" "$POWEROFF"
chmod 0755 "$REMOTE" "$POWEROFF"

# --- sudoers: that script with that argument, and nothing else ---------------------------
TMP_SUDO=$(mktemp)
cat >"$TMP_SUDO" <<EOF
# PVE-UPS appliance: may power this machine off, nothing else (linux-target-setup.sh).
Defaults:$USER_NAME !requiretty
$USER_NAME ALL=(root) NOPASSWD: $POWEROFF --schedule
EOF
if command -v visudo >/dev/null 2>&1 && ! visudo -cqf "$TMP_SUDO"; then
  rm -f "$TMP_SUDO"
  die "the generated sudoers rule did not validate — nothing was changed"
fi
install -o root -g root -m 0440 "$TMP_SUDO" "$SUDOERS"
rm -f "$TMP_SUDO"

# --- hooks ----------------------------------------------------------------------------
mkdir -p "$HOOK_DIR"
if [ ! -e "$HOOK_DIR/README" ]; then
  cat >"$HOOK_DIR/README" <<'EOF'
Executable files in this directory run as root, in name order, before the machine
powers off on a PVE-UPS request. Each one gets HOOK_TIMEOUT seconds (default 120,
override in /etc/pve-ups/remote.conf). A failing hook is logged and skipped — it never
stops the shutdown. Keep the total well inside the battery runtime.

Examples:
  10-docker      #!/bin/sh
                 docker ps -q | xargs -r docker stop -t 60
  20-slurm       #!/bin/sh
                 scontrol update nodename="$(hostname -s)" state=drain reason="UPS on battery"
  30-checkpoint  #!/bin/sh
                 pkill -USR1 -f train.py; sleep 60
EOF
fi

# --- the key: root-owned, so the account cannot widen its own restrictions -----------------
OPTS="restrict,command=\"$REMOTE\""
[ -n "$FROM" ] && OPTS="$OPTS,from=\"$FROM\""
mkdir -p "$KEY_DIR"
chown root:root "$CONF_DIR" "$KEY_DIR"
chmod 0755 "$CONF_DIR" "$KEY_DIR"
printf '%s %s\n' "$OPTS" "$PUBKEY" >"$KEY_DIR/$USER_NAME.authorized_keys"
chown root:root "$KEY_DIR/$USER_NAME.authorized_keys"
chmod 0644 "$KEY_DIR/$USER_NAME.authorized_keys"

# sshd reads that file through a drop-in, for this one user only. Older sshd without
# Include support gets the key in the user's home instead.
SSHD_DROPIN_DIR=/etc/ssh/sshd_config.d
if [ -d "$SSHD_DROPIN_DIR" ] && grep -qiE '^\s*Include\s+/etc/ssh/sshd_config.d' /etc/ssh/sshd_config 2>/dev/null; then
  cat >"$SSHD_DROPIN_DIR/50-pve-ups.conf" <<EOF
# PVE-UPS appliance key (linux-target-setup.sh)
Match User $USER_NAME
    AuthorizedKeysFile $KEY_DIR/%u.authorized_keys
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding no
    X11Forwarding no
    PermitTTY no
EOF
  chmod 0644 "$SSHD_DROPIN_DIR/50-pve-ups.conf"
else
  echo "pve-ups: sshd has no Include for $SSHD_DROPIN_DIR — falling back to the home directory"
  HOME_DIR=/var/lib/$USER_NAME
  mkdir -p "$HOME_DIR/.ssh"
  usermod -d "$HOME_DIR" "$USER_NAME" 2>/dev/null || true
  cp "$KEY_DIR/$USER_NAME.authorized_keys" "$HOME_DIR/.ssh/authorized_keys"
  chown -R root:root "$HOME_DIR"
  chmod 0755 "$HOME_DIR" "$HOME_DIR/.ssh"
  chmod 0644 "$HOME_DIR/.ssh/authorized_keys"
fi

if command -v sshd >/dev/null 2>&1; then
  sshd -t || die "sshd rejects its configuration now — check $SSHD_DROPIN_DIR/50-pve-ups.conf"
  # AllowUsers / AllowGroups silently lock the account out; say so instead.
  if sshd -T 2>/dev/null | grep -qiE '^(allowusers|allowgroups) '; then
    echo "pve-ups: WARNING: sshd uses AllowUsers/AllowGroups — make sure '$USER_NAME' is allowed."
  fi
fi
for svc in ssh sshd; do
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$svc" 2>/dev/null; then
    systemctl reload "$svc" && break
  fi
done

echo "pve-ups: done. User '$USER_NAME' may now power this machine off from the appliance."
echo "pve-ups: press 'Test' on the host card in the appliance to verify and pin the host key."
