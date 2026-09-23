#!/usr/bin/env python3
"""
kimi-voice — push-to-talk voice input for Kimi Code CLI (and any terminal).

Hold the configured key (default: Right Ctrl), speak, release. The transcription
is typed into the focused window or copied to the clipboard on Wayland.

Usage:
    kimi-voice                         # run continuously until Ctrl-C
    kimi-voice --once                  # record one phrase, then exit
    kimi-voice --config ~/.config/kimi-voice/config.json
    kimi-voice /dev/input/event3       # use a specific keyboard device

Inside Kimi Code CLI shell mode:
    !kimi-voice --once
"""

import argparse
import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import traceback
import wave
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
from evdev import InputDevice, list_devices, ecodes as e
from pynput.keyboard import Controller

DEFAULT_CONFIG = Path.home() / ".config" / "kimi-voice" / "config.json"
KEY_NAME_TO_CODE = {
    "RIGHTCTRL": e.KEY_RIGHTCTRL,
    "LEFTCTRL": e.KEY_LEFTCTRL,
    "RIGHTALT": e.KEY_RIGHTALT,
    "LEFTALT": e.KEY_LEFTALT,
    "SCROLLLOCK": e.KEY_SCROLLLOCK,
    "F13": e.KEY_F13,
    "F14": e.KEY_F14,
    "F15": e.KEY_F15,
    "SPACE": e.KEY_SPACE,
}

keyboard = Controller()
recording = False
audio_frames = []
record_thread = None
stop_recording = threading.Event()


def load_config(path: Path):
    if not path.exists():
        return default_config()
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as ex:
        print(f"Config file {path} is invalid JSON: {ex}", file=sys.stderr)
        print("Falling back to defaults.", file=sys.stderr)
        return default_config()


def default_config():
    return {
        "push_to_talk_key": "RIGHTCTRL",
        "whisper_model": "base",
        "language": "en",
        "sample_rate": 16000,
        "wayland_typing": {
            "preferred": ["wtype", "ydotool"],
            "fallback_to_clipboard": True,
        },
        "audio": {"channels": 1, "dtype": "int16"},
    }


def key_code_from_config(config):
    name = config.get("push_to_talk_key", "RIGHTCTRL").upper()
    code = KEY_NAME_TO_CODE.get(name)
    if code is None:
        print(f"Unknown push-to-talk key '{name}'. Using RIGHTCTRL.", file=sys.stderr)
        code = e.KEY_RIGHTCTRL
    return code


def is_virtual_device(dev):
    name = dev.name.lower()
    return any(x in name for x in ["virtual", "ydotool", "dummy", "qemu", "vmware"])


def is_keyboard(dev):
    try:
        caps = dev.capabilities().get(e.EV_KEY, [])
    except Exception:
        return False
    if is_virtual_device(dev):
        return False
    return e.KEY_SPACE in caps and e.KEY_A in caps and len(caps) > 50


def enumerate_devices():
    paths = list_devices()
    if paths:
        return [InputDevice(p) for p in paths]

    devices = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            devices.append(InputDevice(path))
        except Exception:
            pass
    return devices


def find_keyboard_device(preferred_path=None, ptt_key=e.KEY_RIGHTCTRL):
    if preferred_path:
        try:
            dev = InputDevice(preferred_path)
            if is_keyboard(dev):
                return dev
            print(f"{preferred_path} does not look like a keyboard.", file=sys.stderr)
        except Exception as ex:
            print(f"Cannot open {preferred_path}: {ex}", file=sys.stderr)
        return None

    devices = enumerate_devices()
    if not devices:
        return None

    for dev in devices:
        if is_keyboard(dev) and ptt_key in dev.capabilities().get(e.EV_KEY, []):
            return dev

    for dev in reversed(devices):
        if is_keyboard(dev):
            return dev

    return None


def check_microphone():
    try:
        devices = sd.query_devices()
        inputs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
        if not inputs:
            raise RuntimeError("No microphone found.")

        # Prefer software audio servers (PulseAudio/PipeWire) because they
        # resample to any rate and are usually stable. Fall back to the
        # ALSA default, then to the first available input.
        for label in ["pulse", "pipewire", "default"]:
            for idx, dev in inputs:
                if label in dev["name"].lower():
                    return idx, dev["name"]

        return inputs[0][0], inputs[0][1]["name"]
    except Exception as ex:
        raise RuntimeError(f"Could not detect microphone: {ex}")


def record_audio(device, sample_rate, channels, dtype):
    global audio_frames
    audio_frames = []
    stop_recording.clear()

    def callback(indata, frames, _time, status):
        if status:
            logging.warning("Audio status: %s", status)
        audio_frames.append(indata.copy())

    last_error = None
    for attempt in range(3):
        try:
            with sd.InputStream(
                device=device,
                samplerate=sample_rate,
                channels=channels,
                dtype=dtype,
                callback=callback,
            ):
                stop_recording.wait()
            return
        except Exception as ex:
            last_error = ex
            logging.warning("Audio stream failed (attempt %d): %s", attempt + 1, ex)
            if not stop_recording.is_set():
                time.sleep(0.3 * (attempt + 1))

    logging.error("Could not open microphone after 3 attempts: %s", last_error)
    raise last_error


def save_wav(path, sample_rate, channels):
    if not audio_frames:
        return False
    data = np.concatenate(audio_frames, axis=0)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(data.tobytes())
    return True


