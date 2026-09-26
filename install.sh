#!/usr/bin/env bash
# os3-router installer (macOS + Linux)
#   curl -fsSL https://raw.githubusercontent.com/Nesbesss/os3-router/main/install.sh | bash
# Options: --uninstall [--purge]   --no-app   --no-wait   --port N
#          --node-only   only a keep-alive for this machine's rabbit-agent (OS3 nodes that don't run the router)
# Env:     CODEX_OS3_SRC=<local checkout>  (install from a folder instead of GitHub)
#          CODEX_OS3_REF=<branch|tag>       (default: main)
set -euo pipefail

REPO="Nesbesss/os3-router"
REF="${CODEX_OS3_REF:-main}"
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
    *) echo "unknown option $1"; exit 2 ;;
  esac
  shift
done

b() { printf '\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*"; exit 1; }
tty_in() { if [ -r /dev/tty ]; then "$@" </dev/tty; else "$@"; fi; }

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UNIT="$HOME/.config/systemd/user/codex-os3.service"
KLABEL="ai.codexos3.keepalive"
KPLIST="$HOME/Library/LaunchAgents/$KLABEL.plist"
KUNIT="$HOME/.config/systemd/user/codex-os3-keepalive.service"

# --------------------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
  b "Uninstalling os3-router"
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
    pkill -f -- "-m codex_os3 (serve|worker)" 2>/dev/null || true
  fi
  rm -rf "$APP_DIR"
  [ "$PURGE" = 1 ] && rm -rf "$HOME_DIR" && ok "removed all data ($HOME_DIR)"
  ok "done"; exit 0
fi

b "os3-router installer"
case "$OS" in Darwin|Linux) ;; *) die "use install.ps1 on Windows" ;; esac

# launchd services started from an SSH session land outside the GUI session: they can't be
# bootstrapped, and processes they start lose macOS permissions (Accessibility etc.)
if [ "$OS" = Darwin ] && [ -n "${SSH_CONNECTION:-}" ]; then
  die "run this in Terminal on the Mac itself, not over SSH (macOS services started over SSH lose their permissions)"
fi

# --------------------------------------------------------------------------- python
PY=""
for c in python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  [ "$OS" = Darwin ] && die "Python 3.9+ not found. Run: xcode-select --install   (then re-run this installer)"
  die "Python 3.9+ not found. Install it (e.g. sudo apt install python3) and re-run."
fi
ok "python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# --------------------------------------------------------------------------- codex cli
if [ "$NODE_ONLY" = 0 ]; then
if ! command -v codex >/dev/null 2>&1; then
  b "Installing the Codex CLI"
  if command -v npm >/dev/null 2>&1; then npm install -g @openai/codex >/dev/null
  elif command -v brew >/dev/null 2>&1; then brew install codex >/dev/null
  else die "install Node.js (https://nodejs.org) or Homebrew first, then re-run — the Codex CLI needs one of them"; fi
fi
CODEX="$(command -v codex)" || die "codex not on PATH after install"
MIN_CODEX="0.155.0"
codex_ver() { "$CODEX" --version 2>/dev/null | awk '{print $NF}' | cut -d- -f1; }
ver_lt() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)" = "$1" ] && [ "$1" != "$2" ]; }
if ver_lt "$(codex_ver)" "$MIN_CODEX"; then
  b "Updating the Codex CLI ($(codex_ver) is too old for the current models)"
  NPM="$(dirname "$CODEX")/npm"; [ -x "$NPM" ] || NPM="$(command -v npm || true)"
  if [ -n "$NPM" ] && "$NPM" install -g @openai/codex@latest >/dev/null 2>&1; then :
  elif command -v brew >/dev/null 2>&1 && brew upgrade codex >/dev/null 2>&1; then :
  else warn "could not update codex automatically: run  npm i -g @openai/codex@latest"; fi
  hash -r; CODEX="$(command -v codex)"
fi
ok "codex: $CODEX ($(codex_ver))"
if ! "$CODEX" login status >/dev/null 2>&1; then
  b "Log in to Codex with your ChatGPT account"
  tty_in "$CODEX" login || die "codex login failed"
