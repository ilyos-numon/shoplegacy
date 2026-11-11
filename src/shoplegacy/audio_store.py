"""Encrypted audio storage for queued transcription jobs."""

from typing import Any, Protocol

import fsspec

from shoplegacy.config import Settings
from shoplegacy.crypto import Cipher

EXTENSIONS = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "text/plain": ".txt",
}


class AudioStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...


def audio_key(org_id: str, job_id: str, content_type: str) -> str:
    return f"{org_id}/{job_id}{EXTENSIONS.get(content_type, '')}"


class FsspecAudioStore:
    """Encrypted blobs under a ``file://`` or ``s3://`` root."""

    def __init__(self, url: str, cipher: Cipher, endpoint: str = "") -> None:
        options: dict[str, Any] = {}
        if url.startswith("s3://") and endpoint:
            options["client_kwargs"] = {"endpoint_url": endpoint}
        self._fs, root = fsspec.core.url_to_fs(url, **options)
        self._root = str(root).rstrip("/")
        self._cipher = cipher

    def _path(self, key: str) -> str:
        parts = key.split("/")
        if not key or key.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"bad audio key: {key!r}")
        return f"{self._root}/{key}"

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        self._fs.makedirs(path.rsplit("/", 1)[0], exist_ok=True)
        self._fs.pipe_file(path, self._cipher.encrypt_bytes(data))

    def get(self, key: str) -> bytes:
        try:
            data = self._fs.cat_file(self._path(key))
        except FileNotFoundError:
            raise KeyError(key) from None
        return self._cipher.decrypt_bytes(bytes(data))

    def delete(self, key: str) -> None:
        try:
            self._fs.rm_file(self._path(key))
        except FileNotFoundError:
            pass

    def exists(self, key: str) -> bool:
        return bool(self._fs.exists(self._path(key)))


def build_audio_store(settings: Settings, cipher: Cipher) -> AudioStore:
    url = settings.audio_store or f"file://{settings.data_dir.resolve()}/audio"
    return FsspecAudioStore(url, cipher, settings.audio_s3_endpoint)
