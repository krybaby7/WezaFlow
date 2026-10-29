from __future__ import annotations

import argparse
import math
import time
from collections.abc import Callable
from pathlib import Path

from services.asr.audio_capture import AudioRecorder


def record_wav(
    path: str | Path,
    seconds: float = 3.0,
    recorder_factory: Callable[[], AudioRecorder] = AudioRecorder.microphone,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    if not math.isfinite(seconds) or not 0 < seconds <= 120:
        raise ValueError("seconds must be between 0 and 120")
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    recorder = recorder_factory()
    stop_attempted = False
    try:
        try:
            recorder.start()
        except Exception as exc:
            raise RuntimeError(
                "Unable to start microphone recording. Install sounddevice and check "
                "microphone permissions/device availability."
            ) from exc
        sleep(seconds)
        # Poll while the device is still running; stop clears the stream handle.
        poll_status = getattr(recorder, "poll_capture_status", None)
        if callable(poll_status):
            poll_status()
        stop_attempted = True
        wav_bytes = recorder.stop()
        # Drain callbacks can report a fault, so only the final status can admit audio.
        # Older injected recorders need only implement start/stop.
        status = getattr(recorder, "capture_status", None)
        if status is not None and (status.terminal_code or status.cleanup_pending):
            raise RuntimeError("Microphone capture failed; recording was not saved.")
    except BaseException:
        if not stop_attempted:
            try:
                recorder.stop()
            except BaseException:
                pass  # Preserve the original error or interruption.
        raise

    output_path.write_bytes(wav_bytes)
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record a short microphone WAV smoke test.")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--output", type=Path, default=Path("artifacts/test.wav"))
    args = parser.parse_args(argv)

    output_path = record_wav(args.output, seconds=args.seconds)
    print(f"Recorded {args.seconds:g}s WAV to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
