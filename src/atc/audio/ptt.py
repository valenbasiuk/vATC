"""Push-to-talk recording. STATUS: unverified draft (Phase 3).

Hold the key -> record from the default mic at 16 kHz -> release -> return audio.
"""

from __future__ import annotations

import threading


class PushToTalk:
    def __init__(self, key: str = "f9", sample_rate: int = 16000) -> None:
        self.key = key
        self.sample_rate = sample_rate

    def record_once(self):
        """Blocks until the key was pressed and released once. Returns float32 mono array."""
        import numpy as np  # type: ignore
        import sounddevice as sd  # type: ignore
        from pynput import keyboard  # type: ignore

        chunks: list = []
        pressed = threading.Event()
        released = threading.Event()
        target = getattr(keyboard.Key, self.key, None) or keyboard.KeyCode.from_char(self.key)

        def on_press(k):
            if k == target:
                pressed.set()

        def on_release(k):
            if k == target and pressed.is_set():
                released.set()
                return False

        def callback(indata, frames, time_info, status):
            if pressed.is_set() and not released.is_set():
                chunks.append(indata.copy())

        with sd.InputStream(samplerate=self.sample_rate, channels=1, dtype="float32", callback=callback):
            with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
                listener.join()
        return np.concatenate(chunks)[:, 0] if chunks else np.zeros(0, dtype="float32")
