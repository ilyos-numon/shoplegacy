"""Streaming speech-to-text providers."""

import threading
from collections.abc import Callable
from functools import partial
from typing import Any, Protocol

from pydantic import BaseModel

from shoplegacy.config import Settings
from shoplegacy.stt import FAKE_PLACEHOLDER, WHISPER_MISSING

SAMPLE_RATE = 16000
BYTES_PER_SECOND = 32000  # PCM16 mono at 16 kHz


class Segment(BaseModel):
    text: str
    start_s: float
    end_s: float


class StreamSTT(Protocol):
    provider: str

    def feed(self, pcm16: bytes) -> list[Segment]: ...

    def feed_text(self, text: str) -> list[Segment]: ...

    def flush(self) -> list[Segment]: ...


class UnsupportedTextError(ValueError):
    pass


class FakeStreamSTT:
    """Counts audio bytes; text frames come back as one utterance each."""

    provider = "fake"

    def __init__(self) -> None:
        self._bytes = 0
        self._pending = 0
        self._last_end = 0.0

    def _now(self) -> float:
        return self._bytes / BYTES_PER_SECOND

    def feed(self, pcm16: bytes) -> list[Segment]:
        self._bytes += len(pcm16)
        self._pending += len(pcm16)
        return []

    def feed_text(self, text: str) -> list[Segment]:
        start = self._last_end
        end = max(self._now(), start)
        self._last_end = end
        return [Segment(text=text, start_s=start, end_s=end)]

    def flush(self) -> list[Segment]:
        if self._pending < 1:
            return []
        self._pending = 0
        start = self._last_end
        end = max(self._now(), start)
        self._last_end = end
        return [Segment(text=FAKE_PLACEHOLDER, start_s=start, end_s=end)]


_MIN_SILENCE_MS = 700
_SPEECH_PAD_MS = 200
# Long monologues are cut so the VAD never rescans more than this much audio.
_MAX_SPEECH_S = 30.0
# With no speech in sight, only this much trailing audio is kept for the next scan.
_IDLE_KEEP_S = 1.0

_models: dict[str, Any] = {}
_models_lock = threading.Lock()


def _load_model(name: str) -> Any:
    """One faster-whisper model per name, shared by all streams in the process."""
    with _models_lock:
        model = _models.get(name)
        if model is None:
            from faster_whisper import WhisperModel

            model = WhisperModel(name, device="cpu", compute_type="int8")
            _models[name] = model
        return model


class VadWhisperStreamSTT:
    """Silero VAD (bundled with faster-whisper) cuts utterances; whisper transcribes each.

    An utterance is closed once the VAD sees its end followed by enough silence;
    only closed utterances are transcribed, so ``feed`` returns finished text.
    """

    provider = "whisper"

    def __init__(self, model_name: str) -> None:
        import numpy as np
        from faster_whisper.vad import VadOptions

        self.model_name = model_name
        self._np = np
        self._vad_options = VadOptions(
            min_silence_duration_ms=_MIN_SILENCE_MS,
            speech_pad_ms=_SPEECH_PAD_MS,
            max_speech_duration_s=_MAX_SPEECH_S,
        )
        self._buf: Any = np.zeros(0, dtype=np.float32)
        self._offset = 0  # absolute sample index of self._buf[0]
        self._odd = b""  # a trailing half sample carried to the next frame

    def _append(self, pcm16: bytes) -> None:
        np = self._np
        data = self._odd + pcm16
        if len(data) % 2:
            data, self._odd = data[:-1], data[-1:]
        else:
            self._odd = b""
        if not data:
            return
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, samples])

    def _speech(self) -> list[dict[str, int]]:
        from faster_whisper.vad import get_speech_timestamps

        if len(self._buf) == 0:
            return []
        chunks: list[dict[str, int]] = get_speech_timestamps(
            self._buf, self._vad_options, sampling_rate=SAMPLE_RATE
        )
        return chunks

    def _transcribe(self, start: int, end: int) -> Segment | None:
        model = _load_model(self.model_name)
        segments, _info = model.transcribe(self._buf[start:end], language="ko")
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            return None
        return Segment(
            text=text,
            start_s=(self._offset + start) / SAMPLE_RATE,
            end_s=(self._offset + end) / SAMPLE_RATE,
        )

    def _drop(self, upto: int) -> None:
        self._buf = self._buf[upto:]
        self._offset += upto

    def feed(self, pcm16: bytes) -> list[Segment]:
        self._append(pcm16)
        chunks = self._speech()
        silence = SAMPLE_RATE * _MIN_SILENCE_MS // 1000
        closed = [c for c in chunks if len(self._buf) - c["end"] >= silence]
        if not chunks:
            keep = int(SAMPLE_RATE * _IDLE_KEEP_S)
            if len(self._buf) > keep:
                self._drop(len(self._buf) - keep)
            return []
        out: list[Segment] = []
        for chunk in closed:
            segment = self._transcribe(chunk["start"], chunk["end"])
            if segment is not None:
                out.append(segment)
        if closed:
            self._drop(closed[-1]["end"])
        return out

    def feed_text(self, text: str) -> list[Segment]:
        raise UnsupportedTextError("whisper streams take audio only")

    def flush(self) -> list[Segment]:
        out: list[Segment] = []
        for chunk in self._speech():
            segment = self._transcribe(chunk["start"], chunk["end"])
            if segment is not None:
                out.append(segment)
        self._drop(len(self._buf))
        self._odd = b""
        return out


def build_stream_factory(settings: Settings) -> Callable[[], StreamSTT]:
    """A new stream per connection."""
    if settings.stt_provider == "whisper":
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            raise ValueError(WHISPER_MISSING) from None
        return partial(VadWhisperStreamSTT, settings.whisper_model)
    return FakeStreamSTT
