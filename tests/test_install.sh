#!/bin/bash
# Sandboxed end-to-end test for install.sh.
#
# Runs the installer against a throwaway $HOME with sudo/apt/systemctl/
# update-desktop-database stubbed out, then asserts on every file it
# generates. Run from the repo root:  bash tests/test_install.sh
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

FAILURES=0
check() {  # check <description> <command...>
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "ok   - $desc"
    else
        echo "FAIL - $desc"
        FAILURES=$((FAILURES + 1))
    fi
}

# Stub out the commands that need root or a systemd session.
STUBS="$SANDBOX/stubs"
mkdir -p "$STUBS"
for cmd in sudo apt systemctl update-desktop-database; do
    printf '#!/bin/sh\necho "[stub] %s $*" >> "%s/calls.log"\nexit 0\n' \
        "$cmd" "$SANDBOX" > "$STUBS/$cmd"
    chmod +x "$STUBS/$cmd"
done

export HOME="$SANDBOX/home"
mkdir -p "$HOME"
export PATH="$STUBS:$PATH"

cd "$REPO_ROOT"
bash install.sh > "$SANDBOX/install.log" 2>&1
check "install.sh exits 0" test $? -eq 0

APP_DIR="$HOME/.local/share/displayguard"
CONF="$HOME/.config/displayguard/dim.conf"
SERVICE="$HOME/.config/systemd/user/displayguard.service"
DESKTOP="$HOME/.local/share/applications/displayguard.desktop"

# Installed app files
check "GUI installed" test -x "$APP_DIR/displayguard.py"
check "daemon installed" test -x "$APP_DIR/displayguard_service.py"

# Config: right file, right keys (the ones the GUI/daemon actually read)
check "dim.conf created (not config.ini)" test -f "$CONF"
check "no stray config.ini" test ! -e "$HOME/.config/displayguard/config.ini"
check "dim.conf has [displayguard] section" grep -q '^\[displayguard\]' "$CONF"
for key in enabled darkness idle_seconds sleep_enabled sleep_darkness sleep_seconds; do
    check "dim.conf has '$key'" grep -q "^$key = " "$CONF"
done
check "dim.conf has no legacy *_brightness keys" \
    bash -c "! grep -q brightness '$CONF'"

# systemd unit
check "service unit created" test -f "$SERVICE"
check "service ordered after graphical-session.target" \
    grep -q '^After=graphical-session.target' "$SERVICE"
check "service drops privilege escalation (NoNewPrivileges)" \
    grep -q '^NoNewPrivileges=yes' "$SERVICE"
check "service ExecStart points at installed daemon" \
    grep -q "ExecStart=/usr/bin/python3 $APP_DIR/displayguard_service.py" "$SERVICE"

# Desktop entry: @APP_DIR@ substituted with the real install path
check "desktop entry created" test -f "$DESKTOP"
check "desktop Exec points at installed GUI" \
    grep -q "Exec=/usr/bin/python3 $APP_DIR/displayguard.py" "$DESKTOP"
check "no unsubstituted @APP_DIR@ placeholder" \
    bash -c "! grep -q '@APP_DIR@' '$DESKTOP'"

# Correct dependencies requested
check "installs python3-gi + GTK introspection" \
    grep -q 'apt install -y python3-gi gir1.2-gtk-3.0' "$SANDBOX/calls.log"
check "does not install python3-tk/xprintidle" \
    bash -c "! grep -qE 'python3-tk|xprintidle' '$SANDBOX/calls.log'"

# Service enabled via systemctl --user
check "daemon-reload called" grep -q 'systemctl --user daemon-reload' "$SANDBOX/calls.log"
check "service enabled and started" \
    grep -q 'systemctl --user enable --now displayguard.service' "$SANDBOX/calls.log"

# The generated config must round-trip through the daemon's own parser.
check "daemon parses the generated dim.conf with expected values" \
    python3 - "$CONF" <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)) or ".", "tests"))
sys.path.insert(0, "tests")
import test_displayguard_service as t  # installs the fake gi
import displayguard_service as svc
svc.CONFIG_PATH = sys.argv[1]
cfg = svc.DimConfig().load()
assert cfg.enabled is True
assert abs(cfg.darkness - 0.70) < 1e-9
assert cfg.idle_seconds == 600
assert cfg.sleep_enabled is True
assert abs(cfg.sleep_darkness - 0.99) < 1e-9
assert cfg.sleep_seconds == 1800
PYEOF

# Re-running the installer must not clobber an edited config.
sed -i 's/^darkness = .*/darkness = 0.50/' "$CONF"
bash install.sh > "$SANDBOX/install2.log" 2>&1
check "re-install exits 0" test $? -eq 0
check "re-install preserves user-edited dim.conf" \
    grep -q '^darkness = 0.50' "$CONF"

# A $HOME containing sed metacharacters must not corrupt or inject lines
# into the generated desktop entry.
export HOME="$SANDBOX/we&ird|home"
mkdir -p "$HOME"
bash install.sh > "$SANDBOX/install3.log" 2>&1
check "install with hostile \$HOME exits 0" test $? -eq 0
WEIRD_DESKTOP="$HOME/.local/share/applications/displayguard.desktop"
check "hostile \$HOME desktop entry has the literal path" \
    grep -qF "Exec=/usr/bin/python3 $HOME/.local/share/displayguard/displayguard.py" \
    "$WEIRD_DESKTOP"
check "hostile \$HOME injects no extra desktop entries/keys" \
    test "$(grep -c '^Exec=' "$WEIRD_DESKTOP")" = "1"

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "install.sh test: ALL CHECKS PASSED"
else
    echo "install.sh test: $FAILURES CHECK(S) FAILED"
    exit 1
fi
