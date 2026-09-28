#!/usr/bin/env bash
# os3-router installer (macOS + Linux)
#   curl -fsSL https://raw.githubusercontent.com/Nesbesss/os3-router/main/install.sh | bash
# Options: --uninstall [--purge]   --no-app   --no-wait   --port N
#          --node-only   only a keep-alive for this machine's rabbit-agent (OS3 nodes that don't run the router)
# Env:     CODEX_OS3_SRC=<local checkout>  (install from a folder instead of GitHub)
#          CODEX_OS3_REF=<branch|tag>       (default: the latest release)
set -euo pipefail

REPO="Nesbesss/os3-router"
REF="${CODEX_OS3_REF:-}"  # empty: the latest release (main only if GitHub can't say which it is)
HOME_DIR="${CODEX_OS3_HOME:-$HOME/.codex-os3}"
APP_DIR="$HOME_DIR/app"
LABEL="ai.codexos3.router"
OS="$(uname -s)"
NO_APP=0; NO_WAIT=0; UNINSTALL=0; PURGE=0; PORT=""; NODE_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) UNINSTALL=1 ;; --purge) PURGE=1 ;; --no-app) NO_APP=1 ;; --no-wait) NO_WAIT=1 ;;
    --port) PORT="$2"; shift ;;
    --node-only) NODE_ONLY=1 ;;
    -h|--help) sed -n '2,7p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $1 (see --help)"; exit 2 ;;
  esac
  shift
done

