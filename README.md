# kimi-voice

Push-to-talk voice input for Kimi Code CLI on Ubuntu. Hold a key, speak, release — the transcription is typed into the focused window (or copied to the clipboard on Wayland).

## Features

- Local transcription with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (no cloud, no API key).
- Works on both X11 and Wayland.
- Configurable push-to-talk key, model, and language.
- Auto-reconnects if the keyboard device is disconnected.
- Retries the audio stream if the mic is briefly busy.
- Systemd user services for `ydotoold` and `kimi-voice` so it runs automatically.

## Requirements

- Ubuntu (tested on 24.04 with Wayland)
- A microphone
- `sudo` access for installing dependencies and udev rules

## Quick install

```bash
git clone https://github.com/Dancan254/kimi-voice.git
cd kimi-voice
./install.sh
```

The installer will:

1. Install system packages (`ydotool`, `python3-venv`, `portaudio`, `xclip`, etc.).
2. Create a Python virtual environment at `~/.venv/kimi-voice`.
3. Copy the scripts to `~/bin/kimi-voice`.
4. Install udev rules so the script can read `/dev/input/event*` without `sudo`.
5. Add you to the `input` group (log out and back in if this is the first install).
6. Enable and start the systemd user services.

## Usage

After install, the service is already running. Hold **Right Ctrl**, speak, release. The transcription is typed (X11) or copied to the clipboard (Wayland).

Run manually:

```bash
kimi-voice              # continuous mode
kimi-voice --once       # one phrase, then exit
```

Inside Kimi Code CLI shell mode:

```bash
!kimi-voice --once
```

## Configuration

Edit `~/.config/kimi-voice/config.json`:

```json
{
  "push_to_talk_key": "RIGHTCTRL",
  "whisper_model": "base",
  "language": "en",
  "sample_rate": 16000,
  "wayland_typing": {
    "preferred": ["wtype", "ydotool"],
    "fallback_to_clipboard": true
  },
  "audio": {
    "channels": 1,
    "dtype": "int16"
  }
}
```

Supported push-to-talk keys: `RIGHTCTRL`, `LEFTCTRL`, `RIGHTALT`, `LEFTALT`, `SCROLLLOCK`, `F13`, `F14`, `F15`, `SPACE`.

## Wayland typing

On Wayland the script tries, in order:

1. `wtype` — direct typing, but must be built from source on Ubuntu.
2. `ydotool` — uses the `ydotoold` daemon (installed by `install.sh`).
3. Clipboard fallback — copies the text with `wl-copy` / `xclip` so you can paste with `Ctrl+V`.

## Systemd services

```bash
systemctl --user status ydotoold.service
systemctl --user status kimi-voice.service
journalctl --user -u kimi-voice.service -f
```

To stop:

```bash
systemctl --user stop kimi-voice.service
systemctl --user stop ydotoold.service
```

To disable auto-start:

```bash
systemctl --user disable kimi-voice.service
systemctl --user disable ydotoold.service
```

## Logs

The script logs to the systemd journal. Follow live:

```bash
journalctl --user -u kimi-voice.service -f
```

## Security note

This tool reads raw keyboard events from `/dev/input/event*`. It is intended for personal use on your own machine. Do not install it on shared or untrusted systems.

## License

MIT
