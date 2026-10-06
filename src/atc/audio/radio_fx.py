"""Radio effect: narrow band-pass, a little noise, mild clipping. Needs numpy only."""

from __future__ import annotations


def apply_radio_fx(samples, sample_rate: int, low_hz: float = 250.0, high_hz: float = 3600.0, noise: float = 0.005):
    import numpy as np  # type: ignore

    x = np.asarray(samples, dtype=np.float32)
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sample_rate)
    spectrum[(freqs < low_hz) | (freqs > high_hz)] = 0  # crude brick-wall band-pass
    y = np.fft.irfft(spectrum, n=len(x)).astype(np.float32)
    peak = float(np.max(np.abs(y))) or 1.0
    y = y / peak * 0.9
    y = np.clip(y * 1.1, -0.9, 0.9)  # mild clipping = "radio crunch"
    rng = np.random.default_rng(0)
    y = y + rng.normal(0, noise, size=y.shape).astype(np.float32)
    return np.clip(y, -1.0, 1.0)


def squelch_tail(sample_rate: int, seconds: float = 0.09, level: float = 0.18):
    """Short burst of band-limited noise that dies away: the squelch tail heard when a station releases the PTT."""
    import numpy as np  # type: ignore

    n = int(sample_rate * seconds)
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 1, n).astype(np.float32)
    spectrum = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    spectrum[(freqs < 400) | (freqs > 3200)] = 0
    burst = np.fft.irfft(spectrum, n=n).astype(np.float32)
    burst /= float(np.max(np.abs(burst))) or 1.0
    envelope = np.linspace(1.0, 0.0, n, dtype=np.float32) ** 2
    return (burst * envelope * level).astype(np.float32)
