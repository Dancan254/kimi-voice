#!/bin/bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$HOME/.venv/kimi-voice"
CONFIG_DIR="$HOME/.config/kimi-voice"
BIN_DIR="$HOME/bin"

OS="$(uname -s)"

install_python_deps() {
    if [ -d "$VENV_DIR" ]; then
        echo "==> Updating existing Python virtual environment"
        "$VENV_DIR/bin/pip" install --upgrade pip
        "$VENV_DIR/bin/pip" install --upgrade -r "$REPO_DIR/requirements.txt"
    else
        echo "==> Creating Python virtual environment"
        python3 -m venv "$VENV_DIR"
        "$VENV_DIR/bin/pip" install --upgrade pip
        "$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"
    fi
}

install_binaries_and_config() {
    echo "==> Installing binaries and config"
    mkdir -p "$BIN_DIR" "$CONFIG_DIR"
    cp "$REPO_DIR/kimi-voice.py" "$BIN_DIR/kimi-voice.py"
    chmod +x "$BIN_DIR/kimi-voice.py"
    cp "$REPO_DIR/kimi-voice" "$BIN_DIR/kimi-voice"
    chmod +x "$BIN_DIR/kimi-voice"
    if [ ! -f "$CONFIG_DIR/config.json" ]; then
        cp "$REPO_DIR/config.json" "$CONFIG_DIR/config.json"
    fi
}

install_linux() {
    SYSTEMD_DIR="$HOME/.config/systemd/user"

    echo "==> Installing system dependencies"
    sudo apt update
    sudo apt install -y python3-venv python3-dev python3-evdev python3-tk portaudio19-dev xclip libportaudio2 libappindicator3-1 gir1.2-appindicator3-0.1

    # ydotool is used on Wayland; wtype is preferred but harder to install on Ubuntu.
    if ! command -v ydotoold &>/dev/null; then
        echo "==> Installing ydotool"
        sudo apt install -y ydotool
    fi

    install_python_deps
    install_binaries_and_config

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
    mkdir -p "$SYSTEMD_DIR"
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
}

install_macos() {
    LAUNCHD_DIR="$HOME/Library/LaunchAgents"
    PLIST_NAME="com.dancan254.kimi-voice.plist"
    PLIST_PATH="$LAUNCHD_DIR/$PLIST_NAME"

    echo "==> Installing macOS dependencies"
    if ! command -v brew &>/dev/null; then
        echo "!!! Homebrew is required but not found. Install it from https://brew.sh and re-run."
        exit 1
    fi

    if ! brew list portaudio &>/dev/null; then
        echo "==> Installing portaudio"
        brew install portaudio
    fi

    install_python_deps
    install_binaries_and_config

    echo "==> Installing LaunchAgent"
    mkdir -p "$LAUNCHD_DIR"
    sed "s|REPLACE_ME|$USER|g" "$REPO_DIR/launchd/$PLIST_NAME" > "$PLIST_PATH"

    if launchctl list com.dancan254.kimi-voice &>/dev/null; then
        launchctl unload "$PLIST_PATH" || true
    fi
    launchctl load "$PLIST_PATH"

    echo "==> Done. The tray icon should appear shortly."
    echo "    Logs: /tmp/kimi-voice.out.log and /tmp/kimi-voice.err.log"
    echo "    Stop: launchctl unload ~/Library/LaunchAgents/$PLIST_NAME"
    echo "    Start: launchctl load ~/Library/LaunchAgents/$PLIST_NAME"
    echo ""
    echo "NOTE: The first run will prompt for Microphone and Accessibility permissions."
    echo "      Grant both in System Settings > Privacy & Security."
}

case "$OS" in
    Linux)
        install_linux
        ;;
    Darwin)
        install_macos
        ;;
    *)
        echo "Unsupported operating system: $OS"
        exit 1
        ;;
esac
