"""Push-to-talk recording: hold a keyboard key and/or a joystick/yoke button, speak, release.

STATUS: keyboard path unverified on Valen's PC; joystick path (audio/joystick.py) untested with hardware.
The keyboard is watched by one global hook (pynput), so it works while MSFS has the focus.
"""

from __future__ import annotations

import threading
import time


class PushToTalk:
    def __init__(self, key: str | None = "f9", sample_rate: int = 16000, joy_button: int | None = None,
                 joy_device: int | None = None, mic=None) -> None:
        self.key = None if (key or "").lower() in ("", "none") else key
        self.sample_rate = sample_rate
        self.mic = mic  # sounddevice input device (index or name part); None = Windows default
        self.talking = threading.Event()  # set while held: controller-initiated calls and chatter wait
        self._key_down = threading.Event()
        self._listener = None
        self._joy = None
        if joy_button is not None:
            from atc.audio.joystick import JoystickButton

            self._joy = JoystickButton(joy_button, joy_device)

    def describe(self) -> str:
        parts = [self.key.upper()] if self.key else []
        if self._joy is not None:
            parts.append(f"joystick button {self._joy.bit.bit_length()}")
        return " or ".join(parts) or "nothing (no --ptt-key / --ptt-joy)"

    def _start_keyboard(self) -> None:
        if self._listener is not None or self.key is None:
            return
        from pynput import keyboard  # type: ignore

        target = getattr(keyboard.Key, self.key, None) or keyboard.KeyCode.from_char(self.key)

        def on_press(k):
            if k == target:
                self._key_down.set()

        def on_release(k):
            if k == target:
                self._key_down.clear()

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()

    def _down(self) -> bool:
        return self._key_down.is_set() or (self._joy is not None and self._joy.pressed())

    def record_once(self):
        """Blocks until the key/button was pressed and released once. Returns float32 mono samples."""
        import numpy as np  # type: ignore
        import sounddevice as sd  # type: ignore

        self._start_keyboard()
        chunks: list = []
        recording = threading.Event()

        def callback(indata, frames, time_info, status):
            if recording.is_set():
                chunks.append(indata.copy())

        # the mic stream is open before the press, so the first word isn't cut off
        with sd.InputStream(samplerate=self.sample_rate, channels=1, dtype="float32", callback=callback,
                            device=self.mic):
            while not self._down():
                time.sleep(0.02)
            self.talking.set()
            recording.set()
            while self._down():
                time.sleep(0.02)
            recording.clear()
            self.talking.clear()
        return np.concatenate(chunks)[:, 0] if chunks else np.zeros(0, dtype="float32")
