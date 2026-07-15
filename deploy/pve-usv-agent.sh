#!/usr/bin/env bash
# Privileged deploy agent for PVE-USV.
#
# Runs as ROOT, started by pve-usv-agent.timer (every ~20s) and pve-usv-agent.path. It
# drains the job queue the unprivileged app writes to /var/lib/pve-usv/agent/queue and
# performs the actions the hardened, unprivileged service may not do itself: apply an
# uploaded update (replace /opt/pve-usv + reinstall + restart), set the system NTP server
# and set the system timezone. Only this small, auditable script is root.
#
# Robustness: ALL jobs are processed and their job files removed first; the pve-usv service
# is restarted at most once, at the very end — so a restart can never interrupt draining
# and the queue never gets stuck (which would otherwise stop the .path trigger from
# re-firing). The timer guarantees processing even if the .path trigger is missed.
#
# Copyright 2026 Florian Finder
set -uo pipefail

STATE_DIR=/var/lib/pve-usv
AGENT_DIR="$STATE_DIR/agent"
QUEUE="$AGENT_DIR/queue"
RESULT="$AGENT_DIR/result.json"
LOGFILE="$AGENT_DIR/agent.log"
APP_DIR=/opt/pve-usv

mkdir -p "$AGENT_DIR" 2>/dev/null || true
NEED_RESTART=0

log_line() {  # human-readable progress to agent.log + journal (stdout)
  local line; line="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
  echo "$line"
  echo "$line" >>"$LOGFILE" 2>/dev/null || true
  # keep the log bounded (last 500 lines)
  if [ -f "$LOGFILE" ]; then tail -n 500 "$LOGFILE" >"$LOGFILE.tmp" 2>/dev/null && mv "$LOGFILE.tmp" "$LOGFILE" 2>/dev/null || true; fi
}

field() {  # <jobfile> <key>
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1" "$2"
}

read_version() {  # <path to app/__init__.py> -> prints version or empty
  python3 - "$1" <<'PY'
import re, sys
try:
    data = open(sys.argv[1], encoding="utf-8").read()
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", data)
    print(m.group(1) if m else "")
except Exception:
    print("")
PY
}

write_result() {  # <true|false> <job> <vbefore> <vafter> <pkgver> <message...>
  local ok="$1" job="$2" vb="$3" va="$4" pkgv="$5"; shift 5
  local msg="$*" ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  python3 - "$ok" "$job" "$vb" "$va" "$pkgv" "$ts" "$msg" >"$RESULT" <<'PY'
import json, sys
print(json.dumps({
    "ok": sys.argv[1] == "true",
    "job_id": sys.argv[2],
    "version_before": sys.argv[3] or None,
    "version_after": sys.argv[4] or None,
    "package_version": sys.argv[5] or None,
    "ts": sys.argv[6],
    "message": sys.argv[7],
}))
PY
}

install_units() {
  # Mirror our own systemd units into place and (re)enable the timer, so the robust
  # trigger mechanism self-heals on every update without re-running setup-in-container.sh.
  local changed=0 u
  for u in pve-usv-agent.service pve-usv-agent.path pve-usv-agent.timer pve-usv.service; do
    if [ -f "$APP_DIR/deploy/$u" ] && ! cmp -s "$APP_DIR/deploy/$u" "/etc/systemd/system/$u"; then
      install -m 0644 "$APP_DIR/deploy/$u" "/etc/systemd/system/$u" && changed=1
    fi
  done
  if [ "$changed" -eq 1 ]; then
    log_line "systemd units updated; daemon-reload + enabling timer"
    systemctl daemon-reload >/dev/null 2>&1 || true
  fi
  systemctl enable --now pve-usv-agent.timer >/dev/null 2>&1 || true
}