fi
ok "codex is logged in"

fi

# --------------------------------------------------------------------------- rabbit-agent
if [ -f "$HOME/.rabbit-agent/runtime/rabbit-agent.status.json" ]; then
  ok "rabbit-agent found on this machine"
else
  warn "no rabbit OS3 node on this machine yet — install it from OS3 (Settings → add device) first;"
  warn "the router must run on the same machine you select as the LLM device in OS3"
fi

# --------------------------------------------------------------------------- code
b "Installing os3-router"
mkdir -p "$HOME_DIR"
NEW="$HOME_DIR/app.new"; rm -rf "$NEW"; mkdir -p "$NEW"
if [ -n "${CODEX_OS3_SRC:-}" ]; then
  (cd "$CODEX_OS3_SRC" && tar --exclude .git --exclude app/macos/.build --exclude _proto -cf - .) | (cd "$NEW" && tar xf -)
else
  TGZ="$HOME_DIR/src.tgz"
  if curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" -o "$TGZ" 2>/dev/null; then :
  elif command -v gh >/dev/null 2>&1 && gh api "repos/$REPO/tarball/$REF" > "$TGZ" 2>/dev/null; then :  # private repo
  else die "could not download $REPO@$REF"; fi
  tar xzf "$TGZ" -C "$NEW" --strip-components 1 && rm -f "$TGZ"
fi
[ -f "$NEW/codex_os3/__init__.py" ] || die "download looks incomplete"
OLD_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$APP_DIR/codex_os3/__init__.py" 2>/dev/null || true)
rm -rf "$APP_DIR.old"; [ -d "$APP_DIR" ] && mv "$APP_DIR" "$APP_DIR.old"; mv "$NEW" "$APP_DIR"; rm -rf "$APP_DIR.old"
VERSION="$("$PY" -c "import sys; sys.path.insert(0, '$APP_DIR'); import codex_os3; print(codex_os3.__version__)")"
ok "os3-router $VERSION in $APP_DIR"

cd "$APP_DIR"
if [ "$NODE_ONLY" = 1 ]; then
  b "Installing the node keep-alive"
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
    KEEP="cd $APP_DIR && pgrep -f -- '-m codex_os3 keepalive' >/dev/null || CODEX_OS3_HOME=$HOME_DIR nohup $PY -m codex_os3 keepalive >> $HOME_DIR/keepalive.log 2>&1 &"
    { crontab -l 2>/dev/null | grep -v "codex-os3-keepalive" || true
      echo "*/2 * * * * $KEEP # codex-os3-keepalive"; } | crontab - || die "could not write your crontab"
    sh -c "$KEEP"
  fi
  ok "keep-alive running: it restarts this machine's rabbit-agent when it stops or stays disconnected (e.g. after sleep)"
  ok "it updates itself; remove it with: bash install.sh --uninstall"
  exit 0
fi
[ -n "$PORT" ] && "$PY" -c "from codex_os3 import config; config.save({'port': int('$PORT')})"
"$PY" -c "from codex_os3 import config; config.save({'codex_bin': '$CODEX'}); config.ensure_key()"
# this installer brings the matching menu bar app itself; the router only updates apps on later updates
"$PY" -c "from codex_os3 import store, __version__; store.kv_set('apps_version', __version__)"
# "what's new" popup: after an upgrade, everything since the old version; nothing on a fresh install
"$PY" -c "from codex_os3 import store, __version__; store.kv_set('whatsnew_seen', '${OLD_VERSION}' or __version__)"
CLAUDE=$(command -v claude || true)  # optional: lets roles use Claude models via Claude Code
[ -n "$CLAUDE" ] && "$PY" -c "from codex_os3 import config; config.save({'claude_bin': '$CLAUDE'})" && ok "Claude Code found: $CLAUDE"
PORT="$("$PY" -c "from codex_os3 import config; print(config.load()['port'])")"
SVC_PATH="$(dirname "$CODEX"):$(dirname "$(command -v node 2>/dev/null || echo /usr/bin/node)"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# --------------------------------------------------------------------------- service
b "Installing the service"
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
"$PY" -m codex_os3 status 2>/dev/null | grep -q "service: running" && exit 0
cd "$APP_DIR" && PATH="$SVC_PATH" CODEX_OS3_HOME="$HOME_DIR" nohup "$PY" -m codex_os3 serve >> "$HOME_DIR/service.log" 2>&1 &
KEEP
    chmod +x "$KEEP"
    { crontab -l 2>/dev/null | grep -v "codex-os3-keepalive" || true  # no crontab yet: grep/crontab fail
      echo "@reboot $KEEP # codex-os3-keepalive"; echo "*/2 * * * * $KEEP # codex-os3-keepalive"; } | crontab - \
      || die "could not write your crontab"
    [ "$RUNNING" = 1 ] && "$PY" -m codex_os3 reload >/dev/null || "$KEEP"
    ok "no systemd user session: using cron (@reboot + every 2 min) to keep it running"
  fi
