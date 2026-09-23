#!/usr/bin/env python3
"""
kimi-voice — push-to-talk voice input for Kimi Code CLI (and any terminal).

Hold a key, speak, release. The transcription is typed into the focused window
or copied to the clipboard on Wayland. Runs as a tray icon with model switching
and a post-transcription action menu.

Usage:
    kimi-voice                         # run continuously in the tray
    kimi-voice --once                  # record one phrase, then exit
    kimi-voice --config ~/.config/kimi-voice/config.json
    kimi-voice --no-tray               # run without the tray icon

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
import tempfile
import threading
import time
import traceback
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

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

MOUSE_BUTTON_TO_CODE = {
    "MOUSELEFT": e.BTN_LEFT,
    "MOUSERIGHT": e.BTN_RIGHT,
    "MOUSEMIDDLE": e.BTN_MIDDLE,
    "MOUSESIDE": e.BTN_SIDE,
    "MOUSEEXTRA": e.BTN_EXTRA,
}

ALL_KEY_CODES = {**KEY_NAME_TO_CODE, **MOUSE_BUTTON_TO_CODE}

COMMANDS = {
    "period": ".",
    "dot": ".",
    "comma": ",",
    "question mark": "?",
    "exclamation mark": "!",
    "colon": ":",
    "semicolon": ";",
    "new line": "\n",
    "newline": "\n",
    "new paragraph": "\n\n",
    "tab": "\t",
    "open bracket": "(",
    "close bracket": ")",
    "open curly": "{",
    "close curly": "}",
    "open square": "[",
    "close square": "]",
    "quote": '"',
    "single quote": "'",
    "backtick": "`",
    "dash": "-",
    "underscore": "_",
    "equals": "=",
    "plus": "+",
    "slash": "/",
    "backslash": "\\",
    "asterisk": "*",
    "ampersand": "&",
    "percent": "%",
    "dollar": "$",
    "hash": "#",
    "at": "@",
    "exclamation": "!",
}


@dataclass
class State:
    recording: bool = False
    audio_frames: List[np.ndarray] = field(default_factory=list)
    record_thread: Optional[threading.Thread] = None
    stop_recording: threading.Event = field(default_factory=threading.Event)
    last_text: str = ""
    model_name: str = "base"
    tray_status: str = "ready"
    lock: threading.Lock = field(default_factory=threading.Lock)


def default_config():
    return {
        "push_to_talk_key": "RIGHTCTRL",
        "push_to_talk_mode": "hold",
        "double_tap_ms": 300,
        "whisper_model": "base",
        "language": "en",
        "sample_rate": 16000,
        "vad": {
            "enabled": True,
            "energy_threshold": 0.01,
            "min_speech_duration_ms": 250,
            "prefix_ms": 200,
        },
        "commands": {
            "enabled": True,
            "map": COMMANDS,
        },
        "wayland_typing": {
            "preferred": ["wtype", "ydotool"],
            "fallback_to_clipboard": True,
            "action_menu": True,
        },
        "audio": {"channels": 1, "dtype": "int16"},
        "tray": {"enabled": True},
    }


def load_config(path: Path):
    if not path.exists():
        return default_config()
    try:
        with open(path) as f:
            cfg = json.load(f)
        base = default_config()
        base.update(cfg)
        for key in base:
            if isinstance(base[key], dict) and key in cfg:
                base[key] = {**default_config()[key], **cfg[key]}
        return base
    except json.JSONDecodeError as ex:
        logging.error("Config file %s is invalid JSON: %s", path, ex)
        return default_config()


def save_config(path: Path, config: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def key_code_from_config(config):
    name = config.get("push_to_talk_key", "RIGHTCTRL").upper()
    code = ALL_KEY_CODES.get(name)
    if code is None:
        logging.warning("Unknown push-to-talk key '%s'. Using RIGHTCTRL.", name)
        code = e.KEY_RIGHTCTRL
    return code


def is_virtual_device(dev):
    name = dev.name.lower()
    return any(x in name for x in ["virtual", "ydotool", "dummy", "qemu", "vmware", "kimi-voice"])


def is_keyboard(dev):
    try:
        caps = dev.capabilities().get(e.EV_KEY, [])
    except Exception:
        return False
    if is_virtual_device(dev):
        return False
    return e.KEY_SPACE in caps and e.KEY_A in caps and len(caps) > 50


def is_mouse(dev):
    try:
        caps = dev.capabilities()
    except Exception:
        return False
    if is_virtual_device(dev):
        return False
    keys = caps.get(e.EV_KEY, [])
    rel = caps.get(e.EV_REL, [])
    return e.BTN_LEFT in keys and e.REL_X in rel


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


def find_input_devices(ptt_key: int) -> Tuple[List[InputDevice], List[InputDevice]]:
    keyboards, mice = [], []
    for dev in enumerate_devices():
        if is_keyboard(dev):
            keyboards.append(dev)
        elif is_mouse(dev) and ptt_key in [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_SIDE, e.BTN_EXTRA]:
            mice.append(dev)
    return keyboards, mice


def check_microphone():
    devices = sd.query_devices()
    inputs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    if not inputs:
        raise RuntimeError("No microphone found.")
    for label in ["pulse", "pipewire", "default"]:
        for idx, dev in inputs:
            if label in dev["name"].lower():
                return idx, dev["name"]
    return inputs[0][0], inputs[0][1]["name"]


def rms_energy(frame: np.ndarray) -> float:
    data = frame.astype(np.float64)
    if len(data) == 0:
        return 0.0
    return float(np.sqrt(np.mean(data ** 2)))


def trim_silence(frames: List[np.ndarray], sample_rate: int, threshold: float,
                 prefix_ms: int, min_speech_ms: int) -> List[np.ndarray]:
    if not frames:
        return frames

    prefix_frames = max(1, int(prefix_ms * sample_rate / 1000 / frames[0].shape[0]))
    energies = [rms_energy(f) for f in frames]

    first_speech = None
    for i, energy in enumerate(energies):
        if energy > threshold:
            first_speech = max(0, i - prefix_frames)
            break

    if first_speech is None:
        return []

    last_speech = None
    for i in range(len(energies) - 1, -1, -1):
        if energies[i] > threshold:
            last_speech = i
            break

    min_frames = max(1, int(min_speech_ms * sample_rate / 1000 / frames[0].shape[0]))
    last_speech = max(last_speech, first_speech + min_frames - 1)
    return frames[first_speech:last_speech + 1]


def record_audio(state: State, device: int, sample_rate: int, channels: int, dtype: str, config: dict):
    with state.lock:
        state.audio_frames = []
    state.stop_recording.clear()

    def callback(indata, frames, _time, status):
        if status:
            logging.warning("Audio status: %s", status)
        with state.lock:
            state.audio_frames.append(indata.copy())

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
                state.stop_recording.wait()
            return
        except Exception as ex:
            last_error = ex
            logging.warning("Audio stream failed (attempt %d): %s", attempt + 1, ex)
            if not state.stop_recording.is_set():
                time.sleep(0.3 * (attempt + 1))

    logging.error("Could not open microphone after 3 attempts: %s", last_error)
    raise last_error


def save_wav(path: Path, frames: List[np.ndarray], sample_rate: int, channels: int):
    if not frames:
        return False
    data = np.concatenate(frames, axis=0)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(data.tobytes())
    return True


class WhisperTranscriber:
    def __init__(self, model_name: str):
        from faster_whisper import WhisperModel
        self.model_name = model_name
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8")

    def transcribe(self, path: Path, language: str) -> str:
        segments, _ = self.model.transcribe(str(path), language=language, condition_on_previous_text=False)
        return " ".join(segment.text.strip() for segment in segments).strip()

    def reload(self, model_name: str):
        if self.model_name != model_name:
            logging.info("Switching Whisper model from %s to %s", self.model_name, model_name)
            self.__init__(model_name)


def process_commands(text: str, command_map: dict) -> str:
    lower = text.lower()
    for phrase, replacement in command_map.items():
        lower = lower.replace(phrase, replacement)
    return lower


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


def show_action_menu(text: str) -> str:
    """Return 'copy', 'type', or 'discard'."""
    result = {"action": "discard"}

    def on_copy():
        result["action"] = "copy"
        root.destroy()

    def on_type():
        result["action"] = "type"
        root.destroy()

    def on_discard():
        result["action"] = "discard"
        root.destroy()

    try:
        import tkinter as tk
        root = tk.Tk()
        root.title("kimi-voice")
        root.geometry("400x180")
        root.attributes("-topmost", True)
        tk.Label(root, text="Transcription:", anchor="w").pack(fill="x", padx=10, pady=(10, 0))
        entry = tk.Entry(root)
        entry.insert(0, text)
        entry.pack(fill="x", padx=10, pady=5)
        entry.select_range(0, tk.END)
        btn = tk.Frame(root)
        btn.pack(fill="x", padx=10, pady=10)
        tk.Button(btn, text="Type", command=on_type).pack(side="left", expand=True, fill="x", padx=5)
        tk.Button(btn, text="Copy", command=on_copy).pack(side="left", expand=True, fill="x", padx=5)
        tk.Button(btn, text="Discard", command=on_discard).pack(side="left", expand=True, fill="x", padx=5)
        entry.focus_set()
        root.mainloop()
    except Exception as ex:
        logging.error("Action menu failed: %s", ex)

    return result["action"]


class OutputManager:
    def __init__(self, config: dict):
        self.config = config
        self.keyboard = Controller()

    def type_text(self, text: str, force_action_menu: bool = False) -> bool:
        time.sleep(0.1)

        if force_action_menu:
            action = show_action_menu(text)
            if action == "copy":
                return copy_to_clipboard(text)
            if action == "type":
                pass
            else:
                return False

        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session_type == "wayland":
            for tool in self.config.get("wayland_typing", {}).get("preferred", ["wtype", "ydotool"]):
                if tool == "wtype" and type_with_wtype(text):
                    return True
                if tool == "ydotool" and type_with_ydotool(text):
                    return True

            if self.config.get("wayland_typing", {}).get("fallback_to_clipboard", True):
                if self.config.get("wayland_typing", {}).get("action_menu", True):
                    action = show_action_menu(text)
                    if action == "copy":
                        return copy_to_clipboard(text)
                    if action == "type":
                        return type_with_ydotool(text) or copy_to_clipboard(text)
                else:
                    if copy_to_clipboard(text):
                        print("[copied to clipboard — paste with Ctrl+V]")
                        return True

            print(f"[transcription] {text}")
            return False

        try:
            self.keyboard.type(text)
            return True
        except Exception as ex:
            logging.error("Could not type: %s", ex)
            if copy_to_clipboard(text):
                print("[copied to clipboard — paste with Ctrl+V]")
                return True
            print(f"[transcription] {text}")
            return False


class TrayManager:
    def __init__(self, app: "VoiceApp"):
        self.app = app
        self.icon = None
        self._stop = threading.Event()

    def _build_menu(self):
        from pystray import Menu, MenuItem
        models = ["tiny", "base", "small", "medium", "large-v2"]

        def model_item(name):
            def setter(_):
                self.app.set_model(name)
            return MenuItem(name, setter, checked=lambda _, n=name: self.app.state.model_name == n)

        return Menu(
            MenuItem(lambda _: f"Status: {self.app.state.tray_status}", lambda _: None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("Model", Menu(*[model_item(m) for m in models])),
            MenuItem("Reload config", lambda _: self.app.reload_config()),
            Menu.SEPARATOR,
            MenuItem("Exit", lambda _: self.app.stop()),
        )

    def _create_image(self, color: str):
        from PIL import Image, ImageDraw
        width = 64
        height = 64
        image = Image.new("RGB", (width, height), color)
        dc = ImageDraw.Draw(image)
        dc.ellipse([8, 8, width - 8, height - 8], fill="white")
        return image

    def update_status(self, status: str):
        color = {"ready": "green", "recording": "red", "error": "orange"}.get(status, "gray")
        self.state.tray_status = status
        if self.icon:
            self.icon.icon = self._create_image(color)
            self.icon.title = f"kimi-voice ({status})"

    def run(self):
        from pystray import Icon, Menu, MenuItem
        try:
            self.icon = Icon(
                "kimi-voice",
                icon=self._create_image("green"),
                title="kimi-voice (ready)",
                menu=self._build_menu(),
            )
            self.icon.run()
        except Exception as ex:
            logging.error("Tray icon failed: %s", ex)

    def stop(self):
        if self.icon:
            self.icon.stop()


class InputDeviceManager:
    def __init__(self, app: "VoiceApp"):
        self.app = app
        self.devices: List[InputDevice] = []
        self.threads: List[threading.Thread] = []
        self._stop = threading.Event()

    def open_devices(self):
        ptt_key = self.app.ptt_key
        keyboards, mice = find_input_devices(ptt_key)
        targets = keyboards + mice
        if not targets:
            raise RuntimeError("No keyboard or mouse device found.")
        names = ", ".join(f"{d.name} ({d.path})" for d in targets)
        logging.info("Listening on devices: %s", names)
        self.devices = targets
        return targets

    def start(self):
        targets = self.open_devices()
        for dev in targets:
            t = threading.Thread(target=self._read_loop, args=(dev,), daemon=True)
            t.start()
            self.threads.append(t)

    def _read_loop(self, dev: InputDevice):
        ptt_key = self.app.ptt_key
        mode = self.app.config.get("push_to_talk_mode", "hold")
        double_tap_ms = self.app.config.get("double_tap_ms", 300)
        last_release = 0

        while not self._stop.is_set():
            try:
                for event in dev.read_loop():
                    if self._stop.is_set():
                        break
                    if event.type != e.EV_KEY:
                        continue

                    if event.code != ptt_key:
                        continue

                    now = time.time() * 1000

                    if event.value == 1:
                        if mode == "hold":
                            self.app.start_recording()
                        elif mode == "toggle":
                            if not self.app.state.recording:
                                self.app.start_recording()
                            else:
                                self.app.stop_recording()
                        elif mode == "double_tap":
                            if now - last_release < double_tap_ms:
                                if not self.app.state.recording:
                                    self.app.start_recording()
                                else:
                                    self.app.stop_recording()

                    elif event.value == 0:
                        if mode == "hold":
                            self.app.stop_recording()
                        elif mode == "double_tap":
                            last_release = now

            except Exception as ex:
                logging.error("Input device %s error: %s", dev.path, ex)
                time.sleep(2)
                try:
                    dev = InputDevice(dev.path)
                    self.devices = [d if d.path != dev.path else dev for d in self.devices]
                except Exception:
                    pass

    def stop(self):
        self._stop.set()
        for dev in self.devices:
            try:
                dev.close()
            except Exception:
                pass
        for t in self.threads:
            t.join(timeout=1)


class VoiceApp:
    def __init__(self, args, config: dict):
        self.args = args
        self.config_path = args.config
        self.config = config
        self.state = State(model_name=config.get("whisper_model", "base"))
        self.ptt_key = key_code_from_config(config)
        self.transcriber: Optional[WhisperTranscriber] = None
        self.output: Optional[OutputManager] = None
        self.input_manager: Optional[InputDeviceManager] = None
        self.tray: Optional[TrayManager] = None

    def setup(self):
        self.mic_index, self.mic_name = check_microphone()
        self.output = OutputManager(self.config)
        self.transcriber = WhisperTranscriber(self.state.model_name)
        self.input_manager = InputDeviceManager(self)

        logging.info("Using microphone: %s", self.mic_name)
        logging.info("Push-to-talk key: %s", self.config.get("push_to_talk_key", "RIGHTCTRL"))
        logging.info("Mode: %s", self.config.get("push_to_talk_mode", "hold"))

    def start_recording(self):
        with self.state.lock:
            if self.state.recording:
                return
            self.state.recording = True
            self.state.audio_frames = []
        self.state.stop_recording.clear()
        self.state.record_thread = threading.Thread(
            target=record_audio,
            args=(self.state, self.mic_index, self.config.get("sample_rate", 16000),
                  self.config["audio"]["channels"], self.config["audio"]["dtype"], self.config),
            daemon=True,
        )
        self.state.record_thread.start()
        if self.tray:
            self.tray.update_status("recording")
        print("\n[listening...]")

    def stop_recording(self):
        with self.state.lock:
            if not self.state.recording:
                return
        self.state.stop_recording.set()
        if self.state.record_thread:
            self.state.record_thread.join()
        with self.state.lock:
            self.state.recording = False
            frames = list(self.state.audio_frames)

        if self.tray:
            self.tray.update_status("ready")

        self._transcribe_and_type(frames)

    def _transcribe_and_type(self, frames):
        sample_rate = self.config.get("sample_rate", 16000)
        channels = self.config["audio"]["channels"]
        vad = self.config.get("vad", {})

        if vad.get("enabled", True):
            frames = trim_silence(
                frames,
                sample_rate,
                vad.get("energy_threshold", 0.01),
                vad.get("prefix_ms", 200),
                vad.get("min_speech_duration_ms", 250),
            )

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            if not save_wav(tmp_path, frames, sample_rate, channels):
                return

            text = self.transcriber.transcribe(tmp_path, self.config.get("language", "en"))
            if not text:
                return

            if self.config.get("commands", {}).get("enabled", True):
                text = process_commands(text, self.config.get("commands", {}).get("map", COMMANDS))

            self.state.last_text = text
            print(f"[transcribed] {text}")

            self.output.type_text(text + " ")

            if self.args.once:
                self.stop()
        except Exception as ex:
            logging.error("Transcription failed: %s\n%s", ex, traceback.format_exc())
        finally:
            tmp_path.unlink(missing_ok=True)

    def set_model(self, model_name: str):
        self.config["whisper_model"] = model_name
        self.state.model_name = model_name
        save_config(self.config_path, self.config)
        if self.transcriber:
            self.transcriber.reload(model_name)

    def reload_config(self):
        self.config = load_config(self.config_path)
        self.ptt_key = key_code_from_config(self.config)
        if self.transcriber:
            self.transcriber.reload(self.config.get("whisper_model", "base"))
        self.state.model_name = self.config.get("whisper_model", "base")
        if self.output:
            self.output.config = self.config
        logging.info("Config reloaded")

    def stop(self):
        if self.input_manager:
            self.input_manager.stop()
        if self.tray:
            self.tray.stop()
        sys.exit(0)

    def run(self):
        self.setup()

        if self.config.get("tray", {}).get("enabled", True) and not self.args.no_tray:
            self.tray = TrayManager(self)
            tray_thread = threading.Thread(target=self.tray.run, daemon=True)
            tray_thread.start()

        self.input_manager = InputDeviceManager(self)
        self.input_manager.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logging.info("Stopping.")
            self.stop()


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


def main():
    parser = argparse.ArgumentParser(description="Push-to-talk voice input for Kimi Code CLI")
    parser.add_argument("--once", action="store_true", help="record one phrase and exit")
    parser.add_argument("--no-tray", action="store_true", help="run without the tray icon")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to config JSON")
    args = parser.parse_args()

    setup_logging()
    config = load_config(args.config)
    app = VoiceApp(args, config)
    app.run()


if __name__ == "__main__":
    main()
