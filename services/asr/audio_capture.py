from __future__ import annotations

import io
import math
import sys
from array import array
from dataclasses import dataclass
import threading
import wave
from collections.abc import Callable, Iterable
from typing import Any

# False until first use; None when numpy is unavailable. Spawned workers import this
# package, so numpy must never be imported at module load.
_np_module: Any = False


def _numpy():
    global _np_module
    if _np_module is False:
        try:
            import numpy
        except ImportError:  # Pure-Python fallback keeps capture working without numpy.
            numpy = None
        _np_module = numpy
    return _np_module


def _loaded_numpy():
    # The audio callback must not pay for a first import; an ndarray block implies numpy is loaded.
    return _numpy() if 'numpy' in sys.modules else None


# Largest accepted magnitude, compared in float64 exactly as the Python path does.
_SAMPLE_LIMIT = 3.402823466e38

StreamFactory = Callable[[Callable[..., None], int, int], Any]


@dataclass(frozen=True)
class CaptureStatus:
    terminal_code: str | None
    sample_count: int
    max_samples: int
    requested_sample_rate: int
    actual_sample_rate: float | None
    cleanup_pending: bool


class AudioRecorder:
    def __init__(
        self,
        sample_rate: int = 16000,
        blocksize: int = 1024,
        stream_factory: StreamFactory | None = None,
        max_samples: int = 4_800_000,
    ) -> None:
        if type(sample_rate) is not int or sample_rate != 16000:
            raise ValueError("Only 16 kHz capture is supported")
        if type(blocksize) is not int or blocksize <= 0:
            raise ValueError("Block size must be a positive integer")
        if type(max_samples) is not int or not 0 < max_samples <= 4_800_000:
            raise ValueError("Invalid capture sample limit")
        self.max_samples = max_samples
        self.terminal_event = threading.Event()
        self._terminal_code = None
        self._sample_count = 0
        self._actual_rate = None
        self._cleanup_pending = False
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self._stream_factory = stream_factory
        self._stream = None
        self._frames = array('f')
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._is_recording = False
        self._capture_token = None

    @classmethod
    def microphone(cls, sample_rate: int = 16000, blocksize: int = 1024) -> "AudioRecorder":
        return cls(
            sample_rate=sample_rate,
            blocksize=blocksize,
            stream_factory=create_sounddevice_stream,
        )

    @property
    def capture_status(self) -> CaptureStatus:
        with self._lock:
            return CaptureStatus(self._terminal_code, self._sample_count, self.max_samples,
                                 self.sample_rate, self._actual_rate, self._cleanup_pending)

    def poll_capture_status(self) -> CaptureStatus:
        # Device properties may call adapter code: never inspect under the frame lock.
        stream = self._stream
        if stream is not None and self._is_recording:
            try:
                rate = getattr(stream, 'samplerate', None)
                active = getattr(stream, 'active', None)
                with self._lock:
                    self._actual_rate = float(rate) if rate is not None and math.isfinite(float(rate)) and float(rate) > 0 else None
                    if rate is not None and float(rate) != self.sample_rate:
                        self._terminal('sample_rate_mismatch')
                    if active is False:
                        self._terminal('device_stopped')
            except Exception:
                with self._lock:
                    self._terminal('device_stopped')
        return self.capture_status

    def _terminal(self, code):
        if self._terminal_code is None or self._terminal_code == 'duration_limit':
            self._terminal_code = code
        self.terminal_event.set()

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    def start(self) -> None:
        with self._lifecycle_lock:
            self._start_capture()

    def _start_capture(self) -> None:
        with self._lock:
            if self._is_recording or self._stream is not None:
                raise RuntimeError("Audio recorder is already recording")
            self._frames = array('f')
            self._sample_count = 0
            self._terminal_code = None
            self._actual_rate = None
            self.terminal_event.clear()
            self._is_recording = True
            token = self._capture_token = object()

        def callback(indata, frames=None, time=None, status=None):
            del frames, time
            with self._lock:
                if self._is_recording and self._capture_token is token:
                    self._accept(indata, status)

        try:
            if self._stream_factory is not None:
                self._stream = self._stream_factory(callback, self.sample_rate, self.blocksize)
                self._stream.start()
        except BaseException:
            # A stream may already be active when start raises, including Ctrl-C.
            try:
                self.stop()
            except BaseException:
                pass  # Preserve the original startup failure.
            raise

    def stop(self) -> bytes:
        with self._lifecycle_lock:
            return self._stop_capture()

    def _stop_capture(self) -> bytes:
        stream = self._stream
        failure = None
        closed = stream is None
        try:
            if stream is not None:
                try:
                    stream.stop()
                except BaseException as exc:
                    failure = exc
                try:
                    stream.close()
                    closed = True
                except BaseException as exc:
                    failure = failure or exc
        finally:
            with self._lock:
                self._is_recording = False
                self._capture_token = None
                self._cleanup_pending = not closed
                if closed:
                    self._stream = None
                frames, self._frames = self._frames, array('f')
        if failure is not None:
            raise failure
        return self.float_samples_to_wav_bytes(frames, sample_rate=self.sample_rate)

    def _accept(self, indata, status=None):
        if status:
            self._terminal('input_overflow')
            return
        try:
            block = _mono_float32_block(indata)
            if block is None:
                samples = _flatten_mono_samples(indata)
                if any(not math.isfinite(value) or abs(value) > _SAMPLE_LIMIT for value in samples):
                    raise ValueError('invalid samples')
        except (ValueError, TypeError, OverflowError, IndexError):
            self._terminal('invalid_audio')
            return
        if self._terminal_code not in (None, 'duration_limit'):
            return
        room = self.max_samples - len(self._frames)
        if block is not None:
            self._frames.frombytes(block[:room].tobytes())
        else:
            self._frames.extend(samples[:room])
        self._sample_count = len(self._frames)
        if self._sample_count == self.max_samples:
            self._terminal('duration_limit')

    def snapshot_samples(self) -> list[float]:
        with self._lock:
            return list(self._frames)

    def inject_frame(self, samples: Iterable[float]) -> None:
        with self._lock:
            if self._is_recording:
                self._accept(samples)

    def _callback(self, indata, frames=None, time=None, status=None) -> None:
        del frames, time
        with self._lock:
            if self._is_recording:
                self._accept(indata, status)

    @staticmethod
    def float_samples_to_wav_bytes(
        samples: Iterable[float], sample_rate: int = 16000
    ) -> bytes:
        pcm = _float_samples_to_pcm16(samples)

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        return buffer.getvalue()


