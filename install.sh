#!/bin/bash
set -e

APP_DIR="$HOME/.local/share/displayguard"
BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="$HOME/.config/systemd/user"
DESKTOP_DIR="$HOME/.local/share/applications"
CONFIG_DIR="$HOME/.config/displayguard"

mkdir -p "$APP_DIR" "$BIN_DIR" "$SERVICE_DIR" "$DESKTOP_DIR" "$CONFIG_DIR"

sudo apt update
sudo apt install -y python3-tk xprintidle

cp displayguard.py "$APP_DIR/"
cp displayguard_service.py "$APP_DIR/"

chmod +x "$APP_DIR/displayguard.py"
chmod +x "$APP_DIR/displayguard_service.py"

cat > "$CONFIG_DIR/config.ini" <<EOF
[displayguard]
enabled = true
idle_seconds = 600
dim_brightness = 0.3
sleep_enabled = true
sleep_seconds = 1800
sleep_brightness = 0.05
EOF

cat > "$SERVICE_DIR/displayguard.service" <<EOF
[Unit]
Description=DisplayGuard screen dimming service

[Service]
ExecStart=/usr/bin/python3 $APP_DIR/displayguard_service.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

cat > "$DESKTOP_DIR/displayguard.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=DisplayGuard
Comment=Screen dimming and display protection manager
Exec=python3 $APP_DIR/displayguard.py
Icon=preferences-desktop-display
Terminal=false
Categories=Settings;System;
StartupNotify=true
EOF

chmod +x "$DESKTOP_DIR/displayguard.desktop"

systemctl --user daemon-reload
systemctl --user enable --now displayguard.service

update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo "DisplayGuard installed."
echo "Search DisplayGuard in Show Applications."
