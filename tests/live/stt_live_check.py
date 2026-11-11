"""Manual check: transcribe a Korean sample with the real faster-whisper model.

Needs ``uv sync --extra stt``; the first run downloads the model (about 460MB for small).
Usage: uv run python tests/live/stt_live_check.py [--audio PATH] [--model NAME]
"""

import argparse
import mimetypes
import sys
import time
from pathlib import Path

DEFAULT_AUDIO = Path(__file__).parent / "sample_ko.wav"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run WhisperSTT once on a local audio file.")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO, help="audio file path")
    parser.add_argument("--model", default="small", help="faster-whisper model name")
    args = parser.parse_args(argv)

    from shoplegacy.stt import WhisperSTT

    audio_path: Path = args.audio
    if not audio_path.is_file():
        print(f"audio file not found: {audio_path}", file=sys.stderr)
        return 2
    content_type = mimetypes.guess_type(audio_path.name)[0] or "audio/wav"
    stt = WhisperSTT(args.model)
    started = time.perf_counter()
    result = stt.transcribe(audio_path.read_bytes(), audio_path.name, content_type)
    elapsed = time.perf_counter() - started
    print(f"provider: {result.provider}")
    print(f"model: {args.model}")
    print(f"text: {result.text}")
    print(f"audio_duration_s: {result.duration_s}")
    print(f"elapsed_s: {elapsed:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