# ---- look: colours only on a terminal (and not with NO_COLOR)
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m' DIM=$'\033[2m' RED=$'\033[31m' GRN=$'\033[32m' YEL=$'\033[33m' ACC=$'\033[38;5;69m' OFF=$'\033[0m'
else BOLD="" DIM="" RED="" GRN="" YEL="" ACC="" OFF=""; fi
N=0; TOTAL=7; [ "$NODE_ONLY" = 1 ] && TOTAL=4; T0=$SECONDS
b() { N=$((N + 1)); STEP="$*"; printf '\n%s%s%d/%d%s  %s%s%s\n' "$ACC" "$BOLD" "$N" "$TOTAL" "$OFF" "$BOLD" "$*" "$OFF"; }
ok() { printf '     %s✓%s %s\n' "$GRN" "$OFF" "$*"; }
warn() { printf '     %s!%s %s\n' "$YEL" "$OFF" "$*"; }
note() { printf '       %s%s%s\n' "$DIM" "$*" "$OFF"; }
die() { printf '     %s✗ %s%s\n' "$RED" "$*" "$OFF"; exit 1; }
# run "what it's doing" cmd...: the output goes to the log only; on a terminal a spinner shows the seconds so far
SPIN=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
run() {
  local what="$1" pid t=$SECONDS i=0; shift
  if [ "${TTY_OUT:-0}" = 1 ]; then
    "$@" >>"$LOG" 2>&1 &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r     %s%s%s %s %s%ds%s ' "$ACC" "${SPIN[i++ % 10]}" "$OFF" "$what" "$DIM" $((SECONDS - t)) "$OFF" >&3
      sleep 0.1
    done
    printf '\r\033[K' >&3
    wait "$pid"
  else
    "$@" >>"$LOG" 2>&1
  fi
}
# keeps a script running without systemd: cron (at boot + every 2 min) when there is one; otherwise
# (containers, WSL, minimal installs) it runs at every login from ~/.profile. -> 1 without cron
autostart() {
  if command -v crontab >/dev/null 2>&1 && { crontab -l 2>/dev/null | grep -v "codex-os3-keepalive" || true
      echo "@reboot $1 # codex-os3-keepalive"; echo "*/2 * * * * $1 # codex-os3-keepalive"; } | crontab - 2>/dev/null; then
    return 0
  fi
  local prof="$HOME/.profile" rest
  rest="$(grep -v "# codex-os3-keepalive" "$prof" 2>/dev/null || true)"
  printf '%s\n%s\n' "$rest" "[ -x \"$1\" ] && \"$1\" >/dev/null 2>&1 # codex-os3-keepalive" > "$prof"
  return 1
}
# a failed upgrade must not leave the machine without a router: put the previous copy back
restore_previous() {
  [ "${SWAPPED:-0}" = 1 ] && [ -d "$APP_DIR.old" ] || return 0  # (only for the copy this run set aside)
  rm -rf "$APP_DIR"; mv "$APP_DIR.old" "$APP_DIR" || return 0
  printf '  %sThe previous version was put back, so the router you had keeps working.%s\n' "$DIM" "$OFF"
  if [ -n "${PY:-}" ]; then (cd "$APP_DIR" && "$PY" -m codex_os3 reload >/dev/null 2>&1) || true; fi
}
tty_in() { if ( : </dev/tty ) 2>/dev/null; then "$@" </dev/tty; else "$@"; fi; }

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UNIT="$HOME/.config/systemd/user/codex-os3.service"
KLABEL="ai.codexos3.keepalive"
KPLIST="$HOME/Library/LaunchAgents/$KLABEL.plist"
KUNIT="$HOME/.config/systemd/user/codex-os3-keepalive.service"

# --------------------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
  printf '%sUninstalling os3-router%s\n' "$BOLD" "$OFF"
  if [ "$OS" = Darwin ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootout "gui/$(id -u)/$KLABEL" 2>/dev/null || true
    rm -f "$PLIST" "$KPLIST"
    for i in $(seq 1 30); do pgrep -f -- "-m codex_os3 (serve|worker)" >/dev/null || break; sleep 1; done
    pkill -9 -f -- "-m codex_os3 (serve|worker)" 2>/dev/null || true
    pkill -f "Contents/MacOS/CodexOS3" 2>/dev/null || true
    rm -rf "$HOME/Applications/OS3 Router.app" "$HOME/Applications/Codex OS3.app"
  else
    systemctl --user disable --now codex-os3 codex-os3-keepalive 2>/dev/null || true
    rm -f "$HOME/.local/share/applications/os3-router.desktop"
    rm -f "$UNIT" "$KUNIT"; systemctl --user daemon-reload 2>/dev/null || true
    pkill -f -- "-m codex_os3 keepalive" 2>/dev/null || true
    { crontab -l 2>/dev/null | grep -v "codex-os3-keepalive" || true; } | crontab - 2>/dev/null || true
    [ -f "$HOME/.profile" ] && { grep -v "# codex-os3-keepalive" "$HOME/.profile" > "$HOME/.profile.os3" || true; cat "$HOME/.profile.os3" > "$HOME/.profile"; rm -f "$HOME/.profile.os3"; }
    pkill -f -- "-m codex_os3 (serve|worker)" 2>/dev/null || true
  fi
  rm -rf "$APP_DIR" "$APP_DIR.old"
  [ "$PURGE" = 1 ] && rm -rf "$HOME_DIR" && ok "removed all data ($HOME_DIR)"
  ok "done"; exit 0
fi

# everything below also goes to install.log; any failure ends with what to do next
mkdir -p "$HOME_DIR"; LOG="$HOME_DIR/install.log"; : > "$LOG"
TTY_OUT=0; [ -t 1 ] && TTY_OUT=1  # (checked before stdout becomes the pipe to tee)
exec 3>&1  # the terminal itself: spinners go there, not into the log
exec > >(tee -a "$LOG") 2>&1
STEP="starting"; FAILED_CMD=""
trap 'FAILED_CMD=$BASH_COMMAND' ERR
{ echo "--- os3-router install $(date)"; uname -a; command -v sw_vers >/dev/null 2>&1 && sw_vers; echo "SHELL=${SHELL:-} PATH=$PATH"; } >>"$LOG" 2>&1 || true
# what a person helping needs to see, appended to the log when an install stops (nothing here leaves the machine)
diagnostics() {
  { echo "--- diagnostics after the failure (step: $STEP)"
    command -v python3 codex claude node 2>&1 || true
    if [ -n "${PY:-}" ] && [ -f "$APP_DIR/codex_os3/__init__.py" ]; then (cd "$APP_DIR" && "$PY" -m codex_os3 doctor) 2>&1 || true; fi
    tail -n 40 "$HOME_DIR/service.log" 2>/dev/null || true; } >>"$LOG" 2>&1 || true
}
on_exit() {
  code=$?
  [ "$code" = 0 ] && return
  diagnostics
  restore_previous
  printf '\n  %s%sThe install stopped%s during: %s\n' "$RED" "$BOLD" "$OFF" "$STEP"
  [ -n "$FAILED_CMD" ] && printf '  %sfailed command: %s%s\n' "$DIM" "$FAILED_CMD" "$OFF"
  printf '\n  • Running the same install command again is safe: it continues where it can.\n'
  printf '  • Still stuck? Share %s%s%s in the os3-router Discord, or open an issue:\n' "$BOLD" "$LOG" "$OFF"
  printf '    https://github.com/%s/issues/new\n\n' "$REPO"
}
trap on_exit EXIT

printf '\n  %s%s◆ OS3 Router%s  %sinstaller%s\n' "$ACC" "$BOLD" "$OFF" "$DIM" "$OFF"
printf '  %sYour ChatGPT (Codex) subscription as the brain of rabbit OS3. Takes 1–3 minutes.%s\n' "$DIM" "$OFF"

b "Checking this machine"
case "$OS" in Darwin|Linux) ;; *) die "use install.ps1 on Windows" ;; esac
command -v curl >/dev/null 2>&1 || die "curl is missing (Linux: sudo apt install curl)"
# launchd services started from an SSH session land outside the GUI session: they can't be
# bootstrapped, and processes they start lose macOS permissions (Accessibility etc.)
if [ "$OS" = Darwin ] && [ -n "${SSH_CONNECTION:-}" ]; then
  die "run this in Terminal on the Mac itself, not over SSH (macOS services started over SSH lose their permissions)"
fi
if [ "$NODE_ONLY" = 0 ]; then
  case "$(uname -m)" in x86_64|amd64|arm64|aarch64) ;;
    *) die "this processor ($(uname -m)) isn't supported by the Codex CLI: it needs a 64-bit Intel/AMD or ARM machine" ;; esac
fi
curl -fsS -m 15 -o /dev/null https://github.com 2>/dev/null \
  || die "can't reach github.com: check the internet connection (or a proxy / firewall) and re-run"
ok "$( [ "$OS" = Darwin ] && echo "macOS $(sw_vers -productVersion 2>/dev/null)" || echo "Linux" ) · $(uname -m) · online"
if [ -f "$HOME/.rabbit-agent/runtime/rabbit-agent.status.json" ]; then
  ok "rabbit OS3 node found on this machine"
else
  warn "no rabbit OS3 node on this machine yet: add this machine in OS3 (Settings → add device) too;"
  note "the router must run on the same machine you pick as the LLM device in OS3"
fi

# --------------------------------------------------------------------------- python
b "Python"
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; }
UV="$HOME_DIR/bin/uv"
uv_py() { UV_PYTHON_INSTALL_DIR="$HOME_DIR/python" "$UV" "$@"; }
PY=""
# (the fixed locations first: python3 on PATH may be conda/pyenv, which the service then depends on for good)
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
  p="$(command -v "$c" 2>/dev/null)" || continue
  # macOS without the developer tools: /usr/bin/python3 is a stub that pops up an install dialog
  [ "$OS" = Darwin ] && [ "$p" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1 && continue
  py_ok "$p" && { PY="$p"; break; }
done
[ -z "$PY" ] && [ -x "$UV" ] && PY="$(uv_py python find --managed-python 3.12 2>/dev/null || true)"
get_uv() { curl -fsSL --retry 3 https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME_DIR/bin" UV_NO_MODIFY_PATH=1 sh; }
if [ -z "$PY" ]; then  # no Python 3.9+: a private one for the router (nothing else on the system changes)
  note "no Python 3.9+ here: getting a private one just for the router"
  run "Downloading the Python installer (uv)" get_uv \
    || die "could not download uv (the Python installer): check the internet connection and re-run"
  run "Installing Python 3.12" uv_py python install 3.12 \
    || die "could not install Python 3.12 with uv: check the internet connection and re-run"
  PY="$(uv_py python find --managed-python 3.12)"
fi
py_ok "$PY" || die "Python at $PY doesn't work"
ok "Python $("$PY" -c 'import platform; print(platform.python_version())')  ${DIM}$PY${OFF}"

# --------------------------------------------------------------------------- codex cli
if [ "$NODE_ONLY" = 0 ]; then
MIN_CODEX="0.155.0"
codex_ver() { "$CODEX" --version 2>/dev/null | awk '{print $NF}' | cut -d- -f1; }
ver_lt() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)" = "$1" ] && [ "$1" != "$2" ]; }
# OpenAI's own standalone installer: no Node.js or Homebrew needed, installs to ~/.local/bin
get_codex() {
  curl -fsSL --retry 3 https://github.com/openai/codex/releases/latest/download/install.sh \
    | CODEX_NON_INTERACTIVE=1 CODEX_INSTALL_DIR="$HOME/.local/bin" sh
}
codex_official() {
  run "Installing the Codex CLI (OpenAI's installer)" get_codex \
    || die "could not install the Codex CLI: check the internet connection and re-run"
  CODEX="$HOME/.local/bin/codex"
}
npm_update() { "$NPM" install -g @openai/codex@latest; }
b "Codex CLI"
CODEX="$(command -v codex 2>/dev/null || true)"
[ -z "$CODEX" ] && [ -x "$HOME/.local/bin/codex" ] && CODEX="$HOME/.local/bin/codex"  # installed, not on PATH yet
if [ -z "$CODEX" ]; then
  codex_official
elif ver_lt "$(codex_ver)" "$MIN_CODEX"; then
  note "$(codex_ver) is too old for the current models: updating"
  NPM="$(dirname "$CODEX")/npm"; [ -x "$NPM" ] || NPM="$(command -v npm || true)"
  if [ -n "$NPM" ] && run "Updating the Codex CLI (npm)" npm_update; then hash -r; CODEX="$(command -v codex)"
  elif command -v brew >/dev/null 2>&1 && run "Updating the Codex CLI (Homebrew)" brew upgrade codex; then hash -r; CODEX="$(command -v codex)"; fi
  ver_lt "$(codex_ver)" "$MIN_CODEX" && codex_official  # e.g. npm without write access: the standalone one
fi
[ -x "$CODEX" ] && "$CODEX" --version >/dev/null 2>&1 || die "the Codex CLI at $CODEX doesn't start"
ok "Codex CLI $(codex_ver)  ${DIM}$CODEX${OFF}"

b "ChatGPT sign-in"
if ! "$CODEX" login status >/dev/null 2>&1; then
  if [ -n "${SSH_CONNECTION:-}" ] || { [ "$OS" = Linux ] && [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; }; then
    note "no browser here: open the link below on any device and enter the code"
    tty_in "$CODEX" login --device-auth || true
  else
    note "your browser opens: sign in with the ChatGPT account whose plan the router should use"
    tty_in "$CODEX" login || true
  fi
  "$CODEX" login status >/dev/null 2>&1 || die "Codex isn't signed in (the sign-in was cancelled or timed out): re-run to try again"
fi
ok "signed in to ChatGPT"

fi

# --------------------------------------------------------------------------- code
b "OS3 Router"
NEW="$HOME_DIR/app.new"; rm -rf "$NEW"; mkdir -p "$NEW"
if [ -z "$REF" ] && [ -z "${CODEX_OS3_SRC:-}" ]; then  # a fresh install gets what updates would give it: a tested release, not main
  REF="$(curl -fsSL -m 15 -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" 2>/dev/null | sed -n 's|.*/releases/tag/||p')"
  case "$REF" in v[0-9]*.[0-9]*.[0-9]*) ;; *) REF="main"; warn "couldn't find the latest release: installing the newest code (main)" ;; esac
fi
if [ -n "${CODEX_OS3_SRC:-}" ]; then
  (cd "$CODEX_OS3_SRC" && tar --exclude .git --exclude app/macos/.build --exclude _proto -cf - .) | (cd "$NEW" && tar xf -)
else
  TGZ="$HOME_DIR/src.tgz"
  if run "Downloading os3-router" curl -fsSL --retry 3 "https://codeload.github.com/$REPO/tar.gz/$REF" -o "$TGZ"; then :
  elif command -v gh >/dev/null 2>&1 && gh api "repos/$REPO/tarball/$REF" > "$TGZ" 2>/dev/null; then :  # private repo
  else die "could not download $REPO@$REF"; fi
  tar xzf "$TGZ" -C "$NEW" --strip-components 1 && rm -f "$TGZ"
fi
[ -f "$NEW/codex_os3/__init__.py" ] || die "download looks incomplete"
OLD_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$APP_DIR/codex_os3/__init__.py" 2>/dev/null || true)
rm -rf "$APP_DIR.old"; [ -d "$APP_DIR" ] && mv "$APP_DIR" "$APP_DIR.old"  # kept until the new one runs
mv "$NEW" "$APP_DIR" || die "could not put the new version in place ($APP_DIR)"
SWAPPED=1
VERSION="$("$PY" -c "import sys; sys.path.insert(0, '$APP_DIR'); import codex_os3; print(codex_os3.__version__)")"
ok "os3-router $VERSION$( [ -n "$OLD_VERSION" ] && [ "$OLD_VERSION" != "$VERSION" ] && echo " (was $OLD_VERSION)")  ${DIM}$APP_DIR${OFF}"

cd "$APP_DIR"
if [ "$NODE_ONLY" = 1 ]; then
  b "Keep-alive for the OS3 node"
  "$PY" -c "from codex_os3 import store, __version__; store.kv_set('whatsnew_seen', __version__)"
  if [ "$OS" = Darwin ]; then
    mkdir -p "$(dirname "$KPLIST")"
    cat > "$KPLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$KLABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>-m</string><string>codex_os3</string><string>keepalive</string></array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>EnvironmentVariables</key><dict><key>CODEX_OS3_HOME</key><string>$HOME_DIR</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME_DIR/keepalive.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/keepalive.log</string>
</dict></plist>
PLIST
    launchctl bootout "gui/$(id -u)/$KLABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$KPLIST" || die "launchctl bootstrap failed"
  elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$(dirname "$KUNIT")"
    printf '[Unit]\nDescription=os3-router keep-alive for the rabbit-agent\n\n[Service]\nWorkingDirectory=%s\nEnvironment=CODEX_OS3_HOME=%s\nExecStart=%s -m codex_os3 keepalive\nRestart=always\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n' \
      "$APP_DIR" "$HOME_DIR" "$PY" > "$KUNIT"
    systemctl --user daemon-reload && systemctl --user enable --now codex-os3-keepalive >/dev/null 2>&1 || die "systemctl --user enable failed"
    systemctl --user restart codex-os3-keepalive
    loginctl enable-linger "$USER" >/dev/null 2>&1 || warn "could not enable lingering (sudo loginctl enable-linger $USER)"
  else
    KEEP="$HOME_DIR/keepalive-node.sh"
    cat > "$KEEP" <<KEEP
#!/bin/sh
# codex-os3-keepalive (a pid file, not pgrep: minimal systems have no procps)
[ -f "$HOME_DIR/keepalive.pid" ] && kill -0 "\$(cat "$HOME_DIR/keepalive.pid")" 2>/dev/null && exit 0
cd "$APP_DIR" && CODEX_OS3_HOME="$HOME_DIR" nohup "$PY" -m codex_os3 keepalive >> "$HOME_DIR/keepalive.log" 2>&1 &
echo \$! > "$HOME_DIR/keepalive.pid"
KEEP
    chmod +x "$KEEP"
    autostart "$KEEP" || warn "no systemd or cron here: it starts at each login (~/.profile); for start at boot install cron"
    "$KEEP" 3>&-  # (fd 3 is the installer's terminal: a background process must not hold it)
  fi
  rm -rf "$APP_DIR.old"; SWAPPED=0
  ok "keep-alive running: it restarts this machine's rabbit-agent when it stops or stays disconnected (e.g. after sleep)"
  note "it updates itself; remove it with: bash install.sh --uninstall"
  printf '\n  %s%s✓ Done%s in %ds\n\n' "$GRN" "$BOLD" "$OFF" $((SECONDS - T0))
  exit 0
fi
# a port the router can really listen on (another program, or Windows reserving it, means the next free one),
# and where Claude Code is (the service's PATH is not this shell's)
"$PY" -m codex_os3 preflight ${PORT:+"$PORT"} || die "no usable port for the router${PORT:+ (port $PORT)}: pick another with --port"
"$PY" -c "from codex_os3 import config; config.save({'codex_bin': '$CODEX'}); config.ensure_key()"
# this installer brings the matching menu bar app itself; the router only updates apps on later updates
"$PY" -c "from codex_os3 import store, __version__; store.kv_set('apps_version', __version__)"
# "what's new" popup: after an upgrade, everything since the old version; nothing on a fresh install
"$PY" -c "from codex_os3 import store, __version__; store.kv_set('whatsnew_seen', '${OLD_VERSION}' or __version__)"
PORT="$("$PY" -c "from codex_os3 import config; print(config.load()['port'])")"
SVC_PATH="$(dirname "$CODEX"):$HOME/.local/bin:$(dirname "$(command -v node 2>/dev/null || echo /usr/bin/node)"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# --------------------------------------------------------------------------- service
b "Background service"
RUNNING=0
"$PY" -m codex_os3 status 2>/dev/null | grep -q "service: running" && RUNNING=1
if [ "$OS" = Darwin ]; then
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>-m</string><string>codex_os3</string><string>serve</string></array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$SVC_PATH</string><key>CODEX_OS3_HOME</key><string>$HOME_DIR</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME_DIR/service.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/service.log</string>
</dict></plist>
PLIST
  if [ "$RUNNING" = 1 ]; then
    "$PY" -m codex_os3 reload >/dev/null && ok "upgraded without downtime (new worker started, old one drains)"
  else
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" || die "launchctl bootstrap failed"
    ok "launchd service $LABEL (starts at login, restarts on crash)"
  fi
else
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<UNIT
[Unit]
Description=os3-router (Codex / Claude Code subscription as the LLM for rabbit OS3)
After=network-online.target

[Service]
WorkingDirectory=$APP_DIR
Environment=PATH=$SVC_PATH
Environment=CODEX_OS3_HOME=$HOME_DIR
ExecStart=$PY -m codex_os3 serve
ExecReload=$PY -m codex_os3 reload
Restart=always
RestartSec=3
KillMode=mixed
TimeoutStopSec=900

[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload
    if [ "$RUNNING" = 1 ]; then "$PY" -m codex_os3 reload >/dev/null; ok "upgraded without downtime"
    else systemctl --user enable --now codex-os3 >/dev/null 2>&1 || die "systemctl --user enable failed"; ok "systemd user service codex-os3"; fi
    loginctl enable-linger "$USER" >/dev/null 2>&1 && ok "service keeps running when you're logged out" \
      || warn "could not enable lingering: the service only runs while you're logged in (sudo loginctl enable-linger $USER)"
  else
    KEEP="$HOME_DIR/keepalive.sh"
    cat > "$KEEP" <<KEEP
#!/bin/sh
# codex-os3-keepalive
cd "$APP_DIR" || exit 1
"$PY" -m codex_os3 status 2>/dev/null | grep -q "service: running" && exit 0
PATH="$SVC_PATH" CODEX_OS3_HOME="$HOME_DIR" nohup "$PY" -m codex_os3 serve >> "$HOME_DIR/service.log" 2>&1 &
KEEP
    chmod +x "$KEEP"
    if autostart "$KEEP"; then ok "no systemd user session: cron keeps it running (at boot + every 2 min)"
    else
      warn "no systemd or cron here: the router starts now and at every login (~/.profile)"
      note "to have it start at boot as well, install cron (Debian/Ubuntu: sudo apt install cron) and re-run"
    fi
    [ "$RUNNING" = 1 ] && "$PY" -m codex_os3 reload >/dev/null || "$KEEP" 3>&-
  fi
fi

for i in $(seq 1 30); do
  "$PY" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=2)" 2>/dev/null && break
  sleep 1
done || true
"$PY" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=2)" 2>/dev/null \
  && ok "router running on http://127.0.0.1:$PORT" \
  || { tail -n 25 "$HOME_DIR/service.log" 2>/dev/null | sed 's/^/    /'; die "router did not start (the lines above are the end of $HOME_DIR/service.log)"; }
