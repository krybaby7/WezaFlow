from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

# False until first use; None when numpy is unavailable. Spawned workers import this
# package, so numpy must never be imported at module load.
_np_module: Any = False


def _numpy():
    global _np_module
    if _np_module is False:
        try:
            import numpy
        except ImportError:  # Pure-Python fallback keeps VAD working without numpy.
            numpy = None
        _np_module = numpy
    return _np_module


class VADFilter:
    def __init__(
        self,
        threshold: float = 0.02,
        min_duration_ms: int = 250,
        sample_rate: int = 16000,
        trim_padding_ms: int = 0,
        trim_threshold: float | None = None,
    ) -> None:
        self.threshold = threshold
        self.min_duration_ms = min_duration_ms
        self.sample_rate = sample_rate
        self.trim_padding_ms = trim_padding_ms
        self.trim_threshold = trim_threshold if trim_threshold is not None else threshold
        self.last_stats: dict[str, int | float | None] = {}

    def filter(self, audio_float32: Sequence[float] | Iterable[float]):
        samples = audio_float32 if isinstance(audio_float32, list) else list(audio_float32)
        self.last_stats = {
            "input_sample_count": len(samples),
            "output_sample_count": 0,
            "trim_start_samples": None,
            "trim_end_samples": None,
            "max_abs": 0.0,
        }
        if not samples:
            return None

        magnitudes = _finite_magnitudes(samples)
        min_samples = int(self.sample_rate * self.min_duration_ms / 1000)
        if magnitudes is not None:
            active_samples = int(_numpy().count_nonzero(magnitudes >= self.threshold))
            max_abs = float(magnitudes.max())
        else:
            active_samples = sum(1 for sample in samples if abs(float(sample)) >= self.threshold)
            max_abs = max((abs(float(sample)) for sample in samples), default=0.0)
        self.last_stats["max_abs"] = max_abs
        if active_samples < min_samples and max_abs < self.threshold:
            return None
        if self.trim_padding_ms <= 0:
            self.last_stats.update(
                {
                    "output_sample_count": len(samples),
                    "trim_start_samples": 0,
                    "trim_end_samples": 0,
                }
            )
            return samples
        return self._trim_with_padding(samples, magnitudes)

    def _trim_with_padding(self, samples: list[float], magnitudes=None) -> list[float]:
        first_active: int | None = None
        last_active: int | None = None
        if magnitudes is not None:
            active = _numpy().flatnonzero(magnitudes >= self.trim_threshold)
            if len(active):
                first_active, last_active = int(active[0]), int(active[-1])
        else:
            for index, sample in enumerate(samples):
                if abs(float(sample)) >= self.trim_threshold:
                    if first_active is None:
                        first_active = index
                    last_active = index

        if first_active is None or last_active is None:
            self.last_stats.update(
                {
                    "output_sample_count": len(samples),
                    "trim_start_samples": 0,
                    "trim_end_samples": 0,
                }
            )
            return samples

        padding_samples = int(self.sample_rate * self.trim_padding_ms / 1000)
        start = max(0, first_active - padding_samples)
        end = min(len(samples), last_active + padding_samples + 1)
        trimmed = samples[start:end]
        self.last_stats.update(
            {
                "output_sample_count": len(trimmed),
                "trim_start_samples": start,
                "trim_end_samples": len(samples) - end,
            }
        )
        return trimmed


def _finite_magnitudes(samples: list[float]):
    """float64 |samples| for flat finite numeric lists, else None for the Python path.

    Non-finite values fall back so Python's max() and comparison ordering for NaN are kept.
    """
    np = _numpy()
    if np is None:
        return None
    try:
        values = np.asarray(samples)
    except (ValueError, TypeError, OverflowError):
        return None
    if values.ndim != 1 or values.dtype.kind not in 'fiub':
        return None
    magnitudes = np.abs(values.astype(np.float64))
    if not bool(np.all(np.isfinite(magnitudes))):
        return None
    return magnitudes
