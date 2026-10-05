"""Speech-to-text with faster-whisper. STATUS: unverified draft (Phase 3)."""

from __future__ import annotations


class FasterWhisperSTT:
    def __init__(self, model_size: str = "small.en", device: str = "cpu", compute_type: str = "int8") -> None:
        # CPU on purpose: the GPU is busy with MSFS. VERIFY speed with the model size you pick.
        from faster_whisper import WhisperModel  # type: ignore

        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, audio_f32_16k, hint: str = "") -> str:
        """audio_f32_16k: float32 numpy array, mono, 16 kHz. `hint`: names for this flight (station,
        telephony, SID, destination), so "Martinair" isn't heard as "Martin Air"."""
        segments, _info = self._model.transcribe(
            audio_f32_16k,
            language="en",
            beam_size=1,
            vad_filter=True,
            # Biasing toward aviation vocabulary helps a lot with callsigns and phraseology.
            initial_prompt="Air traffic control radio. Runway, taxi, hold short, cleared for takeoff, "
            "downwind, base, final, squawk, QNH, readback, Cessna, niner, tree, fife. " + hint,
        )
        return " ".join(s.text.strip() for s in segments).strip()