rm -rf "$APP_DIR.old"; SWAPPED=0  # the new version runs: the rollback copy isn't needed
# what the running service sees (not this shell): Codex, the sign-in, Claude; plus one real request when someone is watching
VFLAG=""; [ "$NO_WAIT" = 0 ] && { note "testing with a real request (up to a minute)"; VFLAG="--selftest"; }
"$PY" -m codex_os3 verify $VFLAG || warn "something above needs a look; the app's Help page can walk you through it"

# --------------------------------------------------------------------------- app + connect OS3
b "App and OS3 connection"
get_app() {
  for Z in OS3Router-macos.zip CodexOS3-macos.zip; do  # the latter: releases before 0.2.0
    curl -fsSL "https://github.com/$REPO/releases/latest/download/$Z" -o "$ZIP" && return 0
    command -v gh >/dev/null 2>&1 && gh release download -R "$REPO" -p "$Z" -O "$ZIP" && return 0
  done
  return 1
}
if [ "$OS" = Darwin ] && [ "$NO_APP" = 0 ]; then
  ZIP="$HOME_DIR/app.zip"; rm -f "$ZIP"
  if [ -d "$APP_DIR/app/macos/build/OS3 Router.app" ]; then ditto -c -k --keepParent "$APP_DIR/app/macos/build/OS3 Router.app" "$ZIP"
  else run "Downloading the menu bar app" get_app || true; fi
  if [ -s "$ZIP" ]; then
    mkdir -p "$HOME/Applications"; pkill -f "Contents/MacOS/CodexOS3" 2>/dev/null || true
    rm -rf "$HOME/Applications/OS3 Router.app" "$HOME/Applications/Codex OS3.app"  # the latter: name before 0.2.0
    ditto -x -k "$ZIP" "$HOME/Applications/" && rm -f "$ZIP"
    A="$HOME/Applications/OS3 Router.app"; [ -d "$A" ] || A="$HOME/Applications/Codex OS3.app"
    # the app isn't signed by an Apple developer account: mark it downloaded so macOS offers "Open Anyway"
    # (without the mark it refuses silently)
    xattr -w com.apple.quarantine "0083;$(printf %x "$(date +%s)");os3-router;" "$A" 2>/dev/null || true
    open "$A" || true
    ok "menu bar app installed  ${DIM}$A${OFF}"
    warn "the first time, macOS says \"OS3 Router\" Not Opened: click Done, then open"
    note "System Settings → Privacy & Security, scroll down, and click Open Anyway. Only once."
  else
    warn "menu bar app not available for this version (the router works without it)"
  fi
