"""Real faster-whisper streaming: VAD-cut utterances from a Korean sample."""

import os
import re
import wave
from pathlib import Path

import pytest

SAMPLE = Path(__file__).parent / "sample_ko.wav"
HANGUL = re.compile(r"[가-힣]")
FRAME_BYTES = 12800  # 400 ms of PCM16 mono at 16 kHz


def _pcm16(path: Path) -> bytes:
    with wave.open(str(path)) as wav:
        assert wav.getnchannels() == 1, "sample must be mono"
        assert wav.getsampwidth() == 2, "sample must be 16-bit"
        assert wav.getframerate() == 16000, "sample must be 16 kHz"
        return wav.readframes(wav.getnframes())


def test_whisper_stream_segments_korean_sample() -> None:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        pytest.skip("faster-whisper not installed")
    if not SAMPLE.exists():
        pytest.skip(f"{SAMPLE.name} missing")
    from shoplegacy.stt_stream import VadWhisperStreamSTT

    stream = VadWhisperStreamSTT(os.environ.get("SHOPLEGACY_WHISPER_MODEL", "small"))
    with pytest.raises(ValueError):
        stream.feed_text("텍스트")
    pcm = _pcm16(SAMPLE)
    finals = []
    for i in range(0, len(pcm), FRAME_BYTES):
        finals.extend(stream.feed(pcm[i : i + FRAME_BYTES]))
    finals.extend(stream.flush())
    assert len(finals) >= 1
    text = " ".join(s.text for s in finals)
    assert len(HANGUL.findall(text)) >= 3, text
    assert all(s.end_s > s.start_s >= 0.0 for s in finals)
    assert finals[-1].end_s <= len(pcm) / 32000 + 0.01