def create_sounddevice_stream(callback: Callable[..., None], sample_rate: int, blocksize: int):
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("sounddevice is required for microphone capture") from exc

    return sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        callback=callback,
        blocksize=blocksize,
    )


def _mono_float32_block(indata):
    """Validated float32 copy of a numeric mono ndarray block, else None for the Python path.

    Raises ValueError for non-finite or out-of-range samples, like the Python path.
    """
    np = _loaded_numpy()
    if np is None or not isinstance(indata, np.ndarray) or indata.dtype.kind not in 'fiub':
        return None
    if indata.ndim == 2 and indata.shape[1] == 1:
        flat = indata[:, 0]
    elif indata.ndim == 1:
        flat = indata
    else:
        return None
    values = flat.astype(np.float64)
    # NaN compares False, so this rejects NaN, infinities and out-of-range values.
    if not bool(np.all(np.abs(values) <= _SAMPLE_LIMIT)):
        raise ValueError('invalid samples')
    return values.astype(np.float32)


def _float_samples_to_pcm16(samples: Iterable[float]) -> bytes:
    np = _numpy()
    values = _numeric_vector(np, samples)
    if values is None:
        pcm = bytearray()
        for sample in samples:
            clipped = max(-1.0, min(1.0, float(sample)))
            value = int(round(clipped * 32767))
            pcm.extend(value.to_bytes(2, "little", signed=True))
        return bytes(pcm)
    # max(-1.0, min(1.0, nan)) is 1.0 in the Python path.
    values = np.where(np.isnan(values), 1.0, np.clip(values, -1.0, 1.0))
    # rint is round-half-to-even on float64, matching Python round().
    return np.rint(values * 32767).astype('<i2').tobytes()


def _numeric_vector(np, samples):
    """float64 vector for flat numeric sequences, else None so callers use the Python path."""
    if np is None or not isinstance(samples, (list, tuple, array, np.ndarray)):
        return None
    try:
        values = np.asarray(samples)
    except (ValueError, TypeError, OverflowError):
        return None
    if values.ndim != 1 or values.dtype.kind not in 'fiub':
        return None
    return values.astype(np.float64)


def _flatten_mono_samples(indata) -> list[float]:
    samples = []
    for sample in indata:
        if hasattr(sample, '__len__'):
            if len(sample) != 1:
                raise ValueError("Expected mono samples")
            sample = sample[0]
        samples.append(float(sample))
    return samples
