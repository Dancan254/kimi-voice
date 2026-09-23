#!/bin/bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$HOME/.venv/kimi-voice"
CONFIG_DIR="$HOME/.config/kimi-voice"
SYSTEMD_DIR="$HOME/.config/systemd/user"
BIN_DIR="$HOME/bin"

echo "==> Installing system dependencies"
sudo apt update
sudo apt install -y python3-venv python3-dev python3-evdev portaudio19-dev xclip libportaudio2

# ydotool is used on Wayland; wtype is preferred but harder to install on Ubuntu.
if ! command -v ydotoold &>/dev/null; then
    echo "==> Installing ydotool"
    sudo apt install -y ydotool
fi

echo "==> Creating Python virtual environment"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"

echo "==> Installing binaries and config"
mkdir -p "$BIN_DIR" "$CONFIG_DIR" "$SYSTEMD_DIR"
cp "$REPO_DIR/kimi-voice.py" "$BIN_DIR/kimi-voice.py"
chmod +x "$BIN_DIR/kimi-voice.py"
cp "$REPO_DIR/kimi-voice" "$BIN_DIR/kimi-voice"
chmod +x "$BIN_DIR/kimi-voice"
cp "$REPO_DIR/config.json" "$CONFIG_DIR/config.json"

echo "==> Installing udev rules"
sudo cp "$REPO_DIR/udev/50-kimi-voice.rules" /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "==> Adding user to input group"
if ! id -nG "$USER" | grep -qw input; then
    sudo usermod -aG input "$USER"
    echo "!!! Log out and back in for the input group to take effect."
fi

echo "==> Installing systemd user services"
cp "$REPO_DIR/systemd/ydotoold.service" "$SYSTEMD_DIR/ydotoold.service"
cp "$REPO_DIR/systemd/kimi-voice.service" "$SYSTEMD_DIR/kimi-voice.service"
systemctl --user daemon-reload
systemctl --user enable ydotoold.service
systemctl --user enable kimi-voice.service

echo "==> Starting services"
systemctl --user restart ydotoold.service
systemctl --user restart kimi-voice.service

echo "==> Done. Check status with:"
echo "    systemctl --user status kimi-voice.service"
echo "    journalctl --user -u kimi-voice.service -f"