do_update() {
  local job="$1" pkg="$2" vb va out rc pip_out note=""
  vb="$(read_version "$APP_DIR/app/__init__.py")"
  log_line "Update job=$job package=$pkg (running: ${vb:-?})"

  if [ ! -f "$pkg" ]; then
    log_line "ERROR: package not found"
    write_result false "$job" "$vb" "$vb" "" "Package not found: $pkg"
    return 0
  fi

  out="$(python3 - "$pkg" "$APP_DIR" 2>&1 <<'PY'
import os, shutil, sys, tarfile, tempfile, zipfile

pkg, app_dir = sys.argv[1], sys.argv[2]
tmp = tempfile.mkdtemp()
if pkg.endswith(".zip"):
    with zipfile.ZipFile(pkg) as z:
        z.extractall(tmp)
elif pkg.endswith((".tar.gz", ".tgz")):
    with tarfile.open(pkg) as t:
        try:
            t.extractall(tmp, filter="data")  # blocks ../ paths/devices (Python >= 3.12)
        except TypeError:  # older Python without the filter argument
            t.extractall(tmp)
else:
    sys.exit("Unknown format")

src = None
for root, _dirs, files in os.walk(tmp):
    if "pyproject.toml" in files and os.path.isdir(os.path.join(root, "app")):
        src = root
        break
if not src:
    sys.exit("pyproject.toml/app not found in package")


def atomic_copy(src_file, dst_file):
    os.makedirs(os.path.dirname(dst_file), exist_ok=True)
    tmpf = dst_file + ".new"
    shutil.copy2(src_file, tmpf)
    os.replace(tmpf, dst_file)  # keeps a still-open file's old inode intact


dst_app = os.path.join(app_dir, "app")
shutil.rmtree(dst_app, ignore_errors=True)
shutil.copytree(os.path.join(src, "app"), dst_app)
atomic_copy(os.path.join(src, "pyproject.toml"), os.path.join(app_dir, "pyproject.toml"))

deploy_src = os.path.join(src, "deploy")
if os.path.isdir(deploy_src):
    for root, _dirs, files in os.walk(deploy_src):
        rel = os.path.relpath(root, deploy_src)
        for fn in files:
            dst_rel = fn if rel == "." else os.path.join(rel, fn)
            dst = os.path.join(app_dir, "deploy", dst_rel)
            atomic_copy(os.path.join(root, fn), dst)
            if fn.endswith(".sh"):
                os.chmod(dst, 0o755)  # systemd ExecStart needs this; tarballs may lose the x-bit

shutil.rmtree(tmp, ignore_errors=True)
PY
)"
  rc=$?
  if [ $rc -ne 0 ]; then
    log_line "ERROR while unpacking/copying: $out"
    write_result false "$job" "$vb" "$vb" "" "Unpack/copy failed: $out"
    return 0
  fi

  va="$(read_version "$APP_DIR/app/__init__.py")"
  log_line "Files replaced (new: ${va:-?})"

  if ! pip_out="$("$APP_DIR/venv/bin/pip" install --quiet --upgrade "$APP_DIR" 2>&1)"; then
    log_line "ERROR during pip install: $pip_out"
    write_result false "$job" "$vb" "$va" "$va" \
      "pip install failed: $(echo "$pip_out" | tail -n 3 | tr '\n' ' ')"
    return 0
  fi

  install_units          # refresh systemd units from the freshly copied deploy/
  rm -f "$pkg"
  if [ -n "$vb" ] && [ "$vb" = "$va" ]; then
    note=" (note: version unchanged at $va - did the package contain the new version?)"
  fi
  log_line "Success${note}; pve-usv.service will be restarted at the end"
  write_result true "$job" "$vb" "$va" "$va" "Update applied${note}. Service is restarting."
  NEED_RESTART=1
}

do_set_ntp() {
  local job="$1" server="$2"
  if [ -z "$server" ]; then
    write_result false "$job" "" "" "" "No NTP server given"
    return 0
  fi
  install -d -m 0755 /etc/systemd/timesyncd.conf.d
  cat >/etc/systemd/timesyncd.conf.d/pve-usv.conf <<EOF
[Time]
NTP=$server
EOF
  timedatectl set-ntp true >/dev/null 2>&1 || true
  systemctl restart systemd-timesyncd >/dev/null 2>&1 || true
  log_line "NTP server set: $server"
  write_result true "$job" "" "" "" "NTP server set: $server"
}

do_set_timezone() {
  local job="$1" tz="$2"
  if [ -z "$tz" ]; then
    write_result false "$job" "" "" "" "No timezone given"
    return 0
  fi
  if [ ! -f "/usr/share/zoneinfo/$tz" ]; then
    log_line "Unknown timezone: $tz"
    write_result false "$job" "" "" "" "Unknown timezone: $tz"
    return 0
  fi
  # Set the zone robustly for an unprivileged LXC: write the symlink + /etc/timezone
  # directly (filesystem ops the root agent can always do), then nudge timedatectl too.
  ln -sf "/usr/share/zoneinfo/$tz" /etc/localtime
  echo "$tz" >/etc/timezone
  timedatectl set-timezone "$tz" >/dev/null 2>&1 || true
  log_line "Timezone set: $tz"
  write_result true "$job" "" "" "" "Timezone set: $tz. Service is restarting."
  # The running app caches the timezone (glibc) — restart so datetime.now() picks it up
  # (the daily self-test hour is interpreted in local time).
  NEED_RESTART=1
}

[ -d "$QUEUE" ] || exit 0
shopt -s nullglob
processed=0
for jobfile in "$QUEUE"/*.json; do
  processed=1
  jid="$(field "$jobfile" job_id)"
  action="$(field "$jobfile" action)"
  case "$action" in
    update)       do_update "$jid" "$(field "$jobfile" package)" ;;
    set-ntp)      do_set_ntp "$jid" "$(field "$jobfile" server)" ;;
    set-timezone) do_set_timezone "$jid" "$(field "$jobfile" tz)" ;;
    *)            log_line "Unknown action: $action"; write_result false "$jid" "" "" "" "Unknown action: $action" ;;
  esac
  rm -f "$jobfile"          # always drain, even on failure
done

# Restart the app exactly once, after the queue is fully drained.
if [ "$NEED_RESTART" -eq 1 ]; then
  log_line "Restarting pve-usv.service"
  systemctl restart pve-usv.service
fi
exit 0
