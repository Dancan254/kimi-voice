# kimi-voice

Push-to-talk voice input for Kimi Code CLI on Ubuntu and macOS. Hold a key, speak, release — the transcription is typed into the focused window (or copied to the clipboard on Wayland / macOS).

## Features

- Local transcription with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (no cloud, no API key).
- Whisper model is loaded once and reused — no pause between phrases.
- Listens to **all** keyboards and mice simultaneously.
- Configurable push-to-talk key with hold, toggle, or double-tap modes.
- Energy-based voice activity detection trims silence before transcription.
- Spoken punctuation/commands are converted to real characters.
- System tray icon with live status and one-click model switching.
- Post-transcription action menu on Wayland (type, copy, discard).
- Systemd user services for `ydotoold` and `kimi-voice` so it runs automatically.

## Requirements

### Ubuntu

- Ubuntu (tested on 24.04 with Wayland)
- A microphone
- `sudo` access for installing dependencies and udev rules

### macOS

- macOS 12+ (Monterey or later)
- A microphone
- [Homebrew](https://brew.sh)
- Microphone and Accessibility permissions granted on first run

## Quick install

```bash
git clone https://github.com/Dancan254/kimi-voice.git
cd kimi-voice
./install.sh
```

The installer detects your OS (Linux/macOS) and runs the appropriate steps.

## Updating

If you already installed an earlier version, pull the latest code and re-run the installer. It will upgrade the virtual environment, replace the app scripts, and restart the services without overwriting your config file.

```bash
cd kimi-voice
git pull
./install.sh
```

The installer will:

1. Install system packages (`ydotool`, `python3-venv`, `python3-tk`, `portaudio`, `xclip` on Ubuntu; `portaudio` via Homebrew on macOS).
2. Create or upgrade a Python virtual environment at `~/.venv/kimi-voice`.
3. Copy the scripts to `~/bin/kimi-voice`.
4. On Ubuntu: install udev rules so the script can read `/dev/input/event*` without `sudo`.
5. On Ubuntu: add you to the `input` group (log out and back in if this is the first install).
6. On Ubuntu: enable and start the systemd user services. On macOS: install and load a LaunchAgent.

## Usage

After install, the service is already running. Hold **Right Ctrl** (Linux) or **Left Ctrl** (macOS), speak, release. The transcription is typed into the focused window.

On macOS, the system tray icon is disabled by default because the pystray backend can crash on some macOS versions. You can re-enable it by setting `"tray": { "enabled": true }` in `~/.config/kimi-voice/config.json`.

Run manually:

```bash
kimi-voice              # continuous mode with tray icon
kimi-voice --once       # one phrase, then exit
kimi-voice --no-tray    # continuous mode without tray icon
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
  "push_to_talk_mode": "hold",
  "double_tap_ms": 300,
  "max_recording_seconds": 60,
  "whisper_model": "base",
  "whisper_options": {
    "condition_on_previous_text": false,
    "vad_filter": true,
    "beam_size": 5,
    "best_of": 5
  },
  "language": "en",
  "sample_rate": 16000,
  "vad": {
    "enabled": true,
    "energy_threshold": 0.01,
    "min_speech_duration_ms": 250,
    "prefix_ms": 200
  },
  "commands": {
    "enabled": true,
    "map": { "period": ".", "comma": ",", "new line": "\n" }
  },
  "streaming": {
    "enabled": true,
    "chunk_seconds": 10,
    "overlap_seconds": 1,
    "output_mode": "accumulate"
  },
  "wayland_typing": {
    "preferred": ["wtype", "ydotool"],
    "fallback_to_clipboard": true,
    "action_menu": true
  },
  "audio": {
    "channels": 1,
    "dtype": "int16"
  },
  "tray": {
    "enabled": true
  }
}
```

Supported push-to-talk keys:

- Keyboard: `RIGHTCTRL`, `LEFTCTRL`, `RIGHTALT`, `LEFTALT`, `SCROLLLOCK`, `F13`, `F14`, `F15`, `SPACE`
- Mouse: `MOUSELEFT`, `MOUSERIGHT`, `MOUSEMIDDLE`, `MOUSESIDE`, `MOUSEEXTRA`

Push-to-talk modes:

- `hold` — record while the key is held (default)
- `toggle` — tap to start, tap again to stop
- `double_tap` — double-tap to toggle recording

## Wayland typing

On Wayland the script tries, in order:

1. `wtype` — direct typing, but must be built from source on Ubuntu.
2. `ydotool` — uses the `ydotoold` daemon (installed by `install.sh`).
3. Action menu — a small popup lets you type, copy, or discard the transcription.
4. Clipboard fallback — copies the text with `wl-copy` / `xclip` so you can paste with `Ctrl+V`.

## Commands / spoken punctuation

When `commands.enabled` is true, phrases like these are converted:

| Say | Output |
|-----|--------|
| period / dot | `.` |
| comma | `,` |
| new line | `\n` |
| open bracket | `(` |
| close curly | `}` |
| tab | `\t` |

Add your own mappings in `config.json` under `commands.map`.

## System tray

Right-click the tray icon to:

- See current status (ready / recording)
- Switch Whisper models (`tiny`, `base`, `small`, `medium`, `large-v2`)
- Reload config without restarting
- Exit

To disable the tray icon, set `tray.enabled` to `false` or run `kimi-voice --no-tray`.

## Background service

### Ubuntu (systemd)

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

### macOS (LaunchAgent)

```bash
launchctl list com.dancan254.kimi-voice
# Logs:
tail -f /tmp/kimi-voice.out.log /tmp/kimi-voice.err.log
```

To stop:

```bash
launchctl bootout gui/$(id -u)/com.dancan254.kimi-voice
```

To start again:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dancan254.kimi-voice.plist
```

## Streaming / long audio

When `streaming.enabled` is `true`, audio is transcribed in overlapping chunks while you are still holding the push-to-talk key. This makes long dictation sessions feel much faster because most chunks are already done by the time you release the key.

- `chunk_seconds` — how often a chunk is sent to Whisper (default 10).
- `overlap_seconds` — how much audio is kept between chunks for context (default 1).
- `output_mode` — `accumulate` waits until release and outputs everything at once; `realtime` types each chunk as it is ready (X11 only).

Set `streaming.enabled` to `false` to transcribe the whole recording in one go.

## Performance tuning

Speed depends mostly on your CPU/GPU and the Whisper model size.

| Model | Accuracy | Speed | VRAM / RAM |
|-------|----------|-------|------------|
| `tiny` | Lowest | Fastest | ~1 GB |
| `base` | Good | Fast | ~1 GB |
| `small` | Better | Medium | ~2 GB |
| `medium` | High | Slow | ~5 GB |
| `large-v2` | Highest | Slowest | ~10 GB |

**To go faster:**

1. Switch to a smaller model from the tray menu or by editing `whisper_model`.
2. Lower `whisper_options.beam_size` to `1` (fastest, slightly less accurate).
3. Keep `whisper_options.vad_filter` enabled — it skips silence.
4. Reduce `max_recording_seconds` to avoid runaway recordings.
5. Shrink `streaming.chunk_seconds` so each chunk is smaller (more overhead but lower latency).

**If you have an NVIDIA GPU:**

Edit `~/.config/systemd/user/kimi-voice.service` and add an environment variable:

```ini
[Service]
Environment="WHISPER_DEVICE=cuda"
```

Then reload and restart:

```bash
systemctl --user daemon-reload
systemctl --user restart kimi-voice.service
```

GPU support requires the CUDA version of the Whisper backend. On CPU-only machines the default `cpu` / `int8` settings are used.

## Logs

The script logs to the systemd journal. Follow live:

```bash
journalctl --user -u kimi-voice.service -f
```

## macOS permissions

On macOS, `kimi-voice` uses `pynput` to listen for the push-to-talk key and type the transcription. The first time the service runs, macOS will prompt for:

- **Microphone** — needed to record your voice.
- **Accessibility** — needed to listen for global hotkeys and type into other apps.

Grant both in **System Settings > Privacy & Security**. If the app cannot type, re-check the Accessibility permission.

## Security note

This tool reads raw keyboard and mouse events. On Ubuntu it reads from `/dev/input/event*`; on macOS it uses global input listeners via `pynput`. It is intended for personal use on your own machine. Do not install it on shared or untrusted systems.

## Uninstall

### Ubuntu

```bash
# Stop services
systemctl --user stop kimi-voice.service
systemctl --user stop ydotoold.service
systemctl --user disable kimi-voice.service
systemctl --user disable ydotoold.service

# Remove files
rm -f ~/.config/systemd/user/kimi-voice.service
rm -f ~/.config/systemd/user/ydotoold.service
rm -f ~/bin/kimi-voice
rm -f ~/bin/kimi-voice.py
rm -rf ~/.venv/kimi-voice
rm -rf ~/.config/kimi-voice
rm -f /tmp/kimi-voice*.log

# Remove udev rules
sudo rm -f /etc/udev/rules.d/50-kimi-voice.rules
sudo udevadm control --reload-rules
```

### macOS

```bash
# Stop and unload the background service
launchctl bootout gui/$(id -u)/com.dancan254.kimi-voice || true

# Remove files
rm -f ~/Library/LaunchAgents/com.dancan254.kimi-voice.plist
rm -f ~/bin/kimi-voice
rm -f ~/bin/kimi-voice.py
rm -f ~/bin/kimi-voice-macos-wrapper
rm -rf ~/.venv/kimi-voice
rm -rf ~/.config/kimi-voice
rm -f /tmp/kimi-voice*.log
```

Then manually remove the Accessibility permission:

1. **System Settings → Privacy & Security → Accessibility**
2. Select **kimi-voice**
3. Click the **–** button

## License

MIT — see [LICENSE](LICENSE).
