from __future__ import annotations

import io
import math
from array import array
from dataclasses import dataclass
import threading
import wave
from collections.abc import Callable, Iterable
from typing import Any


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
            samples = _flatten_mono_samples(indata)
            if any(not math.isfinite(value) or abs(value) > 3.402823466e38 for value in samples):
                raise ValueError('invalid samples')
        except (ValueError, TypeError, OverflowError, IndexError):
            self._terminal('invalid_audio')
            return
        if self._terminal_code not in (None, 'duration_limit'):
            return
        room = self.max_samples - len(self._frames)
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
        pcm = bytearray()
        for sample in samples:
            clipped = max(-1.0, min(1.0, float(sample)))
            value = int(round(clipped * 32767))
            pcm.extend(value.to_bytes(2, "little", signed=True))

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(bytes(pcm))
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


def _flatten_mono_samples(indata) -> list[float]:
    samples = []
    for sample in indata:
        if hasattr(sample, '__len__'):
            if len(sample) != 1:
                raise ValueError("Expected mono samples")
            sample = sample[0]
        samples.append(float(sample))
    return samples