def transcribe(path, model_name, language):
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(path), language=language, condition_on_previous_text=False)
    return " ".join(segment.text.strip() for segment in segments).strip()


def type_with_wtype(text):
    if shutil.which("wtype") is None:
        return False
    try:
        subprocess.run(["wtype", text], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def type_with_ydotool(text):
    if shutil.which("ydotool") is None or shutil.which("ydotoold") is None:
        return False
    try:
        subprocess.run(["ydotool", "type", text], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def copy_to_clipboard(text):
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type == "wayland":
        try:
            subprocess.run(["wl-copy"], input=text, text=True, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            pass
    try:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def type_text(text, config):
    time.sleep(0.2)

    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type == "wayland":
        for tool in config.get("wayland_typing", {}).get("preferred", ["wtype", "ydotool"]):
            if tool == "wtype" and type_with_wtype(text):
                return
            if tool == "ydotool" and type_with_ydotool(text):
                return

        if config.get("wayland_typing", {}).get("fallback_to_clipboard", True):
            if copy_to_clipboard(text):
                print("[copied to clipboard — paste with Ctrl+V]")
                return

        print(f"[transcription] {text}")
        return

    # X11: use pynput
    try:
        keyboard.type(text)
    except Exception as ex:
        print(f"[could not type: {ex}]", file=sys.stderr)
        if copy_to_clipboard(text):
            print("[copied to clipboard — paste with Ctrl+V]")
        else:
            print(f"[transcription] {text}")


def print_devices(ptt_key):
    print("\nDetected input devices:", file=sys.stderr)
    for d in enumerate_devices():
        try:
            caps = d.capabilities().get(e.EV_KEY, [])
            has_ptt = ptt_key in caps
            print(f"  {d.path}: {d.name} (has_ptt={has_ptt})", file=sys.stderr)
        except Exception:
            print(f"  {d.path}: {d.name} (could not read caps)", file=sys.stderr)


def setup_logging():
    log_path = Path("/tmp/kimi-voice.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stderr),
        ],
    )


def process_events(dev, ptt_key, mic_index, sample_rate, channels, dtype, model_name, language, config, args):
    global recording, record_thread

    for event in dev.read_loop():
        if event.type != e.EV_KEY:
            continue

        if event.code == ptt_key:
            if event.value == 1 and not recording:
                recording = True
                record_thread = threading.Thread(
                    target=record_audio, args=(mic_index, sample_rate, channels, dtype)
                )
                record_thread.start()
                print("\n[listening...]")

            elif event.value == 0 and recording:
                stop_recording.set()
                record_thread.join()
                recording = False

                tmp = Path("/tmp/kimi_voice.wav")
                if save_wav(tmp, sample_rate, channels):
                    try:
                        text = transcribe(tmp, model_name, language)
                        if text:
                            print(f"[transcribed] {text}")
                            type_text(text + " ", config)
                    except Exception as ex:
                        logging.error("Transcription failed: %s", ex)
                    tmp.unlink(missing_ok=True)

                if args.once:
                    return


def main():
    global recording, record_thread

    parser = argparse.ArgumentParser(description="Push-to-talk voice input for Kimi Code CLI")
    parser.add_argument("--once", action="store_true", help="record one phrase and exit")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to config JSON")
    parser.add_argument("device", nargs="?", help="keyboard device path (e.g. /dev/input/event3)")
    args = parser.parse_args()

    setup_logging()
    config = load_config(args.config)
    ptt_key = key_code_from_config(config)

    try:
        mic_index, mic_name = check_microphone()
    except RuntimeError as ex:
        logging.error("%s", ex)
        sys.exit(1)

    dev = find_keyboard_device(args.device, ptt_key)
    if not dev:
        print("No keyboard device found.", file=sys.stderr)
        print_devices(ptt_key)
        print(f"\nTry passing a device path, e.g.:\n  kimi-voice /dev/input/event3", file=sys.stderr)
        sys.exit(1)

    print(f"Using keyboard: {dev.name} at {dev.path}")
    print(f"Using microphone: {mic_name}")
    print(f"Push-to-talk key: {config.get('push_to_talk_key', 'RIGHTCTRL')}")
    if args.once:
        print("Hold push-to-talk key to record one phrase. Release to finish.")
    else:
        print("Hold push-to-talk key to record. Release to transcribe.")
        print("Press Ctrl-C to stop.")

    sample_rate = config.get("sample_rate", 16000)
    channels = config.get("audio", {}).get("channels", 1)
    dtype = config.get("audio", {}).get("dtype", "int16")
    model_name = config.get("whisper_model", "base")
    language = config.get("language", "en")

    try:
        while True:
            try:
                process_events(dev, ptt_key, mic_index, sample_rate, channels, dtype, model_name, language, config, args)
                break
            except OSError as ex:
                logging.error("Input device disconnected (%s). Reconnecting in 2s...", ex)
                time.sleep(2)
                new_dev = find_keyboard_device(args.device, ptt_key)
                if not new_dev:
                    logging.error("Keyboard not available yet. Retrying...")
                    time.sleep(3)
                    continue
                dev = new_dev
                logging.info("Reconnected to keyboard: %s at %s", dev.name, dev.path)
    except KeyboardInterrupt:
        print("\nStopping.")
    except Exception as ex:
        logging.error("Unexpected error: %s\n%s", ex, traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
