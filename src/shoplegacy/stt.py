"""Speech-to-text providers: a fake for tests and a local faster-whisper one."""

import io
from typing import Any, Protocol

from pydantic import BaseModel

from shoplegacy.config import Settings

ALLOWED_TYPES = frozenset(
    {
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "audio/mp4",
        "audio/x-m4a",
        "audio/webm",
        "audio/ogg",
        "text/plain",
    }
)
FAKE_PLACEHOLDER = "녹음 내용을 받아 적었습니다."
WHISPER_MISSING = "faster-whisper is not installed; run uv sync --extra stt"


class Transcript(BaseModel):
    text: str
    duration_s: float | None
    provider: str


class STT(Protocol):
    def transcribe(self, audio: bytes, filename: str, content_type: str) -> Transcript: ...


class UnsupportedAudioError(ValueError):
    pass


class AudioTooLargeError(ValueError):
    pass


class FakeSTT:
    """Returns UTF-8 text uploads as-is; binary audio gets a fixed sentence."""

    def transcribe(self, audio: bytes, filename: str, content_type: str) -> Transcript:
        try:
            text = audio.decode("utf-8").strip()
        except UnicodeDecodeError:
            text = FAKE_PLACEHOLDER
        return Transcript(text=text, duration_s=None, provider="fake")


class WhisperSTT:
    """Local faster-whisper model, loaded on first use (Korean only)."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        return self._model

    def transcribe(self, audio: bytes, filename: str, content_type: str) -> Transcript:
        if content_type == "text/plain":
            raise UnsupportedAudioError("whisper needs audio, not text/plain")
        model = self._load()
        segments, info = model.transcribe(io.BytesIO(audio), language="ko")
        text = " ".join(segment.text.strip() for segment in segments).strip()
        duration = getattr(info, "duration", None)
        return Transcript(
            text=text,
            duration_s=float(duration) if duration is not None else None,
            provider="whisper",
        )


def build_stt(settings: Settings) -> STT:
    if settings.stt_provider == "whisper":
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            raise ValueError(WHISPER_MISSING) from None
        return WhisperSTT(settings.whisper_model)
    return FakeSTT()