fi

for i in $(seq 1 30); do
  "$PY" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=2)" 2>/dev/null && break
  sleep 1
done || true
"$PY" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=2)" 2>/dev/null \
  && ok "router answering on http://127.0.0.1:$PORT" || die "router did not start — see $HOME_DIR/service.log"

# --------------------------------------------------------------------------- macOS app
if [ "$OS" = Darwin ] && [ "$NO_APP" = 0 ]; then
  ZIP="$HOME_DIR/app.zip"; rm -f "$ZIP"
  if [ -d "$APP_DIR/app/macos/build/OS3 Router.app" ]; then ditto -c -k --keepParent "$APP_DIR/app/macos/build/OS3 Router.app" "$ZIP"
  else for Z in OS3Router-macos.zip CodexOS3-macos.zip; do  # the latter: releases before 0.2.0
    curl -fsSL "https://github.com/$REPO/releases/latest/download/$Z" -o "$ZIP" 2>/dev/null && break
    command -v gh >/dev/null 2>&1 && gh release download -R "$REPO" -p "$Z" -O "$ZIP" 2>/dev/null && break
  done; fi
  if [ -s "$ZIP" ]; then
    mkdir -p "$HOME/Applications"; pkill -f "Contents/MacOS/CodexOS3" 2>/dev/null || true
    rm -rf "$HOME/Applications/OS3 Router.app" "$HOME/Applications/Codex OS3.app"  # the latter: name before 0.2.0
    ditto -x -k "$ZIP" "$HOME/Applications/" && rm -f "$ZIP"
    A="$HOME/Applications/OS3 Router.app"; [ -d "$A" ] || A="$HOME/Applications/Codex OS3.app"
    # the app isn't signed by an Apple developer account: mark it downloaded so macOS offers "Open Anyway"
    # (without the mark it refuses silently)
    xattr -w com.apple.quarantine "0083;$(printf %x "$(date +%s)");os3-router;" "$A" 2>/dev/null || true
    open "$A" || true
    ok "app installed: $A"
    warn "macOS will say \"OS3 Router\" Not Opened the first time. Click Done, then open"
    warn "System Settings → Privacy & Security, scroll down, and click Open Anyway. Only once."
  else
    warn "menu bar app not available for this version (the router works without it)"
  fi
fi

# --------------------------------------------------------------------------- connect OS3
"$PY" -m codex_os3 setup-info
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
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && (nohup sh "$APP_DIR/app/linux/os3-router-app" setup >/dev/null 2>&1 &)
elif [ "$NO_APP" = 1 ]; then open "http://localhost:$PORT/app#setup" 2>/dev/null || true
fi  # macOS: the app installed above opens on the setup wizard by itself

if [ "$NO_WAIT" = 0 ] && [ -t 1 ]; then
  b "Waiting for OS3 to connect… (save the connection in OS3 and send it a message; Ctrl-C to skip)"
  if "$PY" -m codex_os3 wait-for-os3 1800 >/dev/null; then ok "OS3 is connected — you're done 🎉"
  else warn "no request from OS3 yet; the dashboard shows when it connects"; fi
fi
