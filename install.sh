#!/bin/bash
set -e

APP_DIR="$HOME/.local/share/displayguard"
BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="$HOME/.config/systemd/user"
DESKTOP_DIR="$HOME/.local/share/applications"
CONFIG_DIR="$HOME/.config/displayguard"

mkdir -p "$APP_DIR" "$BIN_DIR" "$SERVICE_DIR" "$DESKTOP_DIR" "$CONFIG_DIR"

sudo apt update
sudo apt install -y python3-gi gir1.2-gtk-3.0

cp displayguard.py "$APP_DIR/"
cp displayguard_service.py "$APP_DIR/"

chmod +x "$APP_DIR/displayguard.py"
chmod +x "$APP_DIR/displayguard_service.py"

# Seed a default config (the same file the GUI and daemon read), but never
# overwrite an existing one.
if [ ! -f "$CONFIG_DIR/dim.conf" ]; then
    cat > "$CONFIG_DIR/dim.conf" <<EOF
[displayguard]
enabled = true
darkness = 0.70
idle_seconds = 600
sleep_enabled = true
sleep_darkness = 0.99
sleep_seconds = 1800
EOF
fi

cat > "$SERVICE_DIR/displayguard.service" <<EOF
[Unit]
Description=DisplayGuard screen dimming service
After=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 $APP_DIR/displayguard_service.py
Restart=always
RestartSec=3
NoNewPrivileges=yes

[Install]
WantedBy=default.target
EOF

# Escape sed replacement metacharacters so an unusual $HOME (containing
# '|', '&' or '\') can't corrupt or inject lines into the desktop entry.
APP_DIR_SED=$(printf '%s' "$APP_DIR" | sed 's/[&\\|]/\\&/g')
sed "s|@APP_DIR@|$APP_DIR_SED|g" displayguard.desktop > "$DESKTOP_DIR/displayguard.desktop"

chmod +x "$DESKTOP_DIR/displayguard.desktop"

systemctl --user daemon-reload
systemctl --user enable --now displayguard.service

update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo "DisplayGuard installed."
echo "Search DisplayGuard in Show Applications."