fi

if [ "$OS" = Linux ]; then  # the OS3 Router app: app-menu entry, then open it on the setup wizard
  mkdir -p "$HOME/.local/share/applications"
  cat > "$HOME/.local/share/applications/os3-router.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=OS3 Router
Comment=Status, limits and models of your OS3 router
Exec=sh "$APP_DIR/app/linux/os3-router-app"
Icon=$APP_DIR/codex_os3/ui/guide/app-icon.png
Categories=Utility;Network;
StartupWMClass=os3-router
DESKTOP
  ok "app menu: OS3 Router"
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && (nohup sh "$APP_DIR/app/linux/os3-router-app" setup >/dev/null 2>&1 3>&- &)
elif [ "$NO_APP" = 1 ]; then open "http://localhost:$PORT/app#setup" 2>/dev/null || true
fi  # macOS: the app installed above opens on the setup wizard by itself
"$PY" -m codex_os3 setup-info

printf '\n  %s%s✓ Installed%s in %ds\n' "$GRN" "$BOLD" "$OFF" $((SECONDS - T0))
if [ "$NO_WAIT" = 0 ] && [ "$TTY_OUT" = 1 ]; then
  printf '\n  %sLast step:%s save the connection in OS3 and send it a message. %s(Ctrl-C to skip)%s\n' "$BOLD" "$OFF" "$DIM" "$OFF"
  "$PY" -m codex_os3 wait-for-os3 1800 >/dev/null &
  WPID=$!; t=$SECONDS; i=0
  while kill -0 "$WPID" 2>/dev/null; do
    printf '\r     %s%s%s Waiting for OS3 to connect… %s%ds%s ' "$ACC" "${SPIN[i++ % 10]}" "$OFF" "$DIM" $((SECONDS - t)) "$OFF" >&3
    sleep 0.2
  done
  printf '\r\033[K' >&3
  if wait "$WPID"; then ok "OS3 is connected: you're done 🎉"
  else warn "no request from OS3 yet; the dashboard shows when it connects"; fi
fi
echo
