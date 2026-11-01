#!/usr/bin/env bash
# One-time setup on the Pi: run with   bash setup.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ME="$(whoami)"
cd "$DIR"

echo "==> 1/5 Installing packages (emoji font for weather icons)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 fonts-noto-color-emoji

echo "==> 2/5 Config + secret key for the drawing link (keeps your existing settings)"
python3 - <<'PY'
import json, os, secrets
ex = json.load(open("config.example.json"))
c = json.load(open("config.json")) if os.path.exists("config.json") else {}
for k, v in ex.items():
    c.setdefault(k, v)          # add new settings, never overwrite yours
OLD_CLOCKS = [{"name": "Pittsburgh", "tz": "America/New_York"}, {"name": "London", "tz": "Europe/London"}]
if c.get("clocks") == OLD_CLOCKS:  # untouched old default -> new default (adds Mexico City)
    c["clocks"] = ex["clocks"]
if c.get("key") in ("", "CHANGE-ME"):
    c["key"] = secrets.token_urlsafe(12)
json.dump(c, open("config.json", "w"), indent=2)
PY
KEY="$(python3 -c 'import json;print(json.load(open("config.json"))["key"])')"
DRAW_PORT="$(python3 -c 'import json;print(json.load(open("config.json"))["draw_port"])')"
DASH_PORT="$(python3 -c 'import json;print(json.load(open("config.json"))["dashboard_port"])')"

echo "==> 3/5 Installing the server as a service (starts on every boot)"
sudo tee /etc/systemd/system/pi-dashboard.service >/dev/null <<EOF
[Unit]
Description=Pi dashboard server
After=network-online.target
Wants=network-online.target

[Service]
User=$ME
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now pi-dashboard
sudo systemctl restart pi-dashboard

echo "==> 4/5 Opening the dashboard full screen at login"
BROWSER="$(command -v chromium || command -v chromium-browser || true)"
if [ -z "$BROWSER" ]; then
  sudo apt-get install -y -qq chromium || sudo apt-get install -y -qq chromium-browser
  BROWSER="$(command -v chromium || command -v chromium-browser)"
fi
KIOSK="sleep 6 && $BROWSER --kiosk --noerrdialogs --disable-infobars --incognito --password-store=basic --check-for-update-interval=31536000 http://localhost:$DASH_PORT"
mkdir -p "$HOME/.config/labwc"
touch "$HOME/.config/labwc/autostart"
grep -q "localhost:$DASH_PORT" "$HOME/.config/labwc/autostart" || echo "$KIOSK &" >> "$HOME/.config/labwc/autostart"
# Older desktops (wayfire) use a different autostart file
if [ -f "$HOME/.config/wayfire.ini" ] && ! grep -q "localhost:$DASH_PORT" "$HOME/.config/wayfire.ini"; then
  printf '\n[autostart]\ndashboard = %s\n' "$KIOSK" >> "$HOME/.config/wayfire.ini"
fi

echo "==> 5/5 Screen: no automatic blanking; let the dashboard switch the backlight on/off"
sudo raspi-config nonint do_blanking 1 || true
echo 'SUBSYSTEM=="backlight", RUN+="/bin/chmod 0666 /sys/class/backlight/%k/brightness /sys/class/backlight/%k/bl_power"' \
  | sudo tee /etc/udev/rules.d/99-dashboard-backlight.rules >/dev/null
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=backlight
if ls /sys/class/backlight/* >/dev/null 2>&1; then
  echo "    Backlight found: screen will fully switch off at night."
else
  echo "    No controllable backlight on this screen: at night it will show black instead."
fi

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Done!"
echo "  Test the drawing page from a phone on your home Wi-Fi:"
echo "    http://$IP:$DRAW_PORT/#k=$KEY"
echo "  Reboot to start the full-screen dashboard:  sudo reboot"
